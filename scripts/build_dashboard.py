"""Build the standalone "AI Citation Tracker" HTML dashboard from live data.

The dashboard at ``dashboard/ai-citation-tracker.html`` is a self-contained,
compiled single-file web app (a React/Vite bundle). It renders entirely from a
single global ``window.CK_DATA`` object that lives in one of the bundled assets.

This script regenerates that ``CK_DATA`` object from ``data/results.db`` (the
same SQLite store the Streamlit ``app.py`` dashboard reads) and re-injects it
into the bundle, so the standalone HTML reflects your real scrape results
instead of the demo dataset it shipped with.

Re-run it whenever you've scraped new data:

    python scripts/build_dashboard.py                 # rebuild in place, latest run
    python scripts/build_dashboard.py --ts 1780294123 # pick a specific run (ts)
    python scripts/build_dashboard.py --list          # list available runs and exit

It rewrites the file in place (same UUID asset is overwritten each time), so the
build is idempotent — running it twice produces the same file for the same data.
"""
from __future__ import annotations

import argparse
import base64
import gzip
import json
import re
import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

# Reuse the exact parsing the Streamlit dashboard uses, so both views agree.
from core.storage import list_runs, load_run                      # noqa: E402
from core.settings import get_tracked_terms                        # noqa: E402
from core.aggregator import domain_of, _to_list, _SKIP_DOMAINS     # noqa: E402

DASHBOARD = ROOT / "dashboard" / "ai-citation-tracker.html"

# ── Platform presentation (id -> name/short/color + default capabilities) ──
PLATFORM_LABEL = {
    "chatgpt": "ChatGPT",
    "gemini": "Gemini",
    "perplexity": "Perplexity",
    "google": "Google AIO",
    "claude": "Claude",
}
PLATFORM_META = {
    # id:          (color,      short, default citations, default fanout)
    "chatgpt":     ("#10A37F", "GPT", True,  True),
    "gemini":      ("#4385F5", "GEM", True,  True),
    "perplexity":  ("#20808D", "PPX", False, False),
    "google":      ("#EA4335", "AIO", True,  False),
    "claude":      ("#D97757", "CLA", True,  False),
}
PLATFORM_ORDER = ["chatgpt", "gemini", "perplexity", "google", "claude"]


def _fmt_label(ts: float) -> str:
    dt = datetime.fromtimestamp(ts)
    day = str(int(dt.strftime("%d")))  # strip leading zero, cross-platform
    return dt.strftime(f"%b {day}, %Y · %H:%M")


def _build_tracked(terms: list[str]):
    """Map the user's tracked terms (settings.json) onto the dashboard's
    TRACKED/YOURS_DOMAINS shape. A term containing a dot and no space is treated
    as a domain; everything else is a brand/keyword string."""
    tracked, yours = [], []
    seen = set()
    for raw in terms:
        t = raw.strip()
        if not t or t.lower() in seen:
            continue
        seen.add(t.lower())
        is_domain = ("." in t) and (" " not in t)
        if is_domain:
            dom = domain_of("http://" + t) or t.lower()
            label = dom.split(".")[0].replace("-", " ").title()
            if dom not in yours:
                yours.append(dom)
            tracked.append({"term": t.lower(), "label": label, "domain": dom})
        else:
            tracked.append({"term": t.lower(), "label": t, "domain": ""})
    return tracked, yours


