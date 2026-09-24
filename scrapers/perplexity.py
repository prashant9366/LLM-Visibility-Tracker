"""Perplexity — StealthyFetcher on a PERSISTENT logged-in profile.

Two problems have to be solved at once here:

  1. Cloudflare. Perplexity fingerprints hard; patchright/Chromium gets served a
     challenge page (only cloudflare.com anchors, no <main> at all). Scrapling's
     StealthyFetcher actively solves the challenge, so it stays.
  2. Login. Perplexity now gates anonymous search — a logged-out visitor gets
     "Sign up and repeat your request." instead of an answer, which is why this
     scraper used to return 32 characters and no sources.

StealthyFetcher accepts `user_data_dir`, so both are handled by pointing it at
`data/profiles/perplexity/`: the profile carries the login session AND the
cf_clearance cookie between runs. Log in once:

    python3 -m scrapers.login perplexity

NOTE: StealthyFetcher drives Camoufox (Firefox), not Chromium — so the login must
go through the same fetcher to land in a profile it can read. `scrapers/login.py`
does that for this platform.

Being signed in also restores the source links, so `citations` is populated again
(the anonymous version could only ever return `response_text`).

⚠  Automating a logged-in account violates Perplexity's ToS and can get the
   account banned. Use an account you're willing to lose — see README.
"""
from __future__ import annotations

import asyncio
import time
from urllib.parse import quote_plus, urlparse

from scrapling.fetchers import StealthyFetcher

from .base import ScrapeResult, profile_dir

ANSWER_SEL = "div[class*='prose']"

# Hosts that are Perplexity's own chrome (or the challenge wall), not a cited source.
_SKIP_HOSTS = (
    "perplexity.ai", "pplx.ai", "gstatic.com", "googleapis.com",
    "google.com", "apple.com", "cloudflare.com",
)

# Perplexity's logged-out wall. Its text never grows, so polling it to the deadline
# wastes the whole 75s budget on every prompt — detect it and bail immediately.
_GATE_PHRASES = (
    "sign up and repeat your request",
    "sign up to continue",
    "sign in to continue",
    "create an account to continue",
    "you've reached your limit",
    "you have reached your limit",
)


def _is_gated(text: str) -> bool:
    t = (text or "").strip().lower()
    return bool(t) and any(p in t for p in _GATE_PHRASES)


def perplexity_profile() -> str:
    return str(profile_dir("perplexity"))


def _wait_for_answer_text(page) -> None:
    """page_action callback inside Scrapling's page (SYNC API). Wait for the answer
    text to grow past 200 chars and then stay still ~1.4s. Hard cap 75s.

    Returns early on the sign-up wall — that text never grows, so polling it to the
    deadline would waste ~75s per prompt."""
    try:
        page.wait_for_selector(ANSWER_SEL, timeout=30_000)
    except Exception:
        return

    deadline = time.monotonic() + 75
    last_len = -1
    last_change = time.monotonic()
    while time.monotonic() < deadline:
        try:
            txt = page.locator(ANSWER_SEL).last.evaluate("e => (e.innerText || '')") or ""
        except Exception:
            txt, cur = "", last_len
        else:
            cur = len(txt)
            if _is_gated(txt):
                return
        if cur != last_len:
            last_len = cur
            last_change = time.monotonic()
        elif cur >= 200 and (time.monotonic() - last_change) >= 1.4:
            break
        page.wait_for_timeout(250)


_COMPOSER_SELECTORS = (
    "textarea[placeholder*='Ask']",
    "div[contenteditable='true']",
    "textarea",
    "[role='textbox']",
)

# Perplexity's answer container class rotates (div[class*='prose'] is gone as of
# 2026-09). Rather than chase it, watch <main>'s own text: the shell is ~44 chars
# ("Answer / Sources / Ask a follow-up / Search"), so real content is unmistakable.
_SHELL_CHARS = 300


