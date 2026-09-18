"""Excel export for the Citerion dashboard.

Two design principles drive the sheet layout:

  1. Summary sheets stay narrow and human-readable — one row per entity,
     no multi-line cells, so Excel renders them cleanly.
  2. All "this thing belongs to that thing" relationships get their own
     **long-format sheets** (one row per pair). That makes every drill-down
     pivotable in Excel without re-running queries.

Sheets:

  - Responses        — one row per (platform, prompt) call
  - Citation matrix  — wide: keyword × platform with cited?/position + score
  - Matrix URLs      — long: (keyword, URL) pairs with platform counts
  - URL leaderboard  — one row per **domain** (matches the UI's reddit-style
                       dedup) with totals and per-platform cite columns
  - Domain x Keyword — long: (domain, keyword) pairs with citation count
  - Domain paths     — long: (domain, URL) pairs for path-level inspection
  - Mentions         — tracked-term × where-found rows

Uses openpyxl via pandas (already a transitive dep of streamlit).
"""
from __future__ import annotations

import io
import json
from collections import Counter
from typing import Iterable
from urllib.parse import urlparse

import pandas as pd


def _to_list(val) -> list:
    if isinstance(val, list):
        return val
    if isinstance(val, str) and val:
        try:
            return json.loads(val) or []
        except json.JSONDecodeError:
            return []
    return []


def _is_tracked_url(url: str, terms: list[str]) -> bool:
    if not url or not terms:
        return False
    u = url.lower()
    return any(t.lower() in u for t in terms if t)


def _row_citation_position(row: dict, terms: list[str]) -> tuple[bool, int | None, str]:
    if not terms:
        return False, None, ""
    text = (row.get("response_text") or "").lower()
    cit_list = _to_list(row.get("citations"))
    src_list = _to_list(row.get("source_urls"))
    combined: list[str] = []
    seen = set()
    for u in cit_list + src_list:
        if u and u not in seen:
            seen.add(u)
            combined.append(u)
    for i, u in enumerate(combined, start=1):
        if _is_tracked_url(u, terms):
            in_text = any(t.lower() in text for t in terms)
            return True, i, ("text+url" if in_text else "url")
    if any(t.lower() in text for t in terms):
        return True, None, "text"
    return False, None, ""