def _classify(row: dict, terms_lc: list[str]):
    """Mirror of app.py's row_citation_position, but also returns which tracked
    term matched and the cleaned citation list. Returns a cell dict."""
    text = (row.get("response_text") or "").lower()
    combined, seen = [], set()
    for u in _to_list(row.get("citations")) + _to_list(row.get("source_urls")):
        if u and u not in seen:
            seen.add(u)
            combined.append(u)

    # Cleaned, display-ready citation list (drop empties + assistant self-refs).
    citations = []
    for u in combined:
        d = domain_of(u)
        if not d or d in _SKIP_DOMAINS:
            continue
        citations.append({
            "domain": d,
            "url": u,
            "yours": any(t in u.lower() for t in terms_lc),
        })

    fanout = [q.strip() for q in _to_list(row.get("fanout_queries"))
              if isinstance(q, str) and q.strip()]

    # Error cell: only when nothing useful came back.
    err = (row.get("error") or "").strip()
    if err and not text and not citations:
        return {"status": "error", "error": err, "citedTerm": None, "via": None,
                "position": None, "citations": [], "responseText": "", "fanout": fanout}

    # Position of the first cited URL that contains a tracked term (within the
    # displayed list, so #position lines up with what the user sees).
    url_term, url_pos = None, None
    for i, c in enumerate(citations, start=1):
        ul = c["url"].lower()
        hit = next((t for t in terms_lc if t and t in ul), None)
        if hit:
            url_term, url_pos = hit, i
            break

    text_term = next((t for t in terms_lc if t and t in text), None)

    if url_term:
        via = "both" if text_term else "url"
        cited_term, position = url_term, url_pos
        status = "cited"
    elif text_term:
        via, cited_term, position, status = "text", text_term, None, "cited"
    else:
        via, cited_term, position, status = None, None, None, "missing"

    return {
        "status": status,
        "citedTerm": cited_term,
        "via": via,
        "position": position,
        "citations": citations,
        "responseText": row.get("response_text") or "",
        "fanout": fanout,
    }


def _citation_rate(rows: list[dict], terms_lc: list[str]) -> float:
    """cited cells / ran cells, for the trend metric."""
    ran = cited = 0
    for r in rows:
        cell = _classify(r, terms_lc)
        if cell["status"] == "not-run":
            continue
        ran += 1
        if cell["status"] == "cited":
            cited += 1
    return (cited / ran) if ran else 0.0


def _build_cells(rows: list[dict], keywords: list[str], plat_ids: list[str], terms_lc: list[str]) -> dict:
    """cells dict keyed '{ki}_{pi}' over `keywords` (ki) and the GLOBAL platform
    order `plat_ids` (pi). Missing (keyword, platform) pairs -> not-run."""
    by_key = {(r["prompt"], r["platform"]): r for r in rows}
    cells: dict[str, dict] = {}
    for ki, kw in enumerate(keywords):
        for pi, pid in enumerate(plat_ids):
            row = by_key.get((kw, pid))
            cells[f"{ki}_{pi}"] = _classify(row, terms_lc) if row is not None else {"status": "not-run"}
    return cells


def _rate_from_cells(cells: dict) -> float:
    """cited / ran over a prebuilt cells dict (for the combined-view trend)."""
    ran = cited = 0
    for c in cells.values():
        if c.get("status") == "not-run":
            continue
        ran += 1
        if c.get("status") == "cited":
            cited += 1
    return (cited / ran) if ran else 0.0


