#!/usr/bin/env python3
"""
debug_perplexity_dom.py — the scrape returns the page skeleton ("Answer / Sources /
Ask a follow-up"), so `div[class*='prose']` no longer matches the answer. This waits
generously, then reports which selectors actually hold the answer text and where the
source links live.

    python3 debug_perplexity_dom.py "cashkaro"
    python3 debug_perplexity_dom.py "cashkaro" --headed --wait 45
"""
import argparse
import re
import time
from urllib.parse import quote_plus, urlparse

from scrapling.fetchers import StealthyFetcher

from scrapers.perplexity import perplexity_profile, _SKIP_HOSTS

CANDIDATES = [
    "div[class*='prose']",
    "[data-testid='answer']",
    "div[id^='markdown-content']",
    "div[class*='markdown']",
    "div[class*='answer']",
    "div[class*='Answer']",
    "[class*='prose']",
    "main div[dir='auto']",
    "article",
    "main",
]


def main(prompt: str, headless: bool, wait_s: int):
    url = f"https://www.perplexity.ai/search/new?q={quote_plus(prompt)}"
    print(f"profile: {perplexity_profile()}")
    print(f"fetching {url}\n(waiting {wait_s}s for the answer to stream)\n", flush=True)

    def wait_long(page):
        # don't gate on a selector that may no longer exist — just give it time
        deadline = time.monotonic() + wait_s
        while time.monotonic() < deadline:
            page.wait_for_timeout(1000)

    t = time.time()
    page = StealthyFetcher.fetch(
        url, headless=headless, solve_cloudflare=True, block_ads=True,
        page_action=wait_long, timeout=180_000,
        user_data_dir=perplexity_profile(),
    )
    print(f"fetched in {time.time()-t:.0f}s\n")

    html = page.html_content or ""
    print(f"html length: {len(html):,}")
    for marker in ("Sign up and repeat", "Just a moment", "Verifying you are human"):
        if marker in html:
            print(f"  !! page contains {marker!r}")

    print("\nselector probe (count / longest text):")
    print(f"  {'selector':<30} {'n':>4} {'max_chars':>10}")
    best_sel, best_len = None, 0
    for sel in CANDIDATES:
        try:
            els = page.css(sel)
        except Exception as e:
            print(f"  {sel:<30}  ERR {type(e).__name__}")
            continue
        longest = 0
        for e in els or []:
            try:
                longest = max(longest, len((e.get_all_text() or "").strip()))
            except Exception:
                pass
        print(f"  {sel:<30} {len(els or []):>4} {longest:>10}")
        if longest > best_len:
            best_sel, best_len = sel, longest

    print(f"\nbest selector: {best_sel!r} ({best_len} chars)")
    if best_sel:
        try:
            txt = max((e.get_all_text() or "").strip() for e in page.css(best_sel))
            print(f"  head: {txt[:240]!r}")
        except Exception:
            pass

    # where are the source links?
    try:
        hrefs = [a.attrib.get("href", "") for a in (page.css("a") or [])]
    except Exception:
        hrefs = []
    ext = []
    for h in hrefs:
        if not h.startswith(("http://", "https://")):
            continue
        host = (urlparse(h).netloc or "").lower()
        if any(s in host for s in _SKIP_HOSTS):
            continue
        if h not in ext:
            ext.append(h)
    print(f"\nEXTERNAL anchors: {len(ext)}")
    for u in ext[:20]:
        print("     ", u[:110])

    # URLs hiding in attributes (how ChatGPT/Gemini hid theirs)
    attr_urls = set()
    for m in re.finditer(r'(data-[a-z-]+|value)="([^"]*https?://[^"]*)"', html):
        for u in re.findall(r"https?://[^\s\"'<>)\\]+", m.group(2)):
            host = (urlparse(u).netloc or "").lower()
            if not any(s in host for s in _SKIP_HOSTS):
                attr_urls.add(f"{m.group(1)} = {u}")
    print(f"\nEXTERNAL urls in attributes: {len(attr_urls)}")
    for s in sorted(attr_urls)[:20]:
        print("     ", s[:130])

    out = "data/debug/perplexity_dom.html"
    try:
        import os
        os.makedirs("data/debug", exist_ok=True)
        open(out, "w", encoding="utf-8").write(html)
        print(f"\nfull html -> {out}")
    except Exception as e:
        print(f"\ncould not save html: {e}")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("prompt", nargs="?", default="cashkaro")
    ap.add_argument("--headed", action="store_true")
    ap.add_argument("--wait", type=int, default=35)
    a = ap.parse_args()
    main(a.prompt, not a.headed, a.wait)
