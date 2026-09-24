#!/usr/bin/env python3
"""
debug_chatgpt_endpoints.py — ChatGPT renders source chips but exposes no <a href>.
This logs EVERY chatgpt.com response during a scrape (no predicate) and prints any
payload that carries external URLs — i.e. whether the citations are recoverable
from the backend at all, and on which endpoint.

    python3 debug_chatgpt_endpoints.py "best cashback site in india"
"""
import asyncio
import re
import sys

from scrapers import chatgpt as C

EXT_URL = re.compile(r'https?://[^\s"\'<>)\\]+')


def _external(text: str) -> list[str]:
    out = []
    for u in EXT_URL.findall(text or ""):
        if "chatgpt.com" in u or "openai.com" in u or "oaistatic" in u:
            continue
        if u not in out:
            out.append(u)
    return out


async def main(prompt: str):
    seen: dict[str, int] = {}
    hits: list[tuple[str, str, list[str]]] = []

    # fresh session — a stale one from a previous run raises TargetClosedError
    await C.close_session()
    sess = await C._ensure_session()
    page = sess["page"]

    async def on_resp(r):
        u = r.url
        if "chatgpt.com" not in u:
            return
        key = re.sub(r"/[0-9a-f]{8}-[0-9a-f-]{20,}", "/<uuid>", u.split("?")[0])
        seen[key] = seen.get(key, 0) + 1
        ctype = ""
        try:
            ctype = r.headers.get("content-type", "")
        except Exception:
            pass
        if not any(t in ctype for t in ("json", "event-stream", "text/plain")):
            return
        try:
            body = await r.text()
        except Exception:
            return
        ext = _external(body)
        if ext:
            hits.append((key, ctype[:28], ext))

    page.on("response", on_resp)
    await page.goto("https://chatgpt.com/", wait_until="domcontentloaded", timeout=60_000)
    await C._solve_turnstile(page)
    await C._dismiss_modals(page)
    await page.wait_for_timeout(1500)

    loc = page.locator("textarea").first
    await loc.click()
    await loc.fill(prompt)
    await page.keyboard.press("Enter")
    print("submitted; capturing 40s of backend traffic…\n", flush=True)
    await page.wait_for_timeout(40_000)

    print("=== endpoints hit ===")
    for k, n in sorted(seen.items(), key=lambda x: -x[1])[:25]:
        print(f"  {n:>3}x  {k[:115]}")

    print(f"\n=== payloads carrying EXTERNAL urls: {len(hits)} ===")
    if not hits:
        print("  none — the citations are NOT in any chatgpt.com response body.")
    for k, ctype, ext in hits[:5]:
        print(f"\n  --- {k[:95]}  ({ctype})   {len(ext)} urls")
        for u in ext[:15]:
            print(f"       {u[:110]}")

    try:
        page.remove_listener("response", on_resp)
    except Exception:
        pass
    await C.close_session()


if __name__ == "__main__":
    asyncio.run(main(sys.argv[1] if len(sys.argv) > 1 else "best cashback site in india"))
