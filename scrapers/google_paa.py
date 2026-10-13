"""Google "People also ask" (PAA) scraper.

Separate feature from the AI Overview scraper (`google.py`) but built on the
same plumbing: one persistent headed Chrome (patchright + real Chrome) whose
single tab is reused for every keyword in a batch — each new keyword is typed
into the same search box, never URL-prefilled. `close_session()` tears the
browser down once the whole batch finishes (called from the PAA page's run
handler).

Extraction anchors on the `data-q` attribute Google stamps on each PAA
question pair (the question text itself — by far the most durable hook; class
names churn constantly). Each question is clicked open like a human would:
expanding reveals the answer snippet + source link AND makes Google append 2-3
follow-up questions, so the loop keeps clicking the next collapsed question
until `max_questions` answers are open or Google stops loading new ones.

Uses its own browser profile (`google_paa`) so it never fights the AIO
scraper for the same user-data dir if both features run in one sitting.
"""
from __future__ import annotations

import asyncio
import re
import time
from dataclasses import dataclass, field

from .base import DATA_DIR, profile_dir
# Shared SERP plumbing — consent wall, human-flow search typing, redirect
# unwrapping and google-chrome host filtering are identical for AIO and PAA.
from .google import _dismiss_consent, _run_search, _unwrap, _is_source

try:
    from patchright.async_api import async_playwright as async_patchright  # type: ignore
    _HAS_PATCHRIGHT = True
except Exception:  # pragma: no cover
    from playwright.async_api import async_playwright as async_patchright  # type: ignore
    _HAS_PATCHRIGHT = False


_DEBUG_DIR = DATA_DIR / "debug"

# One shared Chrome → serialize keywords so they don't type over each other.
_SCRAPE_LOCK = asyncio.Lock()

# Live browser session, reused across keywords (see module docstring).
_SESSION: dict | None = None

# A real PAA question pair carries data-q AND an aria-expanded toggle button.
# Anchor on the toggle, not bare [data-q]: Google also stamps data-q equal to
# the search query on a non-PAA element, which renders immediately. Keying
# detection on [data-q] alone matches that stray element at t≈0, so we'd report
# "PAA present" before scrolling — and the real PAA module lazy-loads on scroll,
# so it would never appear and we'd extract the stray element as a fake question.
_PAA_TOGGLE = "[data-q] [role='button'][aria-expanded]"
_COLLAPSED_BTN = "[data-q] [role='button'][aria-expanded='false']"

# Walks every PAA pair and returns question + (if expanded) answer + links.
# Prefers pairs inside the [data-initq] PAA module; falls back to any data-q.
_EXTRACT_JS = r"""
() => {
  const norm = s => (s || '').replace(/\s+/g, ' ').trim();
  let pairs = Array.from(document.querySelectorAll('[data-initq] [data-q]'));
  if (!pairs.length)
    pairs = Array.from(document.querySelectorAll('div.related-question-pair, div[data-q]'));
  const items = [];
  const seen = new Set();
  for (const pair of pairs) {
    const btn = pair.querySelector('[role="button"][aria-expanded]');
    if (!btn) continue;  // real PAA questions always have an expand toggle
    let q = norm(pair.getAttribute('data-q') || '');
    if (!q) q = norm((btn.innerText || '').split('\n')[0]);
    if (!q || seen.has(q.toLowerCase())) continue;
    seen.add(q.toLowerCase());
    const expanded = !!(btn && btn.getAttribute('aria-expanded') === 'true');
    // The answer region: aria-controls points at it; else fall back to the
    // whole pair (the question line is stripped out on the Python side).
    let root = null;
    const rid = btn && btn.getAttribute('aria-controls');
    if (rid) root = document.getElementById(rid);
    if (!root) root = pair;
    let answer = '', links = [];
    if (expanded) {
      // When a mini AI Overview renders inside the expansion, its sources
      // carousel (an ARIA list of title/snippet/publisher cards) and the
      // inline "View related links" publisher chips would all leak into
      // innerText. Hide them first — innerText skips display:none, but
      // querySelectorAll below still sees them, so link extraction keeps
      // working. The page is re-searched per keyword, so mutating is safe.
      for (const el of root.querySelectorAll('[role="list"], [aria-label*="View related links"]')) {
        if (el.getAttribute('role') !== 'list' || el.querySelector('a[href]'))
          el.style.display = 'none';
      }
      answer = (root.innerText || '').trim();
      links = Array.from(root.querySelectorAll('a[href]'))
        .filter(a => /^https?:/i.test(a.href))
        .map(a => {
          // Snippet source links wrap a heading (the page title) plus
          // publisher + breadcrumb chrome — prefer just the heading.
          const h = a.querySelector('[role="heading"], h3');
          return { href: a.href, text: norm((h ? h.innerText : a.innerText) || '') };
        });
    }
    items.push({ question: q, answer, links, expanded });
  }
  return items;
}
"""