def build_ck_data(active_ts: float | None, terms_override: list[str] | None = None) -> dict:
    runs_meta = list_runs(limit=200)
    if not runs_meta:
        raise SystemExit("No runs found in data/results.db — scrape something first.")

    # Meaningful runs only (skip 1-keyword smoke tests) for both the active
    # selection and the history list.
    meaningful = [r for r in runs_meta if r["prompt_count"] >= 2]
    pool = meaningful or runs_meta

    if active_ts is None:
        active = pool[0]                       # newest meaningful run
    else:
        active = next((r for r in runs_meta if abs(r["ts"] - active_ts) < 1e-6), None)
        if active is None:
            raise SystemExit(f"No run found with ts={active_ts}. Use --list to see options.")

    terms = terms_override if terms_override is not None else get_tracked_terms()
    terms_lc = [t.lower() for t in terms if t.strip()]
    tracked, yours = _build_tracked(terms)

    # Runs shown in the picker (newest first). Ensure the active run is one.
    history = pool[:15]
    if not any(abs(r["ts"] - active["ts"]) < 1e-6 for r in history):
        history = ([active] + history)[:15]

    # Load every history run once; derive the GLOBAL platform union so the
    # citation matrix keeps a stable set of columns no matter which run is
    # selected (a platform a run didn't use just shows as not-run).
    loaded = {r["ts"]: load_run(r["ts"]) for r in history}
    plat_set = {row["platform"] for rows in loaded.values() for row in rows}
    plat_ids = [p for p in PLATFORM_ORDER if p in plat_set]
    plat_ids += [p for p in sorted(plat_set) if p not in plat_ids]

    # Per-run keyword lists + cells (cells keyed by global platform index).
    per_kw, per_cells = {}, {}
    plat_has_url = {p: False for p in plat_ids}
    plat_has_fanout = {p: False for p in plat_ids}
    for r in history:
        kws = sorted({row["prompt"] for row in loaded[r["ts"]]})
        cells = _build_cells(loaded[r["ts"]], kws, plat_ids, terms_lc)
        for key, cell in cells.items():
            pid = plat_ids[int(key.split("_")[1])]
            if cell.get("citations"):
                plat_has_url[pid] = True
            if cell.get("fanout"):
                plat_has_fanout[pid] = True
        per_kw[r["ts"]], per_cells[r["ts"]] = kws, cells

    platforms = []
    for pid in plat_ids:
        color, short, def_cit, def_fan = PLATFORM_META.get(pid, ("#6B7280", pid[:3].upper(), True, False))
        platforms.append({
            "id": pid,
            "name": PLATFORM_LABEL.get(pid, pid.title()),
            "short": short,
            "color": color,
            "caps": {
                "citations": bool(def_cit or plat_has_url[pid]),
                "text": True,
                "fanout": bool(def_fan or plat_has_fanout[pid]),
            },
        })

    # Per-run trend baseline: citation rate of the next-older run in pool.
    pool_idx = {r["ts"]: i for i, r in enumerate(pool)}
    rate_cache: dict[float, float] = {}

    def _rate_of(ts: float) -> float:
        if ts not in rate_cache:
            rate_cache[ts] = _citation_rate(loaded.get(ts) or load_run(ts), terms_lc)
        return rate_cache[ts]

    def _prev_rate(r: dict) -> float:
        i = pool_idx.get(r["ts"])
        if i is not None and i + 1 < len(pool):
            return _rate_of(pool[i + 1]["ts"])
        return _rate_of(r["ts"])  # no older run -> trend 0

    # Run history + per-run datasets (keyed by the same r1..rN ids).
    runs, runs_data = [], {}
    for i, r in enumerate(history, start=1):
        rid = f"r{i}"
        names = [PLATFORM_LABEL.get(p, p) for p in PLATFORM_ORDER
                 if p in (r["platforms"] or "").split(",")]
        runs.append({
            "id": rid,
            "date": datetime.fromtimestamp(r["ts"]).strftime("%Y-%m-%d"),
            "label": _fmt_label(r["ts"]),
            "kw": r["prompt_count"],
            "calls": r["row_count"],
            "platforms": names,
            "current": abs(r["ts"] - active["ts"]) < 1e-6,
        })
        kws = per_kw[r["ts"]]
        runs_data[rid] = {
            "keywords": kws,
            "KW": [[k] for k in kws],
            "cells": per_cells[r["ts"]],
            "PREV_CITATION_RATE": round(_prev_rate(r), 4),
        }
    if not any(x["current"] for x in runs) and runs:
        runs[0]["current"] = True

    # "All runs combined": union of keywords, latest non-empty cell per kw×plat.
    all_kws = sorted({k for kws in per_kw.values() for k in kws})
    all_cells: dict[str, dict] = {}
    for ki, kw in enumerate(all_kws):
        for pi in range(len(plat_ids)):
            chosen = {"status": "not-run"}
            for r in history:  # newest first
                kws = per_kw[r["ts"]]
                if kw in kws:
                    c = per_cells[r["ts"]].get(f"{kws.index(kw)}_{pi}")
                    if c and c.get("status") not in (None, "not-run"):
                        chosen = c
                        break
            all_cells[f"{ki}_{pi}"] = chosen
    runs_data["all"] = {
        "keywords": all_kws,
        "KW": [[k] for k in all_kws],
        "cells": all_cells,
        "PREV_CITATION_RATE": round(_rate_from_cells(all_cells), 4),
    }

    active_id = next((x["id"] for x in runs if x["current"]), runs[0]["id"] if runs else "r1")
    act = runs_data[active_id]

    return {
        "PLATFORMS": platforms,
        "TRACKED": tracked,
        "YOURS_DOMAINS": yours,
        "COMPETITORS": [],
        "AUTHORITY": [],
        "keywords": act["keywords"],
        "KW": act["KW"],            # selectors use KW for length/index
        "cells": act["cells"],
        "RUNS": runs,
        "RUNS_DATA": runs_data,     # per-run datasets the picker switches between
        "PREV_CITATION_RATE": act["PREV_CITATION_RATE"],
    }


