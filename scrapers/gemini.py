"""Gemini (anonymous gemini.google.com/app?hl=en-IN) with network interception.

In anonymous mode the Angular UI hides citation URLs inside component state,
so plain DOM scraping yields citation_count=0 even when the model is grounded.
The backend, however, returns full grounding metadata via the
`gemini.google.com/_/BardChatUi/data/...` batch endpoint. We attach a response
listener to that endpoint and run our URL-walker over every JSON/text payload.

We still scrape the DOM for the answer text and any anchors we can see, and
union that with the backend-extracted URLs.
"""
from __future__ import annotations

import os
import time
from pathlib import Path

from .base import ScrapeResult, DATA_DIR
from ._browser import (
    new_page,
    wait_quiet,
    make_response_capturer,
    dedupe,
)


RESP_SELECTORS = (
    ".model-response-text",
    "message-content .markdown",
    "message-content",
    "model-response .markdown",
    "model-response",
    ".markdown",
)
RESP_SEL = ", ".join(RESP_SELECTORS)

_SKIP_HOSTS = (
    "google.com", "googleusercontent.com", "gstatic.com", "googletagmanager.com",
    "doubleclick.net", "google-analytics.com", "youtube.com",
    "schema.org", "w3.org", "ogp.me", "gemini.google",
)


def _is_gemini_backend(url: str) -> bool:
    # All backend data flows through /_/BardChatUi/data/ — covers both
    # `batchexecute` (RPC batch) and the `assistant.lamda.BardFrontendService/StreamGenerate`
    # endpoint that streams the answer + grounding chunks.
    return (
        "/_/BardChatUi/data/" in url
        or "assistant.lamda.BardFrontendService" in url
    )


_GEMINI_TEXT_PROBE_JS = """() => {
    const sels = [
        'model-response message-content',
        'model-response .markdown',
        'model-response .response-content',
        'model-response',
        'message-content .markdown',
        'message-content',
        '.model-response-text',
        '.markdown',
        '[data-test-id*="response"]',
        '[class*="response-content"]',
        '[class*="model-response"]',
    ];
    let best = 0;
    const seen = new Set();
    for (const sel of sels) {
        let els;
        try { els = document.querySelectorAll(sel); }
        catch (e) { continue; }
        for (const el of els) {
            if (seen.has(el)) continue;
            seen.add(el);
            const t = (el.innerText || el.textContent || '').length;
            if (t > best) best = t;
        }
    }
    return best;
}"""


async def _wait_for_stable_gemini_text(
    page, *, idle_ms: int = 1200, max_ms: int = 75_000, min_chars: int = 60
) -> int:
    """Poll the text-heaviest candidate element until its length stops
    growing for `idle_ms`. Same selection logic as the eventual extractor,
    so the wait can't get stuck on an empty Angular shell."""
    deadline = time.monotonic() + max_ms / 1000
    last_len = -1
    last_change = time.monotonic()
    while time.monotonic() < deadline:
        try:
            cur = await page.evaluate(_GEMINI_TEXT_PROBE_JS) or 0
        except Exception:
            cur = last_len
        if cur != last_len:
            last_len = cur
            last_change = time.monotonic()
        elif cur >= min_chars and (time.monotonic() - last_change) * 1000 >= idle_ms:
            return cur
        await page.wait_for_timeout(250)
    return max(last_len, 0)


async def _collect_gemini_urls(page) -> list[str]:
    """Best-effort DOM hunt: anchors + every attribute value across the page,
    since Gemini hides citation hrefs in data-* attributes."""
    urls = await page.evaluate(
        """() => {
            const out = new Set();
            const URL_RE = /https?:\\/\\/[^\\s"'<>)]+/g;
            document.querySelectorAll('a').forEach(a => {
                if (a.href && /^https?:/.test(a.href)) out.add(a.href);
            });
            document.querySelectorAll('*').forEach(el => {
                for (const attr of el.attributes) {
                    const m = (attr.value || '').match(URL_RE);
                    if (m) m.forEach(u => out.add(u));
                }
            });
            const root = document.querySelector('model-response, message-content, .model-response-text');
            if (root) {
                const w = document.createTreeWalker(root, NodeFilter.SHOW_TEXT);
                let n;
                while ((n = w.nextNode())) {
                    const m = (n.nodeValue || '').match(URL_RE);
                    if (m) m.forEach(u => out.add(u));
                }
            }
            return Array.from(out);
        }"""
    )
    return urls or []