_COLLECT_JS = ("() => Array.from(document.querySelectorAll('a[href]'))"
               ".map(a => a.href).filter(h => /^https?:/i.test(h))")


def _drive_search(prompt: str, sink: list):
    """page_action: type the prompt, submit, wait for the answer to settle.

    Navigating to /search/new?q=… no longer starts a search for a logged-in
    session — it lands on an empty shell and nothing ever streams (verified: 40s
    wait, <main> still 44 chars, zero anchors). So we drive the composer the same
    way the ChatGPT scraper had to once its ?q= prefill died.
    """
    def action(page):
        # The cookie-policy dialog sits over the bottom-right of the page and can
        # swallow the click/Enter that submits. Dismiss it first.
        for sel in ("button:has-text('Got it')", "button:has-text('Accept all')",
                    "button:has-text('Decline optional')", "button:has-text('Accept')"):
            try:
                b = page.locator(sel).first
                if b.count():
                    b.click(timeout=2000)
                    page.wait_for_timeout(300)
                    break
            except Exception:
                continue

        box = None
        for sel in _COMPOSER_SELECTORS:
            try:
                page.wait_for_selector(sel, timeout=8_000, state="visible")
                loc = page.locator(sel).first
                if loc.count():
                    box = loc
                    break
            except Exception:
                continue
        if box is None:
            return                      # caller reports "no composer"

        try:
            box.click()
            box.fill(prompt)
        except Exception:
            try:
                page.keyboard.type(prompt)
            except Exception:
                return
        page.wait_for_timeout(400)
        page.keyboard.press("Enter")

        # Submission is confirmed by NAVIGATION, not by text length: the homepage
        # shell is ~181 chars on its own, so a character threshold alone reads the
        # composer's own placeholder text as if it were an answer. A real search
        # moves the URL to /search/<id>.
        def _in_search() -> bool:
            return "/search/" in (page.url or "")

        t0 = time.monotonic()
        while time.monotonic() - t0 < 20 and not _in_search():
            page.wait_for_timeout(300)

        if not _in_search():
            # Enter didn't take. The submit control is a TEXTLESS circular arrow
            # button inside the composer, so aria-label/type/testid selectors all
            # miss it. Walk up from the composer to the first ancestor that holds
            # buttons and click the last one — that's the arrow, regardless of how
            # its classes are named this week.
            try:
                page.evaluate(
                    """() => {
                        const box = document.querySelector("div[contenteditable='true']")
                                 || document.querySelector('textarea');
                        if (!box) return 'no-box';
                        let n = box;
                        for (let i = 0; i < 6 && n; i++) {
                            const btns = n.querySelectorAll('button');
                            if (btns.length) {
                                btns[btns.length - 1].click();
                                return 'clicked';
                            }
                            n = n.parentElement;
                        }
                        return 'no-button';
                    }"""
                )
            except Exception:
                pass
            t0 = time.monotonic()
            while time.monotonic() - t0 < 20 and not _in_search():
                page.wait_for_timeout(300)

        if not _in_search():
            return                      # never got a search going

        # Now wait for the answer to stream in and settle.
        deadline = time.monotonic() + 90
        last, last_change = -1, time.monotonic()
        while time.monotonic() < deadline:
            try:
                txt = page.locator("main").first.evaluate(
                    "e => (e.innerText || '')") or ""
            except Exception:
                txt = ""
            if _is_gated(txt):
                return
            cur = len(txt)
            if cur != last:
                last, last_change = cur, time.monotonic()
            elif cur > _SHELL_CHARS and (time.monotonic() - last_change) >= 2.5:
                break
            page.wait_for_timeout(300)

        # Grab the INLINE citation chips now — Answer and Sources are tabs, so
        # opening Sources swaps the view and these leave the DOM. Collecting only
        # after the click traded the inline links (cashkaro) for the Sources list.
        try:
            sink.extend(page.evaluate(_COLLECT_JS) or [])
        except Exception:
            pass

        # Open the Sources tab. The answer body only carries the handful of inline
        # citation chips (2 in testing); the full source list lives behind this tab
        # and never renders into the DOM unless it is opened. Same idea as Gemini's
        # sources sidebar.
        for sel in ("button:has-text('Sources')",
                    "[role='tab']:has-text('Sources')",
                    "div[role='tab']:has-text('Sources')",
                    "a:has-text('Sources')"):
            try:
                tab = page.locator(sel).first
                if tab.count():
                    tab.click(timeout=3000)
                    page.wait_for_timeout(2500)
                    break
            except Exception:
                continue

        # ...and the Sources list, unioned with the inline chips above.
        try:
            sink.extend(page.evaluate(_COLLECT_JS) or [])
        except Exception:
            pass

    return action


