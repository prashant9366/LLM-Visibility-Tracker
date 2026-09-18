"""Shared UI components used by both the Overview (app.py) and Compare Reports
(pages/Compare_Reports.py) pages.

Single source of truth for:
- The KPI card component (works in both 'current' and 'diff' modes)
- The KPI-metrics shape helper (compute_shape)
- Platform palette + pill renderer
- The delta pill / no-data badge rules
- The CSS the components need
"""
from __future__ import annotations

import html


# ============================================================
# Palette — platform brand colors + short codes.
# Fixed everywhere so ChatGPT is always green with GPT badge, etc.
# ============================================================
PLATFORM_LABEL = {
    "chatgpt": "ChatGPT",
    "gemini": "Gemini",
    "perplexity": "Perplexity",
    "google": "Google AIO",
    "claude": "Claude",
}
PLATFORM_COLOR = {
    "chatgpt": "#10A37F",
    "gemini": "#4385F5",
    "perplexity": "#20808D",
    "google": "#EA4335",
    "claude": "#D97757",
}
PLATFORM_SHORT = {
    "chatgpt": "GPT",
    "gemini": "GEM",
    "perplexity": "PPX",
    "google": "AIO",
    "claude": "CLA",
}
# Canonical row order used when rendering a "one row per platform" table.
PLATFORM_ROW_ORDER = ["chatgpt", "gemini", "google", "perplexity", "claude"]


# ============================================================
# Metric shape — one function computes every KPI-card field.
# Called for aggregate rows and per-platform slices; identical output shape.
# ============================================================
def is_tracked_url(url: str, terms: list[str]) -> bool:
    if not url or not terms:
        return False
    u = url.lower()
    return any(t.lower() in u for t in terms if t)


def compute_shape(rows_iter, terms: list[str]) -> dict:
    """Compute the KPI-card metrics shape over any iterable of result rows.
    Each row must expose .get() (dict or pandas Series).
    """
    n_rows = 0
    n_cited = 0
    url_cited_count = 0
    mention_count = 0
    n_urls_total = 0
    n_urls_tracked = 0
    positions: list[int] = []
    ranked_prompts: set = set()

    for row in rows_iter:
        n_rows += 1
        text = (row.get("response_text") or "").lower()
        prompt = row.get("prompt", "")
        has_mention = bool(terms) and any(t.lower() in text for t in terms if t)
        if has_mention:
            mention_count += 1

        seen: set = set()
        combined: list[str] = []
        for u in list(row.get("citations_list") or []) + list(row.get("source_urls_list") or []):
            if not u or u in seen:
                continue
            seen.add(u)
            combined.append(u)
            n_urls_total += 1
            if is_tracked_url(u, terms):
                n_urls_tracked += 1

        first_pos = None
        for i, u in enumerate(combined, start=1):
            if is_tracked_url(u, terms):
                first_pos = i
                break
        if first_pos is not None:
            url_cited_count += 1
            positions.append(first_pos)
            if prompt:
                ranked_prompts.add(prompt)

        if has_mention or (first_pos is not None):
            n_cited += 1

    return {
        "n_rows": n_rows,
        "url_cited_count": url_cited_count,
        "url_cited_rate": (url_cited_count / n_rows * 100) if n_rows else 0.0,
        "mention_count": mention_count,
        "mention_rate": (mention_count / n_rows * 100) if n_rows else 0.0,
        "n_urls_total": n_urls_total,
        "n_urls_tracked": n_urls_tracked,
        "sov": (n_urls_tracked / n_urls_total * 100) if n_urls_total else 0.0,
        "avg_position": (sum(positions) / len(positions)) if positions else None,
        "keywords_ranked": len(ranked_prompts),
        "n_cited": n_cited,
        "citation_rate": (n_cited / n_rows * 100) if n_rows else 0.0,
    }


# ============================================================
# KPI card metadata — one place to change unit / value key / invert.
# ============================================================
KPI_KINDS = ("citation_rate", "mention_rate", "keyword_position", "share_of_voice")

