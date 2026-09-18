"""Google AI Overview (google.com SERP) scraper.

Google's AI Overview ("AIO") is the generative summary block at the top of a
results page. Unlike the chat platforms there is no public backend payload to
intercept, so we extract straight from the rendered DOM and anchor on the
visible **"AI Overview"** label (class/id hooks churn constantly; the label
text is the only durable signal).

Persistent session — per the user's instruction (2026-06-01): DO NOT close the
tab between keywords. Google bot-detects fresh browser launches hard and re-
opening Chrome per keyword is slow, so the session (patchright + real Chrome,
headed) is opened once and reused for every prompt in a batch — each new
keyword is typed into the same search box. `close_session()` tears it down once
the whole scan finishes (called from the app's run handler).

Like the ChatGPT scraper this always runs headed via patchright + real Chrome
(headless Google rarely renders AIO and trips the sorry/CAPTCHA wall). The
`headless` argument is accepted for API parity but ignored. Scrapes are
serialized through a module-level lock because the single shared page can only
handle one search at a time.
"""
from __future__ import annotations

import asyncio
import time
from urllib.parse import urlparse, parse_qs

from .base import ScrapeResult, DATA_DIR, profile_dir
from ._browser import dedupe

try:
    from patchright.async_api import async_playwright as async_patchright  # type: ignore
    _HAS_PATCHRIGHT = True
except Exception:  # pragma: no cover
    from playwright.async_api import async_playwright as async_patchright  # type: ignore
    _HAS_PATCHRIGHT = False


_DEBUG_DIR = DATA_DIR / "debug"

# One shared Chrome per profile dir → serialize scrapes so concurrent prompts
# don't type over each other on the single reused page.
_SCRAPE_LOCK = asyncio.Lock()

# The live browser session, reused across keywords. Bound to the event loop it
# was created in (each "Run scan" is one asyncio.run → one loop); if the loop
# changes we tear down and relaunch.
_SESSION: dict | None = None

# Hosts that are Google's own chrome, not a cited source.
_SKIP_HOSTS = (
    "google.com", "gstatic.com", "googleusercontent.com", "ggpht.com",
    "google.co", "googleapis.com", "schema.org", "w3.org",
)

# Search box: Google moved the homepage input from <input> to <textarea>.
_SEARCH_BOX = "textarea[name='q'], input[name='q']"

_CONSENT_SELECTORS = (
    "button#L2AGLb",                       # classic "Accept all"
    "button:has-text('Accept all')",
    "button:has-text('Reject all')",
    "button:has-text('I agree')",
    "form[action*='consent'] button",
)

# Extracts the AI Overview card text + links by anchoring on the "AI Overview"
# label and walking up to the first ancestor large enough to be the whole card.
_EXTRACT_JS = r"""
() => {
  const norm = s => (s || '').replace(/\s+/g, ' ').trim();
  const cands = Array.from(document.querySelectorAll(
    'h1,h2,h3,div,span,[role="heading"]'
  ));
  let label = null;
  for (const el of cands) {
    const t = norm(el.innerText || el.textContent);
    if (/^AI Overview\b/i.test(t) && t.length < 40) { label = el; break; }
  }
  if (!label) return null;
  let node = label, card = label;
  for (let i = 0; i < 12 && node; i++) {
    const len = (node.innerText || '').length;
    if (len > 250) { card = node; break; }
    card = node;
    node = node.parentElement;
  }
  const links = Array.from(card.querySelectorAll('a[href]'))
    .map(a => a.href)
    .filter(h => /^https?:/i.test(h));
  return { text: (card.innerText || '').trim(), links };
}
"""


def _unwrap(url: str) -> str:
    """Resolve Google redirect wrappers (/url?q=, /aclk?...&url=) to the real
    destination so the cited host is the publisher, not google.com."""
    try:
        pr = urlparse(url)
        if "google." in pr.netloc.lower() and pr.path in ("/url", "/aclk", "/imgres"):
            qs = parse_qs(pr.query)
            for key in ("url", "q", "u"):
                if qs.get(key):
                    return qs[key][0]
    except Exception:
        pass
    return url