# Bottom-of-SERP "People also search for" / "Related searches" block. Best-effort:
# Google only renders it for some queries and intermittently. Anchor on the
# label text (class names churn), then climb to the container that holds the
# related-query links and return their text. Footer nav (tabs, tools, paging)
# lives in a different container, so the climb naturally excludes it; a small
# junk set guards the rest.
_PASF_JS = r"""
() => {
  const norm = s => (s || '').replace(/\s+/g, ' ').trim();
  const isLabel = t => {
    t = t.toLowerCase();
    return t === 'people also search for' || t === 'related searches'
        || t.startsWith('searches related to');
  };
  let label = null;
  for (const el of document.querySelectorAll('div,span,h2,h3,b,strong')) {
    const t = norm(el.textContent);
    if (t.length < 60 && el.children.length <= 2 && isLabel(t)) { label = el; break; }
  }
  if (!label) return [];
  const junk = new Set(['ai mode','short videos','view all','see more','next',
    'previous','images','videos','news','forums','shopping','web','books','maps',
    'more results','past hour','past 24 hours','past week','past month','past year',
    'verbatim']);
  let c = label;
  for (let i = 0; i < 8 && c; i++) {
    const links = Array.from(c.querySelectorAll('a[href*="/search?"]'))
      .map(a => norm(a.innerText))
      .filter(t => t && !isLabel(t) && !junk.has(t.toLowerCase())
                   && !/^\d+$/.test(t) && !t.toLowerCase().startsWith('more results'));
    if (links.length >= 2) {
      const seen = new Set(), out = [];
      for (const t of links) {
        const k = t.toLowerCase();
        if (!seen.has(k)) { seen.add(k); out.push(t); }
      }
      return out.slice(0, 40);
    }
    c = c.parentElement;
  }
  return [];
}
"""


# Lightweight per-pair snapshot (question text + expanded flag), same pair
# selection + ordering as _EXTRACT_JS. Used to diff the question set before vs
# after each click so newly-appended follow-ups can be attributed to the
# question that triggered them (Google appends them at the module bottom, not
# nested in the DOM, so there's no structural parent link to read).
_SNAPSHOT_JS = r"""
() => {
  const norm = s => (s || '').replace(/\s+/g, ' ').trim();
  let pairs = Array.from(document.querySelectorAll('[data-initq] [data-q]'));
  if (!pairs.length)
    pairs = Array.from(document.querySelectorAll('div.related-question-pair, div[data-q]'));
  const out = [];
  const seen = new Set();
  for (const pair of pairs) {
    const btn = pair.querySelector('[role="button"][aria-expanded]');
    if (!btn) continue;
    let q = norm(pair.getAttribute('data-q') || '');
    if (!q) q = norm((btn.innerText || '').split('\n')[0]);
    if (!q || seen.has(q.toLowerCase())) continue;
    seen.add(q.toLowerCase());
    out.push({ q, expanded: btn.getAttribute('aria-expanded') === 'true' });
  }
  return out;
}
"""


