"""Shared async Playwright helpers + network-interception utilities.

Each platform uses a *persistent* user data dir so cookies/logins survive
between runs. The intercept helpers attach a `page.on('response', ...)`
listener that:

  - filters by a URL substring match (per-platform backend endpoint),
  - parses JSON bodies and SSE `data: {...}` lines (incl. double-encoded
    strings),
  - recursively walks the decoded objects for the three things we want:
    `search_model_queries`, `search("...")` tool calls, and any URL hidden
    in `search_result_groups[].entries[].url` (ChatGPT) or
    `groundingChunks`/`groundingSupports` (Gemini).

Set env var `SCRAPER_DEBUG_URLS=1` to dump every backend response URL each
capturer sees to `data/debug/<platform>_urls.log` (and a body sample for
anything JSON/SSE-shaped). Used to diagnose endpoint changes.
"""
from __future__ import annotations

import asyncio
import json
import os
import re
import threading
import time
from contextlib import asynccontextmanager
from pathlib import Path
from typing import AsyncIterator, Callable

from playwright.async_api import async_playwright, BrowserContext, Page

from .base import DATA_DIR, profile_dir

# patchright is a Chromium fork of Playwright with deeper anti-bot patches —
# critical for Cloudflare Turnstile on chatgpt.com which fingerprints vanilla
# Playwright's automation tells. Import is optional so the module still works
# on machines without it (Gemini/Perplexity don't need it).
try:
    from patchright.async_api import async_playwright as async_patchright  # type: ignore
    _HAS_PATCHRIGHT = True
except Exception:  # pragma: no cover
    async_patchright = None  # type: ignore
    _HAS_PATCHRIGHT = False


_DEBUG = os.environ.get("SCRAPER_DEBUG_URLS") == "1"
_DEBUG_DIR = DATA_DIR / "debug"
if _DEBUG:
    _DEBUG_DIR.mkdir(parents=True, exist_ok=True)


def _debug_log(platform: str, url: str, status: int, ctype: str, body_preview: str = "") -> None:
    if not _DEBUG:
        return
    try:
        path = _DEBUG_DIR / f"{platform}_urls.log"
        with open(path, "a", encoding="utf-8") as f:
            f.write(f"{status}\t{ctype}\t{url}\n")
            if body_preview:
                f.write(f"  > {body_preview[:600]}\n")
    except Exception:
        pass


_BLOCKED_RESOURCE_TYPES = {"image", "font", "media"}


def resource_blocking_enabled() -> bool:
    """Whether to intercept and drop image/font/media requests.

    OFF by default. Blocking is a real speed win (a scrape only reads text and
    attributes), but `ctx.route("**/*")` on the patchright + real-Chrome sessions
    DEADLOCKS navigation: with the route attached, page.goto() never returns and the
    tab sits on about:blank forever. Measured directly — identical launch, the only
    difference being the route:

        route attached  -> goto chatgpt.com hangs (>60s, no timeout fired)
        no route        -> goto chatgpt.com returns in 1.4s

    It survived a few runs before starting to hang, so it is not reliably safe.
    Set SCRAPER_BLOCK_RESOURCES=1 to opt in if you want the speed and can watch it.
    """
    return (os.getenv("SCRAPER_BLOCK_RESOURCES", "0").strip().lower()
            in ("1", "true", "yes", "on"))


# Flags that stop Chrome greeting a killed profile with the crash-restore bubble.
# NOTE: do NOT add "--restore-last-session=false". Chrome's switch is presence-based,
# so passing it with =false still reads as PRESENT and turns session restore ON —
# every run then reopens the previous run's tabs and they pile up.
CRASH_BUBBLE_ARGS = (
    "--hide-crash-restore-bubble",
    "--disable-session-crashed-bubble",
)


def sanitize_profile(user_dir: str) -> None:
    """Clear Chrome's "didn't shut down correctly" state for a profile.

    A profile whose Chrome was SIGKILLed — by release_profile, a crash, a killed
    terminal — keeps exit_type="Crashed" in its Preferences. The next launch then
    shows the "Restore pages?" bubble, which takes focus and leaves the tab sitting
    on about:blank until someone dismisses it, so the scrape just waits. Rewriting
    the two flags Chrome checks makes it believe the last exit was clean.
    """
    import json

    for rel in ("Default/Preferences", "Preferences"):
        path = os.path.join(user_dir, rel)
        if not os.path.exists(path):
            continue
        try:
            with open(path, "r", encoding="utf-8") as fh:
                data = json.load(fh)
            prof = data.setdefault("profile", {})
            if prof.get("exit_type") == "Normal" and prof.get("exited_cleanly") is True:
                continue
            prof["exit_type"] = "Normal"
            prof["exited_cleanly"] = True
            with open(path, "w", encoding="utf-8") as fh:
                json.dump(data, fh)
        except Exception:
            # a corrupt/locked Preferences file is not worth failing the scrape over
            pass