def _data_module_js(ck: dict) -> str:
    payload = json.dumps(ck, ensure_ascii=False)
    return (
        "(function(){\n"
        f"  var D = {payload};\n"
        "  D.domainForTerm = function(term){\n"
        "    var t = D.TRACKED.find(function(x){return x.term === term;});\n"
        "    return t ? t.domain : '';\n"
        "  };\n"
        "  window.CK_DATA = D;\n"
        "})();\n"
    )


def _inject(html: str, data_js: str) -> str:
    open_tag = '<script type="__bundler/manifest">'
    i = html.index(open_tag) + len(open_tag)
    j = html.index("</script>", i)
    manifest = json.loads(html[i:j])

    # Find the data-module asset: the one whose source assigns window.CK_DATA.
    target = None
    for uuid, entry in manifest.items():
        if "javascript" not in entry.get("mime", ""):
            continue
        raw = base64.b64decode(entry["data"])
        if entry.get("compressed"):
            try:
                raw = gzip.decompress(raw)
            except OSError:
                continue
        if re.search(rb"window\.CK_DATA\s*=", raw):
            target = uuid
            break
    if target is None:
        raise SystemExit("Could not locate the CK_DATA module in the bundle.")

    comp = gzip.compress(data_js.encode("utf-8"), mtime=0)  # mtime=0 -> reproducible
    manifest[target]["data"] = base64.b64encode(comp).decode("ascii")
    manifest[target]["compressed"] = True
    manifest[target].setdefault("mime", "application/javascript")

    new_manifest = json.dumps(manifest, ensure_ascii=False)
    if "</script>" in new_manifest:
        raise SystemExit("Refusing to write: regenerated manifest contains </script>.")
    html = html[:i] + new_manifest + html[j:]

    # Cosmetic: default the run picker to the active (current) run, not the
    # bundle's hard-coded "r5". The template is JSON-encoded, so quotes escape.
    html = html.replace(
        r'useState(\"r5\")',
        r'useState((window.CK_DATA.RUNS.find(function(r){return r.current;})||window.CK_DATA.RUNS[0]||{}).id)',
        1,
    )
    return html