async def scrape(prompt: str, headless: bool = True) -> ScrapeResult:
    res = ScrapeResult(platform="gemini", prompt=prompt)
    try:
        async with new_page("gemini", headless=headless) as page:
            handler, captured_queries, captured_urls = make_response_capturer(
                _is_gemini_backend, skip_hosts=_SKIP_HOSTS,
                debug_platform="gemini", debug_host_match="gemini.google.com",
            )
            page.on("response", handler)

            await page.goto(
                "https://gemini.google.com/app?hl=en-IN",
                wait_until="domcontentloaded",
                timeout=60_000,
            )
            await wait_quiet(page, idle_ms=800)

            # Composer selector chain — Gemini renames the Angular components
            # every few weeks, and cold loads need up to ~15s for the shell to
            # hydrate. Try each selector in order and give the DOM enough time
            # to attach; only bail if all of them stay empty.
            _COMPOSER_SELECTORS = (
                "rich-textarea div[contenteditable='true']",
                "div.ql-editor[contenteditable='true']",
                "[role='textbox'][contenteditable='true']",
                "textarea",
            )
            composer = None
            for _sel in _COMPOSER_SELECTORS:
                try:
                    await page.wait_for_selector(_sel, timeout=15_000, state="visible")
                    loc = page.locator(_sel).first
                    if await loc.count():
                        composer = loc
                        break
                except Exception:
                    continue

            if composer is None:
                # Dump the page for diagnosis — most common cause is a consent
                # / sign-in interstitial covering the composer.
                try:
                    debug_dir = DATA_DIR / "debug"
                    debug_dir.mkdir(parents=True, exist_ok=True)
                    stamp = int(time.time())
                    (debug_dir / f"gemini_nocomposer_{stamp}.html").write_text(
                        await page.content(), encoding="utf-8"
                    )
                    await page.screenshot(
                        path=str(debug_dir / f"gemini_nocomposer_{stamp}.png"),
                        full_page=True,
                    )
                except Exception:
                    pass
                res.error = "No composer rendered."
                page.remove_listener("response", handler)
                return res

            await composer.click()
            await composer.fill(prompt)
            await page.keyboard.press("Enter")

            try:
                await page.wait_for_selector(RESP_SEL, timeout=45_000)
            except Exception:
                res.error = "Response container never appeared."
                page.remove_listener("response", handler)
                return res

            # Poll for response stability using the SAME text-heaviest scan
            # we use to extract — guarantees the wait correlates with what
            # we'll actually capture, instead of stalling on empty Angular
            # shells when the selector chain matches the wrong element.
            await _wait_for_stable_gemini_text(
                page, idle_ms=1200, max_ms=75_000, min_chars=60
            )

            # Open the Sources sidebar so inline carousels render their
            # URL-bearing data attributes. The 2.5s post-click wait was too
            # tight — the carousel needs ~3s+ to attach href attrs reliably.
            try:
                btn = page.locator(
                    "sources-list sources-sidebar-button button"
                ).first
                if await btn.count():
                    await btn.click(timeout=3000)
                    await page.wait_for_timeout(2500)
            except Exception:
                pass

            # Pick the assistant response from the DOM by SCANNING for the
            # text-heaviest element among plausible containers. Gemini renames
            # its Angular components often, so a fixed selector chain rots.
            # Picking the longest text block among candidates is robust across
            # those renames.
            res.response_text = (await page.evaluate(
                """() => {
                    const sels = [
                        'model-response message-content',
                        'model-response .markdown',
                        'model-response .response-content',
                        'model-response',
                        'message-content .markdown',
                        'message-content',
                        '.model-response-text',
                        '.markdown',
                        '[data-test-id*="response"]',
                        '[class*="response-content"]',
                        '[class*="model-response"]',
                    ];
                    let best = '';
                    const seen = new Set();
                    for (const sel of sels) {
                        let els;
                        try { els = document.querySelectorAll(sel); }
                        catch (e) { continue; }
                        for (const el of els) {
                            if (seen.has(el)) continue;
                            seen.add(el);
                            const t = (el.innerText || el.textContent || '').trim();
                            if (t.length > best.length) best = t;
                        }
                    }
                    return best;
                }"""
            ) or "")[:50000]

            # If the structured walk found nothing, fall back to the largest
            # text block anywhere in <main>, skipping the composer.
            if not res.response_text:
                fallback = await page.evaluate(
                    """() => {
                        const root = document.querySelector('main') || document.body;
                        if (!root) return '';
                        let best = '';
                        root.querySelectorAll('div, section, article').forEach(el => {
                            if (el.closest('rich-textarea')) return;
                            if (el.querySelector('textarea, [contenteditable="true"]')) return;
                            const t = (el.innerText || '').trim();
                            if (t.length > best.length && t.length < 60000) best = t;
                        });
                        return best;
                    }"""
                )
                if fallback:
                    res.response_text = fallback[:50000]

            # Debug: when SCRAPER_DEBUG_DOM=1 and we still got nothing, dump
            # the page HTML so the user can inspect what Gemini actually
            # rendered. Helps diagnose selector drift without re-running.
            if not res.response_text and os.environ.get("SCRAPER_DEBUG_DOM") == "1":
                try:
                    debug_dir = DATA_DIR / "debug"
                    debug_dir.mkdir(parents=True, exist_ok=True)
                    html = await page.content()
                    stamp = int(time.time())
                    path = debug_dir / f"gemini_empty_{stamp}.html"
                    Path(path).write_text(html, encoding="utf-8")
                    res.error = f"Empty response — DOM dumped to {path}"
                except Exception:
                    pass

            dom_urls = await _collect_gemini_urls(page)

            page.remove_listener("response", handler)

            # Filter DOM URLs against the skip list (the backend listener already
            # does this, so captured_urls is clean).
            dom_clean: list[str] = []
            for u in dom_urls:
                if not u or len(u) > 600:
                    continue
                host = u.split("/", 3)[2].lower() if "://" in u else ""
                if any(s in host or s in u for s in _SKIP_HOSTS):
                    continue
                dom_clean.append(u)

            res.citations = dedupe(dom_clean)
            res.source_urls = dedupe(captured_urls)
            res.fanout_queries = dedupe(captured_queries)

    except Exception as e:
        res.error = f"{type(e).__name__}: {e}"
    return res