def build_workbook(rows: Iterable[dict], tracked_terms: list[str]) -> bytes:
    """Build a multi-sheet Excel workbook and return its raw bytes."""
    rows = list(rows)
    buf = io.BytesIO()

    # --- Responses sheet ---
    responses: list[dict] = []
    for r in rows:
        cits = _to_list(r.get("citations"))
        srcs = _to_list(r.get("source_urls"))
        fans = _to_list(r.get("fanout_queries"))
        cited, pos, where = _row_citation_position(r, tracked_terms)
        responses.append({
            "platform": r.get("platform", ""),
            "prompt": r.get("prompt", ""),
            "cited": "Yes" if cited else "No",
            "position": (pos if isinstance(pos, int) and pos > 0 else
                         ("text" if where == "text" else "")),
            "found_in": where,
            "response_text": r.get("response_text", "") or "",
            "citation_count": len(cits),
            "source_url_count": len(srcs),
            "fanout_count": len(fans),
            "citations": "\n".join(cits),
            "source_urls": "\n".join(srcs),
            "fanout_queries": "\n".join(fans),
            "error": r.get("error", "") or "",
        })
    df_resp = pd.DataFrame(responses)

    # --- Citation matrix sheet (wide, narrow, no multi-line) ---
    matrix_rows: list[dict] = []
    matrix_url_rows: list[dict] = []  # long-format companion sheet
    platforms_in_data = sorted({(r.get("platform") or "") for r in rows})
    if rows:
        prompts = sorted({(r.get("prompt") or "") for r in rows})
        for prm in prompts:
            row_out: dict = {"keyword": prm}
            cited_count = 0

            # Build URL → set(platforms) for THIS keyword (used by the long
            # Matrix URLs sheet — the wide sheet stays narrow).
            url_plat_set: dict[str, set[str]] = {}
            for plat in platforms_in_data:
                matching = [r for r in rows if r.get("prompt") == prm and r.get("platform") == plat]
                if not matching:
                    row_out[plat] = "—"
                    continue
                rr = matching[0]
                cited, pos, _ = _row_citation_position(rr, tracked_terms)
                if cited:
                    cited_count += 1
                    row_out[plat] = f"#{pos}" if isinstance(pos, int) and pos > 0 else "text"
                else:
                    row_out[plat] = "missing"
                for u in _to_list(rr.get("citations")) + _to_list(rr.get("source_urls")):
                    if u:
                        url_plat_set.setdefault(u, set()).add(plat)

            row_out["unique_urls"] = len(url_plat_set)
            row_out["total_url_citations"] = sum(len(v) for v in url_plat_set.values())
            row_out["score"] = f"{cited_count}/{len(platforms_in_data)}"
            matrix_rows.append(row_out)

            # Long-format companion: one row per (keyword, URL).
            ordered = sorted(url_plat_set.items(), key=lambda kv: (-len(kv[1]), kv[0]))
            for u, plats in ordered:
                try:
                    d = urlparse(u).netloc.lower() or u
                except Exception:
                    d = u
                matrix_url_rows.append({
                    "keyword": prm,
                    "domain": d,
                    "url": u,
                    "platforms_count": len(plats),
                    "platforms": ", ".join(sorted(plats)),
                    "is_tracked": "Yes" if _is_tracked_url(u, tracked_terms) else "No",
                })
    df_matrix = pd.DataFrame(matrix_rows)
    df_matrix_urls = pd.DataFrame(matrix_url_rows)

    # --- URL leaderboard sheet (DOMAIN-aggregated, narrow) ---
    # Same grouping as the dashboard. Only flat numeric columns here — no
    # multi-line keyword/path lists. Use the Domain x Keyword and Domain
    # paths sheets below for the relational drill-downs.
    domain_total: Counter = Counter()
    domain_per_platform: dict[str, dict[str, int]] = {}
    domain_per_prompt: dict[str, dict[str, int]] = {}
    domain_urls: dict[str, set[str]] = {}
    for r in rows:
        plat = r.get("platform", "")
        prm = r.get("prompt", "")
        for u in _to_list(r.get("citations")) + _to_list(r.get("source_urls")):
            if not u:
                continue
            try:
                d = urlparse(u).netloc.lower() or u
            except Exception:
                d = u
            domain_total[d] += 1
            domain_per_platform.setdefault(d, {})[plat] = domain_per_platform.get(d, {}).get(plat, 0) + 1
            if prm:
                domain_per_prompt.setdefault(d, {})[prm] = domain_per_prompt.get(d, {}).get(prm, 0) + 1
            domain_urls.setdefault(d, set()).add(u)

    leaderboard: list[dict] = []
    for i, (d, cnt) in enumerate(sorted(domain_total.items(), key=lambda kv: kv[1], reverse=True), start=1):
        if tracked_terms:
            is_tracked = (
                any(t.lower() in d.lower() for t in tracked_terms if t)
                or any(_is_tracked_url(u, tracked_terms) for u in domain_urls.get(d, ()))
            )
        else:
            is_tracked = False
        entry: dict = {
            "rank": i,
            "domain": d,
            "is_tracked": "Yes" if is_tracked else "No",
            "total_citations": cnt,
            "unique_paths": len(domain_urls.get(d, ())),
            "keyword_count": len(domain_per_prompt.get(d, {})),
            "platforms_count": len(domain_per_platform.get(d, {})),
        }
        for p in platforms_in_data:
            entry[f"cites_{p}"] = domain_per_platform.get(d, {}).get(p, 0)
        leaderboard.append(entry)
    df_leader = pd.DataFrame(leaderboard)

    # --- Domain x Keyword sheet (long, pivotable) ---
    # One row per (domain, keyword). Pivot in Excel by dropping `domain`
    # in rows, `keyword` in columns, `citations` in values to get the full
    # citation matrix on demand.
    dom_kw_rows: list[dict] = []
    for d, kws in domain_per_prompt.items():
        is_tracked = (
            any(t.lower() in d.lower() for t in tracked_terms if t)
            or any(_is_tracked_url(u, tracked_terms) for u in domain_urls.get(d, ()))
        ) if tracked_terms else False
        for k, n in sorted(kws.items(), key=lambda kv: kv[1], reverse=True):
            dom_kw_rows.append({
                "domain": d,
                "keyword": k,
                "citations": n,
                "is_tracked_domain": "Yes" if is_tracked else "No",
            })
    df_dom_kw = pd.DataFrame(dom_kw_rows)

    # --- Domain paths sheet (long) ---
    # One row per (domain, URL path). Useful for inspecting which specific
    # reddit threads / which YouTube videos / etc. were cited.
    dom_path_rows: list[dict] = []
    for d, urls in domain_urls.items():
        for u in sorted(urls):
            dom_path_rows.append({
                "domain": d,
                "url": u,
                "is_tracked_url": "Yes" if _is_tracked_url(u, tracked_terms) else "No",
            })
    df_dom_paths = pd.DataFrame(dom_path_rows)

    # --- Mentions sheet ---
    mentions: list[dict] = []
    for term in tracked_terms:
        tl = term.lower()
        for r in rows:
            text = (r.get("response_text") or "")
            cits = _to_list(r.get("citations"))
            srcs = _to_list(r.get("source_urls"))
            in_text = tl in text.lower()
            in_cit = any(tl in (u or "").lower() for u in cits)
            in_src = any(tl in (u or "").lower() for u in srcs)
            if in_text or in_cit or in_src:
                where = []
                if in_text: where.append("text")
                if in_cit: where.append("citations")
                if in_src: where.append("sources")
                snippet = ""
                if in_text:
                    idx = text.lower().find(tl)
                    s = max(0, idx - 60)
                    e = min(len(text), idx + len(term) + 60)
                    snippet = ("…" if s > 0 else "") + text[s:e] + ("…" if e < len(text) else "")
                mentions.append({
                    "term": term,
                    "platform": r.get("platform", ""),
                    "keyword": r.get("prompt", ""),
                    "found_in": ", ".join(where),
                    "snippet": snippet,
                })
    df_mentions = pd.DataFrame(mentions)

    # --- Write workbook ---
    def _empty(msg: str) -> pd.DataFrame:
        return pd.DataFrame({"info": [msg]})

    sheets: list[tuple[str, pd.DataFrame]] = [
        ("Responses",       df_resp       if not df_resp.empty       else _empty("No data")),
        ("Citation matrix", df_matrix     if not df_matrix.empty     else _empty("No data")),
        ("Matrix URLs",     df_matrix_urls if not df_matrix_urls.empty else _empty("No URLs cited")),
        ("URL leaderboard", df_leader     if not df_leader.empty     else _empty("No URLs captured")),
        ("Domain x Keyword", df_dom_kw    if not df_dom_kw.empty     else _empty("No data")),
        ("Domain paths",    df_dom_paths  if not df_dom_paths.empty  else _empty("No data")),
        ("Mentions",        df_mentions   if not df_mentions.empty   else _empty("Add tracked terms in the sidebar")),
    ]

    with pd.ExcelWriter(buf, engine="openpyxl") as writer:
        for sheet_name, df in sheets:
            df.to_excel(writer, sheet_name=sheet_name, index=False)

        # Width tuning + freeze the header row on every sheet. No multi-line
        # cells anymore, so no wrap_text needed — long-format sheets keep
        # each row narrow and pivotable.
        from openpyxl.utils import get_column_letter

        per_column_width: dict[str, dict[str, int]] = {
            "keyword": {"width": 38},
            "prompt": {"width": 38},
            "domain": {"width": 32},
            "url": {"width": 60},
            "response_text": {"width": 60},
            "citations": {"width": 50},
            "source_urls": {"width": 50},
            "fanout_queries": {"width": 50},
            "snippet": {"width": 80},
            "platforms": {"width": 24},
            "found_in": {"width": 14},
            "is_tracked": {"width": 12},
            "is_tracked_domain": {"width": 18},
            "is_tracked_url": {"width": 14},
        }
        default_width = 14

        for sheet_name, df in sheets:
            ws = writer.sheets[sheet_name]
            ws.freeze_panes = "A2"
            for idx, col_name in enumerate(df.columns, start=1):
                letter = get_column_letter(idx)
                w = per_column_width.get(str(col_name), {}).get("width", default_width)
                ws.column_dimensions[letter].width = w
    return buf.getvalue()
