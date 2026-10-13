"""Login gate for the Streamlit app.

Streamlit ships no authentication, and the app drives a pool of paid proxies
and signed-in LLM accounts -- so anyone who can reach port 8501 can spend
your credits and burn your sessions. This is the gate in front of that.

What it is NOT: this is a shared-team door lock, not an identity system.
There are no roles, no lockout and no audit trail. It stops a passer-by on
the network, not a determined attacker who already has a foothold.

STAYING SIGNED IN. Streamlit's session_state is bound to the websocket, so
a browser refresh starts a brand-new session and loses it -- which is why
the login used to reappear on every F5. Signing in now also mints a random
token, recorded server-side in data/sessions.json and carried in the URL as
`?s=<token>`, so a refresh re-presents it and lands straight back in.

    Only the SHA-256 of each token is stored, so the file on disk is not a
    set of live keys -- but the URL itself is the credential. Anyone who
    gets the full link (a shared screenshot, a pasted URL, a bookmark on an
    unlocked machine) is signed in as that person until it expires. That is
    the trade for not adding a cookie-component dependency, and it is
    acceptable only because this sits on an internal port behind a firewall
    rule. Do not expose 8501 publicly on the strength of this gate.

Sessions idle out after SESSION_TTL_HOURS. Because every page render
touches `last_seen`, the same records answer "who is using this right now"
-- see active_users() and user_badge_html().

Passwords are stored as PBKDF2-HMAC-SHA256 hashes with a per-user salt,
never as plaintext:

    data/users.json   {"aayush": {"salt": "...", "hash": "...", "rounds": N}}

That file is gitignored. Create or reset it with:

    python scripts/make_users.py            # prints the new passwords ONCE
    python scripts/make_users.py --user suraj

Verification uses compare_digest, so a wrong password takes the same time
as a right one and the comparison leaks nothing by timing.
"""
from __future__ import annotations

import hashlib
import hmac
import html
import json
import os
import secrets
import time
from pathlib import Path

USERS_PATH = Path(__file__).resolve().parent.parent / "data" / "users.json"
SESSIONS_PATH = USERS_PATH.parent / "sessions.json"

# 200k rounds is ~0.1s on the server, which is nothing for a login anyone
# does once a day and real work for anyone grinding the file offline.
ROUNDS = 200_000
SALT_BYTES = 16


def hash_password(password: str, salt: bytes | None = None,
                  rounds: int = ROUNDS) -> dict:
    """{salt, hash, rounds} for storing. A fresh random salt per user."""
    salt = salt or secrets.token_bytes(SALT_BYTES)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"),
                                 salt, rounds)
    return {"salt": salt.hex(), "hash": digest.hex(), "rounds": rounds}


