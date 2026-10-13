"""Google AI Overview (google.co.in SERP, India region) scraper.

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
import os
import time
from urllib.parse import urlparse, parse_qs

from .base import ScrapeResult, DATA_DIR, profile_dir
from ._browser import (dedupe, release_profile, is_profile_locked_error,
                       _route_block, resource_blocking_enabled,
                       sanitize_profile, CRASH_BUBBLE_ARGS, single_page,
                       proxy_config, SlotPool)

try:
    from patchright.async_api import async_playwright as async_patchright  # type: ignore
    _HAS_PATCHRIGHT = True
except Exception:  # pragma: no cover
    from playwright.async_api import async_playwright as async_patchright  # type: ignore
    _HAS_PATCHRIGHT = False


_DEBUG_DIR = DATA_DIR / "debug"

# A POOL of independent Chromes, not one. Each slot gets its own profile dir
# (profiles/google_0, _1, …) because a persistent profile can only be open in one
# browser at a time. GOOGLE_POOL sets the width: 1 restores the old serial
# behaviour, 3 is the default (500 keywords: ~2.8h serial → ~1h).
# More sessions = more concurrent traffic to Google, so raising this raises
# CAPTCHA risk; proxies help spread it across exit IPs.
# Exits that Google has CAPTCHA-walled this run. Skipped when picking a proxy for
# a new session; if every exit is burned the picker falls back to the full list,
# since a flagged IP usually recovers.
_BURNED_PROXIES: set[str] = set()
# How many times one keyword may rotate to a fresh exit before giving up.
CAPTCHA_ROTATIONS = int(os.getenv("GOOGLE_CAPTCHA_ROTATIONS", "2"))

POOL_SIZE = int(os.getenv("GOOGLE_POOL", "3"))
_POOL = SlotPool(POOL_SIZE)

# {slot: session}. Each is reused across keywords like the old single session was,
# and is bound to the event loop that created it.
_SESSIONS: dict[int, dict] = {}

# Hosts that are Google's own chrome, not a cited source.
_SKIP_HOSTS = (
    "google.com", "gstatic.com", "googleusercontent.com", "ggpht.com",
    "google.co", "googleapis.com", "schema.org", "w3.org",
)

# Search box: Google moved the homepage input from <input> to <textarea>.
#
# :visible is essential. A SERP also carries a HIDDEN <input name="q"> holding an
# opaque continuation token, and it comes first in DOM order — so a bare
# "textarea[name='q'], input[name='q']" with .first resolved to that hidden input.
# Typing then went nowhere useful and reads came back as token gibberish
# ("EgRrrzp2GKGVw9UG..."), which is why keywords after the first searched garbage.
_SEARCH_BOX = "textarea[name='q']:visible, input[name='q']:visible"

# Search as if from India. This matters MORE once proxies are on: the proxy pool
# exits from US IPs, and plain google.com would then serve US results — silently
# changing what the tracker measures. The .co.in domain plus gl=IN pins the result
# region; hl=en-IN the interface; pws=0 drops personalisation so runs are
# comparable. Override with GOOGLE_HOME / GOOGLE_GL / GOOGLE_HL if you ever want a
# different market.
GOOGLE_GL = os.getenv("GOOGLE_GL", "IN")
GOOGLE_HL = os.getenv("GOOGLE_HL", "en-IN")
GOOGLE_HOME = os.getenv(
    "GOOGLE_HOME",
    f"https://www.google.co.in/?hl={GOOGLE_HL}&gl={GOOGLE_GL}&pws=0",
)

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


# Google wraps every AI Overview citation in a redirect. /url?q= carried the
# destination in plain text; /goto?url= (the current AIO format) carries an OPAQUE
# protobuf token instead, so the publisher cannot be parsed out of the href at all —
# only the redirect itself knows it. See _resolve_redirects.
_REDIRECT_PATHS = ("/goto", "/url", "/aclk", "/imgres")


def _is_google_redirect(url: str) -> bool:
    try:
        pr = urlparse(url)
        return "google." in pr.netloc.lower() and pr.path in _REDIRECT_PATHS
    except Exception:
        return False


def _unwrap(url: str) -> str:
    """Resolve Google redirect wrappers (/url?q=, /aclk?...&url=) to the real
    destination so the cited host is the publisher, not google.com.

    Only works when the destination is a plain URL in the query string. The AIO
    /goto?url=CAES... token is not, and falls through unchanged — _resolve_redirects
    handles those by actually following the redirect."""
    try:
        pr = urlparse(url)
        if "google." in pr.netloc.lower() and pr.path in _REDIRECT_PATHS:
            qs = parse_qs(pr.query)
            for key in ("url", "q", "u"):
                val = (qs.get(key) or [""])[0]
                if val.startswith(("http://", "https://")):
                    return val
    except Exception:
        pass
    return url


async def _resolve_redirects(page, urls: list[str], *, limit: int = 8) -> dict[str, str]:
    """Follow Google's redirect wrappers to their real destinations.

    Requests go through the page's own context so they carry its cookies and look
    like the same session. Deduped first — the AIO repeats the same source link for
    every sentence that cites it (24 anchors collapsed to 8 links in testing) — and
    run concurrently, since each redirect costs ~1s and serial resolution would add
    ~9s to every keyword.

    Returns {wrapper_url: final_url}; wrappers that fail are simply absent.
    """
    uniq = [u for u in dict.fromkeys(urls) if _is_google_redirect(u)]
    if not uniq:
        return {}
    req = page.context.request
    sem = asyncio.Semaphore(limit)
    out: dict[str, str] = {}

    async def _one(u: str) -> None:
        async with sem:
            try:
                r = await req.get(u, max_redirects=5, timeout=15_000)
                if r.url and r.url != u:
                    out[u] = r.url
            except Exception:
                pass

    await asyncio.gather(*(_one(u) for u in uniq))
    return out


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


async def _ensure_session(headless: bool, slot: int = 0) -> dict:
    """Return a live session for THIS slot, launching one if needed.
    Reuses the open Chrome/tab across keywords (the whole point)."""
    loop = asyncio.get_running_loop()

    sess = _SESSIONS.get(slot)
    if sess is not None:
        same_loop = sess.get("loop") is loop
        page = sess.get("page")
        try:
            alive = same_loop and page is not None and not page.is_closed()
        except Exception:
            alive = False
        if alive:
            return sess
        # Stale (loop changed or page died) — tear this slot down before relaunching.
        await _close_slot(slot)

    # Each slot needs its OWN profile: one directory cannot back two live browsers.
    user_dir = str(profile_dir(f"google_{slot}"))
    # A profile left "crashed" opens on the Restore-pages bubble and parks the tab
    # on about:blank — clear it before every launch, not just after a kill.
    sanitize_profile(user_dir)
    pw = await async_patchright().start()
    kwargs = dict(
        user_data_dir=user_dir,
        headless=False,  # AIO + bot-detection: always headed, like ChatGPT.
        viewport={"width": 1280, "height": 900},
        args=["--no-default-browser-check", *CRASH_BUBBLE_ARGS],
        # Playwright defaults this to False, which injects --no-sandbox --
        # a visible infobar and a cheap automation tell. Set in every
        # module that launches its own context; see scrapers/chatgpt.py.
        chromium_sandbox=True,
    )
    # Route through a proxy when one is configured (proxies.txt / PROXY_LIST).
    # Chosen per LAUNCH, not per keyword: the session is deliberately reused across
    # a whole batch, so rotation happens when the session is rebuilt.
    _proxy = proxy_config(exclude=_BURNED_PROXIES)
    if _proxy:
        kwargs["proxy"] = _proxy
        print(f"[google] slot {slot} via proxy {_proxy['server']}")
    if _HAS_PATCHRIGHT:
        kwargs["channel"] = "chrome"
        kwargs["no_viewport"] = False
    try:
        ctx = await pw.chromium.launch_persistent_context(**kwargs)
    except Exception as e:
        if is_profile_locked_error(e):
            # An interrupted run left Chrome holding this profile — clear and retry.
            n = release_profile(user_dir)
            print(f"[google] profile was locked — released ({n} process(es) killed), retrying")
            ctx = await pw.chromium.launch_persistent_context(**kwargs)
        else:
            # Real Chrome missing — fall back to bundled Chromium.
            kwargs.pop("channel", None)
            try:
                ctx = await pw.chromium.launch_persistent_context(**kwargs)
            except Exception as e2:
                if not is_profile_locked_error(e2):
                    raise
                release_profile(user_dir)
                ctx = await pw.chromium.launch_persistent_context(**kwargs)

    # Built here rather than via persistent_context(), so it didn't inherit that
    # helper's resource blocking — every search was re-downloading images/fonts/media
    # (a SERP is image-heavy). We only read text and hrefs.
    if resource_blocking_enabled():
        try:
            await ctx.route("**/*", _route_block)
        except Exception:
            pass

    page = await single_page(ctx)
    try:
        await page.goto(GOOGLE_HOME, wait_until="domcontentloaded", timeout=60_000)
    except Exception:
        pass
    await _dismiss_consent(page)

    sess = {"pw": pw, "ctx": ctx, "page": page, "loop": loop,
            "proxy": (_proxy or {}).get("server", "")}
    _SESSIONS[slot] = sess
    return sess


async def _close_slot(slot: int) -> None:
    sess = _SESSIONS.pop(slot, None)
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


async def close_session() -> None:
    """Close EVERY pooled Chrome. Called once at the end of a scan batch.
    Best-effort if the owning loop already closed."""
    for slot in list(_SESSIONS):
        await _close_slot(slot)


async def _run_search(page, prompt: str) -> None:
    """Type `prompt` into the search box (clearing any previous query) and
    submit — the human flow, reusing the open tab. Never navigates by URL."""
    box = page.locator(_SEARCH_BOX).first
    await box.wait_for(state="visible", timeout=20_000)
    await box.click()
    # Clear whatever the previous keyword left behind.
    #
    # This used to be Control+A then Delete. On macOS Ctrl+A is the emacs
    # "move to start of line" binding, NOT select-all (that is Cmd+A), so the box
    # was never cleared: the caret jumped to the front, Delete ate a single
    # character, and the next keyword was typed in FRONT of the previous one.
    # Over a batch the query grew into a run-on string —
    #   "ways to save on bus tickets in India" + "hotel booking coupon code"
    #     -> "ways to save on bus tickets in Indiaotel booking coupon code"
    # (note the missing 'h') — so every keyword after the first searched garbage.
    # fill("") clears the field directly and is platform-independent.
    try:
        await box.fill("")
    except Exception:
        # Fall back to a real select-all if fill is refused by the element.
        await page.keyboard.press("Meta+A")
        await page.keyboard.press("Control+A")
        await page.keyboard.press("Delete")
    # Verify it actually emptied — a stale value here corrupts the search.
    try:
        left = await box.input_value()
    except Exception:
        left = ""
    if left:
        try:
            await box.press("Meta+A")
            await box.press("Backspace")
        except Exception:
            pass
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
    async with _POOL.slot() as slot:
      rotations = 0
      while True:
        try:
            sess = await _ensure_session(headless, slot)
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
                # This exit is flagged. Burn it, rebuild the slot on a fresh proxy
                # and try the same keyword again rather than losing it — the wall
                # is tied to the IP, so a different exit usually sails through.
                burned = sess.get("proxy") or ""
                if burned and rotations < CAPTCHA_ROTATIONS:
                    _BURNED_PROXIES.add(burned)
                    rotations += 1
                    print(f"[google] CAPTCHA on {burned} — rotating "
                          f"({rotations}/{CAPTCHA_ROTATIONS})")
                    await _close_slot(slot)
                    continue
                await _dump_failure(page, "captcha")
                res.error = (
                    "Google CAPTCHA wall on every exit tried"
                    if burned else
                    "Google CAPTCHA wall — solve it in the open window, then re-run.")
                return res

            data = await _await_ai_overview(page)
            if not data:
                res.error = "No AI Overview for this query."
                return res

            res.response_text = _clean_text(data.get("text") or "")
            raw_links = data.get("links") or []
            # AIO citations are /goto?url=<opaque token>; only the redirect knows
            # the publisher, so resolve those before filtering. Without this every
            # citation looks like www.google.com and gets dropped by _is_source.
            resolved = await _resolve_redirects(page, raw_links)
            cites: list[str] = []
            for h in raw_links:
                u = resolved.get(h) or _unwrap(h)
                if _is_source(u):
                    cites.append(u)
            res.citations = dedupe(cites)
            return res                      # success — leave the rotation loop
        except Exception as e:
            res.error = f"{type(e).__name__}: {e}"
            return res                      # don't spin: only a CAPTCHA retries
    return res