def release_profile(user_dir: str) -> int:
    """Free a Chromium user-data-dir that a dead run left locked.

    Playwright refuses to open a persistent context whose profile is already in
    use ("Opening in existing browser session"), which is what you get after a
    scrape is interrupted — Ctrl-C in Streamlit, a killed terminal, a crash — and
    the orphaned Chrome keeps the directory. Kill whatever still holds it and drop
    the singleton locks. Returns how many processes were killed.
    """
    import os
    import signal
    import subprocess

    killed = 0
    try:
        out = subprocess.run(["ps", "-axo", "pid=,command="],
                             capture_output=True, text=True, timeout=10).stdout
        for line in out.splitlines():
            if f"--user-data-dir={user_dir}" not in line:
                continue
            pid_s = line.strip().split(None, 1)[0]
            try:
                os.kill(int(pid_s), signal.SIGKILL)
                killed += 1
            except (ProcessLookupError, PermissionError, ValueError):
                pass
    except Exception:
        pass
    # Chromium leaves these behind when it dies uncleanly; they alone can block a relaunch.
    for name in ("SingletonLock", "SingletonCookie", "SingletonSocket"):
        try:
            os.unlink(os.path.join(user_dir, name))
        except OSError:
            pass
    # We just SIGKILLed it, so the profile now reads as "crashed" — clear that here
    # or the next launch opens on the Restore-pages bubble instead of the page.
    sanitize_profile(user_dir)
    return killed


class SlotPool:
    """Hands out one of N slots, so a platform can keep N independent sessions.

    Google and ChatGPT each reused ONE browser behind a module-level lock, so every
    keyword queued behind the previous one — 500 keywords x 20s is 2.8 hours of
    strictly serial work, and SCRAPE_CONCURRENCY did nothing for them. A slot is
    just an integer; the platform keeps `{slot: session}` and gives each slot its
    own profile dir, which is what makes the sessions genuinely independent
    (a persistent profile can only be open in one browser at a time).

    The queue is rebuilt if the event loop changes — each "Run scan" is one
    asyncio.run, i.e. a fresh loop, and a Queue is bound to the loop that made it.
    """

    def __init__(self, size: int):
        self.size = max(1, int(size))
        self._q: asyncio.Queue | None = None
        self._loop = None
        self._lock = asyncio.Lock()

    async def _queue(self) -> asyncio.Queue:
        loop = asyncio.get_running_loop()
        async with self._lock:
            if self._q is None or self._loop is not loop:
                self._q = asyncio.Queue()
                for i in range(self.size):
                    self._q.put_nowait(i)
                self._loop = loop
        return self._q

    @asynccontextmanager
    async def slot(self):
        q = await self._queue()
        i = await q.get()
        try:
            yield i
        finally:
            q.put_nowait(i)


def _load_env_file() -> None:
    """Read the project's .env into the environment.

    Unlike some of the other projects here, this one has no .env loader of its own,
    so PROXY_USER / PROXY_PASS sat in .env and were never picked up. Parsed inline
    rather than via python-dotenv, which isn't in requirements.txt. setdefault, so a
    real exported env var always wins over the file.
    """
    path = Path(__file__).resolve().parent.parent / ".env"
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, val = line.split("=", 1)
        os.environ.setdefault(key.strip(), val.strip().strip('"').strip("'"))


_load_env_file()


