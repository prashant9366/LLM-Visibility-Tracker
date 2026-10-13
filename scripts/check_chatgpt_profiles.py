#!/usr/bin/env python3
"""Show what the ChatGPT scraper sees in each browser profile.

    python scripts/check_chatgpt_profiles.py

Run this when the app says "NO signed-in profile" even though you just
logged in. It answers the only question that matters: WHICH directory holds
your session, and does the scraper's check agree?

Reads cookie NAMES only -- values are encrypted and are never touched. Each
Cookies file is copied to a temp file first, so this works while Chrome is
running.
"""
from __future__ import annotations

import os
import sqlite3
import sys
import shutil
import tempfile
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from scrapers.base import PROFILES_DIR, profile_dir          # noqa: E402
from scrapers import chatgpt as c                            # noqa: E402

COOKIE_RELPATHS = ("Default/Network/Cookies", "Default/Cookies", "Network/Cookies")


def cookies_for(name: str) -> tuple[str | None, list[tuple[str, str, int]]]:
    """(path_used, [(cookie_name, host, expires_utc)]) for one profile."""
    base = profile_dir(name)
    for rel in COOKIE_RELPATHS:
        src = base / rel
        if not src.exists():
            continue
        tmp = None
        try:
            with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as fh:
                tmp = fh.name
            shutil.copyfile(src, tmp)
            con = sqlite3.connect(tmp)
            try:
                rows = con.execute(
                    "SELECT name, host_key, expires_utc FROM cookies "
                    "WHERE host_key LIKE '%chatgpt.com' OR host_key LIKE '%openai.com' "
                    "ORDER BY name").fetchall()
            finally:
                con.close()
            return rel, rows
        except Exception as e:
            print(f"    ! {rel}: {type(e).__name__}: {e}")
        finally:
            if tmp:
                try:
                    os.unlink(tmp)
                except OSError:
                    pass
    return None, []


def looks_like_session(cookie_name: str) -> bool:
    low = cookie_name.lower()
    return (cookie_name in c.SESSION_COOKIE_NAMES
            or "session-token" in low or "next-auth" in low or "authjs" in low)


def main() -> int:
    print(f"\nProfiles dir : {PROFILES_DIR}")
    print(f"Pool width   : CHATGPT_POOL={c.POOL_WIDTH}")
    print(f"Signed-in-only: CHATGPT_SIGNED_IN_ONLY={'on' if c.SIGNED_IN_ONLY else 'OFF'}")

    # Every chatgpt* directory, not only the pooled ones -- the whole point is
    # to catch a login that landed somewhere the scraper does not read.
    try:
        found = sorted(p.name for p in PROFILES_DIR.iterdir()
                       if p.is_dir() and p.name.startswith("chatgpt"))
    except OSError:
        found = []
    pooled = [f"chatgpt_{i}" for i in range(c.POOL_WIDTH)]

    now = (time.time() + 11644473600) * 1_000_000
    signed_in = []

    for name in found:
        used, rows = cookies_for(name)
        in_pool = name in pooled
        tag = "USED by scraper" if in_pool else "not read by the scraper"
        print(f"\n  {name}  ({tag})")
        if used is None:
            print("    no Cookies file — this profile has never been opened")
            continue
        sessions = [(n, h, e) for n, h, e in rows if looks_like_session(n)]
        live = [s for s in sessions if s[2] == 0 or s[2] > now]
        if live:
            signed_in.append(name)
            for n, h, _ in live:
                print(f"    SIGNED IN — {n} on {h}")
        elif sessions:
            for n, h, _ in sessions:
                print(f"    EXPIRED   — {n} on {h}")
        else:
            print(f"    anonymous — {len(rows)} cookie(s), none are a session token")
            if rows:
                print(f"      ({', '.join(n for n, _, _ in rows[:10])})")

        # Agreement check: if this disagrees with the scraper's own function,
        # the bug is in the detector, not in the login.
        if in_pool:
            agrees = c._has_session_cookie(name)
            if agrees != bool(live):
                print(f"    !! scraper._has_session_cookie says {agrees} — MISMATCH")

    print("\n" + "-" * 64)
    if not found:
        print("No chatgpt* profile exists at all. Run:")
        print("    python -m scrapers.login chatgpt")
        return 1

    usable = [n for n in signed_in if n in pooled]
    if usable:
        print(f"OK — {len(usable)} pooled profile(s) signed in: {', '.join(usable)}")
        print("If the app still says NO signed-in profile, RESTART Streamlit:")
        print("the check runs once when the module is imported, so a login")
        print("that happened after the app started is not seen until restart.")
        return 0

    stray = [n for n in signed_in if n not in pooled]
    if stray:
        print(f"Your login is in {', '.join(stray)}, which the scraper never opens.")
        print("It reads chatgpt_0 … chatgpt_{n}. Fix it with:".format(
            n=c.POOL_WIDTH - 1))
        print("    python -m scrapers.login chatgpt")
        print("(that signs in slot 0 and copies it across the pool)")
    else:
        print("No profile holds a live ChatGPT session. Run:")
        print("    python -m scrapers.login chatgpt")
        print("Close every Chrome window first, then finish the login in the")
        print("window it opens and let it close itself.")
    return 1


if __name__ == "__main__":
    sys.exit(main())