KPI_META = {
    "citation_rate":    {"label": "Citation rate",    "value_key": "url_cited_rate", "unit": "pts", "invert": False},
    "mention_rate":     {"label": "Mention rate",     "value_key": "mention_rate",   "unit": "pts", "invert": False},
    "keyword_position": {"label": "Keyword position", "value_key": "avg_position",   "unit": "pos", "invert": True},
    "share_of_voice":   {"label": "Share of voice",   "value_key": "sov",            "unit": "pts", "invert": False},
}


def _kpi_is_missing(m: dict | None, kind: str) -> bool:
    """The metric can't be shown for this run — em dash + 'no data' badge."""
    if m is None:
        return True
    if kind == "keyword_position":
        return m.get("avg_position") is None
    if kind == "share_of_voice":
        return m.get("n_urls_total", 0) == 0
    return m.get("n_rows", 0) == 0


def _kpi_primary_value(m: dict | None, kind: str) -> str:
    """Formatted single value for the primary line, or em dash if missing."""
    if _kpi_is_missing(m, kind):
        return "<span>—</span>"
    v = m[KPI_META[kind]["value_key"]]
    if kind == "keyword_position":
        return f"<span>{v:.1f}</span>"
    return f"<span>{v:.0f}<span class='unit'>%</span></span>"


def _kpi_sub_diff(m: dict | None, kind: str) -> str:
    """Per-metric sub-line fragment for DIFF mode (one side of a base → curr)."""
    if _kpi_is_missing(m, kind):
        return "—"
    if kind == "citation_rate":
        return f"{m['n_urls_tracked']} URLs"
    if kind == "mention_rate":
        return f"{m['mention_count']} mentions"
    if kind == "keyword_position":
        return f"{m['keywords_ranked']} kw"
    if kind == "share_of_voice":
        return f"{m['n_urls_tracked']}/{m['n_urls_total']} URLs"
    return ""


def _kpi_sub_current(m: dict | None, kind: str) -> str:
    """Sub-line for CURRENT mode (Overview) — single-run absolute counts."""
    if _kpi_is_missing(m, kind):
        return "—"
    if kind == "citation_rate":
        return f"{m['n_urls_tracked']:,} cited URLs"
    if kind == "mention_rate":
        return f"{m['mention_count']:,} mentions"
    if kind == "keyword_position":
        return f"{m['keywords_ranked']:,} keywords ranked"
    if kind == "share_of_voice":
        return f"{m['n_urls_tracked']:,} of {m['n_urls_total']:,} URLs"
    return ""


def flat_delta_html(value: float | None, unit: str = "pts", invert: bool = False,
                    missing_label: str | None = None, tooltip: str | None = None) -> str:
    """Delta badge.
    - value None or missing_label given: renders missing_label (or 'no data') as neutral pill
    - |value| < 0.05: neutral +0.0 pill
    - invert=True (position): decrease is improvement (green)
    - tooltip: rendered via native title= attribute (used for 'vs {date}')
    """
    title_attr = f" title='{html.escape(tooltip)}'" if tooltip else ""
    if missing_label is not None:
        return f"<span class='kpi-flat-delta nodata'{title_attr}>{html.escape(missing_label)}</span>"
    if value is None:
        return f"<span class='kpi-flat-delta nodata'{title_attr}>no data</span>"
    if abs(value) < 0.05:
        return f"<span class='kpi-flat-delta zero'{title_attr}>+0.0 {html.escape(unit)}</span>"
    improved = (value < 0) if invert else (value > 0)
    cls = "green" if improved else "red"
    sign = "+" if value > 0 else "−"
    return (
        f"<span class='kpi-flat-delta {cls}'{title_attr}>"
        f"{sign}{abs(value):.1f} {html.escape(unit)}</span>"
    )


