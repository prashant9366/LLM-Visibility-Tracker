#!/usr/bin/env python3
"""
debug_platforms.py — run one query through each platform and report what actually
comes back: response length, citations, and per-platform timing. The companion to
debug_google.py, for checking whether link extraction is working everywhere.

    python3 debug_platforms.py "best cashback site in india"
    python3 debug_platforms.py "cashkaro" --only chatgpt gemini

Opens real browser windows (chatgpt/google are headed by design).
"""
import argparse
import asyncio
import time
from urllib.parse import urlparse

PLATFORMS = ["chatgpt", "gemini", "perplexity", "google"]


def host(u: str) -> str:
    try:
        return (urlparse(u).netloc or "?").lower()
    except Exception:
        return "?"


async def main(prompt: str, only: list[str]):
    import importlib
    results = {}
    for name in only:
        mod = importlib.import_module(f"scrapers.{name}")
        print(f"\n{'='*72}\n{name.upper()}\n{'='*72}", flush=True)
        t = time.monotonic()
        try:
            res = await mod.scrape(prompt, headless=True)
        except Exception as e:
            print(f"  EXCEPTION {type(e).__name__}: {e}")
            results[name] = (0, 0, f"{type(e).__name__}")
            continue
        dt = time.monotonic() - t

        text = res.response_text or ""
        cites = list(res.citations or [])
        srcs = list(getattr(res, "source_urls", None) or [])
        print(f"  time        : {dt:.1f}s")
        print(f"  error       : {res.error or '—'}")
        print(f"  text        : {len(text)} chars")
        print(f"  citations   : {len(cites)}")
        print(f"  source_urls : {len(srcs)}")
        for tag, lst in (("citations", cites), ("source_urls", srcs)):
            if not lst:
                continue
            print(f"    {tag}:")
            for u in lst[:12]:
                print(f"      {u[:110]}")
        shown = cites + srcs
        if shown:
            hosts = {}
            for u in shown:
                hosts[host(u)] = hosts.get(host(u), 0) + 1
            print("  hosts:", ", ".join(f"{h}({n})" for h, n in
                                        sorted(hosts.items(), key=lambda x: -x[1])[:8]))
        results[name] = (len(text), len(cites) + len(srcs), res.error or "")

        # tear down shared sessions so the next platform starts clean
        for closer in ("close_session",):
            fn = getattr(mod, closer, None)
            if fn:
                try:
                    await fn()
                except Exception:
                    pass

    print(f"\n{'='*72}\nSUMMARY  (query: {prompt!r})\n{'='*72}")
    print(f"  {'platform':<12} {'text':>7} {'links':>7}  error")
    for k, (tl, cl, err) in results.items():
        flag = "" if cl else "   <-- NO LINKS"
        print(f"  {k:<12} {tl:>7} {cl:>7}  {err[:40]}{flag}")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("prompt", nargs="?", default="best cashback site in india")
    ap.add_argument("--only", nargs="*", default=PLATFORMS, choices=PLATFORMS)
    a = ap.parse_args()
    asyncio.run(main(a.prompt, a.only))