@dataclass
class PaaResult:
    keyword: str
    # [{position, question, answer, source_title, source_url}]
    items: list = field(default_factory=list)
    # "People also search for" related-query strings (best-effort; often empty).
    related: list = field(default_factory=list)
    error: str = ""

    def to_rows(self) -> list[dict]:
        """Flatten to storage/export rows. PAA questions carry kind='question';
        "People also search for" terms carry kind='related'. A keyword with no
        questions and no related terms still yields one row so its error (or the
        "nothing found" state) stays visible in the UI."""
        rows: list[dict] = []
        if self.items:
            rows.extend({
                "keyword": self.keyword,
                "position": it["position"],
                "question": it["question"],
                "answer": it["answer"],
                "source_title": it["source_title"],
                "source_url": it["source_url"],
                "error": "",
                "kind": "question",
                "parent": it.get("parent", ""),
            } for it in self.items)
        elif not self.related:
            rows.append({
                "keyword": self.keyword, "position": 0, "question": "",
                "answer": "", "source_title": "", "source_url": "",
                "error": self.error, "kind": "question", "parent": "",
            })
        rows.extend({
            "keyword": self.keyword, "position": i, "question": term,
            "answer": "", "source_title": "", "source_url": "",
            "error": "", "kind": "related", "parent": "",
        } for i, term in enumerate(self.related, start=1))
        return rows


async def _dump_failure(page, tag: str) -> None:
    try:
        _DEBUG_DIR.mkdir(parents=True, exist_ok=True)
        stamp = time.strftime("%Y%m%d_%H%M%S")
        base = _DEBUG_DIR / f"paa_fail_{tag}_{stamp}"
        try:
            await page.screenshot(path=str(base.with_suffix(".png")), full_page=True)
        except Exception:
            pass
        try:
            base.with_suffix(".html").write_text(await page.content(), encoding="utf-8")
        except Exception:
            pass
    except Exception:
        pass


async def _ensure_session() -> dict:
    """Return a live session for the current loop, launching one if needed.
    Mirrors scrapers.google._ensure_session but with its own profile dir."""
    global _SESSION
    loop = asyncio.get_running_loop()

    if _SESSION is not None:
        same_loop = _SESSION.get("loop") is loop
        page = _SESSION.get("page")
        try:
            alive = same_loop and page is not None and not page.is_closed()
        except Exception:
            alive = False
        if alive:
            return _SESSION
        await close_session()

    user_dir = str(profile_dir("google_paa"))
    pw = await async_patchright().start()
    kwargs = dict(
        user_data_dir=user_dir,
        headless=False,  # SERP bot-detection: always headed, like the AIO scraper.
        viewport={"width": 1280, "height": 900},
        args=["--no-default-browser-check"],
        # Playwright defaults this to False, which injects --no-sandbox --
        # a visible infobar and a cheap automation tell. Set in every
        # module that launches its own context; see scrapers/chatgpt.py.
        chromium_sandbox=True,
    )
    if _HAS_PATCHRIGHT:
        kwargs["channel"] = "chrome"
        kwargs["no_viewport"] = False
    try:
        ctx = await pw.chromium.launch_persistent_context(**kwargs)
    except Exception:
        kwargs.pop("channel", None)
        ctx = await pw.chromium.launch_persistent_context(**kwargs)

    page = ctx.pages[0] if ctx.pages else await ctx.new_page()
    try:
        await page.goto("https://www.google.com/?hl=en", wait_until="domcontentloaded", timeout=60_000)
    except Exception:
        pass
    await _dismiss_consent(page)

    _SESSION = {"pw": pw, "ctx": ctx, "page": page, "loop": loop}
    return _SESSION


