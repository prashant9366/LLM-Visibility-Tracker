"""One-shot interactive login helper.

Opens a real browser window on the platform's PERSISTENT profile directory, hands
you the keyboard, and waits until you're signed in. The session cookies are written
into `data/profiles/<platform>/`, so every later scrape reuses them headlessly and
never has to log in again.

    python3 -m scrapers.login perplexity
    python3 -m scrapers.login perplexity --timeout 600

Re-run it whenever a scraper reports that the session has expired.

⚠  Automating a logged-in account violates these platforms' Terms of Service and can
   get the account banned. Use an account you are willing to lose — see README.
"""
from __future__ import annotations

import argparse
import asyncio
import sys
import time
from urllib.parse import quote_plus

from .base import profile_dir
from ._browser import persistent_context

# Where to land, and how to tell that a login actually took.
#   cookie_names — any one of these on the site's domain means "signed in"
PLATFORMS = {
    "perplexity": {
        "url": "https://www.perplexity.ai/",
        "cookie_names": ("__Secure-next-auth.session-token",
                         "next-auth.session-token",
                         "__Secure-authjs.session-token",
                         "pplx.session-id"),
        "hint": "Click 'Log in' (top right) and sign in with Google/email.",
    },
    "chatgpt": {
        "url": "https://chatgpt.com/",
        "cookie_names": ("__Secure-next-auth.session-token",
                         "__Session-session-token"),
        "hint": "Click 'Log in' and sign in.",
    },
    "gemini": {
        "url": "https://gemini.google.com/app?hl=en-IN",
        "cookie_names": ("SID", "__Secure-1PSID"),
        "hint": "Sign in with your Google account.",
    },
}


def _login_perplexity(timeout_s: int, query: str = "cashkaro") -> bool:
    """Perplexity goes through StealthyFetcher (Camoufox/Firefox), NOT Chromium.

    Its scraper needs Scrapling to solve Cloudflare, and Scrapling drives Camoufox —
    a Chromium profile's cookies are unreadable to it. So the login has to run through
    the same fetcher, writing into the same `user_data_dir` the scrape later reuses.

    Lands on a real SEARCH, not the homepage: logged out, Perplexity answers with
    "Sign up and repeat your request." and puts the login CTA right there. So you log
    in from the wall itself, then repeat the search in the same window.

    Success is detected from the ANSWER, not from any "am I signed in" guess: once the
    prose block holds real content instead of the gate text, the session demonstrably
    works — which is the only thing the scraper actually needs.
    """
    from scrapling.fetchers import StealthyFetcher
    from .perplexity import perplexity_profile, _is_gated, ANSWER_SEL

    prof = perplexity_profile()
    url = f"https://www.perplexity.ai/search/new?q={quote_plus(query)}"
    print(f"\nProfile: {prof}")
    print(f"Opening a search for {query!r} …")
    print("\n  → Logged out, Perplexity replies 'Sign up and repeat your request.'")
    print("    with a Log in / Sign up button — use it to sign in.")
    print("  → Then repeat the search in the same window (as it asks).")
    print("  → Solve any Cloudflare check if one appears.")
    print(f"  → The window closes by itself once a REAL answer renders "
          f"(or after {timeout_s}s).\n")

    state = {"ok": False}

    def _wait_for_login(page):
        deadline = time.monotonic() + timeout_s
        announced = False
        while time.monotonic() < deadline:
            txt = ""
            try:
                if page.locator(ANSWER_SEL).count():
                    txt = page.locator(ANSWER_SEL).last.evaluate(
                        "e => (e.innerText || '')") or ""
            except Exception:
                txt = ""
            # A substantial, non-gate answer means the session is live and working.
            if txt and not _is_gated(txt) and len(txt.strip()) > 200:
                state["ok"] = True
                page.wait_for_timeout(2500)      # let cookies flush to the profile
                return
            if _is_gated(txt) and not announced:
                announced = True
                print("  … still logged out — sign in via the button, "
                      "then search again in that window.", flush=True)
            page.wait_for_timeout(2000)

    StealthyFetcher.fetch(
        url,
        headless=False,
        solve_cloudflare=True,
        block_ads=True,
        page_action=_wait_for_login,
        timeout=max(timeout_s, 60) * 1000 + 60_000,
        user_data_dir=prof,
    )
    if state["ok"]:
        print(f"\n✓ Got a real answer — session saved to {prof}")
        print('  Verify with:  python3 debug_perplexity.py "cashkaro"')
    else:
        print("\n✗ Never saw a real answer before the timeout.")
        print("  Re-run, and after signing in make sure you repeat the search "
              "in that same window.")
    return state["ok"]


async def _logged_in(ctx, names: tuple[str, ...]) -> bool:
    try:
        cookies = await ctx.cookies()
    except Exception:
        return False
    return any(c.get("name") in names and c.get("value") for c in cookies)


async def interactive_login(platform: str, timeout_s: int = 600,
                            query: str = "cashkaro") -> bool:
    if platform == "perplexity":
        # different browser engine, and logs in from a search — see _login_perplexity
        return await asyncio.to_thread(_login_perplexity, timeout_s, query)
    cfg = PLATFORMS[platform]
    print(f"\nProfile: {profile_dir(platform)}")
    print(f"Opening {cfg['url']} …")
    print(f"\n  → {cfg['hint']}")
    print("  → This window is a real browser; log in normally.")
    print("  → It closes by itself once a session cookie appears "
          f"(or after {timeout_s}s).\n")

    # headed, and never block resources: this is a human-driven login, so the page
    # must render fully (images/fonts included) or the provider buttons look broken.
    async with persistent_context(platform, headless=False, block_resources=False,
                                  engine="patchright") as ctx:
        page = ctx.pages[0] if ctx.pages else await ctx.new_page()
        try:
            await page.goto(cfg["url"], wait_until="domcontentloaded", timeout=60_000)
        except Exception as e:
            print(f"  (navigation warning: {type(e).__name__}: {e})")

        waited = 0
        while waited < timeout_s:
            if await _logged_in(ctx, cfg["cookie_names"]):
                print(f"\n✓ Signed in — session saved to {profile_dir(platform)}")
                print("  Close-out in 3s; future scrapes will reuse it headlessly.")
                await page.wait_for_timeout(3000)
                return True
            try:
                await page.wait_for_timeout(2000)
            except Exception:
                # window closed by the user — check one last time
                break
            waited += 2

        ok = await _logged_in(ctx, cfg["cookie_names"])
        if ok:
            print(f"\n✓ Signed in — session saved to {profile_dir(platform)}")
        else:
            print("\n✗ No session cookie found. Either the login didn't complete, or "
                  "the cookie name changed.\n  Re-run and finish the login, or check "
                  f"PLATFORMS['{platform}']['cookie_names'] in scrapers/login.py.")
        return ok


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("platform", choices=sorted(PLATFORMS))
    ap.add_argument("--timeout", type=int, default=600,
                    help="seconds to wait for you to finish logging in (default 600)")
    ap.add_argument("--query", default="cashkaro",
                    help="perplexity only: the search to open, so the login CTA on the "
                         "sign-up wall is right there (default: cashkaro)")
    args = ap.parse_args()
    ok = asyncio.run(interactive_login(args.platform, args.timeout, args.query))
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