# The BuyProxies account API. The URL embeds the account key, so it lives in
# .env as BUYPROXIES_URL and never in source:
#
#     https://api.buyproxies.org/?a=showProxies&pid=...&key=...&port=12345
#     -> 100 lines of  host:port:user:pass
#
# THE URL IS THE SOURCE OF TRUTH. data/proxies.txt is a crash-mat for when the
# API is unreachable, not a cache to read from in normal operation.
#
# It used to be a real 1-hour cache, and that was wrong: BuyProxies reassigns
# exits, so for up to an hour after a refresh every scrape was being handed
# IPs the account no longer owns. Those do not fail cleanly -- they answer,
# and Google serves a CAPTCHA. Repeated CAPTCHAs with "rotating (1/2), (2/2)"
# in the log is what a stale list looks like from the outside.
#
# PROXY_CACHE_TTL=0 (the default) means always refetch. Set it to a number of
# seconds only if the API starts rate-limiting.
PROXY_CACHE = Path(__file__).resolve().parent.parent / "data" / "proxies.txt"
PROXY_CACHE_TTL = int(os.getenv("PROXY_CACHE_TTL", "0"))
PROXY_FETCH_TIMEOUT = 30

# Collapses the burst when several slots launch at once -- three browsers
# starting together must not mean three identical API calls. Short enough that
# a rotation mid-scan is still picked up on the next launch.
PROXY_FETCH_DEBOUNCE = int(os.getenv("PROXY_FETCH_DEBOUNCE", "10"))
_proxy_memo: dict = {"at": 0.0, "servers": []}

_proxy_lock = threading.Lock()


def _parse_proxy_lines(text: str) -> list[str]:
    """host:port:user:pass -> ['host:port', ...], credentials into the env.

    Every exit on one BuyProxies account shares the same user/pass (verified:
    100 exits, one distinct credential pair), and proxy_config() already reads
    PROXY_USER / PROXY_PASS separately -- so the credentials are lifted out of
    the list and the list itself stays shareable.

    The API response WINS over whatever is already in the environment. It used
    to be setdefault, so a .env credential stuck forever -- and when BuyProxies
    rotates the account password the exits then answer with an auth failure
    that Google reports as a CAPTCHA, not as a proxy error. The list that
    names these exits is the thing that knows their password.

    Set PROXY_CREDS_PIN=1 to keep the .env values regardless.
    """
    pin = (os.getenv("PROXY_CREDS_PIN") or "").strip().lower() in (
        "1", "true", "yes", "on")
    servers: list[str] = []
    user = password = None
    for line in (text or "").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        parts = line.split(":")
        if len(parts) >= 4:
            servers.append(f"{parts[0]}:{parts[1]}")
            user, password = parts[2], ":".join(parts[3:])
        elif len(parts) == 2:
            servers.append(line)
    if user and not pin:
        if os.environ.get("PROXY_USER") not in (None, "", user):
            print("[proxies] account username changed — using the one from "
                  "the API (PROXY_CREDS_PIN=1 keeps your .env value)")
        if os.environ.get("PROXY_PASS") not in (None, "", password):
            print("[proxies] account password changed — using the one from "
                  "the API (PROXY_CREDS_PIN=1 keeps your .env value)")
        os.environ["PROXY_USER"] = user
        os.environ["PROXY_PASS"] = password
    elif user:
        os.environ.setdefault("PROXY_USER", user)
        os.environ.setdefault("PROXY_PASS", password)
    return servers


def _proxies_from_api() -> list[str]:
    """Fetch the account's exits, through the cache. [] when not configured."""
    url = (os.getenv("BUYPROXIES_URL") or "").strip()
    if not url:
        return []
    with _proxy_lock:
        now = time.time()
        # Debounce only — this is not the cache, it just stops N simultaneous
        # launches making N identical calls a second apart.
        if (_proxy_memo["servers"]
                and now - _proxy_memo["at"] < PROXY_FETCH_DEBOUNCE):
            return list(_proxy_memo["servers"])
        # Opt-in disk cache. Off by default (TTL 0) so the URL is always asked.
        if PROXY_CACHE_TTL > 0:
            try:
                if now - PROXY_CACHE.stat().st_mtime < PROXY_CACHE_TTL:
                    return _parse_proxy_lines(
                        PROXY_CACHE.read_text(encoding="utf-8"))
            except OSError:
                pass
        try:
            import requests
            r = requests.get(url, timeout=PROXY_FETCH_TIMEOUT)
            r.raise_for_status()
            servers = _parse_proxy_lines(r.text)
            if servers:
                PROXY_CACHE.parent.mkdir(parents=True, exist_ok=True)
                PROXY_CACHE.write_text(r.text, encoding="utf-8")
                _proxy_memo.update(at=now, servers=list(servers))
                return servers
        except Exception as e:
            print(f"[proxies] API fetch failed ({type(e).__name__}) — "
                  "falling back to the last saved list, which may be stale")
        # API unreachable: a stale list beats no list. These exits usually
        # still work -- the assignment changes rarely -- and failing over to
        # "no proxy" would send every request from the server's own IP, which
        # is the one thing the proxies exist to avoid.
        try:
            return _parse_proxy_lines(PROXY_CACHE.read_text(encoding="utf-8"))
        except OSError:
            return []


