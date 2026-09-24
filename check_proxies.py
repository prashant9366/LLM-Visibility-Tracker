#!/usr/bin/env python3
"""
check_proxies.py — which proxies actually work, and what IP do they exit from?

Credentials are read from the environment (or .env) — never hardcoded here:

    PROXY_USER=... PROXY_PASS=... python3 check_proxies.py
    python3 check_proxies.py --file proxies.txt
    python3 check_proxies.py --target https://www.google.com/

For each proxy it reports:
  * exit IP        — via api.ipify.org, proves traffic really leaves through it
  * target status  — an actual request to the site you plan to scrape
  * latency        — slow proxies are worse than none for a scrape loop
"""
import argparse
import concurrent.futures as cf
import os
import sys
import time

import requests

DEFAULT_PROXIES = [
    "23.108.254.250:12345",
    "23.108.254.3:12345",
    "23.108.254.53:12345",
    "107.175.58.118:12345",
    "192.227.182.185:12345",
    "173.234.151.38:12345",
    "196.197.25.23:12345",
    "196.197.25.145:12345",
]

IP_ECHO = "https://api.ipify.org?format=json"
UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/140.0.0.0 Safari/537.36")


def _load_env() -> None:
    """Pull PROXY_USER/PROXY_PASS from .env if they're not already exported."""
    if os.getenv("PROXY_USER") and os.getenv("PROXY_PASS"):
        return
    for name in (".env", "../.env"):
        path = os.path.join(os.path.dirname(os.path.abspath(__file__)), name)
        if not os.path.exists(path):
            continue
        try:
            for line in open(path, encoding="utf-8"):
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                k, v = line.split("=", 1)
                os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))
        except Exception:
            pass


def check(proxy: str, user: str, pw: str, target: str, timeout: int) -> dict:
    url = f"http://{user}:{pw}@{proxy}"
    proxies = {"http": url, "https": url}
    out = {"proxy": proxy, "exit_ip": "", "ip_ms": 0, "target": "", "target_ms": 0,
           "error": ""}

    t = time.time()
    try:
        r = requests.get(IP_ECHO, proxies=proxies, timeout=timeout,
                         headers={"User-Agent": UA})
        out["ip_ms"] = int((time.time() - t) * 1000)
        if r.status_code == 200:
            out["exit_ip"] = (r.json() or {}).get("ip", "?")
        else:
            out["error"] = f"ip-echo HTTP {r.status_code}"
            return out
    except Exception as e:
        out["error"] = f"{type(e).__name__}"
        out["ip_ms"] = int((time.time() - t) * 1000)
        return out

    t = time.time()
    try:
        r = requests.get(target, proxies=proxies, timeout=timeout,
                         headers={"User-Agent": UA}, allow_redirects=True)
        out["target"] = str(r.status_code)
        out["target_ms"] = int((time.time() - t) * 1000)
    except Exception as e:
        out["target"] = "-"
        out["error"] = f"target: {type(e).__name__}"
        out["target_ms"] = int((time.time() - t) * 1000)
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--file", help="file with one host:port per line")
    ap.add_argument("--target", default="https://www.google.com/",
                    help="site you intend to scrape (default: google.com)")
    ap.add_argument("--timeout", type=int, default=20)
    ap.add_argument("--workers", type=int, default=8)
    args = ap.parse_args()

    _load_env()
    user, pw = os.getenv("PROXY_USER", ""), os.getenv("PROXY_PASS", "")
    if not user or not pw:
        print("✗ PROXY_USER / PROXY_PASS not set.\n"
              "  Put them in .env (gitignored) or pass them inline:\n"
              "    PROXY_USER=xxx PROXY_PASS=yyy python3 check_proxies.py")
        return 2

    if args.file:
        proxies = [l.strip() for l in open(args.file) if l.strip()
                   and not l.startswith("#")]
    else:
        proxies = DEFAULT_PROXIES

    print(f"testing {len(proxies)} proxies as {user!r} -> {args.target}\n")
    results = []
    with cf.ThreadPoolExecutor(max_workers=args.workers) as ex:
        futs = {ex.submit(check, p, user, pw, args.target, args.timeout): p
                for p in proxies}
        for f in cf.as_completed(futs):
            results.append(f.result())

    order = {p: i for i, p in enumerate(proxies)}
    results.sort(key=lambda r: order.get(r["proxy"], 99))

    print(f"  {'proxy':<24} {'exit IP':<16} {'ip ms':>6} {'target':>6} {'ms':>6}  note")
    print("  " + "-" * 78)
    ok = []
    for r in results:
        good = bool(r["exit_ip"]) and r["target"] == "200"
        mark = "OK " if good else "   "
        print(f"{mark}{r['proxy']:<24} {r['exit_ip'] or '-':<16} {r['ip_ms']:>6} "
              f"{r['target'] or '-':>6} {r['target_ms']:>6}  {r['error']}")
        if good:
            ok.append(r)

    print(f"\nWORKING: {len(ok)}/{len(results)}")
    if ok:
        for r in sorted(ok, key=lambda x: x["target_ms"]):
            print(f"   {r['proxy']:<24} exit {r['exit_ip']:<16} {r['target_ms']}ms")
        print("\nfastest first — put these in proxies.txt for the scraper.")
    else:
        print("   none. Check the credentials, the port, or whether your IP is "
              "allow-listed with the provider.")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