def load_users() -> dict:
    try:
        return json.loads(USERS_PATH.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def save_users(users: dict) -> None:
    USERS_PATH.parent.mkdir(parents=True, exist_ok=True)
    USERS_PATH.write_text(json.dumps(users, indent=2) + "\n", encoding="utf-8")
    # Owner-only where the OS honours it. A no-op on Windows, which is why
    # the file is gitignored as well rather than relying on this.
    try:
        os.chmod(USERS_PATH, 0o600)
    except OSError:
        pass


def verify(username: str, password: str) -> bool:
    """True when the password matches. Constant-time, and never raises.

    An unknown user still pays the full PBKDF2 cost against a throwaway
    salt, so "no such user" and "wrong password" take the same time and
    cannot be told apart by watching the clock.
    """
    users = load_users()
    rec = users.get((username or "").strip().lower())
    if not rec:
        hash_password(password or "", b"\0" * SALT_BYTES, ROUNDS)
        return False
    try:
        got = hashlib.pbkdf2_hmac(
            "sha256", (password or "").encode("utf-8"),
            bytes.fromhex(rec["salt"]), int(rec.get("rounds", ROUNDS)))
        return hmac.compare_digest(got.hex(), rec["hash"])
    except (ValueError, KeyError, TypeError):
        return False


# -- server-side sessions -------------------------------------------------

# Idle timeout, not absolute: last_seen is refreshed while the tab is in use,
# so a working day never logs you out mid-scan, and a forgotten tab does.
SESSION_TTL_HOURS = float(os.getenv("AUTH_TTL_HOURS", "168"))  # 7 days
# "Currently using" window for the who's-online badge.
ACTIVE_WINDOW_MIN = float(os.getenv("AUTH_ACTIVE_MIN", "15"))
# Don't rewrite the file on every rerun -- Streamlit reruns on every widget
# interaction, and last_seen only needs to be minute-accurate.
TOUCH_SECONDS = 60


def _token_id(token: str) -> str:
    """Storage key for a token. The raw token never touches disk."""
    return hashlib.sha256((token or "").encode("utf-8")).hexdigest()


def _load_sessions() -> dict:
    try:
        data = json.loads(SESSIONS_PATH.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def _save_sessions(sessions: dict) -> None:
    """Prune expired records, then replace the file atomically.

    Several browser tabs write this concurrently. os.replace is atomic, so a
    reader never sees a half-written file; the worst a race costs is one
    lost last_seen touch, which the next render redoes.
    """
    cutoff = time.time() - SESSION_TTL_HOURS * 3600
    live = {k: v for k, v in sessions.items()
            if isinstance(v, dict) and float(v.get("last_seen") or 0) > cutoff}
    try:
        SESSIONS_PATH.parent.mkdir(parents=True, exist_ok=True)
        tmp = SESSIONS_PATH.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(live, indent=2) + "\n", encoding="utf-8")
        try:
            os.chmod(tmp, 0o600)
        except OSError:
            pass
        os.replace(tmp, SESSIONS_PATH)
    except OSError:
        # A read-only data dir must not take the app down -- it just means
        # sessions stop surviving refresh, which is the old behaviour.
        pass


def start_session(username: str) -> str:
    """Record a new session and return its token (the URL half)."""
    token = secrets.token_urlsafe(32)
    now = time.time()
    sessions = _load_sessions()
    sessions[_token_id(token)] = {
        "user": username, "created": now, "last_seen": now}
    _save_sessions(sessions)
    return token


def resolve_session(token: str) -> str | None:
    """Username for a token, or None when unknown/expired."""
    if not token:
        return None
    rec = _load_sessions().get(_token_id(token))
    if not isinstance(rec, dict):
        return None
    if float(rec.get("last_seen") or 0) < time.time() - SESSION_TTL_HOURS * 3600:
        return None
    return rec.get("user") or None


def touch_session(token: str) -> None:
    """Mark a session as still in use. Also what feeds active_users()."""
    if not token:
        return
    sessions = _load_sessions()
    rec = sessions.get(_token_id(token))
    if isinstance(rec, dict):
        rec["last_seen"] = time.time()
        _save_sessions(sessions)


def end_session(token: str) -> None:
    if not token:
        return
    sessions = _load_sessions()
    if sessions.pop(_token_id(token), None) is not None:
        _save_sessions(sessions)


def active_users(within_min: float | None = None) -> list[tuple[str, float]]:
    """[(username, seconds_since_seen)] for whoever is using the app now.

    One entry per PERSON, not per tab: the same login open in three windows
    is one name, timed by its freshest tab.
    """
    window = (ACTIVE_WINDOW_MIN if within_min is None else within_min) * 60
    now = time.time()
    newest: dict[str, float] = {}
    for rec in _load_sessions().values():
        if not isinstance(rec, dict):
            continue
        name, seen = rec.get("user"), float(rec.get("last_seen") or 0)
        if not name or now - seen > window:
            continue
        if name not in newest or seen > newest[name]:
            newest[name] = seen
    return sorted(((n, now - s) for n, s in newest.items()), key=lambda x: x[1])


# -- the Streamlit gate ---------------------------------------------------

SESSION_KEY = "_auth_user"
TOKEN_KEY = "_auth_token"
TOUCH_KEY = "_auth_touched"
QS_KEY = "s"       # ?s=<token> in the URL
LOGOUT_KEY = "logout"


def current_user(st) -> str | None:
    return st.session_state.get(SESSION_KEY)


def logout(st) -> None:
    """Sign out everywhere: kill the server record, the state and the URL."""
    end_session(st.session_state.get(TOKEN_KEY) or _qs_get(st, QS_KEY))
    for key in (SESSION_KEY, TOKEN_KEY, TOUCH_KEY):
        st.session_state.pop(key, None)
    try:
        st.query_params.clear()
    except Exception:
        pass


def _qs_get(st, key: str) -> str:
    """One query param as a plain string, tolerating list-valued params."""
    try:
        val = st.query_params.get(key)
    except Exception:
        return ""
    if isinstance(val, (list, tuple)):
        val = val[0] if val else ""
    return str(val or "")


def _touch(st, token: str, force: bool = False) -> None:
    """Refresh last_seen, at most once every TOUCH_SECONDS.

    Throttled in session_state rather than skipped on the fast path: a tab
    that never re-reads the token would otherwise go stale and drop off the
    who's-online list while someone is actively working in it.
    """
    if not token:
        return
    now = time.time()
    if not force and now - float(st.session_state.get(TOUCH_KEY) or 0) < TOUCH_SECONDS:
        return
    st.session_state[TOUCH_KEY] = now
    touch_session(token)


def _ago(seconds: float) -> str:
    if seconds < 90:
        return "now"
    return f"{int(seconds // 60)}m ago"


def user_badge_html(st) -> str:
    """Topbar chip: who you are, who else is on, and a sign-out link.

    Returns HTML rather than drawing widgets so it can be dropped inside the
    existing topbar markup instead of landing in its own Streamlit block
    below it. Every name is escaped -- usernames come from a file an admin
    writes, but a chip that renders raw markup is a bad habit either way.
    """
    me = current_user(st)
    if not me:
        return ""
    others = [(n, s) for n, s in active_users() if n != me]
    if others:
        tip = ", ".join(f"{n} ({_ago(s)})" for n, s in others)
        extra = (f'<span class="userchip-others" title="{html.escape(tip)}">'
                 f'+{len(others)} online</span>')
    else:
        extra = ""
    return (
        '<div class="userchip">'
        '<span class="userchip-dot"></span>'
        f'<span class="userchip-name">{html.escape(me)}</span>'
        f'{extra}'
        f'<a class="userchip-out" href="?{LOGOUT_KEY}=1" target="_self">Sign out</a>'
        '</div>')


def require_login(st) -> str:
    """Render the login form and stop the script unless signed in.

    Returns the username when authenticated. Call it immediately after
    st.set_page_config and before anything else draws -- st.stop() below
    is what keeps the rest of the page from rendering to a stranger.
    """
    # Sign-out arrives as ?logout=1 from the topbar link. Handled here rather
    # than with a button so the chip is plain HTML and works on every page.
    if _qs_get(st, LOGOUT_KEY):
        logout(st)

    user = current_user(st)
    if user:
        token = st.session_state.get(TOKEN_KEY, "")
        # Re-stamp the URL. Switching pages can drop the query string while
        # session_state survives; without this the next refresh on that page
        # would have no token to present and would bounce to the login.
        if token and _qs_get(st, QS_KEY) != token:
            try:
                st.query_params[QS_KEY] = token
            except Exception:
                pass
        _touch(st, token)
        return user

    # No live session_state -- this is a refresh, a new tab, or a stranger.
    # A valid ?s= token is what tells the first two apart from the third.
    token = _qs_get(st, QS_KEY)
    if token:
        name = resolve_session(token)
        if name:
            st.session_state[SESSION_KEY] = name
            st.session_state[TOKEN_KEY] = token
            _touch(st, token, force=True)
            return name
        # Expired or revoked: drop the dead token so the URL stops carrying
        # it and the login form isn't rendered under a stale query string.
        try:
            del st.query_params[QS_KEY]
        except Exception:
            pass

    users = load_users()
    st.markdown(
        "<style>[data-testid='stAppViewContainer'] .main .block-container"
        "{max-width:420px;padding-top:12vh;}</style>",
        unsafe_allow_html=True)
    st.markdown("### ◆ Citerion")

    if not users:
        # Said plainly rather than silently refusing every login: an empty
        # users file looks exactly like a wrong password otherwise.
        st.error("No accounts exist yet. On the server, run:\n\n"
                 "`python scripts/make_users.py`")
        st.stop()

    st.caption("Sign in to continue.")
    with st.form("login", clear_on_submit=False):
        name = st.text_input("Username").strip().lower()
        pw = st.text_input("Password", type="password")
        ok = st.form_submit_button("Sign in", use_container_width=True)

    if ok:
        if verify(name, pw):
            token = start_session(name)
            st.session_state[SESSION_KEY] = name
            st.session_state[TOKEN_KEY] = token
            st.session_state[TOUCH_KEY] = time.time()
            try:
                st.query_params[QS_KEY] = token
            except Exception:
                pass
            st.rerun()
        # One message for both cases. Telling a stranger which half was
        # wrong hands them a way to enumerate the usernames.
        st.error("Wrong username or password.")
    st.stop()
