#!/usr/bin/env python3
"""
debug_chatgpt_timing.py — where does a ChatGPT scrape actually spend its time?
Replays the same phases scrapers/chatgpt.py runs, timing each one, so the slow
step is measured rather than guessed.

    python3 debug_chatgpt_timing.py "best cashback site in india"
    python3 debug_chatgpt_timing.py "cashkaro" --second   # also time a 2nd keyword
                                                          # (warm session, the real
                                                          #  per-keyword cost)
"""
import argparse
import asyncio
import time
from urllib.parse import urlencode

from scrapers import chatgpt as C
from scrapers._browser import wait_quiet, wait_for_stable_text


async def one(page, prompt: str, label: str):
    t0 = time.monotonic()
    marks = []

    def mark(tag):
        marks.append((tag, time.monotonic() - t0))

    url = "https://chatgpt.com/?" + urlencode({"q": prompt})
    await page.goto(url, wait_until="domcontentloaded", timeout=60_000)
    mark("goto")

    await C._solve_turnstile(page)
    mark("turnstile")

    await wait_quiet(page, idle_ms=250, net_idle=False)
    mark("wait_quiet")

    await C._dismiss_modals(page)
    mark("dismiss_modals")

    try:
        await page.wait_for_selector(C.RESP_SEL, timeout=60_000)
    except Exception:
        pass
    mark("resp_selector")

    await wait_for_stable_text(page, C.RESP_SEL, idle_ms=1400, max_ms=180_000,
                               min_chars=80)
    mark("stable_text (answer streaming)")

    # the citation-pill wait that is still in scrape()
    try:
        await page.wait_for_selector('[data-testid="webpage-citation-pill"]',
                                     timeout=3000)
    except Exception:
        pass
    mark("citation-pill wait (DEAD selector)")

    # what the real citation hook costs instead
    try:
        await page.wait_for_selector('[data-assistant-sources-payload]', timeout=3000)
    except Exception:
        pass
    mark("sources-payload wait (real hook)")

    print(f"\n--- {label}: {prompt!r} ---")
    prev = 0.0
    for tag, at in marks:
        print(f"  {at:6.1f}s  (+{at-prev:5.1f}s)  {tag}")
        prev = at
    print(f"  TOTAL {marks[-1][1]:.1f}s")
    return marks[-1][1]


async def main(prompt: str, second: bool):
    t = time.monotonic()
    await C.close_session()
    sess = await C._ensure_session()
    launch = time.monotonic() - t
    print(f"session launch (once per batch): {launch:.1f}s")
    page = sess["page"]

    await one(page, prompt, "keyword 1 (cold)")
    if second:
        await one(page, "best coupon site in india", "keyword 2 (warm session)")
    await C.close_session()


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("prompt", nargs="?", default="best cashback site in india")
    ap.add_argument("--second", action="store_true")
    a = ap.parse_args()
    asyncio.run(main(a.prompt, a.second))