def _patch_selectors(html: str) -> tuple[str, bool]:
    """Make the SEL module's data bindings reassignable and add
    ``window.SEL.activateRun(runId)`` so the run picker can swap datasets.
    Idempotent. Returns (html, changed)."""
    open_tag = '<script type="__bundler/manifest">'
    i = html.index(open_tag) + len(open_tag)
    j = html.index("</script>", i)
    manifest = json.loads(html[i:j])

    target, text = None, None
    for uuid, entry in manifest.items():
        if "javascript" not in entry.get("mime", ""):
            continue
        raw = base64.b64decode(entry["data"])
        if entry.get("compressed"):
            try:
                raw = gzip.decompress(raw)
            except OSError:
                continue
        t = raw.decode("utf-8", "replace")
        if "window.SEL = {" in t:
            target, text = uuid, t
            break
    if target is None:
        raise SystemExit("Could not locate the SEL (selectors) module in the bundle.")
    if "function activateRun(" in text:
        return html, False  # already patched

    destr_old = "const { PLATFORMS, keywords, KW, cells, TRACKED, YOURS_DOMAINS } = D;"
    destr_new = "let { PLATFORMS, keywords, KW, cells, TRACKED, YOURS_DOMAINS } = D;"
    sel_anchor = "  window.SEL = {\n    platIndex,"
    if destr_old not in text or sel_anchor not in text:
        raise SystemExit("SEL patch failed: expected source not found (bundle changed).")

    text = text.replace(destr_old, destr_new, 1)
    activate = (
        "  function activateRun(runId){\n"
        "    var r = D.RUNS_DATA && D.RUNS_DATA[runId];\n"
        "    if(!r) return false;\n"
        "    cells = r.cells; keywords = r.keywords;\n"
        "    KW = r.KW || keywords.map(function(k){return [k];});\n"
        "    D.cells = cells; D.keywords = keywords; D.KW = KW;\n"
        "    if(r.PREV_CITATION_RATE != null) D.PREV_CITATION_RATE = r.PREV_CITATION_RATE;\n"
        "    return true;\n"
        "  }\n"
        "  window.SEL = {\n    activateRun, platIndex,"
    )
    text = text.replace(sel_anchor, activate, 1)

    comp = gzip.compress(text.encode("utf-8"), mtime=0)
    manifest[target]["data"] = base64.b64encode(comp).decode("ascii")
    manifest[target]["compressed"] = True
    new_manifest = json.dumps(manifest, ensure_ascii=False)
    if "</script>" in new_manifest:
        raise SystemExit("Refusing to write: regenerated manifest contains </script>.")
    return html[:i] + new_manifest + html[j:], True


def _patch_app_root(html: str) -> tuple[str, bool]:
    """Wire the App root (inline babel in the template): activate the selected
    run before any selector runs, and remount views when the run changes (the
    view selectors are memoized with []). Idempotent. Returns (html, changed)."""
    changed = False
    call_anchor = "const [scanning, setScanning] = useState(false);"
    if "window.SEL.activateRun(run)" not in html:
        if call_anchor not in html:
            raise SystemExit("App-root patch failed: scanning-state line not found.")
        html = html.replace(
            call_anchor,
            call_anchor + " if (window.SEL.activateRun) window.SEL.activateRun(run);",
            1,
        )
        changed = True
    # key={run} forces the content subtree to remount on run change. The
    # template is JSON-encoded, so the JSX quotes appear escaped (\").
    content_old = '<div className=\\"content\\">'
    content_new = '<div className=\\"content\\" key={run}>'
    if content_new not in html and content_old in html:
        html = html.replace(content_old, content_new, 1)
        changed = True
    return html, changed


def _patch_sources_expand(html: str) -> tuple[str, bool]:
    """Make the Source Leaderboard's '+N' keyword chip expand/collapse the full
    keyword list (it was a static, non-clickable span). Idempotent.
    Returns (html, changed)."""
    open_tag = '<script type="__bundler/manifest">'
    i = html.index(open_tag) + len(open_tag)
    j = html.index("</script>", i)
    manifest = json.loads(html[i:j])

    target, text = None, None
    for uuid, entry in manifest.items():
        if "javascript" not in entry.get("mime", ""):
            continue
        raw = base64.b64decode(entry["data"])
        if entry.get("compressed"):
            try:
                raw = gzip.decompress(raw)
            except OSError:
                continue
        t = raw.decode("utf-8", "replace")
        if "function SourcesView()" in t:
            target, text = uuid, t
            break
    if target is None:
        raise SystemExit("Could not locate SourcesView in the bundle.")
    if "setExpanded" in text:
        return html, False  # already patched

    state_old = '  const [q, setQ] = useState("");'
    chips_old = '{top3.map((k) => <span key={k} className="chip chip-mini">{k}</span>)}'
    more_old = '{more > 0 && <span className="chip blue chip-mini">+{more}</span>}'
    if state_old not in text or chips_old not in text or more_old not in text:
        raise SystemExit("Sources-expand patch failed: expected source not found (bundle changed).")

    text = text.replace(state_old, state_old + "\n  const [expanded, setExpanded] = useState({});", 1)
    text = text.replace(
        chips_old,
        '{(expanded[d.domain] ? d.keywords : top3).map((k) => <span key={k} className="chip chip-mini">{k}</span>)}',
        1,
    )
    text = text.replace(
        more_old,
        '{more > 0 && <span className="chip blue chip-mini" style={{ cursor: "pointer" }} '
        'onClick={() => setExpanded((e) => ({ ...e, [d.domain]: !e[d.domain] }))} '
        'title={expanded[d.domain] ? "Show fewer" : "Show all " + d.keywords.length + " keywords"}>'
        '{expanded[d.domain] ? "less" : "+" + more}</span>}',
        1,
    )

    comp = gzip.compress(text.encode("utf-8"), mtime=0)
    manifest[target]["data"] = base64.b64encode(comp).decode("ascii")
    manifest[target]["compressed"] = True
    new_manifest = json.dumps(manifest, ensure_ascii=False)
    if "</script>" in new_manifest:
        raise SystemExit("Refusing to write: regenerated manifest contains </script>.")
    return html[:i] + new_manifest + html[j:], True