def kpi_card_html(base_m: dict | None, curr_m: dict | None, kind: str,
                  mode: str = "diff", delta_tooltip: str | None = None) -> str:
    """Shared KPI card renderer.
    mode='diff'    — Compare Reports: 'base → curr' primary, 'base_sub → curr_sub' sub-line.
    mode='current' — Overview: single value primary, single-run sub-line, delta vs base.
    delta_tooltip  — optional 'vs {date}' text shown on the delta pill.
    """
    meta = KPI_META[kind]
    base_absent = base_m is None
    curr_absent = curr_m is None
    base_missing = _kpi_is_missing(base_m, kind)
    curr_missing = _kpi_is_missing(curr_m, kind)

    if mode == "current":
        primary = _kpi_primary_value(curr_m, kind)
        sub = _kpi_sub_current(curr_m, kind)
        if base_absent:
            delta = f"<span class='kpi-flat-delta nodata'>first scan</span>"
        elif curr_missing or base_missing:
            delta = "<span class='kpi-flat-delta nodata'>no data</span>"
        else:
            d = curr_m[meta["value_key"]] - base_m[meta["value_key"]]
            delta = flat_delta_html(d, unit=meta["unit"], invert=meta["invert"],
                                    tooltip=delta_tooltip)
    else:
        primary = (
            _kpi_primary_value(base_m, kind)
            + "<span class='arrow'>→</span>"
            + _kpi_primary_value(curr_m, kind)
        )
        sub = f"{_kpi_sub_diff(base_m, kind)} → {_kpi_sub_diff(curr_m, kind)}"
        if base_absent and curr_absent:
            delta = "<span class='kpi-flat-delta nodata'>no data</span>"
        elif base_absent:
            delta = "<span class='kpi-flat-delta nodata'>no baseline</span>"
        elif curr_absent:
            delta = "<span class='kpi-flat-delta nodata'>no current</span>"
        elif base_missing or curr_missing:
            delta = "<span class='kpi-flat-delta nodata'>no data</span>"
        else:
            d = curr_m[meta["value_key"]] - base_m[meta["value_key"]]
            delta = flat_delta_html(d, unit=meta["unit"], invert=meta["invert"],
                                    tooltip=delta_tooltip)

    return (
        "<div class='kpi-flat'>"
        f"<div class='kpi-flat-label'>{html.escape(meta['label'])}</div>"
        f"<div class='kpi-flat-primary'>{primary}</div>"
        f"<div class='kpi-flat-sub'>{html.escape(sub)}</div>"
        f"{delta}"
        "</div>"
    )


def kpi_grid_html(base_m: dict | None, curr_m: dict | None,
                  mode: str = "diff", delta_tooltip: str | None = None) -> str:
    """The 4-card row. base_m / curr_m are metric dicts (or None for absence)."""
    return (
        "<div class='kpi-flat-grid'>"
        + "".join(
            kpi_card_html(base_m, curr_m, kind, mode=mode, delta_tooltip=delta_tooltip)
            for kind in KPI_KINDS
        )
        + "</div>"
    )


def kpi_table_cell(base_m: dict | None, curr_m: dict | None, kind: str) -> str:
    """Compact table-cell version of the KPI card — 'base → curr' with a
    delta pill under it. Same missing-data rules as kpi_card_html."""
    meta = KPI_META[kind]
    base_absent = base_m is None
    curr_absent = curr_m is None
    base_missing = _kpi_is_missing(base_m, kind)
    curr_missing = _kpi_is_missing(curr_m, kind)

    def _side(m: dict | None, missing: bool) -> str:
        if missing:
            return "—"
        v = m[meta["value_key"]]
        if kind == "keyword_position":
            return f"{v:.1f}"
        return f"{v:.0f}%"

    base_txt = _side(base_m, base_missing)
    curr_txt = _side(curr_m, curr_missing)

    if base_absent and curr_absent:
        badge = "<span class='pill'>no data</span>"
    elif base_absent:
        badge = "<span class='pill'>no baseline</span>"
    elif curr_absent:
        badge = "<span class='pill'>no current</span>"
    elif base_missing or curr_missing:
        badge = "<span class='pill'>no data</span>"
    else:
        d = curr_m[meta["value_key"]] - base_m[meta["value_key"]]
        if abs(d) < 0.05:
            badge = f"<span class='pill'>+0.0 {html.escape(meta['unit'])}</span>"
        else:
            improved = (d < 0) if meta["invert"] else (d > 0)
            cls = "green" if improved else "red"
            sign = "+" if d > 0 else "−"
            badge = (
                f"<span class='pill {cls}'>"
                f"{sign}{abs(d):.1f} {html.escape(meta['unit'])}</span>"
            )
    return (
        "<td class='num pbd-cell'>"
        f"<div class='pbd-vals'>{base_txt} <span class='pbd-arrow'>→</span> {curr_txt}</div>"
        f"<div class='pbd-delta'>{badge}</div>"
        "</td>"
    )


