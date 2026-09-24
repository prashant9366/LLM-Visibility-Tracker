#!/usr/bin/env python3
"""
debug_perplexity.py — check the Perplexity scrape on its persistent profile: is the
session still valid, does the answer render, and are source links coming back?

    python3 debug_perplexity.py "best cashback site in india"
    python3 debug_perplexity.py "cashkaro" --headed     # watch the browser

If it reports the session is missing/expired, log in once:
    python3 -m scrapers.login perplexity
"""
import argparse
import asyncio
import time

from scrapers.perplexity import scrape, perplexity_profile


async def main(prompt: str, headless: bool):
    print(f"query  : {prompt!r}")
    print(f"profile: {perplexity_profile()}\n")

    t = time.monotonic()
    res = await scrape(prompt, headless=headless)
    dt = time.monotonic() - t

    print(f"  time       : {dt:.1f}s")
    print(f"  error      : {res.error or '—'}")
    print(f"  text       : {len(res.response_text or '')} chars")
    print(f"  citations  : {len(res.citations or [])}")
    for u in (res.citations or [])[:15]:
        print(f"      {u[:110]}")
    if res.response_text:
        print(f"\n  head: {res.response_text[:240]!r}")

    print()
    if res.error and "session" in res.error.lower():
        print("  => NOT LOGGED IN. Run: python3 -m scrapers.login perplexity")
    elif res.error and "cloudflare" in res.error.lower():
        print("  => Cloudflare blocked it. Re-run the login (it refreshes cf_clearance),")
        print("     or try again with --headed.")
    elif res.response_text and res.citations:
        print("  => WORKING (text + sources).")
    elif res.response_text:
        print("  => text OK, but no source links found.")
    else:
        print("  => FAILED.")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("prompt", nargs="?", default="best cashback site in india")
    ap.add_argument("--headed", action="store_true", help="show the browser window")
    a = ap.parse_args()
    asyncio.run(main(a.prompt, headless=not a.headed))
