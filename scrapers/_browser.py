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

import json
import os
import re
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


async def _route_block(route):
    if route.request.resource_type in _BLOCKED_RESOURCE_TYPES:
        return await route.abort()
    return await route.continue_()


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
    use_patchright = engine == "patchright" and _HAS_PATCHRIGHT
    runner = async_patchright if use_patchright else async_playwright
    async with runner() as p:
        kwargs = dict(
            user_data_dir=user_dir,
            headless=headless,
            viewport={"width": 1280, "height": 900},
        )
        if use_patchright:
            # Real Chrome avoids the bundled-Chromium fingerprint that
            # Turnstile flags. Pass minimal args so patchright's own patches
            # are not overridden.
            kwargs["channel"] = "chrome"
            kwargs["no_viewport"] = False
            kwargs["args"] = ["--no-default-browser-check"]
        else:
            kwargs["args"] = [
                "--disable-blink-features=AutomationControlled",
                "--no-default-browser-check",
            ]
        try:
            ctx = await p.chromium.launch_persistent_context(**kwargs)
        except Exception:
            # patchright with channel='chrome' fails if Chrome isn't installed.
            # Fall back to bundled Chromium so the scrape still runs (Turnstile
            # may still gate it but at least we get a clear error path).
            if use_patchright and "channel" in kwargs:
                kwargs.pop("channel", None)
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
        page = ctx.pages[0] if ctx.pages else await ctx.new_page()
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
    net_idle_timeout_ms: int = 6_000,
) -> None:
    """Wait for DOM + a short networkidle window, then a tail idle.

    `net_idle_timeout_ms` caps the networkidle wait separately from
    `timeout_ms`. Streaming SPAs (ChatGPT, Gemini) keep long-poll sockets open
    and never hit networkidle, so pinning it to the full 90s stalled every
    call. 6s is enough for the shell to settle and cheap when it doesn't."""
    await page.wait_for_load_state("domcontentloaded", timeout=timeout_ms)
    try:
        await page.wait_for_load_state("networkidle", timeout=net_idle_timeout_ms)
    except Exception:
        pass
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
