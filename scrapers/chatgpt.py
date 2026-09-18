"""ChatGPT (anonymous chatgpt.com) with network interception.

Driven via URL-prefill at the user's explicit request (2026-05-29): navigate
directly to `https://chatgpt.com/?q=<urlencoded prompt>` so ChatGPT
auto-submits the prompt without us needing to find/click/type into the
ProseMirror composer (which keeps redesigning and breaking selector chains).

Always runs headed via patchright + real Chrome (the only combo that
auto-passes Cloudflare Turnstile on residential IPs). The `headless`
argument is accepted for API parity but ignored.

Persistent session — like the Google AIO scraper (2026-06-03): DO NOT open
and close Chrome per keyword. One headed Chrome (patchright + real Chrome) is
launched once via `_ensure_session`, lands on chatgpt.com, clears Cloudflare
Turnstile a single time, and is then reused for every prompt in a batch —
each new keyword navigates the SAME open tab to the `?q=` prefill URL (the
cf_clearance cookie from the first load means Turnstile no longer re-fires).
`close_session()` tears it down once the whole scan finishes (called from the
app's run handler). Relaunching Chrome per keyword was slow and flashed a new
window every time.

Scrapes are serialized through a module-level lock: the single shared page /
persistent Chromium user-data-dir can only handle one prompt at a time, so
concurrent calls would type over each other / TargetClosedError.

A `page.on('response', ...)` listener captures backend payloads at
`chatgpt.com/backend-anon/...`, surfacing what the DOM hides:

  1. `search_model_queries.queries[]` — the fanout queries ChatGPT issued
     under the hood (what it actually searched, not what the user typed).
  2. `search_result_groups[].entries[].url` — the full source URL list.
  3. `search("...")` tool calls inline in streamed content.
"""
from __future__ import annotations

import asyncio
import time
from urllib.parse import urlencode

from .base import ScrapeResult, DATA_DIR, profile_dir
from ._browser import (
    persistent_context,
    wait_quiet,
    wait_for_stable_text,
    make_response_capturer,
    dedupe,
)

try:
    from patchright.async_api import async_playwright as async_patchright  # type: ignore
    _HAS_PATCHRIGHT = True
except Exception:  # pragma: no cover
    from playwright.async_api import async_playwright as async_patchright  # type: ignore
    _HAS_PATCHRIGHT = False


# The answer container in current ChatGPT is `.markdown` (the rendered
# markdown of the latest assistant turn). `[data-message-author-role]` is no
# longer applied to assistant messages in the anonymous UI. We try a chain.
RESP_SELECTORS = (
    ".markdown",
    "[data-message-author-role='assistant']",
    "[data-testid^='conversation-turn'] .markdown",
    "article .markdown",
)
RESP_SEL = ", ".join(RESP_SELECTORS)

# ChatGPT's anonymous flow has TWO elements sharing id="prompt-textarea":
#   1. <textarea id="prompt-textarea" style="display:none">  — hidden fallback
#   2. <div     id="prompt-textarea" contenteditable="true"> — the real editor
# A bare `#prompt-textarea` selector hits the textarea (first in DOM order),
# which never becomes visible, so wait_for_selector(state="visible") times
# out. List ONLY selectors that uniquely target the real composer.
COMPOSER_SELECTORS = (
    "div#prompt-textarea[contenteditable='true']",
    "div[contenteditable='true'].ProseMirror",
    "div[aria-label='Chat with ChatGPT'][contenteditable='true']",
    "div[role='textbox'][contenteditable='true']",
    "div[contenteditable='true'][data-virtualkeyboard='true']",
    "div[contenteditable='true']",
    "textarea[data-testid='prompt-textarea']",
    "main form textarea",
)
COMPOSER_SEL = ", ".join(COMPOSER_SELECTORS)

DISMISS_SELECTORS = (
    "button:has-text('Stay logged out')",
    "button:has-text('Not now')",
    "button:has-text('Maybe later')",
    "button:has-text('Reject')",
    "button:has-text('Accept all')",
    "button:has-text('Accept')",
    "button[aria-label='Close']",
    "button[aria-label='Dismiss']",
    "[data-testid='close-button']",
    "[data-testid='modal-close-button']",
)

_DEBUG_DIR = DATA_DIR / "debug"

# Persistent Chromium profile dirs are exclusive — one Chrome process per
# user_data_dir, one shared page reused across keywords. Serialize ChatGPT
# scrapes so parallel prompts don't fight over the lock / type over each other.
_SCRAPE_LOCK = asyncio.Lock()

# The live browser session, reused across keywords (mirrors scrapers/google.py).
# Bound to the event loop it was created in (each "Run scan" is one
# asyncio.run → one loop); if the loop changes we tear down and relaunch.
_SESSION: dict | None = None


