"""Perplexity (no login) — TEXT ONLY.

Patchright alone can't beat Perplexity's Cloudflare fingerprinting, so we
use Scrapling's StealthyFetcher (which actively solves CF challenges) but
strip out all the click-expanders / citation-pill / SSE-interception
overhead. Just navigate, wait for the answer text to settle, return.

Result: shorter than the old citation-rich version (~80-120s vs ~310s).
Trade-off: `citations`, `source_urls`, and `fanout_queries` are always
empty for Perplexity — only `response_text` is populated.
"""
from __future__ import annotations

import asyncio
import time
from urllib.parse import quote_plus

from scrapling.fetchers import StealthyFetcher

from .base import ScrapeResult


ANSWER_SEL = "div[class*='prose']"


def _wait_for_answer_text(page) -> None:
    """page_action callback inside Scrapling's patchright page. Wait for the
    answer text to grow past 200 chars and then stay still for 2 seconds.
    Hard cap at 75 seconds (model usually finishes in 10-30s)."""
    try:
        page.wait_for_selector(ANSWER_SEL, timeout=30_000)
    except Exception:
        return

    deadline = time.monotonic() + 75
    last_len = -1
    last_change = time.monotonic()
    while time.monotonic() < deadline:
        try:
            cur = page.locator(ANSWER_SEL).last.evaluate(
                "e => (e.innerText || '').length"
            ) or 0
        except Exception:
            cur = last_len
        if cur != last_len:
            last_len = cur
            last_change = time.monotonic()
        elif cur >= 200 and (time.monotonic() - last_change) >= 1.4:
            break
        page.wait_for_timeout(250)


def _fetch_sync(prompt: str, headless: bool):
    """Sync Scrapling fetch — runs the page_action callback inline."""
    url = f"https://www.perplexity.ai/search/new?q={quote_plus(prompt)}"
    return StealthyFetcher.fetch(
        url,
        headless=headless,
        solve_cloudflare=True,
        block_ads=True,
        page_action=_wait_for_answer_text,
        timeout=180_000,
    )


async def scrape(prompt: str, headless: bool = True) -> ScrapeResult:
    res = ScrapeResult(platform="perplexity", prompt=prompt)
    try:
        # StealthyFetcher.fetch is sync — off-load so we don't block the loop.
        page = await asyncio.to_thread(_fetch_sync, prompt, headless)

        # Just the answer text. No citation pills, no source URLs, no fanout.
        prose = page.css("div[class*='prose']")
        if prose:
            res.response_text = prose[-1].get_all_text().strip()[:50000]
        else:
            main = page.css("main")
            if main:
                res.response_text = main[0].get_all_text().strip()[:50000]

        if not res.response_text and "Just a moment" in (page.html_content or ""):
            res.error = "Cloudflare challenge not solved."
    except Exception as e:
        res.error = f"{type(e).__name__}: {e}"
    return res
