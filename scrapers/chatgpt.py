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
import os
import re
import time
from urllib.parse import urlencode

from .base import ScrapeResult, DATA_DIR, profile_dir
from ._browser import (
    persistent_context,
    wait_quiet,
    wait_for_stable_text,
    make_response_capturer,
    dedupe,
    release_profile,
    is_profile_locked_error,
    _route_block,
    resource_blocking_enabled,
    sanitize_profile,
    CRASH_BUBBLE_ARGS,
    single_page,
    SlotPool,
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
    # Current anonymous UI (the "octane" /unauth-mweb bundle). Its CSS classes are
    # stylex hashes that rotate every build, but these data-* attributes are stable.
    # NOTE the rename: data-message-author-role -> data-message-role.
    "[data-assistant-markdown]",
    "[data-message-role='assistant']",
    # Legacy desktop UI — kept so a logged-in / older render still works.
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
# A POOL of independent Chromes, not one. Each slot gets its own profile dir
# (profiles/chatgpt_0, _1, …) — a persistent profile can only back one live
# browser. CHATGPT_POOL sets the width; 1 restores the old serial behaviour.
# NOTE each slot clears Cloudflare Turnstile separately on its first load, so a
# wider pool means more cold starts (see COLD_BUDGET_S and the warm-up note).
POOL_SIZE = int(os.getenv("CHATGPT_POOL", "3"))
_POOL = SlotPool(POOL_SIZE)

# {slot: session}, each reused across keywords and bound to the loop that made it.
_SESSIONS: dict[int, dict] = {}


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


async def _unlock_one(profile: str, timeout_s: int) -> bool:
    """Clear Turnstile for ONE profile, leaving cf_clearance behind."""
    async with persistent_context(
        profile, headless=False, block_resources=False, engine="patchright",
    ) as ctx:
        page = await single_page(ctx)
        try:
            await page.goto("https://chatgpt.com/", wait_until="domcontentloaded",
                            timeout=60_000)
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


async def unlock_turnstile(timeout_s: int = 600) -> bool:
    """Warm EVERY pooled profile so no keyword pays the cold Turnstile stall.

    Each pool slot has its own profile dir, and Cloudflare clearance is per-profile
    — warming only `chatgpt` would leave chatgpt_0..N-1 cold, so the first keyword
    on each slot would still hang. Opens them one at a time; a window appears for
    each, and closes itself once the composer is reachable.
    """
    ok = True
    for i in range(POOL_SIZE):
        prof = f"chatgpt_{i}"
        print(f"[chatgpt] warming profile {prof} ({i + 1}/{POOL_SIZE}) …")
        try:
            got = await _unlock_one(prof, timeout_s)
        except Exception as e:
            print(f"[chatgpt] {prof}: {type(e).__name__}: {str(e)[:80]}")
            got = False
        print(f"[chatgpt] {prof}: {'cleared' if got else 'NOT cleared'}")
        ok = ok and got
    return ok


_SKIP_HOSTS = ("openai.com", "chatgpt.com", "oaistatic.com", "oaiusercontent.com")


def _is_chatgpt_backend(url: str) -> bool:
    return "chatgpt.com/backend-anon/" in url or "chatgpt.com/backend-api/" in url


async def _ensure_session(slot: int = 0) -> dict:
    """Return a live session for the current loop, launching one headed Chrome
    if needed. Reuses the open Chrome/tab across keywords (the whole point) and
    clears Turnstile once on the homepage so later `?q=` navigations sail through.
    Mirrors scrapers/google.py's `_ensure_session`."""
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
    user_dir = str(profile_dir(f"chatgpt_{slot}"))
    # A profile left "crashed" opens on the Restore-pages bubble and parks the
    # tab on about:blank — clear it before every launch, not just after a kill.
    sanitize_profile(user_dir)
    pw = await async_patchright().start()
    kwargs = dict(
        user_data_dir=user_dir,
        headless=False,  # Turnstile blocks headless regardless of URL — always headed.
        viewport={"width": 1280, "height": 900},
        args=["--no-default-browser-check", *CRASH_BUBBLE_ARGS],
    )
    if _HAS_PATCHRIGHT:
        kwargs["channel"] = "chrome"
        kwargs["no_viewport"] = False
    try:
        ctx = await pw.chromium.launch_persistent_context(**kwargs)
    except Exception as e:
        if is_profile_locked_error(e):
            # An interrupted run left Chrome holding this profile. Clear it and
            # retry once, so the user isn't stuck killing processes by hand.
            n = release_profile(user_dir)
            print(f"[chatgpt] profile was locked — released ({n} process(es) killed), retrying")
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

    # This context is built here rather than via persistent_context(), so it did NOT
    # inherit that helper's resource blocking — every keyword was re-downloading
    # images/fonts/media (the octane bundle, plus a favicon fetch per citation).
    # We only ever read text and attributes, so none of it is needed.
    if resource_blocking_enabled():
        try:
            await ctx.route("**/*", _route_block)
        except Exception:
            pass

    page = await single_page(ctx)
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

    sess = {"pw": pw, "ctx": ctx, "page": page, "loop": loop}
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


# Has this process already cleared Turnstile on a live session? The FIRST load of
# chatgpt.com on a cold profile has to pass the challenge and can take minutes (or
# stall); every load after it rides the cf_clearance cookie and takes ~8-12s.
_WARMED = False

# Hard ceilings. A cold profile must clear Turnstile and can stall in a way that
# ignores page.goto's own timeout (measured: a 150s wait_for overran to 445s), so
# the whole scrape is bounded here rather than trusting the inner timeouts.
COLD_BUDGET_S = 100
WARM_BUDGET_S = 90


async def scrape(prompt: str, headless: bool = True) -> ScrapeResult:
    """Scrape one prompt, retrying once if the COLD session never came up.

    Measured: keyword 1 on a fresh session timed out at 150s, keywords 2 and 3 on
    the same session took 7.9s and 11.4s. That is Turnstile being cleared on the
    first load. Failing keyword 1 of every scan is not acceptable, so a cold-start
    failure tears the session down and tries once more — by then the profile holds
    cf_clearance and the retry is fast.
    """
    global _WARMED
    budget = COLD_BUDGET_S if not _WARMED else WARM_BUDGET_S
    try:
        res = await asyncio.wait_for(_scrape_once(prompt, headless), timeout=budget)
    except asyncio.TimeoutError:
        res = ScrapeResult(platform="chatgpt", prompt=prompt)
        res.error = (f"timed out after {budget}s")

    cold_fail = (not res.response_text) and not _WARMED
    if cold_fail:
        print("[chatgpt] cold session produced nothing — resetting and retrying once")
        try:
            await close_session()
        except Exception:
            pass
        try:
            res = await asyncio.wait_for(_scrape_once(prompt, headless),
                                         timeout=COLD_BUDGET_S)
        except asyncio.TimeoutError:
            res = ScrapeResult(platform="chatgpt", prompt=prompt)
            res.error = (
                "ChatGPT's first load could not clear Cloudflare Turnstile. Warm the "
                "profile once with:  python3 -m scrapers.login chatgpt   "
                "(click the 'Verify you are human' box if it appears, then close). "
                "Later keywords in this scan should work.")
            try:
                await close_session()
            except Exception:
                pass
    if res.response_text:
        _WARMED = True
    return res


async def _scrape_once(prompt: str, headless: bool = True) -> ScrapeResult:
    # `headless` is accepted for API parity but intentionally ignored — see
    # project_chatgpt_turnstile memory. Always headed + patchright + real Chrome.
    # The shared session means we hold the lock for the whole search and reuse
    # the one open tab (see module docstring) instead of relaunching Chrome.
    res = ScrapeResult(platform="chatgpt", prompt=prompt)
    async with _POOL.slot() as slot:
        try:
            sess = await _ensure_session(slot)
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
                # No networkidle: ChatGPT streams over an open socket so it never
                # fires; the RESP_SEL wait below is the real readiness signal.
                await wait_quiet(page, idle_ms=250, net_idle=False)
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

                # Wait for the citation chips to attach. The old
                # [data-testid="webpage-citation-pill"] hook no longer exists in the
                # anonymous UI, so it could only ever burn its full timeout — every
                # keyword paid 3s for nothing. Wait on the attribute the sources are
                # actually read from, which resolves the moment they render.
                try:
                    await page.wait_for_selector(
                        "[data-assistant-sources-payload]", timeout=2500
                    )
                except Exception:
                    pass

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

                # The anonymous "octane" UI renders citations as chips
                # ("C CashKaro +1") that are <button>s, NOT anchors — there is no
                # href anywhere and nothing in the network payloads. The sources
                # ride along in a JSON attribute on the chip:
                #   data-assistant-sources-payload='[{"attribution":"CashKaro",…}]'
                # Parse the JSON, falling back to a URL regex so a schema rename
                # still yields the links.
                try:
                    pills = await page.evaluate(
                        """() => {
                            const out = [];
                            const push = u => {
                                if (typeof u === 'string' && /^https?:/i.test(u)) out.push(u);
                            };
                            document.querySelectorAll(
                                '[data-assistant-sources-payload]'
                            ).forEach(el => {
                                const raw = el.getAttribute(
                                    'data-assistant-sources-payload') || '';
                                let parsed = null;
                                try { parsed = JSON.parse(raw); } catch (e) {}
                                if (parsed) {
                                    (Array.isArray(parsed) ? parsed : [parsed]).forEach(s => {
                                        if (!s || typeof s !== 'object') return;
                                        ['url', 'link', 'href', 'sourceUrl'].forEach(
                                            k => push(s[k]));
                                    });
                                }
                                if (!out.length) {
                                    const m = raw.match(/https?:\\/\\/[^\\s"'<>)\\\\]+/g);
                                    if (m) m.forEach(push);
                                }
                            });
                            return out;
                        }"""
                    )
                except Exception:
                    pills = []
                for h in pills:
                    # strip ChatGPT's referral param so domains aggregate cleanly
                    h = re.sub(r"[?&]utm_source=chatgpt\.com\b", "", h).rstrip("?&")
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