def _is_source(url: str) -> bool:
    if not url or not url.startswith(("http://", "https://")):
        return False
    host = url.split("/", 3)[2].lower() if "://" in url else ""
    return not any(s in host for s in _SKIP_HOSTS)


def _clean_text(text: str) -> str:
    """Strip the leading 'AI Overview' label and trailing SERP/UI chrome."""
    t = text.strip()
    # Drop the leading label row: "AI Overview", the "हिन्दी"/language toggle,
    # "+N" source counters and other short control tokens Google renders above
    # the actual summary. Stop at the first line that looks like real content.
    lines = t.splitlines()
    popped = 0
    while lines and popped < 6 and (
        lines[0].strip().lower().startswith("ai overview")
        or len(lines[0].strip()) <= 12
    ):
        lines.pop(0)
        popped += 1
    t = "\n".join(lines)
    # Cut at known footer / next-module markers.
    for marker in (
        "AI responses may include mistakes",
        "Generative AI is experimental",
        "People also ask",
        "Show less",
    ):
        idx = t.find(marker)
        if idx != -1:
            t = t[:idx]
    return t.strip()[:50000]


async def _dump_failure(page, tag: str) -> None:
    try:
        _DEBUG_DIR.mkdir(parents=True, exist_ok=True)
        stamp = time.strftime("%Y%m%d_%H%M%S")
        base = _DEBUG_DIR / f"google_fail_{tag}_{stamp}"
        try:
            await page.screenshot(path=str(base.with_suffix(".png")), full_page=True)
        except Exception:
            pass
        try:
            base.with_suffix(".html").write_text(await page.content(), encoding="utf-8")
        except Exception:
            pass
        try:
            base.with_suffix(".url.txt").write_text(f"{page.url}\n", encoding="utf-8")
        except Exception:
            pass
    except Exception:
        pass


async def _dismiss_consent(page) -> None:
    for sel in _CONSENT_SELECTORS:
        try:
            loc = page.locator(sel).first
            if await loc.count():
                await loc.click(timeout=1500)
                await page.wait_for_timeout(400)
                return
        except Exception:
            pass


async def _ensure_session(headless: bool) -> dict:
    """Return a live session for the current loop, launching one if needed.
    Reuses the open Chrome/tab across keywords (the whole point)."""
    global _SESSION
    loop = asyncio.get_running_loop()

    if _SESSION is not None:
        same_loop = _SESSION.get("loop") is loop
        page = _SESSION.get("page")
        try:
            alive = same_loop and page is not None and not page.is_closed()
        except Exception:
            alive = False
        if alive:
            return _SESSION
        # Stale (loop changed or page died) — tear it down before relaunching.
        await close_session()

    user_dir = str(profile_dir("google"))
    pw = await async_patchright().start()
    kwargs = dict(
        user_data_dir=user_dir,
        headless=False,  # AIO + bot-detection: always headed, like ChatGPT.
        viewport={"width": 1280, "height": 900},
        args=["--no-default-browser-check"],
    )
    if _HAS_PATCHRIGHT:
        kwargs["channel"] = "chrome"
        kwargs["no_viewport"] = False
    try:
        ctx = await pw.chromium.launch_persistent_context(**kwargs)
    except Exception:
        # Real Chrome missing — fall back to bundled Chromium.
        kwargs.pop("channel", None)
        ctx = await pw.chromium.launch_persistent_context(**kwargs)

    page = ctx.pages[0] if ctx.pages else await ctx.new_page()
    try:
        await page.goto("https://www.google.com/?hl=en", wait_until="domcontentloaded", timeout=60_000)
    except Exception:
        pass
    await _dismiss_consent(page)

    _SESSION = {"pw": pw, "ctx": ctx, "page": page, "loop": loop}
    return _SESSION