async def close_session() -> None:
    """Close the shared Chrome. Called once at the end of a PAA batch."""
    global _SESSION
    sess = _SESSION
    _SESSION = None
    if not sess:
        return
    try:
        await sess["ctx"].close()
    except Exception:
        pass
    try:
        await sess["pw"].stop()
    except Exception:
        pass


async def _await_paa(page, *, max_ms: int = 14_000) -> bool:
    """Wait for the PAA module to render. It can lazy-load, so nudge the page
    with a small scroll if nothing showed after the first few seconds."""
    deadline = time.monotonic() + max_ms / 1000
    nudge_at = time.monotonic() + 4
    while time.monotonic() < deadline:
        try:
            if await page.locator(_PAA_TOGGLE).count():
                return True
        except Exception:
            pass
        if time.monotonic() >= nudge_at:
            try:
                await page.mouse.wheel(0, 900)
            except Exception:
                pass
            nudge_at = time.monotonic() + 2
        await page.wait_for_timeout(450)
    return False


async def _expand_paa(page, max_questions: int) -> dict:
    """Click collapsed questions in DOM order until `max_questions` are open or
    Google stops appending follow-ups. Each click reveals an answer and loads
    2-3 new questions at the bottom of the module.

    Returns a `child_question_lower -> parent_question_text` map: before each
    click we snapshot the question set, after it we diff — any question that
    just appeared is attributed to the question we clicked. Questions present
    before any click (the initial module) are roots and never get a parent.
    """
    parent_map: dict[str, str] = {}
    seen: set[str] = set()
    last_parent = ""  # question expanded in the previous iteration
    expanded = 0
    stale = 0

    def _attribute(snap, parent):
        # Any question we haven't seen is a follow-up of `parent` (the click
        # that surfaced it). With parent="" — the initial module — they're roots.
        for s in snap:
            ql = s["q"].lower()
            if ql not in seen:
                seen.add(ql)
                if parent and ql != parent.lower():
                    parent_map[ql] = parent

    while expanded < max_questions and stale < 2:
        try:
            snap = await page.evaluate(_SNAPSHOT_JS) or []
        except Exception:
            snap = []
        # Late-rendering follow-ups from the previous click land here — credit
        # them to that previous parent before moving on.
        _attribute(snap, last_parent)

        # The question we're about to click = first collapsed pair in DOM order,
        # which is what _COLLAPSED_BTN.first resolves to (same ordering).
        parent_q = next((s["q"] for s in snap if not s["expanded"]), "")

        loc = page.locator(_COLLAPSED_BTN)
        try:
            if not await loc.count():
                # No collapsed question right now — follow-ups may still be
                # loading from the last click; give them a beat, then give up.
                stale += 1
                await page.wait_for_timeout(800)
                continue
            btn = loc.first
            await btn.scroll_into_view_if_needed(timeout=2500)
            await btn.click(timeout=3000)
        except Exception:
            stale += 1
            await page.wait_for_timeout(600)
            continue
        stale = 0
        expanded += 1
        last_parent = parent_q
        # Let the answer render + follow-up questions append.
        await page.wait_for_timeout(900)

        # Fast-rendering follow-ups: credit them to the question just expanded.
        try:
            after = await page.evaluate(_SNAPSHOT_JS) or []
        except Exception:
            after = []
        _attribute(after, parent_q)

    return parent_map


def _squash(s: str) -> str:
    """Lowercase alnum-only — lets 'Urban Money' match host 'urbanmoney.com'."""
    return re.sub(r"[^a-z0-9]", "", (s or "").lower())


