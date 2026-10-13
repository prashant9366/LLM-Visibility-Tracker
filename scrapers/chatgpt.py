"""ChatGPT (chatgpt.com) with network interception.

Driven by TYPING into the composer and pressing Enter (2026-10-13, at the
user's request). Each keyword loads plain `https://chatgpt.com/` — which also
guarantees a fresh conversation, so keywords never pile into one thread — then
the prompt is typed in and sent with Enter.

This replaced the `?q=<urlencoded prompt>` prefill URL used since 2026-05-29.
That worked only while the scraper ran anonymous: ChatGPT auto-submits a `q`
param for a signed-out visitor, but once SIGNED IN it merely fills the
composer and waits for a human. The prompt sat there unsent while the scrape
waited out its 60s response timeout and reported "Assistant response never
appeared". Typing + Enter behaves identically on both.

Always runs headed via patchright + real Chrome (the only combo that
auto-passes Cloudflare Turnstile on residential IPs). The `headless`
argument is accepted for API parity but ignored.

Persistent session — like the Google AIO scraper (2026-06-03): DO NOT open
and close Chrome per keyword. One headed Chrome (patchright + real Chrome) is
launched once via `_ensure_session`, lands on chatgpt.com, clears Cloudflare
Turnstile a single time, and is then reused for every prompt in a batch —
each new keyword reloads chatgpt.com in the SAME open tab (the
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
import shutil
import sqlite3
import tempfile
import time

from .base import ScrapeResult, DATA_DIR, profile_dir
from ._browser import (
    persistent_context,
    wait_quiet,
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

# Which driver launches the browser.
#   patchright  (default) — patched Chromium driver, real Chrome
#   playwright            — plain Playwright, still real Chrome. The "simple
#                           browser": no patching, nothing clever.
#   chromium              — plain Playwright on its own bundled Chromium.
#                           NOTE this can lose the login: recent Chrome
#                           encrypts cookie values with a key bound to the
#                           Chrome executable, which bundled Chromium cannot
#                           decrypt. Expect to sign in again if you use it.
#   simple                — bare Playwright Chromium. No profile directory,
#                           no cookies, no login, none of the hardening. One
#                           browser.launch(), one context, one page. Runs
#                           ANONYMOUS, which is the path Cloudflare checks
#                           hardest, so expect Turnstile.
CHATGPT_ENGINE = (os.getenv("CHATGPT_ENGINE", "patchright").strip().lower()
                  or "patchright")

# The bare path, chosen 2026-10-13. Deliberately skips the signed-in-profile
# requirement: there is no profile to be signed in. Everything that exists to
# protect or reuse a login is bypassed, not just unused.
SIMPLE = CHATGPT_ENGINE == "simple"

try:
    if CHATGPT_ENGINE != "patchright":
        raise ImportError(f"CHATGPT_ENGINE={CHATGPT_ENGINE}")
    from patchright.async_api import async_playwright as async_patchright  # type: ignore
    _HAS_PATCHRIGHT = True
except Exception as _e:  # pragma: no cover
    from playwright.async_api import async_playwright as async_patchright  # type: ignore
    _HAS_PATCHRIGHT = False
    # Said out loud, because the fallback is otherwise silent. Vanilla
    # Playwright launches Chrome with --disable-blink-features=Automation-
    # Controlled among its defaults, which raises Chrome's yellow
    # "unsupported command-line flag" infobar -- visible proof to anyone
    # looking, and an automation tell. If that banner is on screen, this
    # branch is why.
    if CHATGPT_ENGINE == "patchright":
        print("[chatgpt] patchright NOT available "
              f"({type(_e).__name__}) — falling back to vanilla Playwright, "
              "which Cloudflare fingerprints. Fix with:\n"
              "    pip install patchright && python -m patchright install chrome")
    elif CHATGPT_ENGINE == "simple":
        pass                      # announced by _resolve_profiles instead
    else:
        print(f"[chatgpt] engine: {CHATGPT_ENGINE} (plain Playwright"
              + (", bundled Chromium)" if CHATGPT_ENGINE == "chromium"
                 else ", real Chrome)"))


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

# Selectors that identify an assistant TURN (a whole message), tried in order.
# Checked before the generic list because `.markdown` also matches fragments
# nested inside a rich answer -- the product cards in a shopping reply each
# carry their own block.
ANSWER_CONTAINERS = (
    "[data-message-author-role='assistant']",
    "[data-message-role='assistant']",
    "[data-assistant-markdown]",
    "[data-testid^='conversation-turn']:last-of-type .markdown",
)

# Pick the answer in ONE synchronous page call.
#
# Replaces `page.locator(RESP_SEL).last`, which was the bug behind "it is on
# screen but nothing is fetched": `.last` is the last node matching ANY of the
# six selectors in DOM order, and in a rich answer that is a trailing fragment
# -- one card's block -- not the message. The scrape then stored a few words,
# or nothing, while a complete answer sat in the window.
#
# Done in JS rather than via locators because ChatGPT re-renders the markdown
# subtree on every streamed token: an element handle resolved on the Python
# side keeps detaching mid-call.
# Returns {text, links} from the SAME node. Measured live 2026-10-13: the
# citations are ordinary <a href> inside the answer -- the old
# [data-assistant-sources-payload] chips are gone (0 nodes on the page) and
# the network capture yields nothing. So the anchors ARE the sources, and
# reading them from a different element than the text is how they go missing.
_PICK_ANSWER_JS = """
(sels) => {
  const textOf = (n) => ((n && n.innerText) || '').trim();
  const linksOf = (n) => !n ? [] : Array.from(n.querySelectorAll('a'))
      .map(a => a.href).filter(h => /^https?:/i.test(h));
  const pack = (n) => ({ text: textOf(n), links: linksOf(n) });
  // 1. A real assistant turn, newest first.
  for (const s of sels.containers) {
    let m;
    try { m = document.querySelectorAll(s); } catch (e) { continue; }
    if (m.length) {
      const got = pack(m[m.length - 1]);
      if (got.text) return got;
    }
  }
  // 2. Otherwise the node with the LONGEST text. Length is the tie-break
  //    that makes this robust to class names rotating: the answer is always
  //    the biggest thing on the page. Chosen as a NODE, not as a string, so
  //    its anchors come back with it.
  let best = null, bestLen = -1;
  for (const s of sels.any) {
    let m;
    try { m = document.querySelectorAll(s); } catch (e) { continue; }
    for (const n of m) {
      const len = textOf(n).length;
      if (len > bestLen) { bestLen = len; best = n; }
    }
  }
  return best ? pack(best) : { text: '', links: [] };
}
"""


# ChatGPT's own render failure: the turn exists, the action row is there, but
# the body says "This response couldn't load" (usually under a "Worked for Ns"
# line). The backend produced something the client could not show, so there is
# no answer to extract and waiting changes nothing -- the fix is to press
# regenerate, which re-requests the same prompt in the same thread.
_RESPONSE_FAILED_RE = re.compile(
    r"this response (?:couldn'?t|could not|didn'?t|did not) load", re.I)

REGENERATE_SELECTORS = (
    "button[data-testid='regenerate-thread-button']",
    "button[aria-label='Retry']",
    "button[aria-label='Regenerate']",
    "button[aria-label='Try again']",
    "button:has-text('Regenerate')",
    "button:has-text('Try again')",
)


async def _regenerate_if_failed(page, *, attempts: int = 2) -> bool:
    """Press regenerate while the answer says it could not load.

    Returns True if a retry was issued (so the caller re-waits), False when
    there was nothing wrong or nothing to click.
    """
    issued = False
    for _ in range(attempts):
        try:
            body = (await page.inner_text("body"))[:6000]
        except Exception:
            return issued
        if not _RESPONSE_FAILED_RE.search(body):
            return issued
        clicked = False
        for sel in REGENERATE_SELECTORS:
            try:
                loc = page.locator(sel).last
                if await loc.count() and await loc.is_visible():
                    await loc.click(timeout=5_000)
                    clicked = True
                    break
            except Exception:
                continue
        if not clicked:
            print("[chatgpt] response failed to load and no regenerate button "
                  "was found")
            return issued
        issued = True
        print("[chatgpt] response failed to load — regenerating")
        try:
            await page.wait_for_timeout(2_000)
        except Exception:
            break
        await _wait_answer_stable(
            page, idle_ms=1400, max_ms=ANSWER_MAX_S * 1000, min_chars=80)
    return issued


# Screen-reader label the turn container carries ("ChatGPT said:" / "You
# said:"). Seen live on the extracted text; stripped so it does not show up
# as the first line of every stored answer.
_SPEAKER_PREFIX_RE = re.compile(r"^\s*(?:chatgpt|you)\s+said:\s*", re.I)


async def _answer_payload(page) -> dict:
    """{'text': str, 'links': [str]} for the assistant turn, from one node."""
    try:
        got = await page.evaluate(
            _PICK_ANSWER_JS,
            {"containers": list(ANSWER_CONTAINERS),
             "any": list(RESP_SELECTORS)})
    except Exception:
        return {"text": "", "links": []}
    if not isinstance(got, dict):
        return {"text": "", "links": []}
    return {
        "text": _SPEAKER_PREFIX_RE.sub("", got.get("text") or "").strip(),
        "links": [u for u in (got.get("links") or []) if isinstance(u, str)],
    }


async def _answer_text(page) -> str:
    """The assistant's answer, or '' when it is not on the page yet."""
    return (await _answer_payload(page))["text"]