def proxy_servers() -> list[str]:
    """Configured proxies as 'host:port'.

    In order: PROXY_LIST (explicit override), the BuyProxies API, then the
    file at PROXY_FILE (default: proxies.txt beside the app). Empty result =
    no proxying, which is still the default when nothing is configured.
    """
    raw = (os.getenv("PROXY_LIST") or "").strip()
    if raw:
        return [p.strip() for p in raw.split(",") if p.strip()]
    got = _proxies_from_api()
    if got:
        return got
    path = (os.getenv("PROXY_FILE") or "").strip() or str(
        Path(__file__).resolve().parent.parent / "proxies.txt")
    try:
        with open(path, encoding="utf-8") as fh:
            return _parse_proxy_lines(fh.read())
    except OSError:
        return []


def proxy_config(pick: str | None = None,
                 exclude: "set[str] | tuple[str, ...]" = ()) -> dict | None:
    """Playwright proxy dict, or None when no proxies are configured.

    GOOGLE AIO ONLY. scrapers/google.py is the single caller, deliberately:
    Google's SERP bot-detection is what the exits are there for. ChatGPT,
    Gemini and Perplexity run direct and must stay that way --

      * Gemini and Perplexity sit behind a SIGNED-IN profile. Moving an
        established session onto a datacentre IP in another country is the
        shape of a stolen cookie, and it gets the account challenged or
        banned.
      * Perplexity goes through Cloudflare via Camoufox; a datacentre exit
        raises the challenge rate rather than lowering it. Anonymous ChatGPT
        is the same story -- it was tried there on 2026-10-13 and removed,
        see the note in scrapers/chatgpt.py.
      * they are not IP-rate-limited the way a bare SERP scrape is, so the
        exits would buy nothing and cost a hop each way.

    REGION: every exit in the pool is a US datacentre IP (ColoCrossing), so
    anything routed through here answers as a US visitor. Google AIO can
    accept that because the SERP is fetched with explicit `gl`/`hl`
    parameters that pin the region regardless of exit. A chat UI has no such
    override, so proxying one trades the Indian region away outright --
    which, on machines that already egress from India, is a pure loss.

    Do not widen this to other platforms without re-testing those accounts.

    Credentials come from PROXY_USER / PROXY_PASS (env or .env) — never from the
    proxy list itself, so the list can be committed/shared without leaking them.

    A persistent session is reused across every keyword in a batch, so the proxy is
    chosen once per LAUNCH, not per keyword; rotation happens whenever the session
    is rebuilt. Pass `pick` to force a specific one.
    """
    servers = proxy_servers()
    if not servers:
        return None
    import random
    if not pick:
        # Skip exits that have already been CAPTCHA-walled. If every one is burned,
        # fall back to the full list rather than refusing to run — a flagged IP
        # usually recovers after a while, so retrying beats stopping.
        fresh = [s for s in servers if s not in set(exclude)]
        pick = random.choice(fresh or servers)
    server = pick
    cfg = {"server": server if "://" in server else f"http://{server}"}
    user = (os.getenv("PROXY_USER") or "").strip()
    password = (os.getenv("PROXY_PASS") or "").strip()
    if user:
        cfg["username"] = user
    if password:
        cfg["password"] = password
    return cfg


async def single_page(ctx):
    """Return ONE page for this context, closing every other tab.

    A persistent profile can come back with tabs from a previous run (session
    restore, a popup, a crashed shutdown). Blindly taking pages[0] then leaves the
    extras open — they accumulate every run, and the scraper may end up driving a
    tab that isn't the one it navigated. Collapse to a single tab each time.
    """
    try:
        pages = list(ctx.pages)
    except Exception:
        pages = []
    if not pages:
        return await ctx.new_page()
    page = pages[0]
    for extra in pages[1:]:
        try:
            await extra.close()
        except Exception:
            pass
    return page