def kpi_single_cell(m: dict | None, kind: str, show_bar: bool = False) -> str:
    """Single-run table-cell — no baseline comparison. Used on the Overview
    platform table. `show_bar=True` renders an inline fill bar (SoV column)."""
    meta = KPI_META[kind]
    if _kpi_is_missing(m, kind):
        return "<td class='num pbd-cell'><span class='pill'>no data</span></td>"
    v = m[meta["value_key"]]
    if kind == "keyword_position":
        val_txt = f"{v:.1f}"
    else:
        val_txt = f"{v:.0f}%"
    bar_html = ""
    if show_bar:
        pct = max(0.0, min(100.0, v))
        bar_html = (
            "<div class='pbd-bar'>"
            f"<div class='pbd-bar-fill' style='width:{pct:.1f}%;'></div>"
            "</div>"
        )
    return (
        "<td class='num pbd-cell'>"
        f"<div class='pbd-vals'>{val_txt}</div>"
        f"{bar_html}"
        "</td>"
    )


# ============================================================
# Platform pill — brand dot + label + short code. Used everywhere.
# ============================================================
def plat_pill_html(platform: str) -> str:
    color = PLATFORM_COLOR.get(platform, "#7C5CFF")
    label = PLATFORM_LABEL.get(platform, platform)
    code = PLATFORM_SHORT.get(platform, platform[:3].upper())
    return (
        "<span class='plat-pill'>"
        f"<span class='plat-dot' style='background:{color};'></span>"
        f"{html.escape(label)}"
        f"<span class='plat-code'>{html.escape(code)}</span>"
        "</span>"
    )