async def close_session() -> None:
    """Close the shared Chrome. Called once at the end of a scan batch.
    No-op if no session (and best-effort if the owning loop already closed)."""
    global _SESSION
    sess = _SESSION
    _SESSION = None
    if not sess:
        return
    try:
        await sess["ctx"].close()
    except Exception:
        pass
    try:
        await sess["pw"].stop()
    except Exception:
        pass


async def _run_search(page, prompt: str) -> None:
    """Type `prompt` into the search box (clearing any previous query) and
    submit — the human flow, reusing the open tab. Never navigates by URL."""
    box = page.locator(_SEARCH_BOX).first
    await box.wait_for(state="visible", timeout=20_000)
    await box.click()
    # Clear whatever the previous keyword left behind.
    await page.keyboard.press("Control+A")
    await page.keyboard.press("Delete")
    await box.type(prompt, delay=18)
    await page.wait_for_timeout(250)
    await page.keyboard.press("Enter")
    try:
        await page.wait_for_load_state("domcontentloaded", timeout=30_000)
    except Exception:
        pass


async def _await_ai_overview(page, *, appear_ms: int = 16_000, stable_ms: int = 2200,
                             max_ms: int = 60_000) -> dict | None:
    """Poll for the AI Overview card to appear, then wait for its text to stop
    growing (it streams in). Returns {text, links} or None if no AIO showed."""
    deadline = time.monotonic() + appear_ms / 1000
    data = None
    while time.monotonic() < deadline:
        try:
            data = await page.evaluate(_EXTRACT_JS)
        except Exception:
            data = None
        if data and len((data.get("text") or "")) > 30:
            break
        await page.wait_for_timeout(400)
    if not data or len((data.get("text") or "")) <= 30:
        return None

    # Expand the collapsed card so we get the full text + every source.
    for sel in ("div[aria-expanded='false']:has-text('Show more')",
                "button:has-text('Show more')",
                "a:has-text('Show more')"):
        try:
            loc = page.locator(sel).first
            if await loc.count():
                await loc.click(timeout=2000)
                await page.wait_for_timeout(600)
                break
        except Exception:
            pass

    # Wait for streaming text to settle.
    hard_deadline = time.monotonic() + max_ms / 1000
    last_len, last_change = -1, time.monotonic()
    while time.monotonic() < hard_deadline:
        try:
            data = await page.evaluate(_EXTRACT_JS) or data
        except Exception:
            pass
        cur = len((data.get("text") or "")) if data else 0
        if cur != last_len:
            last_len, last_change = cur, time.monotonic()
        elif (time.monotonic() - last_change) * 1000 >= stable_ms:
            break
        await page.wait_for_timeout(350)
    return data


async def scrape(prompt: str, headless: bool = True) -> ScrapeResult:
    # `headless` accepted for API parity but ignored — always headed (see module
    # docstring). The shared session means we hold the lock for the whole search.
    res = ScrapeResult(platform="google", prompt=prompt)
    async with _SCRAPE_LOCK:
        try:
            sess = await _ensure_session(headless)
            page = sess["page"]
            try:
                await _run_search(page, prompt)
            except Exception as e:
                await _dump_failure(page, "search")
                res.error = f"search failed: {type(e).__name__}: {e}"
                return res

            await _dismiss_consent(page)

            # Detect the sorry/CAPTCHA wall (rare with real Chrome + profile).
            if "/sorry/" in page.url:
                await _dump_failure(page, "captcha")
                res.error = "Google CAPTCHA wall — solve it in the open window, then re-run."
                return res

            data = await _await_ai_overview(page)
            if not data:
                res.error = "No AI Overview for this query."
                return res

            res.response_text = _clean_text(data.get("text") or "")
            cites: list[str] = []
            for h in data.get("links") or []:
                u = _unwrap(h)
                if _is_source(u):
                    cites.append(u)
            res.citations = dedupe(cites)
        except Exception as e:
            res.error = f"{type(e).__name__}: {e}"
    return res