def _fetch_sync(prompt: str, headless: bool, sink: list):
    """Sync Scrapling fetch on the persistent profile — runs page_action inline."""
    return StealthyFetcher.fetch(
        "https://www.perplexity.ai/",
        headless=headless,
        solve_cloudflare=True,
        block_ads=True,
        page_action=_drive_search(prompt, sink),
        timeout=180_000,
        user_data_dir=perplexity_profile(),   # carries login + cf_clearance
    )


def _external_links(page) -> list[str]:
    """Cited sources: external anchors, Perplexity's own chrome dropped."""
    out, seen = [], set()
    for sel in (f"{ANSWER_SEL} a", "main a", "a"):
        try:
            els = page.css(sel)
        except Exception:
            continue
        for e in els or []:
            href = (e.attrib.get("href") or "").strip() if hasattr(e, "attrib") else ""
            if not href.startswith(("http://", "https://")) or href in seen:
                continue
            host = (urlparse(href).netloc or "").lower()
            if any(s in host for s in _SKIP_HOSTS):
                continue
            seen.add(href)
            out.append(href)
        if out:
            break          # prefer the tightest container that yielded anything
    return out


async def scrape(prompt: str, headless: bool = True) -> ScrapeResult:
    res = ScrapeResult(platform="perplexity", prompt=prompt)
    try:
        # StealthyFetcher.fetch is sync — off-load so we don't block the loop.
        sink: list[str] = []
        page = await asyncio.to_thread(_fetch_sync, prompt, headless, sink)

        # ANSWER_SEL first (works on the older layout / if it ever returns), then
        # <main>, whose text is the answer once the shell has filled in.
        text = ""
        for sel in (ANSWER_SEL, "main"):
            try:
                els = page.css(sel)
            except Exception:
                continue
            for e in (els or []):
                try:
                    t = (e.get_all_text() or "").strip()
                except Exception:
                    continue
                if len(t) > len(text):
                    text = t
            if len(text) > _SHELL_CHARS:
                break
        if len(text) <= _SHELL_CHARS:
            text = ""          # only the page shell rendered — not an answer

        html = page.html_content or ""
        if _is_gated(text):
            # Report the wall instead of storing "Sign up and repeat your request."
            # as if it were the model's answer to the prompt.
            res.error = ("Perplexity session expired or missing — run "
                         "`python3 -m scrapers.login perplexity` and retry.")
            return res
        if not text:
            if "Just a moment" in html or "challenge" in html.lower():
                res.error = "Cloudflare challenge not solved."
            else:
                res.error = "No answer rendered."
            return res

        res.response_text = text[:50000]
        # union: inline chips + Sources tab (collected live) + anything still
        # parseable from the returned HTML
        merged, seen = [], set()
        for u in list(sink) + _external_links(page):
            if not u or u in seen:
                continue
            host = (urlparse(u).netloc or '').lower()
            if any(sk in host for sk in _SKIP_HOSTS):
                continue
            seen.add(u)
            merged.append(u)
        res.citations = merged
    except Exception as e:
        res.error = f"{type(e).__name__}: {e}"
    return res