def _clean_answer(question: str, raw: str, source_title: str, source_url: str) -> str:
    """The answer region's innerText includes the question line, source
    title/publisher/breadcrumb chrome, and — when Google renders a mini AI
    Overview inside the expansion — an 'AI Overview' label, '+N' source-chip
    counters and a 'responses may include mistakes' footer. Strip all of that,
    keep the snippet itself."""
    host = ""
    if "://" in source_url:
        try:
            host = source_url.split("/", 3)[2].lower()
        except Exception:
            pass
    junk = {
        question.strip().lower(), "feedback", "search for:", "more results",
        "show more", "show less", "ai overview", "related links",
        source_title.strip().lower(), host, ("www." + host) if host else "",
    }
    lines: list[str] = []
    for ln in (raw or "").splitlines():
        s = ln.strip()
        low = s.lower()
        if not s or low in junk:
            continue
        # AIO-in-PAA footer — everything after it is disclaimers/controls.
        if low.startswith(("ai responses may include mistakes",
                           "generative ai is experimental")):
            break
        # '+N' source-chip counters and URL breadcrumbs ('site.com › path').
        if re.fullmatch(r"\+\d+", s) or (("›" in s) and low.startswith(("http", "www."))):
            continue
        lines.append(s)
    # Trailing source chrome: the page title and publisher name render as the
    # last lines of the region. Pop anything that matches the picked source.
    title_sq = _squash(source_title)
    host_sq = _squash(host.removeprefix("www.").split(".")[0]) if host else ""
    for _ in range(4):
        if not lines or len(lines[-1]) > 160:
            break
        sq = _squash(lines[-1])
        if sq and (sq == host_sq or (title_sq and (sq == title_sq or sq in title_sq))):
            lines.pop()
        else:
            break
    return "\n".join(lines).strip()[:5000]


def _pick_source(links: list[dict]) -> tuple[str, str]:
    """First offsite link in the answer region = the cited source."""
    for l in links or []:
        u = _unwrap(l.get("href") or "")
        if _is_source(u):
            return u, (l.get("text") or "").strip()
    return "", ""


async def scrape_paa(keyword: str, max_questions: int = 10) -> PaaResult:
    """Search `keyword`, expand the People-also-ask module and return up to
    `max_questions` question/answer/source triples."""
    res = PaaResult(keyword=keyword)
    async with _SCRAPE_LOCK:
        try:
            sess = await _ensure_session()
            page = sess["page"]
            try:
                await _run_search(page, keyword)
            except Exception as e:
                await _dump_failure(page, "search")
                res.error = f"search failed: {type(e).__name__}: {e}"
                return res

            await _dismiss_consent(page)

            if "/sorry/" in page.url:
                await _dump_failure(page, "captcha")
                res.error = "Google CAPTCHA wall — solve it in the open window, then re-run."
                return res

            if await _await_paa(page):
                parent_map = await _expand_paa(page, max_questions)
                # Settle: last answer + appended follow-ups finish rendering.
                await page.wait_for_timeout(800)

                # Include every question surfaced — the ones we expanded (with
                # answers) plus the follow-ups they appended (nested under their
                # parent, often without answers). Capped to avoid runaway DOMs.
                raw_items = await page.evaluate(_EXTRACT_JS) or []
                for i, it in enumerate(raw_items[:200], start=1):
                    src_url, src_title = _pick_source(it.get("links") or [])
                    res.items.append({
                        "position": i,
                        "question": it["question"],
                        "answer": _clean_answer(it["question"], it.get("answer") or "",
                                                src_title, src_url),
                        "source_title": src_title,
                        "source_url": src_url,
                        "parent": parent_map.get(it["question"].lower(), ""),
                    })

            # "People also search for" — scroll to the page bottom so it
            # lazy-loads, then grab it if present (best-effort; many queries
            # never render this block). Failures here never sink the PAA result.
            try:
                for _ in range(6):
                    await page.mouse.wheel(0, 1600)
                    await page.wait_for_timeout(300)
                res.related = await page.evaluate(_PASF_JS) or []
            except Exception:
                pass

            if not res.items and not res.related:
                res.error = "No People Also Ask or related searches for this query."
        except Exception as e:
            res.error = f"{type(e).__name__}: {e}"
    return res
