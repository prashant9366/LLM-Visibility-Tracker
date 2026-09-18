"""One-time Cloudflare Turnstile unlock for the ChatGPT scraper.

Opens chatgpt.com in a visible Chrome window using the *same persistent
profile* the scraper uses (`data/profiles/chatgpt`). You manually click the
"Verify you are human" checkbox once. Cloudflare then issues a
`cf_clearance` cookie that gets saved to the profile dir, so every
subsequent headless scrape can reuse it.

Re-run this script whenever ChatGPT scrapes start failing again with
"Composer never rendered." — the clearance cookie typically expires after
30 minutes to a couple of hours.

Usage:
    python scripts/chatgpt_unlock.py

The script waits until the ChatGPT composer renders (proxy for "Turnstile
solved AND app loaded"), then closes. If you don't solve within 10 minutes
it exits non-zero.
"""
from __future__ import annotations

import asyncio
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from scrapers.chatgpt import COMPOSER_SEL  # reuse the broad selector chain
from scrapers._browser import persistent_context


WAIT_TIMEOUT_S = 600  # 10 minutes


async def main() -> int:
    print("[unlock] opening headed Chrome with the ChatGPT persistent profile…")
    print("[unlock] when it opens: tick the 'Verify you are human' checkbox.")
    print(f"[unlock] you have {WAIT_TIMEOUT_S // 60} minutes; this window auto-closes once the composer appears.")
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
        except Exception as e:
            print(f"[unlock] navigation error: {e}")
            return 1

        deadline = time.monotonic() + WAIT_TIMEOUT_S
        last_log = 0.0
        while time.monotonic() < deadline:
            try:
                cnt = await page.locator(COMPOSER_SEL).count()
            except Exception:
                cnt = 0
            if cnt:
                print("[unlock] composer detected — Turnstile passed. Cookies saved.")
                # Give Chrome a moment to flush cookies to disk before we kill it.
                await page.wait_for_timeout(1500)
                return 0
            now = time.monotonic()
            if now - last_log > 10:
                print("[unlock] waiting for you to solve Turnstile…  (still on the challenge page)")
                last_log = now
            await page.wait_for_timeout(1000)

        print("[unlock] timed out — Turnstile was not solved within the window.")
        return 2


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