async def _wait_answer_stable(page, *, idle_ms: int = 1400,
                              max_ms: int = 180_000,
                              poll_ms: int = 350,
                              min_chars: int = 80) -> str:
    """Poll until the answer stops growing for `idle_ms`. Returns the text.

    chatgpt-local rather than _browser.wait_for_stable_text because that
    helper measures `locator(sel).last` -- the same wrong element. Measuring
    one node and then reading another is how a 180s wait could expire on a
    fragment that never reached min_chars while the real answer sat finished
    beside it.
    """
    deadline = time.monotonic() + max_ms / 1000
    text = ""
    last_len = -1
    last_change = time.monotonic()
    while time.monotonic() < deadline:
        cur = await _answer_text(page)
        if cur:
            text = cur
        if len(cur) != last_len:
            last_len = len(cur)
            last_change = time.monotonic()
        elif last_len >= min_chars and (time.monotonic() - last_change) * 1000 >= idle_ms:
            return text
        try:
            await page.wait_for_timeout(poll_ms)
        except Exception:
            break
    return text

# ChatGPT's anonymous flow has TWO elements sharing id="prompt-textarea":
#   1. <textarea id="prompt-textarea" style="display:none">  — hidden fallback
#   2. <div     id="prompt-textarea" contenteditable="true"> — the real editor
# A bare `#prompt-textarea` selector hits the textarea (first in DOM order),
# which never becomes visible, so wait_for_selector(state="visible") times
# out. List ONLY selectors that uniquely target the real composer.
COMPOSER_SELECTORS = (
    # Measured live 2026-10-13: the anonymous UI is a plain <textarea
    # id="mobile-composer-prompt">; every contenteditable selector below
    # matches ZERO elements there. Listed first so the common case resolves
    # on the first try, with the div chain kept for the signed-in UI.
    "textarea#mobile-composer-prompt",
    "textarea#prompt-textarea",
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

# The send control. Only a FALLBACK -- Enter is the normal way the prompt
# goes (see _type_and_send). data-testid first (stable across builds), id
# second, the aria-labels last because the wording changes.
SEND_SELECTORS = (
    "button[data-testid='send-button']",
    "#composer-submit-button",
    "button[aria-label='Send prompt']",
    "button[aria-label='Send message']",
    "form button[type='submit']",
)

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
POOL_WIDTH = int(os.getenv("CHATGPT_POOL", "3"))

# Cookie names that mean "this profile is signed in". Matched by PATTERN as
# well as by exact name: OpenAI has renamed this cookie more than once, and a
# stale list here reads as "you are not logged in" however many times you log
# in. Anonymous profiles carry oai-did / oai-sc / _cfuvid and none of these.
SESSION_COOKIE_NAMES = ("__Secure-next-auth.session-token",
                        "__Session-session-token",
                        "__Secure-authjs.session-token")
SESSION_COOKIE_PATTERNS = ("%session-token%", "%next-auth%", "%authjs%")
# Default ON: when ANY pooled profile is signed in, use only the signed-in
# ones. Signing in by hand covers the single window that happened to be open,
# so the other slots stay anonymous -- and anonymous is the path Cloudflare
# scrutinises hardest. Mixing them means a third of keywords silently take the
# harder route and fail, which reads as "the login did nothing".
# CHATGPT_SIGNED_IN_ONLY=0 restores the old behaviour (use every slot).
SIGNED_IN_ONLY = (os.getenv("CHATGPT_SIGNED_IN_ONLY", "1").strip().lower()
                  not in ("0", "false", "no", "off"))


def _has_session_cookie(profile_name: str) -> bool:
    """True when this profile dir holds a live ChatGPT login.

    Chrome keeps cookies in a SQLite file it holds open, so it is copied to a
    temp file and read there -- querying it in place fails with 'database is
    locked' whenever a browser is running. Only cookie NAMES are read; the
    values are encrypted and we never need them.
    """
    base = profile_dir(profile_name)
    now_webkit = (time.time() + 11644473600) * 1_000_000
    for rel in ("Default/Network/Cookies", "Default/Cookies", "Network/Cookies"):
        src = base / rel
        if not src.exists():
            continue
        tmp = None
        try:
            with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as fh:
                tmp = fh.name
            shutil.copyfile(src, tmp)
            con = sqlite3.connect(tmp)
            try:
                marks = ",".join("?" * len(SESSION_COOKIE_NAMES))
                likes = " OR ".join(["name LIKE ?"] * len(SESSION_COOKIE_PATTERNS))
                row = con.execute(
                    f"SELECT 1 FROM cookies WHERE (name IN ({marks}) OR {likes}) "
                    "AND host_key LIKE '%chatgpt.com' "
                    "AND (expires_utc = 0 OR expires_utc > ?) LIMIT 1",
                    (*SESSION_COOKIE_NAMES, *SESSION_COOKIE_PATTERNS,
                     now_webkit)).fetchone()
            finally:
                con.close()
            if row:
                return True
        except Exception:
            continue
        finally:
            if tmp:
                try:
                    os.unlink(tmp)
                except OSError:
                    pass
    return False


def _resolve_profiles() -> list[str]:
    """Profile dirs this run will drive, in slot order."""
    everything = [f"chatgpt_{i}" for i in range(POOL_WIDTH)]
    if SIMPLE:
        # No profiles at all. The names are placeholders that only size the
        # pool; nothing reads them. Returning them keeps the "do we have
        # somewhere to run?" guards satisfied without a special case at each.
        print(f"[chatgpt] engine: simple — bare Chromium, no profile, "
              f"NO LOGIN, {POOL_WIDTH} browser(s)")
        return everything
    if not SIGNED_IN_ONLY:
        return everything
    signed = [p for p in everything if _has_session_cookie(p)]
    if not signed:
        # Deliberately NOT falling back to the anonymous profiles. Doing that
        # silently swapped in a logged-out browser whenever the signed-in one
        # was unavailable, so a run looked like it was using the account when
        # it was not -- and the anonymous path is the one Cloudflare blocks.
        # Better to stop and say so.
        print("[chatgpt] NO signed-in profile — refusing to fall back to an "
              "anonymous browser. Run `python -m scrapers.login chatgpt` "
              "(or set CHATGPT_SIGNED_IN_ONLY=0 to allow logged-out runs).")
        return []
    if len(signed) < len(everything):
        print(f"[chatgpt] using {len(signed)} signed-in profile(s) "
              f"({', '.join(signed)}); {len(everything) - len(signed)} "
              "anonymous slot(s) idle. Run `python -m scrapers.login chatgpt` "
              "to sign in the rest.")
    return signed


# Slot N drives PROFILES[N] — NOT chatgpt_N. The two diverge as soon as only
# some profiles are signed in, so every slot->dir lookup must go through here.
PROFILES = _resolve_profiles()
POOL_SIZE = len(PROFILES)
# max(1, …) only so SlotPool is constructible when PROFILES is empty; nothing
# runs in that state — _scrape_once refuses before it reaches a browser.
_POOL = SlotPool(max(1, POOL_SIZE))

NO_PROFILE_ERROR = (
    "No signed-in ChatGPT profile. Run `python -m scrapers.login chatgpt` on "
    "this machine, then re-run the scan. (CHATGPT_SIGNED_IN_ONLY=0 allows "
    "anonymous runs, which Cloudflare usually blocks.)")

# {slot: session}, each reused across keywords and bound to the loop that made it.
_SESSIONS: dict[int, dict] = {}

# NO PROXY. Tested 2026-10-13 and removed: routing this scraper through the
# BuyProxies pool did not change the Turnstile failure, and it costs the thing
# the tracker is measuring.
#
#   * REGION. Every exit in that pool is a US datacentre IP (ColoCrossing:
#     23.x / 107.x / 192.3.x / 198.46.x), while the machines running this
#     already egress from India. Direct IS the Indian path; proxying is what
#     loses it, and a chat UI has no gl/hl override to win it back the way
#     the Google AIO scraper does.
#   * CLOUDFLARE. Anonymous ChatGPT is the hardest-scrutinised path (no
#     account history to lean on) and a datacentre ASN is the strongest
#     single negative signal Turnstile has.
#
# If exits are ever revisited here they need to be INDIAN RESIDENTIAL. This
# pool cannot supply those, so there is nothing to re-enable.


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


# The verification failure that appears INSIDE the composer, not as a
# full-page interstitial: "An error occurred during verification, please try
# again." with a Retry link beside it. _solve_turnstile never sees this --
# the page title is not "Just a moment" and the challenge iframe is not
# mounted, so it reports "no challenge" and the scrape waits for an answer
# that is never coming.
_VERIFY_ERROR_RE = re.compile(
    r"error occurred during verification", re.I)


async def _retry_verification(page, *, attempts: int = 3) -> bool:
    """Click Retry while the composer is showing a verification error.

    Returns True if the error was present at least once. Each Retry re-arms
    Turnstile, so the solver is given another go in between -- the challenge
    sometimes mounts properly on the second attempt when it failed silently
    on the first.
    """
    seen = False
    for _ in range(attempts):
        try:
            body = (await page.inner_text("body"))[:4000]
        except Exception:
            return seen
        if not _VERIFY_ERROR_RE.search(body):
            return seen
        seen = True
        clicked = False
        # The link is a plain control with no stable test id, so match on
        # its text -- scoped to the composer so a "Retry" elsewhere on the
        # page cannot be hit by mistake.
        for sel in ("form button:has-text('Retry')",
                    "form a:has-text('Retry')",
                    "button:has-text('Retry')",
                    "a:has-text('Retry')"):
            try:
                loc = page.locator(sel).first
                if await loc.count():
                    await loc.click(timeout=4_000)
                    clicked = True
                    break
            except Exception:
                continue
        if not clicked:
            return seen
        try:
            await page.wait_for_timeout(1_500)
        except Exception:
            pass
        await _solve_turnstile(page, total_timeout_ms=20_000)
    return seen


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
    if SIMPLE:
        # Nothing persists between runs, so there is no clearance to pre-earn.
        print("[chatgpt] engine=simple — no profiles to warm")
        return True
    if not PROFILES:
        print(f"[chatgpt] nothing to warm — {NO_PROFILE_ERROR}")
        return False
    ok = True
    for i, prof in enumerate(PROFILES):
        print(f"[chatgpt] warming profile {prof} ({i + 1}/{len(PROFILES)}) …")
        try:
            got = await _unlock_one(prof, timeout_s)
        except Exception as e:
            print(f"[chatgpt] {prof}: {type(e).__name__}: {str(e)[:80]}")
            got = False
        print(f"[chatgpt] {prof}: {'cleared' if got else 'NOT cleared'}")
        ok = ok and got
    return ok


async def _composer(page):
    """The real editor, resolved by selector PRIORITY — not DOM order.

    `page.locator(COMPOSER_SEL).first` looks right and is not: a comma-joined
    selector returns the first match in DOCUMENT order, so the hidden
    <textarea id="prompt-textarea"> that ChatGPT ships as a fallback wins over
    the contenteditable div that comes after it. Typing then lands in the
    visible editor (keyboard input follows focus) while press()/click() are
    aimed at the hidden one -- which is exactly "text is in the box but Enter
    does nothing". Walk the list in order and take the first VISIBLE match.
    """
    for sel in COMPOSER_SELECTORS:
        try:
            loc = page.locator(sel).first
            if await loc.count() and await loc.is_visible():
                return loc
        except Exception:
            continue
    return None


# Read the composer's CURRENT content, whichever element type it is.
#
# Measured live 2026-10-13: anonymous chatgpt.com now ships a plain
# <textarea id="mobile-composer-prompt"> -- every contenteditable selector
# matches zero elements. `inner_text()` on a textarea returns its original
# markup, NOT what was typed, so it reads '' no matter what is in the box:
#
#     inner_text()  -> ''
#     input_value() -> 'best cashback site in india'
#
# That single mismatch was the whole "stuck after typing" failure. The typed
# text verification below saw '', concluded the prompt had not landed, retyped
# it, saw '' again and gave up -- returning before it ever pressed Enter.
_COMPOSER_VALUE_JS = (
    "e => (e.value !== undefined && e.value !== null) "
    "? String(e.value) : (e.innerText || '')"
)


async def _composer_text(page) -> str | None:
    """Text sitting in the composer, or None when there is no composer."""
    try:
        comp = await _composer(page)
        if comp is None:
            return None
        return (await comp.evaluate(_COMPOSER_VALUE_JS)) or ""
    except Exception:
        return None


# Click the send button from inside the page. Bypasses Playwright's
# actionability checks entirely, which is the point: a button covered by an
# invisible overlay, or one Playwright judges "not stable", silently refuses a
# normal click and raises nothing useful.
_JS_SEND = """
(sels) => {
  for (const s of sels) {
    let b;
    try { b = document.querySelector(s); } catch (e) { continue; }
    if (b && !b.disabled && b.offsetParent !== null) { b.click(); return s; }
  }
  return null;
}
"""


async def _wait_send_ready(page, timeout_ms: int = 20_000):
    """Poll until a send button is visible AND enabled, then return it.

    This is the "wait for the page to settle" step. ChatGPT mounts the
    composer before the send button is wired up, and the button stays
    `disabled` until the editor reports content -- clicking in that window
    does nothing at all and looks exactly like a missing selector.
    """
    deadline = time.monotonic() + timeout_ms / 1000
    while time.monotonic() < deadline:
        for sel in SEND_SELECTORS:
            try:
                loc = page.locator(sel).first
                if (await loc.count() and await loc.is_visible()
                        and await loc.is_enabled()):
                    return loc
            except Exception:
                continue
        try:
            await page.wait_for_timeout(250)
        except Exception:
            break
    return None


async def _sent(page, timeout_ms: int = 6_000) -> bool:
    """True once the composer has emptied, i.e. the prompt really went.

    Checked rather than assumed: a click that lands on a disabled or
    overlaid button raises nothing and leaves the text sitting there, which
    is exactly the stall this function exists to detect.
    """
    deadline = time.monotonic() + timeout_ms / 1000
    while time.monotonic() < deadline:
        try:
            await page.wait_for_timeout(250)
        except Exception:
            return False
        left = await _composer_text(page)
        if left is None or not left.strip():
            return True
    return False


async def _clear_composer(page) -> None:
    """Empty the composer before typing.

    A reused tab can come back with a restored draft, and ChatGPT keeps one
    per conversation. Typing into that appends, so the previous keyword
    would ride along on the front of this one.
    """
    for combo in ("ControlOrMeta+a", "Control+a", "Meta+a"):
        try:
            await page.keyboard.press(combo)
            await page.keyboard.press("Backspace")
            return
        except Exception:
            continue


async def _type_and_send(page, prompt: str) -> bool:
    """Type the prompt into the composer and press Enter. True when sent.

    This is the ONLY submit path (2026-10-13, at the user's request). The
    `?q=` prefill URL it replaced auto-submitted for anonymous visitors but
    merely filled the box when signed in, so prompts sat there unsent.
    Typing and pressing Enter behaves the same either way.

    ENTER is the action, the send button only a fallback. Enter goes through
    the composer's own key handler -- the same path a person takes. Clicking
    the button depends on hitting the right element while it is enabled and
    unobscured, which is what was silently doing nothing.
    """
    try:
        await page.wait_for_selector(COMPOSER_SEL, state="visible",
                                     timeout=30_000)
    except Exception:
        pass
    composer = await _composer(page)
    if composer is None:
        print("[chatgpt] composer never appeared — cannot type the prompt")
        return False

    try:
        await composer.click(timeout=8_000)
    except Exception:
        pass
    await _clear_composer(page)

    try:
        # insert_text, not type(): ProseMirror handles a paste-style insert
        # cleanly, and a long prompt typed key-by-key is slow enough that the
        # send control can re-disable part-way through.
        await page.keyboard.insert_text(prompt)
    except Exception:
        return False

    # Confirm the text actually landed. insert_text goes to whatever holds
    # focus, so a stolen focus silently types the prompt into nothing.
    got = (await _composer_text(page) or "")
    if prompt[:40] not in got:
        try:
            await composer.click(timeout=5_000)
            await _clear_composer(page)
            await composer.type(prompt, delay=8, timeout=60_000)
        except Exception:
            pass
        got = (await _composer_text(page) or "")
        if prompt[:40] not in got:
            print("[chatgpt] prompt did not land in the composer")
            return False

    # Settle before acting. The send button going enabled is the signal that
    # the editor has registered its content and the composer is wired up --
    # an Enter pressed before that is swallowed.
    await _wait_send_ready(page)

    # 1. Enter, from the composer itself.
    try:
        await composer.press("Enter", timeout=5_000)
        if await _sent(page):
            return True
    except Exception:
        pass

    # 2. Enter again at the keyboard level, in case focus moved.
    try:
        await composer.focus(timeout=3_000)
        await page.keyboard.press("Enter")
        if await _sent(page):
            return True
    except Exception:
        pass

    # 3. The send button, clicked normally.
    btn = await _wait_send_ready(page, timeout_ms=5_000)
    if btn is not None:
        try:
            await btn.click(timeout=5_000)
            if await _sent(page):
                return True
        except Exception:
            pass

    # 4. The send button, clicked from inside the page. Last resort because it
    #    skips every actionability guarantee -- but an overlay or a "not
    #    stable" verdict is precisely what would have defeated step 3.
    try:
        hit = await page.evaluate(_JS_SEND, list(SEND_SELECTORS))
        if hit:
            print(f"[chatgpt] sent via in-page click on {hit}")
            if await _sent(page):
                return True
    except Exception:
        pass

    print("[chatgpt] prompt is still sitting in the composer — Enter, the send "
          "button and an in-page click all failed to submit it")
    return False


_SKIP_HOSTS = ("openai.com", "chatgpt.com", "oaistatic.com", "oaiusercontent.com")


def _is_chatgpt_backend(url: str) -> bool:
    return "chatgpt.com/backend-anon/" in url or "chatgpt.com/backend-api/" in url


async def _ensure_session(slot: int = 0) -> dict:
    """Return a live session for the current loop, launching one headed Chrome
    if needed. Reuses the open Chrome/tab across keywords (the whole point) and
    clears Turnstile once on the homepage so later navigations sail through.
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

    if SIMPLE:
        # Bare Playwright. launch() + new_context() + new_page(), nothing
        # else: no user_data_dir, no channel, no args, no sandbox flag, no
        # crash-bubble handling, no profile sanitising. Each slot gets its
        # own browser, which is free here because there is no profile
        # directory to contend over.
        pw = await async_patchright().start()
        browser = await pw.chromium.launch(headless=False)
        ctx = await browser.new_context(viewport={"width": 1280, "height": 900})
        page = await ctx.new_page()
        try:
            await page.goto("https://chatgpt.com/",
                            wait_until="domcontentloaded", timeout=60_000)
        except Exception:
            pass
        # Still worth clearing once per browser: the cf_clearance cookie lives
        # in the context, so later keywords on this slot skip the challenge.
        await _solve_turnstile(page)
        await _dismiss_modals(page)
        sess = {"pw": pw, "browser": browser, "ctx": ctx, "page": page,
                "loop": loop}
        _SESSIONS[slot] = sess
        return sess

    # Each slot needs its OWN profile: one directory cannot back two live
    # browsers. Indexed through PROFILES, which may hold only the signed-in
    # dirs — so slot 1 is not necessarily chatgpt_1.
    user_dir = str(profile_dir(PROFILES[slot % len(PROFILES)]))
    # A profile left "crashed" opens on the Restore-pages bubble and parks the
    # tab on about:blank — clear it before every launch, not just after a kill.
    sanitize_profile(user_dir)
    pw = await async_patchright().start()
    kwargs = dict(
        user_data_dir=user_dir,
        headless=False,  # Turnstile blocks headless regardless of URL — always headed.
        viewport={"width": 1280, "height": 900},
        args=["--no-default-browser-check", *CRASH_BUBBLE_ARGS],
        # Playwright defaults this to False, which injects --no-sandbox.
        # Chrome then shows the yellow "unsupported command-line flag"
        # infobar, and Turnstile reads that flag as automation and answers
        # "An error occurred during verification, please try again."
        # Anonymous ChatGPT worked before precisely because the anonymous
        # path is the one Turnstile scrutinises hardest -- it has no account
        # history to fall back on, so a single cheap tell is enough.
        #
        # This module launches its own context rather than going through
        # _browser.persistent_context, so the same setting has to be made
        # in both places. Changing one and not the other is why the banner
        # survived the first fix.
        chromium_sandbox=True,
    )
    # Real Chrome unless explicitly asked for bundled Chromium. Keeping the
    # channel is what preserves the signed-in profile: the cookie encryption
    # key in recent Chrome is bound to the Chrome executable.
    if CHATGPT_ENGINE != "chromium":
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
    # persists for the rest of the batch so per-keyword loads don't re-gate.
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
    # Only the simple path has a separate Browser object; a persistent context
    # IS the browser. Leaving it unclosed leaks a Chromium process per slot.
    if sess.get("browser") is not None:
        try:
            await sess["browser"].close()
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

# How long the ANSWER itself may take to finish streaming. A prompt that makes
# ChatGPT run a web search spends 20-60s searching before a single character
# appears, then streams a long reply -- so this is minutes, not seconds.
ANSWER_MAX_S = int(os.getenv("CHATGPT_ANSWER_MAX_S", "180"))
# How long to wait for the assistant turn to APPEAR at all. Separate from the
# streaming budget above: with a web search this is the long silent gap while
# ChatGPT searches, before any text exists to be stable.
FIRST_TOKEN_S = int(os.getenv("CHATGPT_FIRST_TOKEN_S", "90"))

# Hard ceilings for the WHOLE scrape. A cold profile must clear Turnstile and
# can stall in a way that ignores page.goto's own timeout (measured: a 150s
# wait_for overran to 445s), so the scrape is bounded here rather than trusting
# the inner timeouts.
#
# These MUST stay larger than ANSWER_MAX_S plus the navigation and submit
# steps. They were 100/90 against a 180s answer wait, so the outer timeout
# always fired first: the browser sat on a complete, correct answer while the
# scrape was killed at 90s and reported "timed out" — which looks exactly like
# a broken selector and is not one.
COLD_BUDGET_S = int(os.getenv("CHATGPT_COLD_BUDGET_S", str(ANSWER_MAX_S + 120)))
WARM_BUDGET_S = int(os.getenv("CHATGPT_WARM_BUDGET_S", str(ANSWER_MAX_S + 90)))


async def scrape(prompt: str, headless: bool = True) -> ScrapeResult:
    """Scrape one prompt, retrying once if the COLD session never came up.

    Measured: keyword 1 on a fresh session timed out at 150s, keywords 2 and 3 on
    the same session took 7.9s and 11.4s. That is Turnstile being cleared on the
    first load. Failing keyword 1 of every scan is not acceptable, so a cold-start
    failure tears the session down and tries once more — by then the profile holds
    cf_clearance and the retry is fast.
    """
    global _WARMED
    if not PROFILES:
        # Fail every keyword the same way, immediately. Retrying cannot
        # conjure a login, and the retry must not reach for an anonymous
        # browser instead.
        res = ScrapeResult(platform="chatgpt", prompt=prompt)
        res.error = NO_PROFILE_ERROR
        return res
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
    if not PROFILES:
        # Stop here rather than launching a logged-out browser.
        res.error = NO_PROFILE_ERROR
        return res
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
                # 1. Navigate the EXISTING tab to a fresh chat. Plain
                #    chatgpt.com, NOT the `?q=` prefill: that auto-submitted
                #    for anonymous visitors but only filled the box once
                #    signed in, leaving the prompt unsent. We type it and
                #    press Enter instead, which behaves the same either way.
                #    Reloading the base URL also guarantees an empty
                #    conversation, so keywords never pile into one thread.
                try:
                    await page.goto(
                        "https://chatgpt.com/",
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
                # Turnstile can fail AFTER the prompt is submitted, as an
                # inline composer error rather than an interstitial. Without
                # this the scrape sits on wait_for_selector(RESP_SEL) for the
                # full 60s and reports "Assistant response never appeared",
                # which hides the real cause.
                if await _retry_verification(page):
                    await wait_quiet(page, idle_ms=250, net_idle=False)

                # 1b. Type the prompt and press Enter.
                if await _type_and_send(page, prompt):
                    await wait_quiet(page, idle_ms=250, net_idle=False)
                    # Verification fails on SUBMIT, so re-check afterwards.
                    if await _retry_verification(page):
                        await wait_quiet(page, idle_ms=250, net_idle=False)

                # 2. Wait for the assistant turn.
                try:
                    await page.wait_for_selector(RESP_SEL,
                                                 timeout=FIRST_TOKEN_S * 1000)
                except Exception:
                    await _dump_failure(page, "response")
                    # Name the real cause when it is still on screen. "Assistant
                    # response never appeared" sent us looking at selectors and
                    # timeouts for a run that was actually blocked by Turnstile.
                    try:
                        stuck = _VERIFY_ERROR_RE.search(
                            (await page.inner_text("body"))[:4000])
                    except Exception:
                        stuck = None
                    # Still in the composer? Then nothing was ever asked, and
                    # "no response" is a misleading way to say so.
                    try:
                        unsent = (await _composer_text(page) or "").strip()
                    except Exception:
                        unsent = ""
                    if unsent and not stuck:
                        res.error = ("Prompt was never submitted — it is still "
                                     "sitting in the composer (Enter and the "
                                     "send button both did nothing).")
                        return res
                    if stuck:
                        base = ("Cloudflare verification failed on submit "
                                "(Retry did not clear it).")
                        if SIMPLE:
                            res.error = (
                                base + " CHATGPT_ENGINE=simple runs ANONYMOUS "
                                "with no cookies, which gets the strictest "
                                "check. Unset it to use the signed-in profile.")
                        elif SIGNED_IN_ONLY and _has_session_cookie(
                                PROFILES[slot % len(PROFILES)]):
                            res.error = base
                        else:
                            res.error = (
                                base + " This profile is ANONYMOUS, which gets "
                                "the strictest check -- run "
                                "`python -m scrapers.login chatgpt`.")
                    else:
                        res.error = "Assistant response never appeared."
                    return res

                stable_text = await _wait_answer_stable(
                    page, idle_ms=1400, max_ms=ANSWER_MAX_S * 1000,
                    min_chars=80,
                )

                # ChatGPT may render the turn but fail to load its content
                # ("This response couldn't load"). Waiting longer never fixes
                # that -- regenerate is the only thing that does.
                if await _regenerate_if_failed(page):
                    stable_text = await _answer_text(page) or stable_text

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
                # Same picker the stability wait used, so what we measured is
                # what we read. Re-run rather than trusting `stable_text`: the
                # citation wait above can attach chips that add text.
                payload = await _answer_payload(page)
                txt = payload["text"] or stable_text
                if txt and _RESPONSE_FAILED_RE.search(txt[:400]):
                    # Don't store ChatGPT's failure notice as the answer --
                    # it would look like a real (and very wrong) result in
                    # the report.
                    await _dump_failure(page, "response-failed")
                    res.error = ("ChatGPT rendered the turn but its content "
                                 "failed to load, and regenerating did not "
                                 "fix it.")
                    return res
                if txt:
                    res.response_text = txt.strip()[:50000]

                if not res.response_text:
                    # Everything matched nothing. Keep the page for inspection
                    # instead of reporting an empty answer with no evidence —
                    # this is the case where the selectors really have drifted.
                    await _dump_failure(page, "empty")
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

                # Anchors from the SAME node the text came from. (This block
                # used to read `last_msg`, a variable that stopped existing
                # when the text extraction above was rewritten -- so link
                # collection died with a NameError before it looked at a
                # single anchor. That is why no links were coming through.)
                visible_anchors = payload["links"]

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