# ============================================================
# CSS — every class the shared components use. Inject once per page via
# st.markdown(SHARED_CSS, unsafe_allow_html=True).
# ============================================================
SHARED_CSS = """
<style>
:root {
  --violet: #7C5CFF;
  --cyan: #22D3EE;
  --focus-ring: 0 0 0 3px rgba(124,92,255,0.35);
}

/* KPI grid — 4 cols desktop / 2 tablet / 1 mobile. Flat surface, no gradient. */
.kpi-flat-grid {
  display: grid;
  grid-template-columns: repeat(4, minmax(0, 1fr));
  gap: 14px;
  margin: 12px 0 20px;
}
@media (max-width: 1200px) { .kpi-flat-grid { grid-template-columns: repeat(2, minmax(0, 1fr)); } }
@media (max-width: 640px)  { .kpi-flat-grid { grid-template-columns: 1fr; } }
.kpi-flat {
  background: var(--surface-2, rgba(255,255,255,0.045));
  border: 1px solid var(--border, rgba(255,255,255,0.08));
  border-radius: 14px;
  padding: 18px 20px;
  display: flex; flex-direction: column; gap: 8px;
  min-height: 132px;
  min-width: 0;
}
.kpi-flat-label {
  font-family: 'Geist Mono', ui-monospace, monospace;
  font-size: 10px; letter-spacing: 0.14em;
  text-transform: uppercase;
  color: var(--text-3, #8482B0); font-weight: 600;
}
.kpi-flat-primary {
  display: flex; align-items: baseline; flex-wrap: wrap; gap: 6px;
  font-family: 'Instrument Serif', ui-serif, serif;
  font-size: 32px; line-height: 1; letter-spacing: -0.02em;
  color: var(--text, #F5F3FF);
}
.kpi-flat-primary .arrow {
  color: var(--text-3, #8482B0); font-size: 20px;
  font-family: 'Geist', sans-serif; margin: 0 2px;
}
.kpi-flat-primary .unit {
  font-size: 15px; color: var(--text-3, #8482B0);
  font-family: 'Geist Mono', ui-monospace, monospace; margin-left: 1px;
}
.kpi-flat-sub {
  font-family: 'Geist Mono', ui-monospace, monospace; font-size: 11px;
  color: var(--text-3, #8482B0); min-height: 14px;
}
.kpi-flat-delta {
  display: inline-flex; align-items: center; gap: 6px;
  padding: 4px 12px; border-radius: 999px;
  font-family: 'Geist Mono', ui-monospace, monospace; font-size: 11px;
  border: 1px solid var(--border, rgba(255,255,255,0.08));
  background: rgba(255,255,255,0.04);
  color: var(--text-2, #C7C5EC);
  align-self: flex-start;
  margin-top: auto;
}
.kpi-flat-delta.green  { color: #7DF0B4; background: rgba(31,164,99,0.14); border-color: rgba(31,164,99,0.36); }
.kpi-flat-delta.red    { color: #FFB4AE; background: rgba(234,67,53,0.14); border-color: rgba(234,67,53,0.36); }
.kpi-flat-delta.zero   { color: var(--text-2, #C7C5EC); background: rgba(255,255,255,0.04); border-color: var(--border, rgba(255,255,255,0.08)); }
.kpi-flat-delta.nodata { color: var(--text-3, #8482B0); background: rgba(255,255,255,0.03); border-color: var(--border, rgba(255,255,255,0.08)); }

/* Platform table cells */
.pbd-cell { min-width: 130px; vertical-align: middle; }
.pbd-vals {
  font-family: 'Geist Mono', ui-monospace, monospace; font-size: 13px;
  color: var(--text, #F5F3FF); white-space: nowrap;
}
.pbd-arrow { color: var(--text-3, #8482B0); margin: 0 4px; }
.pbd-delta { margin-top: 6px; }
.pbd-bar {
  height: 6px; width: 100%; margin-top: 6px;
  background: rgba(255,255,255,0.06);
  border-radius: 999px; overflow: hidden;
}
.pbd-bar-fill {
  height: 100%;
  background: linear-gradient(90deg, var(--violet) 0%, var(--cyan) 100%);
  border-radius: 999px;
}

/* Platform pill — brand dot + label + short code */
.plat-pill { display: inline-flex; align-items: center; gap: 8px; font-size: 13px; color: var(--text, #F5F3FF); }
.plat-dot  { width: 9px; height: 9px; border-radius: 50%; box-shadow: 0 0 0 3px rgba(255,255,255,0.05); }
.plat-code {
  font-family: 'Geist Mono', ui-monospace, monospace; font-size: 10px;
  color: var(--text-3, #8482B0);
  border: 1px solid var(--border, rgba(255,255,255,0.08));
  border-radius: 6px; padding: 1px 6px;
}

/* Generic pill (used inside table cells) */
.pill {
  display: inline-flex; align-items: center; gap: 6px;
  padding: 3px 10px; border-radius: 999px;
  font-family: 'Geist Mono', ui-monospace, monospace; font-size: 11px;
  border: 1px solid var(--border, rgba(255,255,255,0.08));
  background: rgba(255,255,255,0.04);
  color: var(--text-2, #C7C5EC);
}
.pill.green { color: #7DF0B4; background: rgba(31,164,99,0.14); border-color: rgba(31,164,99,0.36); }
.pill.red   { color: #FFB4AE; background: rgba(234,67,53,0.14); border-color: rgba(234,67,53,0.36); }
.pill.amber { color: #FFD98A; background: rgba(245,158,11,0.14); border-color: rgba(245,158,11,0.36); }

/* Segmented control — single source of truth for every st.segmented_control
   instance (Mode switch, KPI toggle, platform filter, sidebar tool switcher).
   No fixed width on the container, no shrinking on the buttons, no ellipsis
   anywhere — labels always render in full and wrap to a new row instead. */
div[data-testid="stButtonGroup"] {
  display: inline-flex !important;
  width: fit-content !important;
  max-width: 100% !important;
  flex-wrap: wrap !important;
  gap: 4px !important;
  background: rgba(255,255,255,0.04);
  border: 1px solid var(--border, rgba(255,255,255,0.08));
  border-radius: 12px;
  padding: 4px !important;
}
div[data-testid="stButtonGroup"] button {
  flex: 0 0 auto !important;
  width: auto !important;
  min-width: 0 !important;
  max-width: none !important;
  white-space: nowrap !important;
  overflow: visible !important;
  text-overflow: clip !important;
  padding: 8px 16px !important;
  border: none !important;
  border-radius: 9px !important;
  background: transparent !important;
  color: var(--text-2, #C7C5EC) !important;
  font-size: 12px !important;
  font-weight: 600 !important;
  letter-spacing: 0.01em;
}
div[data-testid="stButtonGroup"] button p,
div[data-testid="stButtonGroup"] button span {
  color: inherit !important;
  font-size: 12px !important;
  white-space: nowrap !important;
  overflow: visible !important;
  text-overflow: clip !important;
  max-width: none !important;
  -webkit-line-clamp: none !important;
}
div[data-testid="stButtonGroup"] button[data-testid="stBaseButton-segmented_controlActive"] {
  background: linear-gradient(135deg, #7C5CFF 0%, #22D3EE 100%) !important;
  color: #ffffff !important;
  box-shadow: 0 6px 16px rgba(124,92,255,0.35);
}

/* Sidebar nav items — same no-truncation rule; matches the "Compare Repo…"
   fix requested. Only nav / toggle labels; long URLs in table cells keep
   their own ellipsis via .url-cell / .acc-url-full / .kw-chip. */
section[data-testid="stSidebar"] a,
section[data-testid="stSidebar"] a span,
section[data-testid="stSidebar"] a p {
  white-space: nowrap !important;
  overflow: visible !important;
  text-overflow: clip !important;
  max-width: none !important;
  -webkit-line-clamp: none !important;
}

/* ================================================================
   Sidebar layout — fixed 260px width so the source toggle inputs
   don't cramp. Nav lives in the top bar now; sidebar segmented
   controls (Manual | Google Sheet | Excel) inherit the horizontal
   pill row from the shared segmented-control rules above.
   ================================================================ */
section[data-testid="stSidebar"] {
  width: 260px !important;
  min-width: 260px !important;
}
section[data-testid="stSidebar"] > div,
section[data-testid="stSidebar"] [data-testid="stSidebarContent"] {
  min-width: 260px !important;
}
section[data-testid="stSidebar"] [data-testid="stSidebarUserContent"],
section[data-testid="stSidebar"] [data-testid="stSidebarContent"] {
  padding-left: 16px !important;
  padding-right: 16px !important;
}

/* Focus rings — keyboard visibility on run selector, buttons, toggles, nav */
button:focus-visible,
a:focus-visible,
[role="button"]:focus-visible,
[data-baseweb="select"]:focus-within {
  outline: none !important;
  box-shadow: var(--focus-ring) !important;
  border-radius: 8px;
}

/* Reduced motion */
@media (prefers-reduced-motion: reduce) {
  * { animation-duration: 0.01ms !important; transition-duration: 0.01ms !important; }
}
</style>
"""