def is_profile_locked_error(exc: Exception) -> bool:
    """True for the 'profile already in use' launch failure specifically."""
    msg = str(exc)
    return ("Opening in existing browser session" in msg
            or "already in use" in msg
            or "ProcessSingleton" in msg)


async def _route_block(route):
    """Abort image/font/media, let everything else through.

    Every call is guarded: an interception handler that raises leaves its request
    hanging forever, and the page then sits on a spinning about:blank until the
    navigation timeout. A route that's already been handled (or whose page is
    closing) raises exactly that way, so failures must degrade to "let it load"
    rather than propagate.
    """
    try:
        blocked = route.request.resource_type in _BLOCKED_RESOURCE_TYPES
    except Exception:
        blocked = False
    try:
        if blocked:
            await route.abort()
        else:
            await route.continue_()
    except Exception:
        # last resort: try to release the request so it can't hang the page
        try:
            await route.continue_()
        except Exception:
            pass


@asynccontextmanager
async def persistent_context(
    platform: str, headless: bool = True, block_resources: bool = True,
    engine: str = "playwright",
) -> AsyncIterator[BrowserContext]:
    """Launch a persistent Chromium context.

    engine="playwright" — vanilla Playwright Chromium (default).
    engine="patchright" — patched Chromium for Cloudflare Turnstile bypass.
        Falls back to playwright if patchright isn't importable. Per
        patchright's own guidance: do NOT pass --disable-blink-features=
        AutomationControlled (it leaks more than it hides), and prefer real
        Chrome via channel='chrome' so the user-agent / build fingerprint
        matches a real browser.
    """
    user_dir = str(profile_dir(platform))
    # Clear any "crashed" marker first, or Chrome opens on the Restore-pages bubble
    # and the tab sits on about:blank waiting to be dismissed.
    sanitize_profile(user_dir)
    use_patchright = engine == "patchright" and _HAS_PATCHRIGHT
    runner = async_patchright if use_patchright else async_playwright
    async with runner() as p:
        kwargs = dict(
            user_data_dir=user_dir,
            headless=headless,
            viewport={"width": 1280, "height": 900},
            # Playwright defaults chromium_sandbox to False, which injects
            # --no-sandbox. Chrome then shows the yellow "unsupported
            # command-line flag" infobar, and that flag is one of the
            # cheapest automation tells there is -- ChatGPT's Turnstile
            # answers it with "An error occurred during verification".
            # A real browser runs sandboxed, so this one does too.
            chromium_sandbox=True,
        )
        if use_patchright:
            # Real Chrome avoids the bundled-Chromium fingerprint that
            # Turnstile flags. Pass minimal args so patchright's own patches
            # are not overridden.
            kwargs["channel"] = "chrome"
            kwargs["no_viewport"] = False
            kwargs["args"] = ["--no-default-browser-check", *CRASH_BUBBLE_ARGS]
        else:
            kwargs["args"] = [
                "--disable-blink-features=AutomationControlled",
                "--no-default-browser-check",
                *CRASH_BUBBLE_ARGS,
            ]
        try:
            ctx = await p.chromium.launch_persistent_context(**kwargs)
        except Exception as e:
            if is_profile_locked_error(e):
                # An interrupted run left Chrome holding this profile — clear and retry.
                n = release_profile(user_dir)
                print(f"[{platform}] profile was locked — released "
                      f"({n} process(es) killed), retrying")
                ctx = await p.chromium.launch_persistent_context(**kwargs)
            elif use_patchright and "channel" in kwargs:
                # patchright with channel='chrome' fails if Chrome isn't installed.
                # Fall back to bundled Chromium so the scrape still runs (Turnstile
                # may still gate it but at least we get a clear error path).
                kwargs.pop("channel", None)
                try:
                    ctx = await p.chromium.launch_persistent_context(**kwargs)
                except Exception as e2:
                    if not is_profile_locked_error(e2):
                        raise
                    release_profile(user_dir)
                    ctx = await p.chromium.launch_persistent_context(**kwargs)
            else:
                raise
        if block_resources:
            await ctx.route("**/*", _route_block)
        try:
            yield ctx
        finally:
            await ctx.close()


