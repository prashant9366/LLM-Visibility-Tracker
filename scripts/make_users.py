#!/usr/bin/env python3
"""Create or reset the app's login accounts.

  python scripts/make_users.py                 # all four, only if missing
  python scripts/make_users.py --user suraj    # reset one
  python scripts/make_users.py --reset-all     # new password for everyone
  python scripts/make_users.py --list          # who exists, no passwords

Prints each new password EXACTLY ONCE. Only the PBKDF2 hash is stored, so
nothing here can show it again -- if it scrolls away, reset that user.

The passwords are word-word-NN: two words from a short list and two digits.
Readable over a desk or a chat message, which is the point for a shared
internal tool, while still carrying enough entropy (~21 bits) that nobody
guesses one by hand. They are NOT strong enough to survive the login being
exposed to the internet -- keep the firewall rule scoped, as
DEPLOY-WINDOWS.md says.
"""
from __future__ import annotations

import argparse
import os
import secrets
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core.auth import USERS_PATH, hash_password, load_users, save_users

TEAM = ("aayush", "suraj", "abhishek", "prashant")

# Short, unambiguous, no lookalike pairs to misread aloud.
ADJECTIVES = ("amber", "brisk", "clever", "dusty", "eager", "fresh", "golden",
              "happy", "ivory", "jolly", "keen", "lucky", "mellow", "noble",
              "olive", "proud", "quiet", "rapid", "sunny", "tidy", "urban",
              "vivid", "warm", "zesty")
NOUNS = ("anchor", "badger", "cedar", "dolphin", "ember", "falcon", "garnet",
         "harbor", "island", "jaguar", "kettle", "lantern", "meadow", "nebula",
         "orchid", "pebble", "quartz", "ridge", "summit", "tiger", "umbrella",
         "violet", "willow", "zephyr")


def make_password() -> str:
    """word-word-NN. secrets, not random: this is a credential."""
    return (f"{secrets.choice(ADJECTIVES)}-{secrets.choice(NOUNS)}-"
            f"{secrets.randbelow(90) + 10}")


def main():
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--user", action="append", metavar="NAME",
                    help="only this user (repeatable). Resets if they exist.")
    ap.add_argument("--reset-all", action="store_true",
                    help="new password for every existing account")
    ap.add_argument("--list", action="store_true",
                    help="show which accounts exist")
    args = ap.parse_args()

    users = load_users()

    if args.list:
        if not users:
            print(f"no accounts yet ({USERS_PATH} does not exist)")
            return
        print(f"{len(users)} account(s) in {USERS_PATH}:")
        for name in sorted(users):
            print(f"   {name}")
        return

    if args.user:
        wanted = [u.strip().lower() for u in args.user if u.strip()]
    elif args.reset_all:
        wanted = sorted(users) or list(TEAM)
    else:
        wanted = list(TEAM)

    made = []
    for name in wanted:
        # Without --user or --reset-all, an existing account is left alone:
        # re-running the bare command must not silently invalidate the
        # password someone is already using.
        if name in users and not (args.user or args.reset_all):
            continue
        pw = make_password()
        users[name] = hash_password(pw)
        made.append((name, pw))

    if not made:
        print("Every account already exists. Nothing changed.\n"
              "  --user NAME    reset one\n"
              "  --reset-all    reset everyone")
        return

    save_users(users)

    width = max(len(n) for n, _ in made)
    print(f"\n  Written to {USERS_PATH}  (hashes only)\n")
    print(f"  {'user'.ljust(width)}   password")
    print(f"  {'-' * width}   {'-' * 18}")
    for name, pw in made:
        print(f"  {name.ljust(width)}   {pw}")
    print("\n  Shown once. Only the hash is stored -- reset the user if lost.")
    print("  Hand these over directly, not in a shared channel.\n")


if __name__ == "__main__":
    main()