def _patch_tracked_pill(html: str) -> tuple[str, bool]:
    """Cap the top-bar tracked-terms pill to a few chips + '+N' (full list in the
    tooltip) and bound its width, so many tracked terms can't overflow and break
    the header. Idempotent. Returns (html, changed)."""
    open_tag = '<script type="__bundler/manifest">'
    i = html.index(open_tag) + len(open_tag)
    j = html.index("</script>", i)
    manifest = json.loads(html[i:j])

    target, text = None, None
    for uuid, entry in manifest.items():
        if "javascript" not in entry.get("mime", ""):
            continue
        raw = base64.b64decode(entry["data"])
        if entry.get("compressed"):
            try:
                raw = gzip.decompress(raw)
            except OSError:
                continue
        t = raw.decode("utf-8", "replace")
        if 'className="tb-pill tracked-pill"' in t:
            target, text = uuid, t
            break
    if target is None:
        raise SystemExit("Could not locate the tracked-terms pill in the bundle.")
    if "/*trk2*/" in text:
        return html, False  # already at the current (v2) layout

    # v2: the pill may shrink; its term list ellipsizes (overflow + min-width:0)
    # while the "+N" badge is a non-shrinking sibling, so it's always visible.
    pill_v2 = ('<span className="tb-pill tracked-pill" style={{ flexShrink: 1, minWidth: 0 }} '
               'title={"Tracked: " + tracked.map((t) => t.label).join(", ")}>')
    inner_v2 = ('{/*trk2*/}<span style={{ overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap", minWidth: 0 }}>'
                '{tracked.slice(0, 6).map((t, i) => <strong key={t.term} style={{ fontWeight: 700 }}>'
                '{t.label}{i < Math.min(6, tracked.length) - 1 ? "," : ""}&nbsp;</strong>)}</span>'
                '{tracked.length > 6 && <strong style={{ fontWeight: 700, color: "var(--ck-blue)", flexShrink: 0, marginLeft: 4 }}>'
                '+{tracked.length - 6}</strong>}')

    # Recognise both the pristine bundle and the earlier (v1) capped layout.
    pill_orig = '<span className="tb-pill tracked-pill" title="Tracked brands / domains">'
    pill_v1 = ('<span className="tb-pill tracked-pill" '
               'style={{ maxWidth: 460, overflow: "hidden", whiteSpace: "nowrap" }} '
               'title={"Tracked: " + tracked.map((t) => t.label).join(", ")}>')
    inner_orig = ('{tracked.map((t, i) => <strong key={t.term} style={{ fontWeight: 700 }}>'
                  '{t.label}{i < tracked.length - 1 ? "," : ""}&nbsp;</strong>)}')
    inner_v1 = ('{tracked.slice(0, 5).map((t, i) => <strong key={t.term} style={{ fontWeight: 700 }}>'
                '{t.label}{i < Math.min(5, tracked.length) - 1 ? "," : ""}&nbsp;</strong>)}'
                '{tracked.length > 5 && <strong style={{ fontWeight: 700, color: "var(--ck-blue)" }}>'
                '+{tracked.length - 5}</strong>}')

    if pill_orig in text:
        text = text.replace(pill_orig, pill_v2, 1)
    elif pill_v1 in text:
        text = text.replace(pill_v1, pill_v2, 1)
    else:
        raise SystemExit("Tracked-pill patch failed: pill open tag not found (bundle changed).")

    if inner_orig in text:
        text = text.replace(inner_orig, inner_v2, 1)
    elif inner_v1 in text:
        text = text.replace(inner_v1, inner_v2, 1)
    else:
        raise SystemExit("Tracked-pill patch failed: term list not found (bundle changed).")

    comp = gzip.compress(text.encode("utf-8"), mtime=0)
    manifest[target]["data"] = base64.b64encode(comp).decode("ascii")
    manifest[target]["compressed"] = True
    new_manifest = json.dumps(manifest, ensure_ascii=False)
    if "</script>" in new_manifest:
        raise SystemExit("Refusing to write: regenerated manifest contains </script>.")
    return html[:i] + new_manifest + html[j:], True


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--ts", type=float, default=None, help="Run timestamp to load (default: latest meaningful run)")
    ap.add_argument("--src", type=Path, default=DASHBOARD, help="Source bundle HTML to inject into (default: the dashboard file)")
    ap.add_argument("--out", type=Path, default=DASHBOARD, help="Output HTML path (default: in place)")
    ap.add_argument("--list", action="store_true", help="List available runs and exit")
    ap.add_argument("--terms", default=None,
                    help="Override tracked terms (comma-separated) instead of reading settings.json")
    args = ap.parse_args()

    if args.list:
        for r in list_runs(limit=200):
            print(f"{r['ts']:.1f}  {_fmt_label(r['ts'])}  "
                  f"{r['prompt_count']:>3} kw  {r['row_count']:>3} calls  {r['platforms']}")
        return

    if not args.src.exists():
        raise SystemExit(f"Source bundle not found: {args.src}\n"
                         f"Seed it once from the downloaded 'AI Citation Tracker.html'.")

    terms_override = None
    if args.terms is not None:
        terms_override = [t.strip() for t in args.terms.split(",") if t.strip()]
    ck = build_ck_data(args.ts, terms_override)
    html = args.src.read_text(encoding="utf-8")
    html = _inject(html, _data_module_js(ck))
    # Wire the run picker so selecting a date actually loads that run's report.
    html, sel_patched = _patch_selectors(html)
    html, app_patched = _patch_app_root(html)
    # Make the Source Leaderboard '+N' keyword chip expand to the full list.
    html, src_patched = _patch_sources_expand(html)
    # Cap the tracked-terms pill so many terms can't overflow the header.
    html, pill_patched = _patch_tracked_pill(html)
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(html, encoding="utf-8")

    cited = sum(1 for c in ck["cells"].values() if c.get("status") == "cited")
    runs_data = ck["RUNS_DATA"]
    print(f"Built {args.out.relative_to(ROOT)}")
    print(f"  active run  : {next((r['label'] for r in ck['RUNS'] if r['current']), '?')}")
    print(f"  keywords    : {len(ck['keywords'])}")
    print(f"  platforms   : {', '.join(p['name'] for p in ck['PLATFORMS'])}")
    print(f"  cells       : {len(ck['cells'])} ({cited} cited)")
    print(f"  switchable  : {len(ck['RUNS'])} runs + 'All runs combined'")
    print(f"  runs_data   : {sum(len(d['cells']) for d in runs_data.values())} cells across {len(runs_data)} datasets")
    print(f"  run-picker  : selectors {'patched' if sel_patched else 'already wired'}, "
          f"app-root {'patched' if app_patched else 'already wired'}")
    print(f"  sources     : expand chip {'patched' if src_patched else 'already wired'}")
    print(f"  tracked-pill: {'patched (capped)' if pill_patched else 'already capped'}")
    print(f"  tracked     : {', '.join(t['label'] for t in ck['TRACKED']) or '(none set)'}")


if __name__ == "__main__":
    main()
