"""One-shot interactive login helper.

Opens a real browser window on the profile directory the scraper actually reads,
hands you the keyboard, and waits until you're signed in. The session cookies are
written into `data/profiles/`, so every later scrape reuses them and never has to
log in again.

    python3 -m scrapers.login perplexity
    python3 -m scrapers.login chatgpt
    python3 -m scrapers.login perplexity --timeout 600

POOLED PLATFORMS (chatgpt, google) run several browsers at once, each on its own
profile dir — chatgpt_0, chatgpt_1, chatgpt_2. One login cannot cover them from a
shared directory, so this signs in on slot 0 and then COPIES that profile over
every other slot. Signing in by hand into one scraper window only ever covers the
slot that window happened to own; the rest keep running anonymous.

    python3 -m scrapers.login chatgpt        # all CHATGPT_POOL slots

Re-run it whenever a scraper reports that the session has expired.

⚠  Automating a logged-in account violates these platforms' Terms of Service and can
   get the account banned. Use an account you are willing to lose — see README.
"""
from __future__ import annotations

import argparse
import asyncio
import os
import shutil
import sys
import time
from urllib.parse import quote_plus

from .base import profile_dir
from ._browser import persistent_context

# Platforms whose scraper runs a POOL of browsers, each on its own profile dir
# (chatgpt_0, chatgpt_1, …) because one directory cannot back two live Chromes.
#
# This is why signing in used to appear to do nothing: the helper wrote
# data/profiles/chatgpt while the scraper only ever opens chatgpt_<slot>, so
# the session was saved to a directory nothing reads. Logging in now targets
# slot 0 and REPLICATES it across the pool, so every slot is signed in.
POOLED = {
    "chatgpt": ("CHATGPT_POOL", 3),
    "google": ("GOOGLE_POOL", 3),
}

# Junk that must not be copied between slots: lock files make the copy look
# like a running browser, and the caches are large and rebuilt on demand.
_NO_COPY = shutil.ignore_patterns(
    "Singleton*", "*.lock", "LOCK", "lockfile",
    "Cache", "Code Cache", "GPUCache", "ShaderCache", "GrShaderCache",
    "DawnCache", "DawnGraphiteCache", "DawnWebGPUCache",
    "Crashpad", "CrashpadMetrics*", "component_crx_cache", "extensions_crx_cache",
)


def pool_names(platform: str) -> list[str]:
    """Profile names the scraper will actually open, in slot order."""
    spec = POOLED.get(platform)
    if not spec:
        return [platform]
    env, default = spec
    try:
        size = max(1, int(os.getenv(env, str(default))))
    except ValueError:
        size = default
    return [f"{platform}_{i}" for i in range(size)]


def replicate_profile(src_name: str, dst_names: list[str]) -> int:
    """Copy a signed-in profile over every other slot. Returns the count done.

    MUST run with every browser closed -- Chrome flushes cookies lazily, so a
    copy taken while it is live can miss the session entirely.

    The whole directory is copied, not just Cookies: on Windows the cookie
    values are encrypted with a key held in that profile's own `Local State`,
    so a Cookies file lifted on its own lands somewhere that cannot decrypt it.
    """
    src = profile_dir(src_name)
    done = 0
    for name in dst_names:
        dst = profile_dir(name)
        if str(dst) == str(src):
            continue
        try:
            # Replace rather than merge: a half-overwritten profile mixing two
            # sessions' state is worse than either one on its own.
            shutil.rmtree(dst, ignore_errors=True)
            shutil.copytree(src, dst, ignore=_NO_COPY, dirs_exist_ok=True)
            done += 1
            print(f"  → copied to {dst.name}")
        except OSError as e:
            print(f"  ! could not copy to {name}: {type(e).__name__}: {e}")
    return done

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
    # Google AIO works signed-out, so this is optional — but a signed-in
    # profile gets the consent wall out of the way and draws fewer CAPTCHAs.
    "google": {
        "url": "https://www.google.com/",
        "cookie_names": ("SID", "__Secure-1PSID"),
        "hint": "Sign in (top right), or just accept the consent dialog.",
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
    # Sign in on the profile the scraper's first slot opens, NOT on a bare
    # data/profiles/<platform> that nothing reads.
    slots = pool_names(platform)
    target = slots[0]
    print(f"\nProfile: {profile_dir(target)}")
    if len(slots) > 1:
        print(f"Pool: {len(slots)} slots — this login will be copied to "
              f"{', '.join(slots[1:])} once it takes.")
    print(f"Opening {cfg['url']} …")
    print(f"\n  → {cfg['hint']}")
    print("  → This window is a real browser; log in normally.")
    print("  → It closes by itself once a session cookie appears "
          f"(or after {timeout_s}s).\n")

    # headed, and never block resources: this is a human-driven login, so the page
    # must render fully (images/fonts included) or the provider buttons look broken.
    ok = False
    async with persistent_context(target, headless=False, block_resources=False,
                                  engine="patchright") as ctx:
        page = ctx.pages[0] if ctx.pages else await ctx.new_page()
        try:
            await page.goto(cfg["url"], wait_until="domcontentloaded", timeout=60_000)
        except Exception as e:
            print(f"  (navigation warning: {type(e).__name__}: {e})")

        waited = 0
        while waited < timeout_s:
            if await _logged_in(ctx, cfg["cookie_names"]):
                ok = True
                print(f"\n✓ Signed in — session saved to {profile_dir(target)}")
                # Chrome writes cookies lazily. Closing the window too early is
                # how a completed login still leaves an empty Cookies file.
                print("  Letting cookies flush (3s) …")
                await page.wait_for_timeout(3000)
                break
            try:
                await page.wait_for_timeout(2000)
            except Exception:
                # window closed by the user — check one last time
                break
            waited += 2

        if not ok:
            ok = await _logged_in(ctx, cfg["cookie_names"])
            if ok:
                print(f"\n✓ Signed in — session saved to {profile_dir(target)}")
            else:
                print("\n✗ No session cookie found. Either the login didn't complete, "
                      "or the cookie name changed.\n  Re-run and finish the login, or "
                      f"check PLATFORMS['{platform}']['cookie_names'] in "
                      "scrapers/login.py.")

    # OUTSIDE the context: the browser has now closed and the profile is fully
    # written, which is the only safe moment to copy it.
    if ok and len(slots) > 1:
        print("\nCopying the signed-in profile across the pool …")
        n = replicate_profile(target, slots[1:])
        print(f"  {n + 1} of {len(slots)} slots signed in.")
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
