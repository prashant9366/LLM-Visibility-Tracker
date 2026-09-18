"""Smoke-test each scraper with one prompt. Prints platform → status →
citations, source URLs, and fanout queries. Use headless=True."""
from __future__ import annotations

import argparse
import asyncio
import sys
import time

# Windows console can't print non-cp1252 chars (e.g. ₹). Force UTF-8 stdout.
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

if sys.platform == "win32":
    asyncio.set_event_loop_policy(asyncio.WindowsProactorEventLoopPolicy())

from scrapers import SCRAPERS

PROMPT = "best credit cards in india for online shopping 2025"


async def _run_one(platform: str, prompt: str):
    fn = SCRAPERS[platform]
    t0 = time.time()
    try:
        r = await fn(prompt, headless=True)
    except Exception as e:
        return platform, None, time.time() - t0, f"CRASH {type(e).__name__}: {e}"
    return platform, r, time.time() - t0, None


def _print_one(platform: str, r, dt: float, crash_msg):
    print(f"\n=== {platform} ===", flush=True)
    if crash_msg:
        print(f"  {crash_msg}")
        return
    print(f"  time     {dt:.1f}s")
    print(f"  error    {r.error or '-'}")
    print(f"  resp_len {len(r.response_text)}")
    print(f"  citations    {len(r.citations)}")
    for u in r.citations[:10]:
        print(f"    - {u}")
    print(f"  source_urls  {len(r.source_urls)}")
    for u in r.source_urls[:10]:
        print(f"    + {u}")
    print(f"  fanout       {len(r.fanout_queries)}")
    for q in r.fanout_queries[:10]:
        print(f"    ? {q}")
    if r.response_text:
        print(f"  ---- response text ----")
        for line in r.response_text.splitlines():
            if line.strip():
                print(f"  | {line}")
        print(f"  ---- end ----")


async def _main(prompt: str) -> int:
    # All platforms fire in parallel, matching how app.py runs them.
    # Print results AS EACH ONE FINISHES so we can see progress live.
    print(f"Scraping '{prompt[:60]}' across {len(SCRAPERS)} platforms in parallel…", flush=True)
    t0 = time.time()
    coros = [_run_one(p, prompt) for p in SCRAPERS]
    finished = 0
    for fut in asyncio.as_completed(coros):
        platform, r, dt, crash_msg = await fut
        finished += 1
        _print_one(platform, r, dt, crash_msg)
        print(f"   ({finished}/{len(SCRAPERS)} platforms done; elapsed {time.time()-t0:.1f}s)", flush=True)
    total = time.time() - t0
    print(f"\n--- TOTAL: {total:.1f}s (parallel across {len(SCRAPERS)} platforms) ---")
    return 0


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--prompt", default=PROMPT)
    args, _ = ap.parse_known_args()
    raise SystemExit(asyncio.run(_main(args.prompt)))