async def _dump_failure(page, tag: str) -> None:
    """Save screenshot + HTML so we can see what blocked the scraper."""
    try:
        _DEBUG_DIR.mkdir(parents=True, exist_ok=True)
        stamp = time.strftime("%Y%m%d_%H%M%S")
        base = _DEBUG_DIR / f"chatgpt_fail_{tag}_{stamp}"
        try:
            await page.screenshot(path=str(base.with_suffix(".png")), full_page=True)
        except Exception:
            pass
        try:
            (base.with_suffix(".html")).write_text(
                await page.content(), encoding="utf-8"
            )
        except Exception:
            pass
        try:
            (base.with_suffix(".url.txt")).write_text(
                f"{page.url}\n", encoding="utf-8"
            )
        except Exception:
            pass
    except Exception:
        pass


async def _dismiss_modals(page) -> None:
    for sel in DISMISS_SELECTORS:
        try:
            loc = page.locator(sel).first
            if await loc.count():
                await loc.click(timeout=600)
        except Exception:
            pass


_TURNSTILE_IFRAME = "iframe[src*='challenges.cloudflare.com']"


async def _solve_turnstile(page, *, total_timeout_ms: int = 30_000) -> bool:
    """Detect + click the Cloudflare Turnstile 'Verify you are human' checkbox.

    Returns True if the challenge was present (whether or not we successfully
    cleared it), False if no challenge was detected.
    """
    try:
        title = (await page.title()).lower()
        iframe_count = await page.locator(_TURNSTILE_IFRAME).count()
        if "just a moment" not in title and iframe_count == 0:
            return False

        deadline = time.monotonic() + total_timeout_ms / 1000

        try:
            await page.wait_for_selector(
                _TURNSTILE_IFRAME, state="visible", timeout=10_000
            )
        except Exception:
            try:
                await page.wait_for_function(
                    "() => !document.title.toLowerCase().includes('just a moment')",
                    timeout=15_000,
                )
            except Exception:
                pass
            return True

        iframe_loc = page.locator(_TURNSTILE_IFRAME).first
        clicked = False

        try:
            frame = page.frame_locator(_TURNSTILE_IFRAME).first
            checkbox = frame.locator("input[type='checkbox']").first
            if await checkbox.count():
                await checkbox.click(timeout=4_000)
                clicked = True
        except Exception:
            pass

        if not clicked:
            try:
                box = await iframe_loc.bounding_box()
                if box:
                    cx = box["x"] + 30
                    cy = box["y"] + box["height"] / 2
                    try:
                        await page.mouse.move(cx - 60, cy - 20, steps=8)
                        await page.mouse.move(cx, cy, steps=10)
                    except Exception:
                        pass
                    await page.mouse.click(cx, cy, delay=80)
                    clicked = True
            except Exception:
                pass

        remaining_ms = max(1000, int((deadline - time.monotonic()) * 1000))
        try:
            await page.wait_for_function(
                """() => {
                    const t = (document.title || '').toLowerCase();
                    if (t.includes('just a moment')) return false;
                    const f = document.querySelector("iframe[src*='challenges.cloudflare.com']");
                    if (f && f.offsetParent !== null) return false;
                    return true;
                }""",
                timeout=remaining_ms,
            )
        except Exception:
            pass
        return True
    except Exception:
        return False


async def unlock_turnstile(timeout_s: int = 600) -> bool:
    """Open a headed Chrome window on chatgpt.com so the user can clear the
    Cloudflare challenge by hand. Persists cf_clearance to the same profile
    the scraper uses."""
    async with _SCRAPE_LOCK:
        async with persistent_context(
            "chatgpt", headless=False, block_resources=False, engine="patchright",
        ) as ctx:
            page = ctx.pages[0] if ctx.pages else await ctx.new_page()
            try:
                await page.goto(
                    "https://chatgpt.com/",
                    wait_until="domcontentloaded",
                    timeout=60_000,
                )
            except Exception:
                return False

            deadline = time.monotonic() + timeout_s
            while time.monotonic() < deadline:
                try:
                    if await page.locator(COMPOSER_SEL).count():
                        await page.wait_for_timeout(1500)
                        return True
                except Exception:
                    pass
                await page.wait_for_timeout(800)
            return False


_SKIP_HOSTS = ("openai.com", "chatgpt.com", "oaistatic.com", "oaiusercontent.com")


def _is_chatgpt_backend(url: str) -> bool:
    return "chatgpt.com/backend-anon/" in url or "chatgpt.com/backend-api/" in url