@asynccontextmanager
async def new_page(
    platform: str, headless: bool = True, block_resources: bool = True,
    stealth: bool = True, engine: str = "playwright",
) -> AsyncIterator[Page]:
    async with persistent_context(
        platform, headless=headless, block_resources=block_resources, engine=engine,
    ) as ctx:
        page = await single_page(ctx)
        # patchright already patches navigator.webdriver et al at the binary
        # level; injecting playwright-stealth on top of it can RE-LEAK tells
        # (the two patch sets fight). Skip stealth when using patchright.
        if stealth and engine != "patchright":
            # playwright-stealth patches the obvious automation tells
            # (navigator.webdriver, plugins length, permissions API, etc.) —
            # critical for Perplexity which Cloudflare-fingerprints aggressively.
            try:
                from playwright_stealth import Stealth
                await Stealth().apply_stealth_async(page)
            except Exception:
                # Fall back to the manual marker hide if the package isn't usable.
                await page.add_init_script(
                    "Object.defineProperty(navigator,'webdriver',{get:()=>undefined});"
                )
        elif engine != "patchright":
            await page.add_init_script(
                "Object.defineProperty(navigator,'webdriver',{get:()=>undefined});"
            )
        try:
            yield page
        finally:
            try:
                await page.close()
            except Exception:
                pass


async def wait_quiet(
    page: Page, *, idle_ms: int = 800, timeout_ms: int = 90_000,
    net_idle_timeout_ms: int = 6_000, net_idle: bool = True,
) -> None:
    """Wait for DOM + (optionally) a short networkidle window, then a tail idle.

    `net_idle_timeout_ms` caps the networkidle wait separately from
    `timeout_ms`. Streaming SPAs (ChatGPT, Gemini) keep long-poll sockets open
    and never hit networkidle, so pinning it to the full 90s stalled every
    call. 6s is enough for the shell to settle and cheap when it doesn't.

    Pass `net_idle=False` when the caller immediately waits on a real selector
    anyway: on those SPAs the networkidle wait never succeeds, so it just burns
    its whole timeout on every prompt before the useful wait even starts."""
    await page.wait_for_load_state("domcontentloaded", timeout=timeout_ms)
    if net_idle:
        try:
            await page.wait_for_load_state("networkidle", timeout=net_idle_timeout_ms)
        except Exception:
            pass
    if idle_ms:
        await page.wait_for_timeout(idle_ms)


async def wait_for_stable_text(
    page: Page,
    selector: str,
    *,
    idle_ms: int = 2500,
    max_ms: int = 240_000,
    poll_ms: int = 350,
    min_chars: int = 80,
) -> int:
    """Poll the LAST element matching `selector` until its text stops growing
    for `idle_ms`. Returns final length (0 if never appeared)."""
    import time
    deadline = time.monotonic() + max_ms / 1000
    last_len = -1
    last_change = time.monotonic()
    while time.monotonic() < deadline:
        try:
            loc = page.locator(selector).last
            if await loc.count():
                cur = await loc.evaluate("e => (e.innerText || '').length") or 0
            else:
                cur = 0
        except Exception:
            cur = last_len
        if cur != last_len:
            last_len = cur
            last_change = time.monotonic()
        elif cur >= min_chars and (time.monotonic() - last_change) * 1000 >= idle_ms:
            return cur
        await page.wait_for_timeout(poll_ms)
    return max(last_len, 0)


async def collect_external_links(
    page: Page,
    container_selector: str | None = None,
    skip_hosts: tuple[str, ...] = (),
) -> list[str]:
    """Pull href values from a container (or whole page), keep only external
    http(s) URLs, dropping any host that contains a string in `skip_hosts`."""
    if container_selector:
        loc = page.locator(f"{container_selector} a")
    else:
        loc = page.locator("a")
    hrefs = await loc.evaluate_all("els => els.map(e => e.href).filter(Boolean)")
    seen, out = set(), []
    for h in hrefs:
        if not h or not h.startswith(("http://", "https://")):
            continue
        if h in seen:
            continue
        host = h.split("/", 3)[2].lower() if "://" in h else ""
        if any(s in host for s in skip_hosts):
            continue
        seen.add(h)
        out.append(h)
    return out


# ---------------------------------------------------------------------------
# Network interception
# ---------------------------------------------------------------------------

_SEARCH_CALL_RE = re.compile(r'search\s*\(\s*\\?["\'](.*?)\\?["\']\s*\)')
_URL_RE = re.compile(r'https?://[^\s"\'<>)\]\\]+')