async def _ensure_session() -> dict:
    """Return a live session for the current loop, launching one headed Chrome
    if needed. Reuses the open Chrome/tab across keywords (the whole point) and
    clears Turnstile once on the homepage so later `?q=` navigations sail through.
    Mirrors scrapers/google.py's `_ensure_session`."""
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

    user_dir = str(profile_dir("chatgpt"))
    pw = await async_patchright().start()
    kwargs = dict(
        user_data_dir=user_dir,
        headless=False,  # Turnstile blocks headless regardless of URL — always headed.
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
        await page.goto(
            "https://chatgpt.com/", wait_until="domcontentloaded", timeout=60_000
        )
    except Exception:
        pass
    # Clear the Cloudflare challenge a single time; the cf_clearance cookie then
    # persists for the rest of the batch so per-keyword `?q=` loads don't re-gate.
    await _solve_turnstile(page)
    await _dismiss_modals(page)

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


async def scrape(prompt: str, headless: bool = True) -> ScrapeResult:
    # `headless` is accepted for API parity but intentionally ignored — see
    # project_chatgpt_turnstile memory. Always headed + patchright + real Chrome.
    # The shared session means we hold the lock for the whole search and reuse
    # the one open tab (see module docstring) instead of relaunching Chrome.
    res = ScrapeResult(platform="chatgpt", prompt=prompt)
    async with _SCRAPE_LOCK:
        try:
            sess = await _ensure_session()
            page = sess["page"]

            handler, captured_queries, captured_urls = make_response_capturer(
                _is_chatgpt_backend, skip_hosts=_SKIP_HOSTS,
                debug_platform="chatgpt", debug_host_match="chatgpt.com",
            )
            page.on("response", handler)
            try:
                # 1. Navigate the EXISTING tab to the search URL — ChatGPT
                #    auto-submits the prompt from the `q` query param (no
                #    composer interaction, sidestepping ProseMirror selector
                #    drift). Reusing the warm tab means Turnstile is already
                #    cleared, so this load goes straight to the answer.
                prefill_url = "https://chatgpt.com/?" + urlencode({"q": prompt})
                try:
                    await page.goto(
                        prefill_url,
                        wait_until="domcontentloaded",
                        timeout=60_000,
                    )
                except Exception as e:
                    await _dump_failure(page, "goto")
                    res.error = f"goto failed: {type(e).__name__}: {e}"
                    return res

                await _solve_turnstile(page)
                await wait_quiet(page, idle_ms=400)
                await _dismiss_modals(page)

                # 2. Wait for the assistant turn.
                try:
                    await page.wait_for_selector(RESP_SEL, timeout=60_000)
                except Exception:
                    await _dump_failure(page, "response")
                    res.error = "Assistant response never appeared."
                    return res

                await wait_for_stable_text(
                    page, RESP_SEL, idle_ms=1400, max_ms=180_000, min_chars=80
                )

                try:
                    await page.wait_for_selector(
                        '[data-testid="webpage-citation-pill"]', timeout=3000
                    )
                except Exception:
                    pass
                await page.wait_for_timeout(300)

                # ---- DOM extraction ----
                # Read via `evaluate("e => e.innerText")`, NOT `inner_text()`.
                # ChatGPT's `markdown-new-styling` renderer re-renders the
                # `.markdown` subtree on every streamed token, so the element
                # handle `inner_text()` resolves keeps detaching mid-call and
                # the actionability retry loop times out (returning empty text
                # while citations still populate). A one-shot synchronous
                # innerText read grabs whatever node matches at that instant —
                # the same trick `wait_for_stable_text` already relies on.
                last_msg = page.locator(RESP_SEL).last
                try:
                    txt = (await last_msg.evaluate("e => e.innerText || ''")).strip()
                    if txt:
                        res.response_text = txt[:50000]
                except Exception:
                    pass

                if not res.response_text:
                    for sel in RESP_SELECTORS:
                        try:
                            loc = page.locator(sel).last
                            if await loc.count():
                                txt = (await loc.evaluate("e => e.innerText || ''")).strip()
                                if txt:
                                    res.response_text = txt[:50000]
                                    break
                        except Exception:
                            continue

                visible_anchors: list[str] = []
                if await last_msg.count():
                    try:
                        visible_anchors = await last_msg.locator("a").evaluate_all(
                            "els => els.map(e => e.href).filter(h => h && /^https?:/.test(h))"
                        )
                    except Exception:
                        visible_anchors = []

                citations: list[str] = []
                for h in visible_anchors:
                    host = h.split("/", 3)[2].lower() if "://" in h else ""
                    if any(s in host for s in _SKIP_HOSTS):
                        continue
                    citations.append(h)

                try:
                    pills = await page.locator(
                        '[data-testid="webpage-citation-pill"] a[href]'
                    ).evaluate_all(
                        "els => els.map(e => e.href).filter(h => h && /^https?:/.test(h))"
                    )
                except Exception:
                    pills = []
                for h in pills:
                    host = h.split("/", 3)[2].lower() if "://" in h else ""
                    if any(s in host for s in _SKIP_HOSTS):
                        continue
                    citations.append(h)

                res.citations = dedupe(citations)
                res.source_urls = dedupe(captured_urls)
                res.fanout_queries = dedupe(captured_queries)
            finally:
                # Always detach this scrape's listener; the page lives on for
                # the next keyword, so a leaked listener would keep re-reading
                # every response body and pile up across the batch.
                try:
                    page.remove_listener("response", handler)
                except Exception:
                    pass

        except Exception as e:
            res.error = f"{type(e).__name__}: {e}"
    return res