def find_queries_and_urls(obj) -> tuple[list[str], list[str]]:
    """Recursively walk a decoded JSON object, returning
    (fanout_queries, source_urls).

    Picks up:
      - ChatGPT: `search_model_queries.queries[]`, `search_result_groups[].entries[].url`,
        and `content.content_type == "code"` with `text == search("...")`.
      - Gemini grounding payloads: looks for any dict with a `url`/`uri` key
        under arbitrary nesting (groundingChunks / web.uri).
      - Inline `search(\"...\")` calls inside arbitrary string values.
    """
    queries: list[str] = []
    urls: list[str] = []

    def _walk(o):
        if isinstance(o, dict):
            for k, v in o.items():
                # ChatGPT fanout queries (metadata style)
                if k == "search_model_queries" and isinstance(v, dict):
                    q_list = v.get("queries", [])
                    if isinstance(q_list, list):
                        queries.extend([q for q in q_list if isinstance(q, str)])
                # ChatGPT tool-call style: {"content_type":"code","text":"search(\"q\")"}
                elif k == "content" and isinstance(v, dict):
                    if v.get("content_type") == "code":
                        txt = v.get("text", "") or ""
                        if "search(" in txt:
                            m = _SEARCH_CALL_RE.search(txt)
                            if m:
                                queries.append(m.group(1))
                # ChatGPT source URLs
                elif k == "search_result_groups" and isinstance(v, list):
                    for group in v:
                        if not isinstance(group, dict):
                            continue
                        for entry in group.get("entries", []):
                            if not isinstance(entry, dict):
                                continue
                            url = entry.get("url")
                            if isinstance(url, str):
                                clean = url
                                if "](" in url and url.endswith(")"):
                                    clean = url.split("](")[1][:-1]
                                urls.append(clean)
                # Gemini grounding chunks: web.uri / web.url
                elif k == "web" and isinstance(v, dict):
                    for key in ("uri", "url"):
                        val = v.get(key)
                        if isinstance(val, str) and val.startswith(("http://", "https://")):
                            urls.append(val)
                # Generic url key
                elif k in ("url", "uri") and isinstance(v, str) and v.startswith(("http://", "https://")):
                    urls.append(v)
                else:
                    _walk(v)
        elif isinstance(o, list):
            for item in o:
                _walk(item)
        elif isinstance(o, str):
            # Catch search("...") buried in stringified payloads
            if "search(" in o:
                for m in _SEARCH_CALL_RE.finditer(o):
                    queries.append(m.group(1))

    _walk(obj)
    return queries, urls


def _parse_google_batchexecute(text: str) -> list:
    """Parse Google's `)]}'`+chunked-length JSON format used by Gemini's
    `/_/BardChatUi/data/batchexecute` and `/StreamGenerate` endpoints.

    Body layout (whitespace varies):
        )]}'
        <len1>
        <json_array_1>
        <len2>
        <json_array_2>
        ...

    Each JSON array typically looks like `[["wrb.fr","rpcId","<inner_json_str>",null,null,null,"generic"], ["di",N], ...]`
    where `<inner_json_str>` is itself a JSON document we want to walk.

    Returns a flat list of every parsed JSON value (outer chunks AND any
    inner string payloads that re-decoded as JSON).
    """
    if not text:
        return []
    s = text.lstrip()
    if s.startswith(")]}'"):
        s = s[4:]
    out: list = []
    i = 0
    n = len(s)
    while i < n:
        # Skip whitespace
        while i < n and s[i] in " \r\n\t":
            i += 1
        if i >= n:
            break
        # Read leading length digits
        j = i
        while j < n and s[j].isdigit():
            j += 1
        if j == i:
            # No length prefix — try parsing remainder as a single JSON doc.
            try:
                out.append(json.loads(s[i:]))
            except json.JSONDecodeError:
                pass
            break
        try:
            length = int(s[i:j])
        except ValueError:
            break
        # Skip whitespace after length
        i = j
        while i < n and s[i] in " \r\n\t":
            i += 1
        chunk = s[i:i + length]
        i += length
        try:
            data = json.loads(chunk)
        except json.JSONDecodeError:
            continue
        out.append(data)
        # Walk for inner JSON strings (e.g. `[["wrb.fr","id","<json>",...]]`).
        if isinstance(data, list):
            for entry in data:
                if isinstance(entry, list) and len(entry) >= 3 and isinstance(entry[2], str):
                    s2 = entry[2].strip()
                    if s2.startswith(("[", "{")):
                        try:
                            out.append(json.loads(s2))
                        except json.JSONDecodeError:
                            pass
    return out


def parse_sse_or_json(text: str) -> tuple[list[str], list[str]]:
    """Try to parse `text` as one of:
      - a full JSON document,
      - an SSE stream of `data: {...}` lines (incl. double-encoded strings),
      - Google's `)]}'`+chunked-length batchexecute format (Gemini).

    Returns (queries, urls) accumulated across every parseable chunk.
    """
    queries: list[str] = []
    urls: list[str] = []

    if not text:
        return queries, urls

    # Path 0: Google batchexecute / StreamGenerate (Gemini).
    if text.lstrip().startswith(")]}'"):
        for obj in _parse_google_batchexecute(text):
            q, u = find_queries_and_urls(obj)
            queries.extend(q)
            urls.extend(u)
        # Regex fallback for raw http URLs the walker may not key on.
        for m in _URL_RE.finditer(text):
            urls.append(m.group(0))
        return queries, urls

    # Path 1: maybe it's a JSON document.
    stripped = text.lstrip()
    if stripped.startswith(("{", "[")):
        try:
            data = json.loads(text)
            q, u = find_queries_and_urls(data)
            queries.extend(q)
            urls.extend(u)
        except json.JSONDecodeError:
            pass

    # Path 2: SSE — walk every `data: ...` line.
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line.startswith("data: "):
            continue
        chunk = line[6:].strip()
        if chunk == "[DONE]" or not chunk:
            continue
        try:
            data = json.loads(chunk)
        except json.JSONDecodeError:
            continue
        # Double-encoded: `data: "{\"foo\":..}"`
        if isinstance(data, str):
            try:
                data = json.loads(data)
            except json.JSONDecodeError:
                pass
        q, u = find_queries_and_urls(data)
        queries.extend(q)
        urls.extend(u)

    # Path 3: regex fallback for anything URL-shaped that the walker missed.
    if not urls and ("search_result" in text or "groundingChunks" in text):
        for m in _URL_RE.finditer(text):
            urls.append(m.group(0))

    return queries, urls


def make_response_capturer(
    url_predicate: Callable[[str], bool],
    *,
    skip_hosts: tuple[str, ...] = (),
    debug_platform: str | None = None,
    debug_host_match: str | None = None,
) -> tuple[Callable, list[str], list[str]]:
    """Build a `page.on('response', handler)` callback that accumulates
    fanout queries and source URLs whenever a response URL matches
    `url_predicate`.

    When `SCRAPER_DEBUG_URLS=1` and `debug_platform` is set, every response
    whose URL contains `debug_host_match` (or the platform name if no match
    given) is logged to `data/debug/<platform>_urls.log` regardless of
    whether the predicate matched — used to find the real endpoints.

    Returns (handler, queries_list, urls_list). Caller registers the handler
    via `page.on("response", handler)` and reads the lists after the scrape.
    """
    queries: list[str] = []
    urls: list[str] = []
    host_filter = (debug_host_match or debug_platform or "").lower()

    async def handler(response):
        try:
            url = response.url
            matched = url_predicate(url)

            # Debug path: log non-matches too if their host looks relevant.
            if _DEBUG and debug_platform and host_filter and host_filter in url.lower():
                ctype = ""
                try:
                    ctype = response.headers.get("content-type", "")
                except Exception:
                    pass
                preview = ""
                if any(t in ctype for t in ("json", "event-stream", "text/plain")):
                    try:
                        body = await response.text()
                        preview = (body or "")[:500].replace("\n", " ")
                    except Exception:
                        pass
                _debug_log(debug_platform, url, response.status, ctype, preview)

            if not matched:
                return
            try:
                text = await response.text()
            except Exception:
                return
            if not text:
                return
            q, u = parse_sse_or_json(text)
            if q:
                queries.extend(q)
            if u:
                # Filter skip_hosts at capture time so callers can decide policy
                for h in u:
                    host = h.split("/", 3)[2].lower() if "://" in h else ""
                    if any(s in host for s in skip_hosts):
                        continue
                    urls.append(h)
        except Exception:
            # Listener exceptions are swallowed by Playwright but bubble noise
            # in some envs — keep this handler quiet.
            pass

    return handler, queries, urls


def dedupe(items: list[str]) -> list[str]:
    seen, out = set(), []
    for x in items:
        if x and x not in seen:
            seen.add(x)
            out.append(x)
    return out
