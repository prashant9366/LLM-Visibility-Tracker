"""Compare Reports — diff two scrape runs or trend across many.

Reuses the standalone dashboard's visual language (deep indigo bg, violet→cyan
gradient scoreband, brand-coloured platform pills) so the Compare page feels
native to that UI. All runs are read from `data/results.db` via `core/storage`.
"""
from __future__ import annotations

import html
import json
import sys
from datetime import datetime
from functools import cmp_to_key
from pathlib import Path
from urllib.parse import quote

import pandas as pd
import streamlit as st

# Pages run as their own script — make sure the project root is importable.
ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from core.aggregator import build_dataframes  # noqa: E402
from core.storage import list_runs, load_run  # noqa: E402
from core.settings import get_tracked_terms  # noqa: E402
from core.ui import (  # noqa: E402
    PLATFORM_ROW_ORDER, PLATFORM_LABEL as PLATFORM_LABEL_FOR_FILTER, KPI_META,
    is_tracked_url as _is_tracked_url,
    compute_shape,
    kpi_grid_html, kpi_single_cell,
    plat_pill_html as _plat_pill_html,
    flat_delta_html as _flat_delta,
    SHARED_CSS,
)
from core.ui import _kpi_is_missing  # noqa: E402 — used by per-keyword compare

st.set_page_config(
    page_title="Compare Reports · Citerion",
    page_icon="◆",
    layout="wide",
    initial_sidebar_state="collapsed",
)


# ============================================================
# CSS — mirror the standalone dashboard palette:
#   --bg: #0B0A1F, sidebar #18162E, violet #6D5EFB/#7C5CFF, cyan #22D3EE,
#   scoreband gradient #0B0A1F -> #4B2FB5 -> #7C5CFF.
# ============================================================
CUSTOM_CSS = """
<style>
@import url('https://fonts.googleapis.com/css2?family=Instrument+Serif:ital@0;1&family=Geist:wght@300;400;500;600;700;800&family=Geist+Mono:wght@400;500;600&display=swap');
@import url('https://fonts.googleapis.com/css2?family=Material+Symbols+Rounded:opsz,wght,FILL,GRAD@20..48,100..700,0..1,-50..200&display=block');

:root {
  --bg: #0B0A1F;
  --bg-2: #0F0E27;
  --side-bg: #0B0A1F;
  --side-bg-2: #18162E;
  --surface: rgba(255,255,255,0.03);
  --surface-2: rgba(255,255,255,0.055);
  --surface-3: rgba(255,255,255,0.09);
  --border: rgba(255,255,255,0.08);
  --border-strong: rgba(255,255,255,0.18);
  --text: #F5F3FF;
  --text-2: #C7C5EC;
  --text-3: #8482B0;
  --violet: #7C5CFF;
  --violet-2: #6D5EFB;
  --violet-3: #4B2FB5;
  --violet-soft: #A78BFA;
  --cyan: #22D3EE;
  --green: #1FA463;
  --green-bg: rgba(31,164,99,0.14);
  --green-border: rgba(31,164,99,0.36);
  --red: #EA4335;
  --red-bg: rgba(234,67,53,0.14);
  --red-border: rgba(234,67,53,0.36);
  --amber: #F59E0B;
  --amber-bg: rgba(245,158,11,0.14);
  --amber-border: rgba(245,158,11,0.36);
  --radius: 12px;
  --radius-lg: 18px;
  --radius-xl: 22px;
  --shadow-md: 0 10px 30px rgba(0,0,0,0.45);
  --shadow-lg: 0 24px 60px rgba(11,10,31,0.6);
}

html, body, .stApp {
  background: var(--bg) !important;
  background-image:
    radial-gradient(at 12% -5%, rgba(124,92,255,0.22) 0px, transparent 55%),
    radial-gradient(at 92% 0%,  rgba(34,211,238,0.10) 0px, transparent 45%),
    radial-gradient(at 50% 100%, rgba(75,47,181,0.18) 0px, transparent 55%) !important;
  background-attachment: fixed !important;
  color: var(--text) !important;
  font-family: 'Geist', -apple-system, BlinkMacSystemFont, sans-serif !important;
  -webkit-font-smoothing: antialiased;
}
.stApp p, .stApp span, .stApp label {
  color: var(--text); font-family: 'Geist', sans-serif;
}
.stMarkdown, .stMarkdown p, .stMarkdown li { color: var(--text) !important; }
header[data-testid="stHeader"] { background: transparent; }
.block-container { padding-top: 1.6rem; max-width: 1360px; }

/* Sidebar — dashboard side-bg tone. */
section[data-testid="stSidebar"] {
  background: var(--side-bg) !important;
  border-right: 1px solid var(--border);
}
section[data-testid="stSidebar"] > div:first-child {
  background: linear-gradient(180deg, var(--side-bg) 0%, #100E28 100%) !important;
}
section[data-testid="stSidebar"] * { color: var(--text-2); }
section[data-testid="stSidebar"] h3, section[data-testid="stSidebar"] h4 {
  color: var(--text) !important;
  font-family: 'Geist', sans-serif !important;
  font-size: 12px !important;
  font-weight: 600 !important;
  text-transform: uppercase;
  letter-spacing: 0.10em;
  margin: 18px 0 8px !important;
  opacity: 0.85;
}

/* Sidebar brand chip — same aesthetic as the dashboard sidebar brand */
.side-brand {
  display: flex; align-items: center; gap: 10px;
  padding: 2px 0 14px;
}
.side-brand-mark {
  width: 34px; height: 34px; border-radius: 10px;
  display: grid; place-items: center; color: #fff;
  background: linear-gradient(135deg, #7C5CFF 0%, #22D3EE 100%);
  box-shadow: 0 6px 18px rgba(124,92,255,0.55);
  font-weight: 800; font-size: 16px;
  font-family: 'Instrument Serif', serif;
}
.side-brand-name {
  font-weight: 800; font-size: 22px; letter-spacing: -0.02em;
  color: #ffffff; font-family: 'Geist', sans-serif;
}

/* Inputs */
.stTextInput input, .stTextArea textarea {
  background: var(--surface-2) !important;
  color: var(--text) !important;
  -webkit-text-fill-color: var(--text) !important;
  border: 1px solid var(--border) !important;
  border-radius: 10px !important;
  font-size: 13px !important;
  caret-color: var(--violet) !important;
}
.stTextInput input:focus, .stTextArea textarea:focus {
  border-color: var(--violet) !important;
  box-shadow: 0 0 0 3px rgba(124,92,255,0.22) !important;
}
[data-testid="stTextArea"], [data-testid="stTextArea"] > div,
[data-testid="stTextArea"] [data-baseweb="textarea"],
[data-testid="stTextArea"] [data-baseweb="base-input"] {
  background: transparent !important;
}
.stTextInput input::placeholder, .stTextArea textarea::placeholder {
  color: var(--text-3) !important;
  -webkit-text-fill-color: var(--text-3) !important;
}

.stSelectbox div[data-baseweb="select"] > div,
.stMultiSelect div[data-baseweb="select"] > div {
  background: var(--surface-2) !important;
  color: var(--text) !important;
  border: 1px solid var(--border) !important;
  border-radius: 10px !important;
  min-height: 42px;
}
.stSelectbox div[data-baseweb="select"] span,
.stMultiSelect div[data-baseweb="select"] span {
  color: var(--text) !important;
}
div[data-baseweb="popover"] [role="listbox"], div[data-baseweb="popover"] ul {
  background: #14122C !important;
  border: 1px solid var(--border) !important;
  border-radius: 10px;
}
div[data-baseweb="popover"] [role="option"] { color: var(--text) !important; }
div[data-baseweb="popover"] [role="option"]:hover,
div[data-baseweb="popover"] [role="option"][aria-selected="true"] {
  background: rgba(124,92,255,0.20) !important;
}

/* CTAs — same violet→cyan gradient as sidebar mark */
.stButton button, .stDownloadButton button {
  background: linear-gradient(135deg, #7C5CFF 0%, #22D3EE 100%) !important;
  color: #ffffff !important;
  border: none !important;
  border-radius: 10px !important;
  font-size: 13px !important;
  font-weight: 600 !important;
  padding: 8px 16px !important;
  box-shadow: 0 8px 22px rgba(124,92,255,0.35);
}
.stButton button p, .stDownloadButton button p { color: #ffffff !important; }
.stDownloadButton button:hover, .stButton button:hover { filter: brightness(1.06); }

/* Page hero */
.page-title {
  font-family: 'Instrument Serif', serif;
  font-weight: 400; font-size: 52px; letter-spacing: -0.02em;
  margin: 4px 0 6px; line-height: 1.02;
  background: linear-gradient(180deg, #ffffff 0%, #C7C5EC 100%);
  -webkit-background-clip: text;
  -webkit-text-fill-color: transparent;
}
.page-sub { font-size: 13px; color: var(--text-3); margin: 0; }

.tag {
  display: inline-flex; align-items: center; gap: 6px;
  background: var(--surface-2);
  border: 1px solid var(--border);
  padding: 5px 12px; border-radius: 999px;
  font-size: 11px; color: var(--text-2);
  font-family: 'Geist Mono', monospace;
}
.tag.violet {
  color: #E8DFFF;
  background: rgba(124,92,255,0.14);
  border-color: rgba(124,92,255,0.32);
}

/* Scoreband — the hero card the dashboard uses under the header.
   Deep indigo → violet gradient, big serif number on the left, deltas on the right. */
.scoreband {
  position: relative;
  background: linear-gradient(120deg, #0B0A1F 0%, #4B2FB5 55%, #7C5CFF 100%);
  border-radius: var(--radius-xl);
  padding: 28px 34px;
  margin: 20px 0 22px;
  box-shadow: var(--shadow-lg);
  overflow: hidden;
  border: 1px solid rgba(255,255,255,0.06);
}
.scoreband::after {
  content: ""; position: absolute; inset: 0;
  background:
    radial-gradient(circle at 88% 15%, rgba(34,211,238,0.32) 0px, transparent 42%),
    radial-gradient(circle at 15% 90%, rgba(255,255,255,0.06) 0px, transparent 45%);
  pointer-events: none;
}
.scoreband-inner {
  position: relative; z-index: 1;
  display: grid; grid-template-columns: 1.1fr 1fr; gap: 30px; align-items: center;
}
@media (max-width: 900px) { .scoreband-inner { grid-template-columns: 1fr; } }
.sb-label {
  font-size: 11px; text-transform: uppercase; letter-spacing: 0.14em;
  color: rgba(255,255,255,0.75); font-family: 'Geist Mono', monospace;
  margin-bottom: 10px;
}
.sb-value {
  font-family: 'Instrument Serif', serif;
  font-size: 76px; line-height: 1; letter-spacing: -0.03em;
  color: #ffffff;
  display: flex; align-items: baseline; gap: 8px;
}
.sb-value .pct { font-size: 26px; color: rgba(255,255,255,0.75); font-family: 'Geist Mono', monospace; }
.sb-arrow { color: rgba(255,255,255,0.55); font-size: 34px; margin: 0 8px; }
.sb-sub { color: rgba(255,255,255,0.75); font-size: 13px; margin-top: 8px; font-family: 'Geist Mono', monospace; }
.sb-side { display: flex; flex-direction: column; gap: 14px; }
.sb-row {
  display: grid; grid-template-columns: 1fr auto; gap: 16px; align-items: center;
  padding: 14px 18px;
  background: rgba(255,255,255,0.08);
  border: 1px solid rgba(255,255,255,0.10);
  border-radius: 14px;
  backdrop-filter: blur(6px);
}
.sb-row-lbl { color: rgba(255,255,255,0.75); font-size: 12px; font-family: 'Geist Mono', monospace; text-transform: uppercase; letter-spacing: 0.08em; }
.sb-row-val { color: #ffffff; font-family: 'Instrument Serif', serif; font-size: 26px; letter-spacing: -0.02em; }
.sb-delta {
  display: inline-flex; align-items: center; gap: 6px;
  padding: 4px 10px; border-radius: 999px;
  font-family: 'Geist Mono', monospace; font-size: 11px;
  background: rgba(255,255,255,0.12);
  border: 1px solid rgba(255,255,255,0.18);
  color: #ffffff;
}
.sb-delta.green { background: rgba(31,164,99,0.28); border-color: rgba(31,164,99,0.55); }
.sb-delta.red   { background: rgba(234,67,53,0.28); border-color: rgba(234,67,53,0.55); }

/* Section header — larger, more like the dashboard's stacked titles */
.section-header { margin: 28px 0 12px; }
.section-title {
  font-family: 'Instrument Serif', serif;
  font-size: 30px; font-weight: 400; letter-spacing: -0.02em;
  color: var(--text); margin: 0;
}
.section-meta {
  font-size: 11px; color: var(--text-3);
  font-family: 'Geist Mono', monospace;
  text-transform: uppercase; letter-spacing: 0.10em;
}

/* KPI grid — deeper cards, bigger numbers */
.diff-grid { display: grid; grid-template-columns: repeat(4, 1fr); gap: 14px; margin-bottom: 8px; }
@media (max-width: 1100px) { .diff-grid { grid-template-columns: repeat(2, 1fr); } }
.diff-card {
  background: linear-gradient(180deg, rgba(255,255,255,0.04) 0%, rgba(255,255,255,0.015) 100%);
  border: 1px solid var(--border);
  border-radius: var(--radius-lg);
  padding: 20px 22px;
  box-shadow: var(--shadow-md);
  position: relative; overflow: hidden;
}
.diff-card::before {
  content: ""; position: absolute;
  left: 0; top: 0; height: 3px; width: 100%;
  background: linear-gradient(90deg, var(--violet) 0%, var(--cyan) 100%);
  opacity: 0.75;
}
.diff-card-label {
  font-size: 10px; color: var(--text-2);
  text-transform: uppercase; letter-spacing: 0.12em; font-weight: 600;
  margin-bottom: 12px; font-family: 'Geist Mono', monospace;
}
.diff-card-row {
  display: flex; align-items: baseline; gap: 8px;
  font-family: 'Geist Mono', monospace; font-size: 12px; color: var(--text-3);
}
.diff-card-row b {
  font-family: 'Instrument Serif', serif; font-weight: 400;
  font-size: 34px; color: var(--text); letter-spacing: -0.02em;
}
.diff-card-row .pct { font-size: 15px; color: var(--text-3); margin-left: 2px; }
.diff-card-delta {
  display: inline-flex; align-items: center; gap: 6px;
  margin-top: 10px;
  padding: 4px 12px; border-radius: 999px;
  font-family: 'Geist Mono', monospace; font-size: 11px;
  border: 1px solid var(--border);
  background: var(--surface-2); color: var(--text-2);
}
.diff-card-delta.green { color: #7DF0B4; background: var(--green-bg); border-color: var(--green-border); }
.diff-card-delta.red   { color: #FFB4AE; background: var(--red-bg);   border-color: var(--red-border); }

/* Flat KPI grid — the 4-card row that replaces the two-run gradient hero.
   4 cols desktop / 2 tablet / 1 mobile. Flat surface, no gradient fill. */
.kpi-flat-grid {
  display: grid;
  grid-template-columns: repeat(4, 1fr);
  gap: 14px;
  margin: 20px 0 22px;
}
@media (max-width: 1100px) { .kpi-flat-grid { grid-template-columns: repeat(2, 1fr); } }
@media (max-width: 640px)  { .kpi-flat-grid { grid-template-columns: 1fr; } }
.kpi-flat {
  background: var(--surface-2);
  border: 1px solid var(--border);
  border-radius: var(--radius-lg);
  padding: 20px 22px;
  display: flex; flex-direction: column; gap: 10px;
  min-height: 140px;
}
.kpi-flat-label {
  font-family: 'Geist Mono', monospace;
  font-size: 10px; letter-spacing: 0.14em;
  text-transform: uppercase;
  color: var(--text-3); font-weight: 600;
}
.kpi-flat-primary {
  display: flex; align-items: baseline; flex-wrap: wrap; gap: 8px;
  font-family: 'Instrument Serif', serif;
  font-size: 32px; line-height: 1; letter-spacing: -0.02em;
  color: var(--text);
}
.kpi-flat-primary .arrow {
  color: var(--text-3); font-size: 20px;
  font-family: 'Geist', sans-serif; margin: 0 2px;
}
.kpi-flat-primary .unit {
  font-size: 15px; color: var(--text-3);
  font-family: 'Geist Mono', monospace; margin-left: 1px;
}
.kpi-flat-sub {
  font-family: 'Geist Mono', monospace; font-size: 11px;
  color: var(--text-3); min-height: 14px;
}
.kpi-flat-delta {
  display: inline-flex; align-items: center; gap: 6px;
  padding: 4px 12px; border-radius: 999px;
  font-family: 'Geist Mono', monospace; font-size: 11px;
  border: 1px solid var(--border);
  background: rgba(255,255,255,0.04);
  color: var(--text-2);
  align-self: flex-start;
  margin-top: auto;
}
.kpi-flat-delta.green  { color: #7DF0B4; background: var(--green-bg); border-color: var(--green-border); }
.kpi-flat-delta.red    { color: #FFB4AE; background: var(--red-bg);   border-color: var(--red-border); }
.kpi-flat-delta.zero   { color: var(--text-2); background: rgba(255,255,255,0.04); border-color: var(--border); }
.kpi-flat-delta.nodata { color: var(--text-3); background: rgba(255,255,255,0.03); border-color: var(--border); }

/* Platform-breakdown table cells — compact 'base → curr' with a delta pill below. */
.pbd-cell { min-width: 140px; vertical-align: middle; }
.pbd-vals {
  font-family: 'Geist Mono', monospace; font-size: 13px;
  color: var(--text); white-space: nowrap;
}
.pbd-arrow { color: var(--text-3); margin: 0 4px; }
.pbd-delta { margin-top: 6px; }

/* Tables — dashboard-style rounded card wrapper */
.diff-table-wrap {
  background: linear-gradient(180deg, rgba(255,255,255,0.03) 0%, rgba(255,255,255,0.01) 100%);
  border: 1px solid var(--border);
  border-radius: var(--radius-lg);
  overflow: hidden;
  box-shadow: var(--shadow-md);
}
.diff-table { width: 100%; border-collapse: separate; border-spacing: 0; font-size: 13px; color: var(--text); }
.diff-table th {
  text-align: left; padding: 12px 16px;
  background: rgba(255,255,255,0.045);
  font-family: 'Geist Mono', monospace; font-size: 11px;
  color: var(--text-2); font-weight: 600;
  text-transform: uppercase; letter-spacing: 0.08em;
  border-bottom: 1px solid var(--border);
}
.diff-table td {
  padding: 12px 16px;
  border-bottom: 1px solid var(--border);
  vertical-align: middle;
}
.diff-table tr:last-child td { border-bottom: none; }
.diff-table tr:hover td { background: rgba(124,92,255,0.06); }
.diff-table td.num, .diff-table th.num {
  font-family: 'Geist Mono', monospace; text-align: right; white-space: nowrap;
}

.pill {
  display: inline-flex; align-items: center; gap: 6px;
  padding: 3px 10px; border-radius: 999px;
  font-family: 'Geist Mono', monospace; font-size: 11px;
  border: 1px solid var(--border); background: var(--surface-2);
  color: var(--text-2);
}
.pill.green { color: #7DF0B4; background: var(--green-bg); border-color: var(--green-border); }
.pill.red   { color: #FFB4AE; background: var(--red-bg);   border-color: var(--red-border); }
.pill.amber { color: #FFD98A; background: var(--amber-bg); border-color: var(--amber-border); }

/* Platform pill — brand-coloured dot + short code, matches dashboard */
.plat-pill {
  display: inline-flex; align-items: center; gap: 8px;
  font-size: 13px; color: var(--text);
}
.plat-dot { width: 9px; height: 9px; border-radius: 50%; box-shadow: 0 0 0 3px rgba(255,255,255,0.05); }
.plat-code {
  font-family: 'Geist Mono', monospace; font-size: 10px; color: var(--text-3);
  border: 1px solid var(--border); border-radius: 6px; padding: 1px 6px;
}

/* Empty state */
.empty {
  border: 1px dashed var(--border);
  border-radius: var(--radius-xl);
  padding: 60px 28px;
  text-align: center;
  color: var(--text-3);
  background: linear-gradient(180deg, rgba(255,255,255,0.02), rgba(255,255,255,0.005));
}
.empty h4 {
  color: var(--text); margin-bottom: 10px; font-size: 24px;
  font-weight: 400; font-family: 'Instrument Serif', serif; font-style: italic;
}

/* Segmented control styles live in core.ui.SHARED_CSS — single source of truth.
   Only the sidebar page-nav hider remains here (Streamlit's default link list). */
[data-testid="stSidebarNav"] { display: none; }

/* Section header with an inline widget on the right (sort selectbox etc.). */
.section-header.inline-header { display: block; }
.section-header.inline-header .section-title { margin-bottom: 4px; }

/* Keyword × Platform matrix — Latest-mode "Keyword level" section.
   Reuses .diff-table styling; adds uppercase header, centered platform cells,
   dotted row dividers, and a muted em-dash treatment. */
.kwmx-wrap { margin-top: 4px; }
.kwmx-tbl thead th {
  text-transform: uppercase;
  letter-spacing: 0.10em;
  font-size: 11px;
}
.kwmx-tbl thead th.kwmx-col { text-align: center; }
.kwmx-tbl thead th.kwmx-col-absent { color: var(--text-3); opacity: 0.5; }
.kwmx-tbl tbody td { text-align: center; }
.kwmx-tbl td.kwmx-kw {
  text-align: left;
  font-family: 'Geist Mono', ui-monospace, monospace;
  color: var(--text);
}
.kwmx-tbl th.kwmx-kw-head { text-align: left; }
.kwmx-tbl tbody tr td { border-bottom: 1px dashed var(--border); }
.kwmx-tbl tbody tr:last-child td { border-bottom: none; }
.kwmx-tbl .kwmx-muted {
  color: var(--text-3);
  font-family: 'Geist Mono', ui-monospace, monospace;
}
.kwmx-tbl .kwmx-pos {
  color: var(--text);
  font-family: 'Geist Mono', ui-monospace, monospace;
  font-variant-numeric: tabular-nums;
}
.kwmx-tbl .kwmx-meta {
  color: var(--text-2);
  font-family: 'Geist Mono', ui-monospace, monospace;
  font-variant-numeric: tabular-nums;
  text-align: right;
  white-space: nowrap;
}

/* Click-to-sort headers on the Keyword level table — form-button, not <a>. */
.kwmx-tbl thead th.sortable-th { user-select: none; padding: 0; }
.kwmx-tbl thead th.sortable-th .sort-form { display: block; margin: 0; padding: 0; }
.kwmx-tbl thead th.sortable-th .sort-btn {
  /* Reset native button chrome so the header reads as plain text. */
  appearance: none; -webkit-appearance: none;
  background: transparent;
  border: none;
  box-shadow: none;
  margin: 0;
  outline: none;
  width: 100%;
  /* Match the table's th typography. */
  display: inline-flex; align-items: center; gap: 4px;
  padding: 10px 14px;
  color: var(--text-2);
  cursor: pointer;
  text-transform: uppercase;
  letter-spacing: 0.10em;
  font-size: 11px;
  font-family: 'Geist Mono', ui-monospace, monospace;
  font-weight: 600;
  text-align: inherit;
  line-height: inherit;
}
.kwmx-tbl thead th.sortable-th .sort-btn:hover { color: var(--text); }
.kwmx-tbl thead th.sortable-th.active .sort-btn { color: var(--text); }
.kwmx-tbl thead th.sortable-th.num .sort-btn { justify-content: flex-end; }
.kwmx-tbl thead th.sortable-th .sort-label { -webkit-user-select: none; user-select: none; }
.kwmx-tbl thead th.sortable-th .caret {
  display: inline-block;
  width: 12px; text-align: center;
  font-size: 10px;
  color: var(--violet, #7C5CFF);
}
/* Kill any highlight (accidental text-selection double-click). */
.kwmx-tbl thead th.sortable-th::selection,
.kwmx-tbl thead th.sortable-th *::selection { background: transparent; color: inherit; }

/* Compare-mode Keyword level cells — pill → arrow → pill with tinted arrow. */
.kwmx-tbl .cmp-cell {
  display: inline-flex; align-items: center; gap: 6px;
  flex-wrap: wrap; justify-content: center;
}
.kwmx-tbl .cmp-arrow {
  font-family: 'Geist Mono', ui-monospace, monospace;
  font-size: 12px; font-weight: 700;
  padding: 0 2px;
}
.kwmx-tbl .cmp-arrow-green  { color: #7DF0B4; }
.kwmx-tbl .cmp-arrow-red    { color: #FFB4AE; }
.kwmx-tbl .cmp-arrow-muted  { color: var(--text-3); }
.kwmx-tbl .cmp-pill-em {
  color: var(--text-3);
  background: rgba(255,255,255,0.03);
  border-color: var(--border);
}
/* new/dropped tag inline with the keyword name */
.kwmx-side-tag {
  display: inline-block;
  margin-left: 8px;
  padding: 1px 8px; border-radius: 999px;
  background: rgba(255,255,255,0.06);
  border: 1px solid var(--border);
  color: var(--text-3);
  font-family: 'Geist Mono', ui-monospace, monospace;
  font-size: 10px; letter-spacing: 0.08em;
  text-transform: uppercase; font-weight: 600;
}
.kwmx-side-tag.red {
  color: #FFB4AE;
  background: rgba(234,67,53,0.14);
  border-color: rgba(234,67,53,0.36);
}

/* View URL popup — Citation view only. <details> in the keyword cell. */
.kwmx-kw-details { margin: 0; }
.kwmx-kw-details > summary {
  list-style: none; cursor: pointer;
  display: inline-flex; align-items: center; gap: 12px;
}
.kwmx-kw-details > summary::-webkit-details-marker { display: none; }
.kwmx-kw-name {
  font-family: 'Geist Mono', ui-monospace, monospace;
  color: var(--text);
}
.kwmx-view-url {
  font-family: 'Geist Mono', ui-monospace, monospace;
  font-size: 10px;
  letter-spacing: 0.06em;
  text-transform: uppercase;
  padding: 3px 9px;
  border-radius: 999px;
  border: 1px solid rgba(124,92,255,0.32);
  background: rgba(124,92,255,0.14);
  color: #E8DFFF;
  white-space: nowrap;
}
.kwmx-kw-details > summary:hover .kwmx-view-url { background: rgba(124,92,255,0.28); }
.kwmx-kw-details[open] > summary .kwmx-view-url { background: rgba(124,92,255,0.36); }
.kwmx-urls {
  margin: 10px 0 4px;
  padding: 12px 14px;
  background: rgba(255,255,255,0.03);
  border: 1px solid var(--border);
  border-radius: 10px;
  font-family: 'Geist', sans-serif;
  font-size: 12px;
  color: var(--text-2);
  min-width: 320px;
  max-width: 640px;
}
.kwmx-url-block { margin-bottom: 10px; }
.kwmx-url-block:last-child { margin-bottom: 0; }
.kwmx-url-plat {
  display: inline-block; margin-bottom: 4px;
  font-family: 'Geist Mono', ui-monospace, monospace;
  font-size: 10px;
  letter-spacing: 0.10em;
  text-transform: uppercase;
  color: var(--text-3);
  font-weight: 600;
}
.kwmx-url-links { display: flex; flex-direction: column; gap: 4px; }
.kwmx-url-links a {
  color: #C7C5EC;
  text-decoration: none;
  font-family: 'Geist Mono', ui-monospace, monospace;
  font-size: 11px;
  word-break: break-all;
}
.kwmx-url-links a:hover { color: var(--violet, #7C5CFF); text-decoration: underline; }
.kwmx-url-empty { color: var(--text-3); font-style: italic; }

/* Platform toggle — custom button row scoped by an anchor marker Streamlit
   renders just before it. Same pill/gradient aesthetic as the segmented
   controls above; supports per-option disabled + tooltip which segmented
   controls do not. */
.plat-toggle-anchor { display: none; }
[data-testid="stElementContainer"]:has(.plat-toggle-anchor) + [data-testid="stElementContainer"] [data-testid="stHorizontalBlock"],
[data-testid="stElementContainer"]:has(.plat-toggle-anchor) + [data-testid="stHorizontalBlock"] {
  background: rgba(255,255,255,0.04);
  border: 1px solid var(--border);
  border-radius: 12px;
  padding: 4px !important;
  gap: 4px !important;
  margin: 6px 0 18px !important;
  display: flex !important;
  align-items: stretch;
}
[data-testid="stElementContainer"]:has(.plat-toggle-anchor) + [data-testid="stElementContainer"] [data-testid="stHorizontalBlock"] > [data-testid="stColumn"],
[data-testid="stElementContainer"]:has(.plat-toggle-anchor) + [data-testid="stHorizontalBlock"] > [data-testid="stColumn"] {
  flex: 1 1 0 !important;
  padding: 0 !important;
  min-width: 0 !important;
  width: auto !important;
}
[data-testid="stElementContainer"]:has(.plat-toggle-anchor) + [data-testid="stElementContainer"] [data-testid="stHorizontalBlock"] button,
[data-testid="stElementContainer"]:has(.plat-toggle-anchor) + [data-testid="stHorizontalBlock"] button {
  width: 100% !important;
  background: transparent !important;
  color: var(--text-2) !important;
  box-shadow: none !important;
  border: none !important;
  border-radius: 9px !important;
  font-size: 12px !important;
  font-weight: 600 !important;
  padding: 8px 10px !important;
  gap: 8px !important;
  display: flex !important;
  align-items: center;
  justify-content: center;
}
[data-testid="stElementContainer"]:has(.plat-toggle-anchor) + [data-testid="stElementContainer"] [data-testid="stHorizontalBlock"] button p,
[data-testid="stElementContainer"]:has(.plat-toggle-anchor) + [data-testid="stHorizontalBlock"] button p {
  color: inherit !important;
  font-size: 12px !important;
  font-weight: 600 !important;
}
/* Coloured dot ::before each button — order matches the option list:
   1=All, 2=ChatGPT, 3=Gemini, 4=Google AIO, 5=Perplexity. */
[data-testid="stElementContainer"]:has(.plat-toggle-anchor) + [data-testid="stElementContainer"] [data-testid="stHorizontalBlock"] > [data-testid="stColumn"] button::before,
[data-testid="stElementContainer"]:has(.plat-toggle-anchor) + [data-testid="stHorizontalBlock"] > [data-testid="stColumn"] button::before {
  content: ""; display: inline-block;
  width: 9px; height: 9px; border-radius: 50%;
  flex-shrink: 0;
  background: rgba(255,255,255,0.2);
}
[data-testid="stElementContainer"]:has(.plat-toggle-anchor) + [data-testid="stElementContainer"] [data-testid="stHorizontalBlock"] > [data-testid="stColumn"]:nth-child(1) button::before,
[data-testid="stElementContainer"]:has(.plat-toggle-anchor) + [data-testid="stHorizontalBlock"] > [data-testid="stColumn"]:nth-child(1) button::before {
  background: linear-gradient(135deg, #7C5CFF 0%, #22D3EE 100%);
}
[data-testid="stElementContainer"]:has(.plat-toggle-anchor) + [data-testid="stElementContainer"] [data-testid="stHorizontalBlock"] > [data-testid="stColumn"]:nth-child(2) button::before,
[data-testid="stElementContainer"]:has(.plat-toggle-anchor) + [data-testid="stHorizontalBlock"] > [data-testid="stColumn"]:nth-child(2) button::before {
  background: #10A37F; /* ChatGPT */
}
[data-testid="stElementContainer"]:has(.plat-toggle-anchor) + [data-testid="stElementContainer"] [data-testid="stHorizontalBlock"] > [data-testid="stColumn"]:nth-child(3) button::before,
[data-testid="stElementContainer"]:has(.plat-toggle-anchor) + [data-testid="stHorizontalBlock"] > [data-testid="stColumn"]:nth-child(3) button::before {
  background: #4385F5; /* Gemini */
}
[data-testid="stElementContainer"]:has(.plat-toggle-anchor) + [data-testid="stElementContainer"] [data-testid="stHorizontalBlock"] > [data-testid="stColumn"]:nth-child(4) button::before,
[data-testid="stElementContainer"]:has(.plat-toggle-anchor) + [data-testid="stHorizontalBlock"] > [data-testid="stColumn"]:nth-child(4) button::before {
  background: #EA4335; /* Google AIO */
}
[data-testid="stElementContainer"]:has(.plat-toggle-anchor) + [data-testid="stElementContainer"] [data-testid="stHorizontalBlock"] > [data-testid="stColumn"]:nth-child(5) button::before,
[data-testid="stElementContainer"]:has(.plat-toggle-anchor) + [data-testid="stHorizontalBlock"] > [data-testid="stColumn"]:nth-child(5) button::before {
  background: #20808D; /* Perplexity */
}
/* Active state — Streamlit sets kind="primary" on the selected button. */
[data-testid="stElementContainer"]:has(.plat-toggle-anchor) + [data-testid="stElementContainer"] [data-testid="stHorizontalBlock"] button[kind="primary"],
[data-testid="stElementContainer"]:has(.plat-toggle-anchor) + [data-testid="stHorizontalBlock"] button[kind="primary"],
[data-testid="stElementContainer"]:has(.plat-toggle-anchor) + [data-testid="stElementContainer"] [data-testid="stHorizontalBlock"] button[data-testid="stBaseButton-primary"],
[data-testid="stElementContainer"]:has(.plat-toggle-anchor) + [data-testid="stHorizontalBlock"] button[data-testid="stBaseButton-primary"] {
  background: linear-gradient(135deg, #7C5CFF 0%, #22D3EE 100%) !important;
  color: #ffffff !important;
  box-shadow: 0 6px 16px rgba(124,92,255,0.35) !important;
}
/* Disabled — dim and stripped of hover cursor; tooltip comes from st.button help */
[data-testid="stElementContainer"]:has(.plat-toggle-anchor) + [data-testid="stElementContainer"] [data-testid="stHorizontalBlock"] button:disabled,
[data-testid="stElementContainer"]:has(.plat-toggle-anchor) + [data-testid="stHorizontalBlock"] button:disabled {
  opacity: 0.32 !important;
  cursor: not-allowed !important;
  background: transparent !important;
  color: var(--text-3) !important;
}

/* Streamlit dataframe theming — used for the trend metric table */
[data-testid="stDataFrame"] {
  border: 1px solid var(--border);
  border-radius: var(--radius-lg);
  overflow: hidden;
  background: rgba(255,255,255,0.02);
}
</style>
"""
st.markdown(CUSTOM_CSS, unsafe_allow_html=True)
st.markdown(SHARED_CSS, unsafe_allow_html=True)


# ============================================================
# Cached loaders — same pattern as app.py: cache by immutable ts.
# ============================================================
@st.cache_data(show_spinner=False)
def _cached_load_run(ts: float):
    return load_run(ts)


@st.cache_data(show_spinner=False)
def _cached_dataframes(rows_key: str, rows: list):
    return build_dataframes(rows)


# ============================================================
# Scoring helpers — thin wrappers around core.ui.compute_shape so this page
# can compose the per-run + per-platform breakdown it needs.
# ============================================================
def _run_metrics(rows: list[dict], terms: list[str]) -> dict:
    """Return { ...aggregate, by_platform, by_prompt, platforms, keyword_hits, domain_counts }.
    `by_prompt[kw]` is a full metric shape aggregated across all platforms for that keyword —
    the per-keyword table reads directly from it.
    """
    if not rows:
        base = compute_shape([], terms)
        return {
            **base,
            "n_prompts": 0, "n_platforms": 0,
            "platform_rates": {}, "keyword_hits": {},
            "domain_counts": {}, "platforms": [],
            "by_platform": {},
            "by_prompt": {},
            "by_platform_prompt": {},
        }
    dfs = _cached_dataframes(f"cmp::{id(rows)}::{len(rows)}", rows)
    results = dfs["results"]
    top_domains = dfs.get("top_domains", pd.DataFrame())

    all_rows = [row for _, row in results.iterrows()]
    agg = compute_shape(all_rows, terms)

    by_platform: dict[str, dict] = {}
    for p in sorted({row.get("platform", "") for row in all_rows}):
        plat_rows = [row for row in all_rows if row.get("platform") == p]
        by_platform[p] = compute_shape(plat_rows, terms)

    # Per-keyword aggregation — same shape as by_platform, keyed by prompt.
    # Feeds the KPI-scoped keyword table (KEYWORD | BASELINE | CURRENT | DELTA).
    by_prompt: dict[str, dict] = {}
    for prompt in sorted({row.get("prompt", "") for row in all_rows}):
        prompt_rows = [row for row in all_rows if row.get("prompt") == prompt]
        by_prompt[prompt] = compute_shape(prompt_rows, terms)

    # Per-(platform, prompt) — powers the "scope the keyword table to a
    # specific LLM" filter. Single pass over rows keeps it cheap.
    by_platform_prompt: dict[str, dict[str, dict]] = {}
    _pp_buckets: dict[tuple[str, str], list] = {}
    for row in all_rows:
        _pp_buckets.setdefault(
            (row.get("platform", ""), row.get("prompt", "")), []
        ).append(row)
    for (p, prompt), rs in _pp_buckets.items():
        by_platform_prompt.setdefault(p, {})[prompt] = compute_shape(rs, terms)

    platform_rates = {p: by_platform[p]["citation_rate"] for p in by_platform}

    keyword_hits: dict[str, dict[str, bool]] = {}
    for row in all_rows:
        prompt = row.get("prompt", "")
        p = row.get("platform", "")
        text = (row.get("response_text") or "").lower()
        has_mention = bool(terms) and any(t.lower() in text for t in terms if t)
        seen_row: set = set()
        url_hit = False
        for u in list(row.get("citations_list") or []) + list(row.get("source_urls_list") or []):
            if not u or u in seen_row:
                continue
            seen_row.add(u)
            if _is_tracked_url(u, terms):
                url_hit = True
                break
        keyword_hits.setdefault(prompt, {})[p] = has_mention or url_hit

    domain_counts = (
        {r["domain"]: int(r["citations"]) for _, r in top_domains.iterrows()}
        if not top_domains.empty else {}
    )

    return {
        **agg,
        "n_prompts": int(results["prompt"].nunique()) if not results.empty else 0,
        "n_platforms": int(results["platform"].nunique()) if not results.empty else 0,
        "platform_rates": platform_rates,
        "keyword_hits": keyword_hits,
        "domain_counts": domain_counts,
        "platforms": sorted(by_platform.keys()),
        "by_platform": by_platform,
        "by_prompt": by_prompt,
        "by_platform_prompt": by_platform_prompt,
    }


def _run_label(r: dict) -> str:
    """Single formatter for run labels — date + time, 24-hour.
    Reused everywhere a run is displayed: dropdowns, Latest label, tooltips."""
    return datetime.fromtimestamp(r["ts"]).strftime("%d %b %Y · %H:%M")


# ============================================================
# Compare-shape data layer.
#
# _compare_by({key: shape}, {key: shape}) returns:
#   { key: {
#       "citation_rate":    {baseline, current, delta, base_missing, curr_missing,
#                            base_absent, curr_absent, invert},
#       "mention_rate":     {...}, "keyword_position": {...}, "share_of_voice": {...}
#     }, ... }
# Same output whether keys are keywords, platforms, or anything else — this is
# the sole source of pre-computed deltas for the shared table component.
# ============================================================
_KPI_TO_KEY = {
    "citation_rate":    ("url_cited_rate", False),
    "mention_rate":     ("mention_rate",   False),
    "keyword_position": ("avg_position",   True),
    "share_of_voice":   ("sov",            False),
}


def _compare_by(base_dict: dict, curr_dict: dict) -> dict:
    keys = sorted(set(base_dict.keys()) | set(curr_dict.keys()))
    out: dict[str, dict] = {}
    for k in keys:
        base_m = base_dict.get(k)
        curr_m = curr_dict.get(k)
        row: dict[str, dict] = {}
        for kpi in ("citation_rate", "mention_rate", "keyword_position", "share_of_voice"):
            base_missing = _kpi_is_missing(base_m, kpi)
            curr_missing = _kpi_is_missing(curr_m, kpi)
            value_key, invert = _KPI_TO_KEY[kpi]
            base_val = None if base_missing else base_m[value_key]
            curr_val = None if curr_missing else curr_m[value_key]
            delta = (
                (curr_val - base_val)
                if (base_val is not None and curr_val is not None) else None
            )
            row[kpi] = {
                "baseline": base_val,
                "current": curr_val,
                "delta": delta,
                "base_missing": base_missing,
                "curr_missing": curr_missing,
                "base_absent": base_m is None,
                "curr_absent": curr_m is None,
                "invert": invert,
            }
        out[k] = row
    return out


# ============================================================
# Shared KPI compare table — one component drives both the per-keyword
# and per-platform sections. Toggle-scoped to a single KPI at a time.
# ============================================================
_KPI_TOGGLE_OPTIONS = [
    "Citation Rate", "Mention Rate", "Keyword Position", "Share of Voice",
]
_KPI_TOGGLE_MAP = {
    "Citation Rate":    "citation_rate",
    "Mention Rate":     "mention_rate",
    "Keyword Position": "keyword_position",
    "Share of Voice":   "share_of_voice",
}


def _kpi_compare_table_html(
    kpi_key: str,
    compare: dict,
    label_fn=None,
    pinned_keys: list[str] | None = None,
    header_label: str = "Item",
    empty_msg: str = "No rows to display.",
) -> str:
    """Render {header_label} | BASELINE | CURRENT | Δ for the selected KPI.
    - `label_fn(key) -> HTML`: how to render the row label. Defaults to escape.
    - `pinned_keys`: rendered first in given order, bypasses the sort. Used for
      the 'All platforms' aggregate row.
    - Non-pinned rows sort worst-delta-first (respecting invert for position).
    - One-sided rows render 'new' / 'dropped' badges instead of numeric deltas.
    """
    label_fn = label_fn or (lambda k: html.escape(k))
    pinned = list(pinned_keys or [])
    pinned_set = set(pinned)
    meta = KPI_META[kpi_key]
    unit = meta["unit"]
    invert = meta["invert"]

    def _fmt_val(v):
        if v is None:
            return "—"
        return f"{v:.1f}" if kpi_key == "keyword_position" else f"{v:.0f}%"

    def _sort_key(k: str):
        d = compare[k][kpi_key]
        if d["base_absent"] and d["curr_absent"]:
            return (2, k)
        if d["base_absent"] or d["curr_absent"] or d["delta"] is None:
            return (1, k)
        return (0, -d["delta"] if invert else d["delta"])

    def _render(k: str) -> str:
        d = compare[k][kpi_key]
        base_txt = _fmt_val(d["baseline"])
        curr_txt = _fmt_val(d["current"])

        if d["base_absent"] and d["curr_absent"]:
            badge = "<span class='pill'>no data</span>"
        elif d["base_absent"]:
            badge = "<span class='pill'>new</span>"
        elif d["curr_absent"]:
            badge = "<span class='pill'>dropped</span>"
        elif d["base_missing"] or d["curr_missing"] or d["delta"] is None:
            badge = "<span class='pill'>no data</span>"
        else:
            delta = d["delta"]
            if abs(delta) < 0.05:
                badge = f"<span class='pill'>+0.0 {html.escape(unit)}</span>"
            else:
                improved = (delta < 0) if invert else (delta > 0)
                cls = "green" if improved else "red"
                sign = "+" if delta > 0 else "−"
                badge = (
                    f"<span class='pill {cls}'>"
                    f"{sign}{abs(delta):.1f} {html.escape(unit)}</span>"
                )
        return (
            "<tr>"
            f"<td>{label_fn(k)}</td>"
            f"<td class='num'><span class='pbd-vals'>{base_txt}</span></td>"
            f"<td class='num'><span class='pbd-vals'>"
            f"<span class='pbd-arrow'>→</span> {curr_txt}"
            "</span></td>"
            f"<td class='num'>{badge}</td>"
            "</tr>"
        )

    other_keys = sorted(
        (k for k in compare.keys() if k not in pinned_set),
        key=_sort_key,
    )
    body_rows: list[str] = []
    for k in pinned:
        if k in compare:
            body_rows.append(_render(k))
    for k in other_keys:
        body_rows.append(_render(k))

    if not body_rows:
        return f"<div class='tag'>{html.escape(empty_msg)}</div>"

    return (
        "<div class='diff-table-wrap'><table class='diff-table'>"
        "<thead><tr>"
        f"<th>{html.escape(header_label)}</th>"
        "<th class='num'>Baseline</th>"
        "<th class='num'>Current</th>"
        "<th class='num'>Δ</th>"
        "</tr></thead>"
        f"<tbody>{''.join(body_rows)}</tbody></table></div>"
    )


# ============================================================
# Keyword × Platform matrix — Latest-mode "Keyword level" section.
#
# Fixed column order per the spec: ChatGPT, Gemini, Google AIO, Perplexity.
# Perplexity is flagged supportsPosition=false because its citation UI never
# exposes a stable per-source rank — null-position from other platforms means
# "cited but unranked" and must render differently.
# ============================================================
_KW_MTX_COLUMNS = ["chatgpt", "gemini", "google", "perplexity"]
_POSITION_UNSUPPORTED = {"perplexity"}


def _run_keyword_metadata(rows: list[dict]) -> tuple[list[str], dict[str, dict[str, str]]]:
    """Pull the keyword-metadata payload saved with the run rows. Every row in
    a run stores the same JSON blob, so we just read the first one. Falls
    back to the current sidebar's session-state (from `cmp_kw`) so metadata
    that hasn't been persisted yet still shows in the table."""
    for r in rows:
        blob = r.get("keyword_metadata")
        if not blob:
            continue
        try:
            payload = json.loads(blob)
        except (TypeError, json.JSONDecodeError):
            continue
        cols = payload.get("columns") or []
        by_kw = payload.get("by_keyword") or {}
        if cols:
            return list(cols), dict(by_kw)
    # Fallback — read from the Compare Reports sidebar's live session state.
    from core.gsheets import get_keyword_metadata  # noqa: E402
    return get_keyword_metadata("cmp_kw")


def _coerce_url_list(val) -> list[str]:
    """Handle both raw SQLite rows (citations stored as JSON string) and
    already-parsed dataframe rows (Python list). Silent on malformed JSON."""
    if isinstance(val, list):
        return val
    if isinstance(val, str) and val:
        try:
            parsed = json.loads(val)
            return parsed if isinstance(parsed, list) else []
        except json.JSONDecodeError:
            return []
    return []


def _keyword_platform_matrix(rows: list[dict], terms: list[str]) -> tuple[dict, set[str]]:
    """Build { keyword: { platform: {cited, position, mentioned, supportsPosition} } }.
    Also returns the set of platform IDs actually present in this run so the
    component can dim absent columns.

    Accepts either raw SQLite rows (citations/source_urls as JSON strings) or
    already-parsed dataframe rows — falls back to JSON parsing when the
    `_list` fields aren't present. That's the difference between reading
    zero URL citations and reading them all.
    """
    matrix: dict[str, dict[str, dict]] = {}
    platforms_present: set[str] = set()
    for row in rows:
        prompt = (row.get("prompt") or "").strip()
        platform = (row.get("platform") or "").strip()
        if not prompt or not platform:
            continue
        platforms_present.add(platform)
        text = (row.get("response_text") or "").lower()
        mentioned = bool(terms) and any(t.lower() in text for t in terms if t)

        # Prefer the parsed list fields; fall back to JSON strings on raw rows.
        cit_list = row.get("citations_list")
        if cit_list is None:
            cit_list = _coerce_url_list(row.get("citations"))
        src_list = row.get("source_urls_list")
        if src_list is None:
            src_list = _coerce_url_list(row.get("source_urls"))

        seen: set = set()
        combined: list[str] = []
        for u in list(cit_list) + list(src_list):
            if not u or u in seen:
                continue
            seen.add(u)
            combined.append(u)
        first_pos = None
        tracked_urls: list[dict] = []
        for i, u in enumerate(combined, start=1):
            if _is_tracked_url(u, terms):
                if first_pos is None:
                    first_pos = i
                tracked_urls.append({"url": u, "position": i})
        url_cited = first_pos is not None
        matrix.setdefault(prompt, {})[platform] = {
            "cited": url_cited or mentioned,
            "position": first_pos,
            "mentioned": mentioned,
            "supportsPosition": platform not in _POSITION_UNSUPPORTED,
            "tracked_urls": tracked_urls,
        }
    return matrix, platforms_present


# ============================================================
# Keyword × Platform COMPARE matrix — merges two runs into a single per-cell
# {baseline, current} shape so the shared component can render both modes.
# ============================================================
def _keyword_platform_compare_matrix(
    base_rows: list[dict],
    curr_rows: list[dict],
    terms: list[str],
) -> tuple[dict, set[str]]:
    """Return (merged, platforms_present).

    merged[keyword] = {
        "chatgpt":    {"baseline": <cell|None>, "current": <cell|None>, "supportsPosition": bool},
        "gemini":     {...}, "google": {...}, "perplexity": {...},
        "score":      {"baseline": int|None, "current": int|None, "total": int},
        "_baseline_present": bool,
        "_current_present": bool,
    }
    Cells are the same shape as _keyword_platform_matrix (cited, position,
    mentioned, tracked_urls). `None` means the (keyword,platform) was absent
    from that run.
    """
    base_matrix, base_plats = _keyword_platform_matrix(base_rows, terms)
    curr_matrix, curr_plats = _keyword_platform_matrix(curr_rows, terms)
    all_keys = set(base_matrix.keys()) | set(curr_matrix.keys())
    total_cols = len(_KW_MTX_COLUMNS)

    def _score(kw_cells: dict | None) -> int | None:
        if kw_cells is None:
            return None
        return sum(
            1 for p in _KW_MTX_COLUMNS
            if (c := kw_cells.get(p)) is not None and c.get("cited")
        )

    merged: dict[str, dict] = {}
    for kw in all_keys:
        base_kw = base_matrix.get(kw)
        curr_kw = curr_matrix.get(kw)
        row: dict = {
            "_baseline_present": kw in base_matrix,
            "_current_present": kw in curr_matrix,
        }
        for p in _KW_MTX_COLUMNS:
            row[p] = {
                "baseline": (base_kw or {}).get(p),
                "current":  (curr_kw or {}).get(p),
                "supportsPosition": p not in _POSITION_UNSUPPORTED,
            }
        row["score"] = {
            "baseline": _score(base_kw),
            "current":  _score(curr_kw),
            "total":    total_cols,
        }
        merged[kw] = row
    return merged, (base_plats | curr_plats)


# ============================================================
# Compare-mode cell renderers — separate from Latest renderers because the
# cell shape differs ({baseline, current, supportsPosition} vs the flat dict).
# ============================================================
def _cmp_pill_citation(x: dict | None) -> str:
    if x is None:
        return "<span class='pill cmp-pill-em'>—</span>"
    if x.get("cited"):
        pos = x.get("position")
        label = f"✓ #{pos}" if isinstance(pos, int) and pos > 0 else "✓ Text"
        return f"<span class='pill green'>{html.escape(label)}</span>"
    return "<span class='pill red'>✕ Missing</span>"


def _cmp_pill_mention(x: dict | None) -> str:
    if x is None:
        return "<span class='pill cmp-pill-em'>—</span>"
    if x.get("mentioned"):
        return "<span class='pill green'>Mentioned</span>"
    return "<span class='pill red'>Not Mentioned</span>"


def _cmp_cite_rank(x: dict | None):
    """Return a comparable rank for citation-view arrow tinting:
    None = missing/not cited, 'text' = cited unranked, or an int position."""
    if x is None or not x.get("cited"):
        return None
    pos = x.get("position")
    return pos if isinstance(pos, int) and pos > 0 else "text"


def _cmp_arrow_class_citation(b: dict | None, c: dict | None) -> str:
    br, cr = _cmp_cite_rank(b), _cmp_cite_rank(c)
    if br is None and cr is None:
        return "muted"
    if br is None:
        return "green"  # gained citation
    if cr is None:
        return "red"    # lost
    if br == cr:
        return "muted"
    if br == "text" and isinstance(cr, int):
        return "green"  # text → ranked
    if cr == "text" and isinstance(br, int):
        return "red"    # ranked → text-only
    if isinstance(br, int) and isinstance(cr, int):
        if cr < br:
            return "green"  # rank improved
        if cr > br:
            return "red"    # rank worsened
    return "muted"


def _cmp_arrow_class_mention(b: dict | None, c: dict | None) -> str:
    b_m = None if b is None else bool(b.get("mentioned"))
    c_m = None if c is None else bool(c.get("mentioned"))
    if b_m is None or c_m is None or b_m == c_m:
        return "muted"
    return "green" if (not b_m and c_m) else "red"


def _cell_citation_cmp(cell: dict | None) -> str:
    if cell is None:
        return "<td class='num kwmx-muted'>—</td>"
    b, c = cell.get("baseline"), cell.get("current")
    arrow = _cmp_arrow_class_citation(b, c)
    return (
        "<td class='num'><span class='cmp-cell'>"
        f"{_cmp_pill_citation(b)}"
        f"<span class='cmp-arrow cmp-arrow-{arrow}'>→</span>"
        f"{_cmp_pill_citation(c)}"
        "</span></td>"
    )


def _cell_mention_cmp(cell: dict | None) -> str:
    if cell is None:
        return "<td class='num kwmx-muted'>—</td>"
    b, c = cell.get("baseline"), cell.get("current")
    arrow = _cmp_arrow_class_mention(b, c)
    return (
        "<td class='num'><span class='cmp-cell'>"
        f"{_cmp_pill_mention(b)}"
        f"<span class='cmp-arrow cmp-arrow-{arrow}'>→</span>"
        f"{_cmp_pill_mention(c)}"
        "</span></td>"
    )


def _cell_position_cmp(cell: dict | None) -> str:
    if cell is None:
        return "<td class='num kwmx-muted'>—</td>"
    if not cell.get("supportsPosition", True):
        return "<td class='num kwmx-muted'>N/A</td>"
    b, c = cell.get("baseline"), cell.get("current")

    def _pos(x: dict | None):
        if x is None or not x.get("cited"):
            return None
        p = x.get("position")
        return p if isinstance(p, int) and p > 0 else None

    b_pos, c_pos = _pos(b), _pos(c)

    if b_pos is None and c_pos is None:
        return "<td class='num kwmx-muted'>—</td>"
    if b_pos is None:
        return (
            "<td class='num'><span class='cmp-cell'>"
            "<span class='kwmx-muted'>—</span>"
            "<span class='cmp-arrow cmp-arrow-green'>→</span>"
            f"<span class='kwmx-pos'>{c_pos}</span>"
            "<span class='pill'>new</span>"
            "</span></td>"
        )
    if c_pos is None:
        return (
            "<td class='num'><span class='cmp-cell'>"
            f"<span class='kwmx-pos'>{b_pos}</span>"
            "<span class='cmp-arrow cmp-arrow-red'>→</span>"
            "<span class='kwmx-muted'>—</span>"
            "<span class='pill red'>dropped</span>"
            "</span></td>"
        )
    delta = c_pos - b_pos  # inverted: negative = better
    if delta == 0:
        arrow_cls, badge = "muted", "<span class='pill'>+0 pos</span>"
    elif delta < 0:
        arrow_cls = "green"
        badge = f"<span class='pill green'>{delta} pos</span>"
    else:
        arrow_cls = "red"
        badge = f"<span class='pill red'>+{delta} pos</span>"
    return (
        "<td class='num'><span class='cmp-cell'>"
        f"<span class='kwmx-pos'>{b_pos}</span>"
        f"<span class='cmp-arrow cmp-arrow-{arrow_cls}'>→</span>"
        f"<span class='kwmx-pos'>{c_pos}</span>"
        f"{badge}"
        "</span></td>"
    )


def _score_cell_cmp(row_score: dict) -> str:
    b = row_score.get("baseline")
    c = row_score.get("current")
    total = row_score.get("total") or 1

    def _fmt(v):
        if v is None:
            return "—"
        pct = int(round(v * 100 / total))
        return f"{v}/{total} ({pct}%)"

    if b is None or c is None:
        return (
            "<td class='num'><span class='cmp-cell'>"
            f"<span class='kwmx-pos'>{_fmt(b)}</span>"
            "<span class='cmp-arrow cmp-arrow-muted'>→</span>"
            f"<span class='kwmx-pos'>{_fmt(c)}</span>"
            "<span class='pill'>no data</span>"
            "</span></td>"
        )
    b_pct = b * 100 / total
    c_pct = c * 100 / total
    delta = c_pct - b_pct
    if abs(delta) < 0.5:
        arrow_cls, badge = "muted", "<span class='pill'>+0 pts</span>"
    elif delta > 0:
        arrow_cls = "green"
        badge = f"<span class='pill green'>+{delta:.0f} pts</span>"
    else:
        arrow_cls = "red"
        badge = f"<span class='pill red'>{delta:.0f} pts</span>"
    return (
        "<td class='num'><span class='cmp-cell'>"
        f"<span class='kwmx-pos'>{_fmt(b)}</span>"
        f"<span class='cmp-arrow cmp-arrow-{arrow_cls}'>→</span>"
        f"<span class='kwmx-pos'>{_fmt(c)}</span>"
        f"{badge}"
        "</span></td>"
    )


# ============================================================
# Comparator factory — pure data layer, no rendering.
#
# get_comparator(column, direction, view) → cmp fn for sorted(key=cmp_to_key).
# - MISSING/N-A/absent values always sort last regardless of direction.
# - Ties always fall back to keyword ascending (case-insensitive).
# ============================================================
_MISSING_BUCKET = float("inf")


def _kw_bucket_val(kw: str, m: dict, col: str, view: str):
    """Return (bucket, primary_value). Bucket ordering:
       0 = best / active category, higher = later, _MISSING_BUCKET = always last.
    """
    if col in ("keyword", "", None):
        return (0, kw.casefold())

    if col == "score":
        hits = sum(
            1 for p in _KW_MTX_COLUMNS
            if (c := m.get(p)) is not None and c.get("cited")
        )
        return (0, hits)

    # Platform column.
    cell = m.get(col)
    if cell is None:
        return (_MISSING_BUCKET, 0)

    if view == "citation":
        # Ascending: ranked pos ↑ → cited text → not cited → absent (already MISSING)
        if cell.get("cited"):
            pos = cell.get("position")
            if isinstance(pos, int) and pos > 0:
                return (0, pos)
            return (1, 0)
        return (2, 0)

    if view == "mention":
        return ((0 if cell.get("mentioned") else 1), 0)

    # position view
    if not cell.get("supportsPosition") or not cell.get("cited"):
        return (_MISSING_BUCKET, 0)
    pos = cell.get("position")
    if not isinstance(pos, int) or pos <= 0:
        return (_MISSING_BUCKET, 0)
    return (0, pos)


def _str_cmp_asc(a: str, b: str) -> int:
    x, y = a.casefold(), b.casefold()
    return -1 if x < y else (1 if x > y else 0)


def get_kw_comparator(col: str, direction: str, view: str):
    """Return a two-arg cmp function suitable for functools.cmp_to_key."""
    desc = direction == "desc"

    def cmp(a: tuple[str, dict], b: tuple[str, dict]) -> int:
        kw_a, m_a = a
        kw_b, m_b = b
        ba, va = _kw_bucket_val(kw_a, m_a, col, view)
        bb, vb = _kw_bucket_val(kw_b, m_b, col, view)

        a_missing = ba == _MISSING_BUCKET
        b_missing = bb == _MISSING_BUCKET
        # Missing always last, regardless of direction.
        if a_missing != b_missing:
            return 1 if a_missing else -1
        if a_missing and b_missing:
            return _str_cmp_asc(kw_a, kw_b)

        # Different non-missing buckets — bucket order flips with direction.
        if ba != bb:
            if desc:
                return 1 if ba < bb else -1
            return -1 if ba < bb else 1

        # Same bucket — compare values, direction-aware.
        if va != vb:
            if isinstance(va, (int, float)) and isinstance(vb, (int, float)):
                if desc:
                    return 1 if va < vb else -1
                return -1 if va < vb else 1
            # string
            base = _str_cmp_asc(va, vb) if isinstance(va, str) else 0
            return -base if desc else base

        # Tie — keyword ascending, stable.
        return _str_cmp_asc(kw_a, kw_b)

    return cmp


def _cmp_cell_delta(cell: dict | None, view: str):
    """Return a numeric delta for compare-mode sorting.
    - Citation/Mention: 1 = lost (regression), -1 = gained, 0 = unchanged
    - Position: current_pos - baseline_pos (positive = worsened)
    - MISSING (no valid comparison) = _MISSING_BUCKET
    """
    if cell is None:
        return _MISSING_BUCKET
    b, c = cell.get("baseline"), cell.get("current")
    if view == "citation":
        b_cited = bool(b and b.get("cited"))
        c_cited = bool(c and c.get("cited"))
        if b is None and c is None:
            return _MISSING_BUCKET
        return int(b_cited) - int(c_cited)  # positive = lost
    if view == "mention":
        b_m = bool(b and b.get("mentioned"))
        c_m = bool(c and c.get("mentioned"))
        if b is None and c is None:
            return _MISSING_BUCKET
        return int(b_m) - int(c_m)
    # position
    if not cell.get("supportsPosition", True):
        return _MISSING_BUCKET
    if not (b and c and b.get("cited") and c.get("cited")):
        return _MISSING_BUCKET
    b_pos, c_pos = b.get("position"), c.get("position")
    if not (isinstance(b_pos, int) and isinstance(c_pos, int) and b_pos > 0 and c_pos > 0):
        return _MISSING_BUCKET
    return c_pos - b_pos  # positive = worsened


def get_kw_compare_comparator(col: str, direction: str, view: str):
    """Comparator for compare-mode Keyword level table.
    - col == '' → view-specific default: sort by biggest regression first.
    - col == 'keyword' → alphabetical, direction-aware.
    - col == 'score' → sort by score % delta.
    - platform col → sort by that cell's delta (view-aware).
    - Missing always last regardless of direction.
    - Ties → keyword ascending.
    """
    desc = direction == "desc"

    def _row_regression(m: dict) -> int:
        """Count platforms where this keyword lost citation/mention."""
        if view == "position":
            # Largest position increase (worst regression) across platforms.
            worst = -_MISSING_BUCKET
            for p in _KW_MTX_COLUMNS:
                d = _cmp_cell_delta(m.get(p), view)
                if d != _MISSING_BUCKET and d > worst:
                    worst = d
            return worst if worst != -_MISSING_BUCKET else -1
        # citation / mention
        return sum(
            1 for p in _KW_MTX_COLUMNS
            if _cmp_cell_delta(m.get(p), view) == 1
        )

    def cmp(a: tuple[str, dict], b: tuple[str, dict]) -> int:
        kw_a, m_a = a
        kw_b, m_b = b

        # DEFAULT: no column clicked → biggest regression first.
        if not col:
            ra, rb = _row_regression(m_a), _row_regression(m_b)
            if ra != rb:
                return -1 if ra > rb else 1  # bigger regression first
            return _str_cmp_asc(kw_a, kw_b)

        if col == "keyword":
            base = _str_cmp_asc(kw_a, kw_b)
            return -base if desc else base

        if col == "score":
            def _delta_pct(m: dict):
                s = m.get("score", {}) or {}
                bb, cc = s.get("baseline"), s.get("current")
                if bb is None or cc is None:
                    return None
                total = s.get("total") or 1
                return (cc - bb) * 100 / total
            da, db2 = _delta_pct(m_a), _delta_pct(m_b)
            if da is None and db2 is None:
                return _str_cmp_asc(kw_a, kw_b)
            if da is None:
                return 1
            if db2 is None:
                return -1
            if da != db2:
                if desc:
                    return -1 if da > db2 else 1
                return -1 if da < db2 else 1
            return _str_cmp_asc(kw_a, kw_b)

        # Platform column — sort by that cell's view-aware delta.
        da = _cmp_cell_delta(m_a.get(col), view)
        db2 = _cmp_cell_delta(m_b.get(col), view)
        a_missing = da == _MISSING_BUCKET
        b_missing = db2 == _MISSING_BUCKET
        if a_missing != b_missing:
            return 1 if a_missing else -1
        if a_missing:
            return _str_cmp_asc(kw_a, kw_b)
        if da != db2:
            if desc:
                return -1 if da > db2 else 1
            return -1 if da < db2 else 1
        return _str_cmp_asc(kw_a, kw_b)

    return cmp


# ============================================================
# Shared sortable-header renderer — used by Keyword and Platform tables.
# ============================================================
def _sortable_th_html(
    col_id: str,
    label: str,
    sort_col: str,
    sort_dir: str,
    params_for_col: dict,
    extra_cls: str = "",
) -> str:
    """One <th> as a form-button (no anchor). `params_for_col` is the dict of
    query params the button will submit via GET on click. Active column shows
    a ▲/▼ caret; inactive columns reserve the same space with &nbsp;."""
    hidden = "".join(
        f"<input type='hidden' name='{html.escape(str(k))}' "
        f"value='{html.escape(str(v))}'>"
        for k, v in params_for_col.items()
    )
    is_active = col_id == sort_col
    active_cls = " active" if is_active else ""
    if is_active:
        caret = f"<span class='caret'>{'▲' if sort_dir == 'asc' else '▼'}</span>"
    else:
        caret = "<span class='caret'>&nbsp;</span>"
    return (
        f"<th class='{extra_cls} sortable-th{active_cls}'>"
        "<form method='get' class='sort-form'>"
        f"{hidden}"
        "<button type='submit' class='sort-btn'>"
        f"<span class='sort-label'>{html.escape(label)}</span>"
        f"{caret}"
        "</button></form></th>"
    )


def _cycle_sort_params(
    col_id: str,
    cur_col: str,
    cur_dir: str,
    col_key: str,
    dir_key: str,
    all_params: dict,
) -> dict:
    """Compute the query-param dict for a header click. Cycle rule:
    inactive → asc; active-asc → desc; active-desc → clear (default)."""
    params = dict(all_params)
    if col_id == cur_col:
        if cur_dir == "asc":
            params[col_key] = col_id
            params[dir_key] = "desc"
        else:
            params.pop(col_key, None)
            params.pop(dir_key, None)
    else:
        params[col_key] = col_id
        params[dir_key] = "asc"
    return params


# ============================================================
# Platform breakdown comparator — same missing-last rule as keywords.
# ============================================================
_PLAT_SORT_FIELDS = {
    "citation_rate":    "url_cited_rate",
    "mention_rate":     "mention_rate",
    "keyword_position": "avg_position",
    "share_of_voice":   "sov",
}


def _plat_bucket_val(pid: str, m: dict | None, col: str):
    if col in ("platform", "", None):
        label = PLATFORM_LABEL_FOR_FILTER.get(pid, pid)
        return (0, label.casefold())
    field = _PLAT_SORT_FIELDS.get(col)
    if not field or m is None:
        return (_MISSING_BUCKET, 0)
    v = m.get(field)
    if v is None:
        return (_MISSING_BUCKET, 0)
    return (0, v)


def get_plat_comparator(col: str, direction: str):
    """Return a two-arg cmp fn for platform (pid, shape_dict) pairs."""
    desc = direction == "desc"

    def cmp(a: tuple[str, dict], b: tuple[str, dict]) -> int:
        pa, ma = a
        pb, mb = b
        ba, va = _plat_bucket_val(pa, ma, col)
        bb, vb = _plat_bucket_val(pb, mb, col)

        a_missing = ba == _MISSING_BUCKET
        b_missing = bb == _MISSING_BUCKET
        if a_missing != b_missing:
            return 1 if a_missing else -1
        if a_missing and b_missing:
            return _str_cmp_asc(pa, pb)

        if ba != bb:
            if desc:
                return 1 if ba < bb else -1
            return -1 if ba < bb else 1

        if va != vb:
            if isinstance(va, (int, float)) and isinstance(vb, (int, float)):
                if desc:
                    return 1 if va < vb else -1
                return -1 if va < vb else 1
            base = _str_cmp_asc(va, vb) if isinstance(va, str) else 0
            return -base if desc else base

        return _str_cmp_asc(pa, pb)

    return cmp


def _keyword_platform_table_html(
    view: str,
    matrix: dict,
    platforms_present: set[str],
    ordered_keys: list[str],
    sort_col: str = "",
    sort_dir: str = "asc",
    header_href=None,
    mode: str = "latest",
    metadata_columns: list[str] | None = None,
    metadata_by_kw: dict[str, dict[str, str]] | None = None,
) -> str:
    """Render KEYWORD | CHATGPT | GEMINI | GOOGLE AIO | PERPLEXITY | SCORE.
    `view` ∈ {'citation', 'mention', 'position'}.
    `mode`:
      'latest'  → cells are flat dicts (single run).
      'compare' → cells are {baseline, current, supportsPosition}; each cell
                  renders baseline pill → arrow → current pill (or numeric
                  delta for position / score).
    Row order comes from `ordered_keys` — the component does not sort.
    `header_href(col_id) -> params_dict` builds the click-to-sort URL for a
    header; the active column shows a ▲/▼ caret next to its label."""

    is_compare = mode == "compare"

    def _cell_citation(cell: dict | None) -> str:
        if cell is None:
            return "<td class='num kwmx-muted'>—</td>"
        if cell["cited"]:
            pos = cell["position"]
            label = f"✓ #{pos}" if isinstance(pos, int) and pos > 0 else "✓ Text"
            return f"<td class='num'><span class='pill green'>{html.escape(label)}</span></td>"
        return "<td class='num'><span class='pill red'>✕ Missing</span></td>"

    def _cell_mention(cell: dict | None) -> str:
        if cell is None:
            return "<td class='num kwmx-muted'>—</td>"
        if cell["mentioned"]:
            return "<td class='num'><span class='pill green'>Mentioned</span></td>"
        return "<td class='num'><span class='pill red'>Not Mentioned</span></td>"

    def _cell_position(cell: dict | None) -> str:
        if cell is None:
            return "<td class='num kwmx-muted'>—</td>"
        if not cell["supportsPosition"]:
            return "<td class='num kwmx-muted'>N/A</td>"
        if not cell["cited"]:
            return "<td class='num kwmx-muted'>—</td>"
        pos = cell["position"]
        if isinstance(pos, int) and pos > 0:
            return f"<td class='num kwmx-pos'>{pos}</td>"
        # Cited but no rank (e.g. text-only mention on a platform that supports positions).
        return "<td class='num kwmx-muted'>N/A</td>"

    if is_compare:
        renderer = {
            "citation": _cell_citation_cmp,
            "mention":  _cell_mention_cmp,
            "position": _cell_position_cmp,
        }.get(view, _cell_citation_cmp)
    else:
        renderer = {
            "citation": _cell_citation,
            "mention":  _cell_mention,
            "position": _cell_position,
        }.get(view, _cell_citation)

    # Header — each cell is a click-to-sort form-button (no anchor). Active
    # column shows a ▲/▼ caret; inactive columns reserve the same width.
    show_score = view != "position"

    def _sort_th(col_id: str, label: str, extra_cls: str = "") -> str:
        params = header_href(col_id) if header_href else {}
        return _sortable_th_html(col_id, label, sort_col, sort_dir, params, extra_cls)

    header_cells = []
    for p in _KW_MTX_COLUMNS:
        label = PLATFORM_LABEL_FOR_FILTER.get(p, p)
        extra = "num kwmx-col" + ("" if p in platforms_present else " kwmx-col-absent")
        header_cells.append(_sort_th(p, label, extra))
    if show_score:
        header_cells.append(_sort_th("score", "Score", "num kwmx-col"))

    # Build the keyword cell.
    # Latest citation view → <details> "View URL" popup with per-platform links.
    # Compare mode → plain keyword text + optional new/dropped tag (from the
    # merged matrix's _baseline_present / _current_present flags).
    def _keyword_cell(kw: str) -> str:
        if is_compare:
            row = matrix[kw]
            tag = ""
            b_present = row.get("_baseline_present", True)
            c_present = row.get("_current_present", True)
            if b_present and not c_present:
                tag = " <span class='kwmx-side-tag red'>dropped</span>"
            elif c_present and not b_present:
                tag = " <span class='kwmx-side-tag'>new</span>"
            return f"<td class='kwmx-kw'>{html.escape(kw)}{tag}</td>"
        if view != "citation":
            return f"<td class='kwmx-kw'>{html.escape(kw)}</td>"
        blocks: list[str] = []
        for p in _KW_MTX_COLUMNS:
            cell = matrix[kw].get(p)
            if cell is None:
                continue
            plat_label = PLATFORM_LABEL_FOR_FILTER.get(p, p)
            urls = cell.get("tracked_urls") or []
            if urls:
                links = "".join(
                    f"<a href='{html.escape(u['url'])}' target='_blank' rel='noopener'>"
                    f"#{u['position']} · {html.escape(u['url'])}"
                    "</a>"
                    for u in urls
                )
                blocks.append(
                    f"<div class='kwmx-url-block'>"
                    f"<span class='kwmx-url-plat'>{html.escape(plat_label)}</span>"
                    f"<div class='kwmx-url-links'>{links}</div>"
                    "</div>"
                )
            elif cell.get("mentioned"):
                blocks.append(
                    f"<div class='kwmx-url-block'>"
                    f"<span class='kwmx-url-plat'>{html.escape(plat_label)}</span>"
                    "<div class='kwmx-url-links kwmx-url-empty'>Text mention · no URL</div>"
                    "</div>"
                )
            else:
                blocks.append(
                    f"<div class='kwmx-url-block'>"
                    f"<span class='kwmx-url-plat'>{html.escape(plat_label)}</span>"
                    "<div class='kwmx-url-links kwmx-url-empty'>Not cited</div>"
                    "</div>"
                )
        body_html = "".join(blocks) if blocks else "<div class='kwmx-url-empty'>No tracked URLs cited.</div>"
        return (
            "<td class='kwmx-kw'>"
            "<details class='kwmx-kw-details'>"
            "<summary>"
            f"<span class='kwmx-kw-name'>{html.escape(kw)}</span>"
            "<span class='kwmx-view-url'>View URL ▾</span>"
            "</summary>"
            f"<div class='kwmx-urls'>{body_html}</div>"
            "</details>"
            "</td>"
        )

    # Body — order comes from `ordered_keys` (caller ran the comparator).
    # Score in Latest mode = platforms cited / total. Score in Compare mode
    # = baseline % → current % with a pts delta badge (from row['score']).
    total_cols = len(_KW_MTX_COLUMNS)

    body_rows: list[str] = []
    # Score is view-aware in BOTH modes:
    #   Citation view → count platforms where cell.cited is True
    #   Mention view  → count platforms where cell.mentioned is True
    #   Position view → score column is hidden (show_score is False)
    _score_field = "cited" if view == "citation" else "mentioned"
    for kw in ordered_keys:
        cells = [_keyword_cell(kw)]
        row = matrix[kw]
        hit_count = 0
        # For Compare mode, tally baseline + current independently so the
        # score cell can render N/4 → N/4. Falls back to None when the
        # keyword was absent from that run entirely.
        b_hits: int | None = 0
        c_hits: int | None = 0
        if is_compare:
            if not row.get("_baseline_present", True):
                b_hits = None
            if not row.get("_current_present", True):
                c_hits = None
        for p in _KW_MTX_COLUMNS:
            cell = row.get(p)
            cells.append(renderer(cell))
            if is_compare:
                if cell is not None:
                    b_cell = cell.get("baseline")
                    c_cell = cell.get("current")
                    if b_hits is not None and b_cell and b_cell.get(_score_field):
                        b_hits += 1
                    if c_hits is not None and c_cell and c_cell.get(_score_field):
                        c_hits += 1
            elif cell is not None and cell.get(_score_field):
                hit_count += 1
        if show_score:
            if is_compare:
                cells.append(_score_cell_cmp({
                    "baseline": b_hits,
                    "current":  c_hits,
                    "total":    total_cols,
                }))
            else:
                if hit_count == 0:
                    score_cls = "red"
                elif hit_count * 2 > total_cols:
                    score_cls = "green"
                else:
                    score_cls = "amber"
                pct = int(round(hit_count * 100 / total_cols)) if total_cols else 0
                cells.append(
                    "<td class='num'>"
                    f"<span class='pill {score_cls} kwmx-score'>"
                    f"{hit_count}/{total_cols} ({pct}%)"
                    "</span></td>"
                )
        body_rows.append(f"<tr>{''.join(cells)}</tr>")

    if not body_rows:
        empty_msg = (
            "No keywords in either run."
            if is_compare
            else "No keywords in this run."
        )
        return f"<div class='tag'>{html.escape(empty_msg)}</div>"

    keyword_header = _sort_th("keyword", "Keyword", "kwmx-kw-head")
    # Metadata columns (Search Volume, Difficulty, …) sit between Keyword
    # and the platform columns. Values are looked up from `metadata_by_kw`;
    # missing rows render an em dash.
    meta_cols = list(metadata_columns or [])
    meta_by_kw = metadata_by_kw or {}
    meta_header_html = "".join(
        f"<th class='num kwmx-col'>{html.escape(c)}</th>" for c in meta_cols
    )
    if meta_cols:
        new_body_rows: list[str] = []
        for kw, row_html in zip(ordered_keys, body_rows):
            row_meta = meta_by_kw.get(kw) or meta_by_kw.get(kw.lower()) or {}
            # the em-dash fallback is hoisted out: a backslash inside an f-string
            # expression is a SyntaxError before Python 3.12
            meta_dash = "<span class='kwmx-muted'>—</span>"
            meta_cells = "".join(
                "<td class='num kwmx-meta'>"
                f"{html.escape(str(row_meta.get(c, '')).strip()) or meta_dash}"
                "</td>"
                for c in meta_cols
            )
            # Insert metadata cells right after the keyword cell (first <td>).
            close_kw = row_html.find("</td>") + len("</td>")
            new_body_rows.append(row_html[:close_kw] + meta_cells + row_html[close_kw:])
        body_rows = new_body_rows
    return (
        "<div class='diff-table-wrap kwmx-wrap'><table class='diff-table kwmx-tbl'>"
        "<thead><tr>"
        f"{keyword_header}"
        f"{meta_header_html}"
        f"{''.join(header_cells)}"
        "</tr></thead>"
        f"<tbody>{''.join(body_rows)}</tbody></table></div>"
    )


# ============================================================
# Sidebar deleted — hide the rail + collapse chevron via CSS.
# ============================================================
st.markdown(
    "<style>"
    "section[data-testid='stSidebar'] { display: none !important; }"
    "[data-testid='stSidebarCollapsedControl'] { display: none !important; }"
    "</style>",
    unsafe_allow_html=True,
)


# ============================================================
# Top nav — three tools horizontally, above the page title.
# ============================================================
_top_nav = st.segmented_control(
    "Tool", ["LLM Tracker", "People Also Ask", "Report"],
    default="Report", label_visibility="collapsed",
    key="nav_top_cmp",
)
if _top_nav == "LLM Tracker":
    st.switch_page("app.py")
elif _top_nav == "People Also Ask":
    st.switch_page("pages/People_Also_Ask.py")


# ============================================================
# Header — title only, no chips, no description.
# ============================================================
st.markdown(
    '<h1 class="page-title">LLM Visibility</h1>',
    unsafe_allow_html=True,
)


# ============================================================
# Mode toggle — Latest (single-run snapshot) or Compare (two-run diff).
# Sits directly below the H1 so the report's primary control is visible
# immediately; Setup expander drops below.
# ============================================================
mode = st.segmented_control(
    "Mode", ["Latest", "Compare"],
    default="Latest", label_visibility="collapsed",
    key="cmp_mode",
)


# ============================================================
# Tracked terms come from settings.json (populated by the LLM Tracker's
# hero setup card). Scans only run on LLM Tracker — the Report tab is
# read-only, so the Input Source / Track Terms controls are gone.
# ============================================================
override_terms = get_tracked_terms()

runs = list_runs(limit=200)
if not runs:
    st.markdown(
        """
        <div class="empty">
          <h4>No runs stored yet</h4>
          <div>Switch to <b>LLM Tracker</b> in the sidebar, run a scan, then come back.</div>
        </div>
        """,
        unsafe_allow_html=True,
    )
    st.stop()


# ============================================================
# LATEST MODE — most recent run only, no pickers.
# ============================================================
if mode == "Latest":
    latest_run = runs[0]  # list_runs is newest-first.
    with st.spinner("Loading latest run…"):
        latest_rows = _cached_load_run(latest_run["ts"])
        curr = _run_metrics(latest_rows, override_terms)

    # Static date + time label above the report (single source: _run_label).
    st.markdown(
        "<div class='section-header'>"
        f"<span class='section-meta'>◆ Latest run · {html.escape(_run_label(latest_run))}</span>"
        "</div>",
        unsafe_allow_html=True,
    )

    # Single-run KPI grid — no baseline comparison, no delta pills.
    st.markdown(kpi_grid_html(None, curr, mode="current"), unsafe_allow_html=True)

    # Platform breakdown — click-to-sort headers via query params. Default =
    # canonical brand order with no active caret.
    st.markdown(
        """
        <div class="section-header">
          <h2 class="section-title">Platform breakdown</h2>
          <span class="section-meta">◆ One row per LLM · this run</span>
        </div>
        """,
        unsafe_allow_html=True,
    )

    latest_by_plat = curr.get("by_platform", {}) or {}
    canonical_ordered = [p for p in PLATFORM_ROW_ORDER if p in latest_by_plat]
    for p in sorted(latest_by_plat.keys()):
        if p not in canonical_ordered:
            canonical_ordered.append(p)

    _plat_sort_col = st.query_params.get("plat_sort_col", "")
    _plat_sort_dir = st.query_params.get("plat_sort_dir", "asc")
    if _plat_sort_dir not in ("asc", "desc"):
        _plat_sort_dir = "asc"

    if _plat_sort_col:
        _plat_items = [(p, latest_by_plat.get(p) or {}) for p in canonical_ordered]
        _plat_items.sort(
            key=cmp_to_key(get_plat_comparator(_plat_sort_col, _plat_sort_dir))
        )
        ordered_plats = [p for p, _ in _plat_items]
    else:
        ordered_plats = canonical_ordered

    def _plat_header_href(col_id: str) -> dict:
        return _cycle_sort_params(
            col_id, _plat_sort_col, _plat_sort_dir,
            "plat_sort_col", "plat_sort_dir",
            dict(st.query_params),
        )

    _plat_header_cells = "".join([
        _sortable_th_html("platform", "Platform",
                          _plat_sort_col, _plat_sort_dir,
                          _plat_header_href("platform")),
        _sortable_th_html("citation_rate", "Citation rate",
                          _plat_sort_col, _plat_sort_dir,
                          _plat_header_href("citation_rate"), "num"),
        _sortable_th_html("mention_rate", "Mention rate",
                          _plat_sort_col, _plat_sort_dir,
                          _plat_header_href("mention_rate"), "num"),
        _sortable_th_html("keyword_position", "Keyword position",
                          _plat_sort_col, _plat_sort_dir,
                          _plat_header_href("keyword_position"), "num"),
        _sortable_th_html("share_of_voice", "Share of voice",
                          _plat_sort_col, _plat_sort_dir,
                          _plat_header_href("share_of_voice"), "num"),
    ])

    _rows: list[str] = []
    for p in ordered_plats:
        pm = latest_by_plat.get(p)
        _rows.append(
            "<tr>"
            f"<td>{_plat_pill_html(p)}</td>"
            + kpi_single_cell(pm, "citation_rate")
            + kpi_single_cell(pm, "mention_rate")
            + kpi_single_cell(pm, "keyword_position")
            + kpi_single_cell(pm, "share_of_voice", show_bar=True)
            + "</tr>"
        )
    if _rows:
        st.markdown(
            "<div class='diff-table-wrap'><table class='diff-table kwmx-tbl'>"
            f"<thead><tr>{_plat_header_cells}</tr></thead>"
            f"<tbody>{''.join(_rows)}</tbody></table></div>",
            unsafe_allow_html=True,
        )
    else:
        st.markdown("<div class='tag'>No platforms in this run.</div>",
                    unsafe_allow_html=True)

    # ---------- Keyword level — click-to-sort table headers ----------
    # 48px gap above; single-run values only. Sort state lives in query params
    # so clicking a header link triggers a Streamlit rerun that preserves
    # session state (view toggle, run selection, etc.).
    st.markdown(
        """
        <div class="section-header" style="margin-top:48px;">
          <h2 class="section-title">Keyword level</h2>
          <span class="section-meta">◆ One row per keyword · this run</span>
        </div>
        """,
        unsafe_allow_html=True,
    )

    _KW_LEVEL_OPTIONS = ["Citation", "Mention", "Keyword Position"]
    _KW_LEVEL_MAP = {
        "Citation": "citation",
        "Mention":  "mention",
        "Keyword Position": "position",
    }
    _kw_level_label = st.segmented_control(
        "View",
        _KW_LEVEL_OPTIONS,
        default="Citation",
        label_visibility="collapsed",
        key="latest_kw_view",
    ) or "Citation"
    _kw_level_view = _KW_LEVEL_MAP[_kw_level_label]

    _kw_matrix, _kw_platforms_present = _keyword_platform_matrix(
        latest_rows, override_terms
    )

    # Sort state from URL. Empty col = default (keyword ascending, no caret).
    _kw_sort_col = st.query_params.get("kw_sort_col", "")
    _kw_sort_dir = st.query_params.get("kw_sort_dir", "asc")
    if _kw_sort_dir not in ("asc", "desc"):
        _kw_sort_dir = "asc"

    # Order rows via the shared comparator. Default (no col) = keyword asc.
    _kw_items = list(_kw_matrix.items())
    _kw_effective_col = _kw_sort_col or "keyword"
    _kw_items.sort(
        key=cmp_to_key(
            get_kw_comparator(_kw_effective_col, _kw_sort_dir, _kw_level_view)
        )
    )
    _kw_ordered_keys = [k for k, _ in _kw_items]

    # Header form-params builder — cycles asc → desc → default and preserves
    # other query params so unrelated state doesn't get wiped.
    def _kw_header_href(col_id: str) -> dict:
        return _cycle_sort_params(
            col_id, _kw_sort_col, _kw_sort_dir,
            "kw_sort_col", "kw_sort_dir",
            dict(st.query_params),
        )

    _md_cols, _md_by_kw = _run_keyword_metadata(latest_rows)
    st.markdown(
        _keyword_platform_table_html(
            _kw_level_view, _kw_matrix, _kw_platforms_present,
            ordered_keys=_kw_ordered_keys,
            sort_col=_kw_sort_col,
            sort_dir=_kw_sort_dir,
            header_href=_kw_header_href,
            metadata_columns=_md_cols,
            metadata_by_kw=_md_by_kw,
        ),
        unsafe_allow_html=True,
    )


# ============================================================
# COMPARE MODE — two-run diff.
# ============================================================
else:
    st.markdown(
        "<div class='section-header'><span class='section-meta'>◆ Pick two runs</span></div>",
        unsafe_allow_html=True,
    )

    # Cross-exclusion state. Kept as ts values so widget defaults track it
    # cleanly even after runs are added/deleted.
    _run_ts_set = {r["ts"] for r in runs}
    if st.session_state.get("cmp_current_ts") not in _run_ts_set:
        st.session_state["cmp_current_ts"] = runs[0]["ts"]
    if st.session_state.get("cmp_baseline_ts") not in _run_ts_set:
        st.session_state["cmp_baseline_ts"] = (
            runs[1]["ts"] if len(runs) > 1 else runs[0]["ts"]
        )
    # Force distinct — if both landed on the same run, bump baseline.
    if st.session_state["cmp_baseline_ts"] == st.session_state["cmp_current_ts"]:
        other = next(
            (r["ts"] for r in runs if r["ts"] != st.session_state["cmp_current_ts"]),
            st.session_state["cmp_current_ts"],
        )
        st.session_state["cmp_baseline_ts"] = other

    current_ts_state = st.session_state["cmp_current_ts"]
    baseline_ts_state = st.session_state["cmp_baseline_ts"]

    baseline_options = [r for r in runs if r["ts"] != current_ts_state]
    current_options = [r for r in runs if r["ts"] != baseline_ts_state]
    baseline_idx = next(
        (i for i, r in enumerate(baseline_options) if r["ts"] == baseline_ts_state), 0
    )
    current_idx = next(
        (i for i, r in enumerate(current_options) if r["ts"] == current_ts_state), 0
    )

    col_a, col_b = st.columns(2)
    with col_a:
        baseline_pick = st.selectbox(
            "Baseline (earlier)",
            options=baseline_options,
            index=baseline_idx,
            format_func=_run_label,
        )
    with col_b:
        current_pick = st.selectbox(
            "Current (later)",
            options=current_options,
            index=current_idx,
            format_func=_run_label,
        )

    # Persist ts values so the next rerun's exclusion logic picks them up.
    st.session_state["cmp_baseline_ts"] = baseline_pick["ts"]
    st.session_state["cmp_current_ts"] = current_pick["ts"]

    if baseline_pick["ts"] == current_pick["ts"]:
        st.info("Pick two different runs to see a diff.")
        st.stop()

    # Auto-swap so 'baseline' is genuinely earlier.
    if baseline_pick["ts"] > current_pick["ts"]:
        baseline_pick, current_pick = current_pick, baseline_pick

    with st.spinner("Loading runs…"):
        base_rows = _cached_load_run(baseline_pick["ts"])
        curr_rows = _cached_load_run(current_pick["ts"])
        base = _run_metrics(base_rows, override_terms)
        curr = _run_metrics(curr_rows, override_terms)

    # ---------- KPI grid — 4 flat cards, shared with the platform-scoped grid ----------
    st.markdown(kpi_grid_html(base, curr), unsafe_allow_html=True)

    # ---------- Platform breakdown — KPI-toggle table (same pattern as keywords) ----------
    st.markdown(
        """
        <div class="section-header">
          <h2 class="section-title">Platform breakdown</h2>
        </div>
        """,
        unsafe_allow_html=True,
    )

    _plat_selected_label = st.segmented_control(
        "Metric",
        _KPI_TOGGLE_OPTIONS,
        default="Citation Rate",
        label_visibility="collapsed",
        key="cmp_plat_kpi",
    ) or "Citation Rate"
    _plat_selected_kpi = _KPI_TOGGLE_MAP[_plat_selected_label]

    st.markdown(
        "<div class='section-header' style='margin:8px 0 10px;'>"
        f"<span class='section-meta'>◆ {html.escape(_plat_selected_label)} · baseline → current</span>"
        "</div>",
        unsafe_allow_html=True,
    )

    # Compose the compare dict: aggregate ("__all__") + per-platform slices.
    # "__all__" is pinned to the top; the rest sort worst-first inside the
    # shared component.
    _plat_base = {"__all__": base, **(base.get("by_platform", {}) or {})}
    _plat_curr = {"__all__": curr, **(curr.get("by_platform", {}) or {})}
    # Filter out platforms present in NEITHER run's by_platform (nothing to show).
    _plats_present = set(base.get("platforms") or []) | set(curr.get("platforms") or [])
    _plat_base = {k: v for k, v in _plat_base.items() if k == "__all__" or k in _plats_present}
    _plat_curr = {k: v for k, v in _plat_curr.items() if k == "__all__" or k in _plats_present}
    _plat_compare = _compare_by(_plat_base, _plat_curr)

    def _plat_label_fn(k: str) -> str:
        return "<b>All platforms</b>" if k == "__all__" else _plat_pill_html(k)

    st.markdown(
        _kpi_compare_table_html(
            _plat_selected_kpi,
            _plat_compare,
            label_fn=_plat_label_fn,
            pinned_keys=["__all__"],
            header_label="Platform",
            empty_msg="No platforms in either run.",
        ),
        unsafe_allow_html=True,
    )

    # ---------- Keyword level (Compare) — baseline → current per cell ----------
    # Mirrors the Latest section: same heading, toggle, columns, sort headers.
    # Cell shape switches to {baseline, current, supportsPosition} via the
    # merged compare matrix, and cells render `pill → arrow → pill` (or
    # numeric transitions for position / score) with the arrow tinted by
    # improvement / regression.
    st.markdown(
        """
        <div class="section-header" style="margin-top:48px;">
          <h2 class="section-title">Keyword level</h2>
          <span class="section-meta">◆ One row per keyword · baseline → current</span>
        </div>
        """,
        unsafe_allow_html=True,
    )

    _CMP_KW_VIEWS = ["Citation", "Mention", "Keyword Position"]
    _CMP_KW_VIEW_MAP = {
        "Citation": "citation",
        "Mention":  "mention",
        "Keyword Position": "position",
    }
    _cmp_kw_view_label = st.segmented_control(
        "View",
        _CMP_KW_VIEWS,
        default="Citation",
        label_visibility="collapsed",
        key="cmp_kw_view",
    ) or "Citation"
    _cmp_kw_view = _CMP_KW_VIEW_MAP[_cmp_kw_view_label]

    _cmp_matrix, _cmp_platforms = _keyword_platform_compare_matrix(
        base_rows, curr_rows, override_terms,
    )

    # Sort state from URL — namespaced separately from Latest's kw_sort_* so
    # switching between modes doesn't stomp each other's state.
    _cmp_kw_sort_col = st.query_params.get("cmp_kw_sort_col", "")
    _cmp_kw_sort_dir = st.query_params.get("cmp_kw_sort_dir", "asc")
    if _cmp_kw_sort_dir not in ("asc", "desc"):
        _cmp_kw_sort_dir = "asc"

    _cmp_items = list(_cmp_matrix.items())
    _cmp_items.sort(
        key=cmp_to_key(
            get_kw_compare_comparator(_cmp_kw_sort_col, _cmp_kw_sort_dir, _cmp_kw_view)
        )
    )
    _cmp_ordered = [k for k, _ in _cmp_items]

    def _cmp_kw_header_href(col_id: str) -> dict:
        return _cycle_sort_params(
            col_id, _cmp_kw_sort_col, _cmp_kw_sort_dir,
            "cmp_kw_sort_col", "cmp_kw_sort_dir",
            dict(st.query_params),
        )

    # Metadata columns for Compare mode: prefer the CURRENT run's payload,
    # fall back to Baseline's if Current has none, else session state.
    _cmp_md_cols, _cmp_md_by_kw = _run_keyword_metadata(curr_rows)
    if not _cmp_md_cols:
        _cmp_md_cols, _cmp_md_by_kw = _run_keyword_metadata(base_rows)

    st.markdown(
        _keyword_platform_table_html(
            _cmp_kw_view,
            _cmp_matrix,
            _cmp_platforms,
            ordered_keys=_cmp_ordered,
            sort_col=_cmp_kw_sort_col,
            sort_dir=_cmp_kw_sort_dir,
            header_href=_cmp_kw_header_href,
            mode="compare",
            metadata_columns=_cmp_md_cols,
            metadata_by_kw=_cmp_md_by_kw,
        ),
        unsafe_allow_html=True,
    )

    # ---------- Top domains gained / lost / shifted ----------
    st.markdown(
        """
        <div class="section-header" style="margin-top:32px;">
          <h2 class="section-title">Domains gained · lost · shifted</h2>
          <span class="section-meta">◆ Distinct citations per domain across all platforms</span>
        </div>
        """,
        unsafe_allow_html=True,
    )
    base_dom = base["domain_counts"]
    curr_dom = curr["domain_counts"]
    all_dom = set(base_dom) | set(curr_dom)
    dom_rows: list[dict] = []
    for d in all_dom:
        b = base_dom.get(d, 0)
        c = curr_dom.get(d, 0)
        dom_rows.append({"domain": d, "baseline": b, "current": c, "delta": c - b})

    gained = [r for r in dom_rows if r["baseline"] == 0 and r["current"] > 0]
    lost = [r for r in dom_rows if r["current"] == 0 and r["baseline"] > 0]
    shifted = [r for r in dom_rows if r["baseline"] > 0 and r["current"] > 0 and r["delta"] != 0]
    gained.sort(key=lambda r: r["current"], reverse=True)
    lost.sort(key=lambda r: r["baseline"], reverse=True)
    shifted.sort(key=lambda r: abs(r["delta"]), reverse=True)

    def _domain_table(rows: list[dict], empty_msg: str, label_delta: bool = True) -> str:
        if not rows:
            return f"<div class='tag' style='display:inline-block;'>{empty_msg}</div>"
        body = []
        for r in rows[:25]:
            d = r["delta"]
            if label_delta:
                if d > 0:
                    dh = f"<span class='pill green'>↑ +{d}</span>"
                elif d < 0:
                    dh = f"<span class='pill red'>↓ {d}</span>"
                else:
                    dh = "<span class='pill'>→ 0</span>"
            else:
                dh = ""
            body.append(
                "<tr>"
                f"<td>{html.escape(r['domain'])}</td>"
                f"<td class='num'>{r['baseline']}</td>"
                f"<td class='num'>{r['current']}</td>"
                f"<td class='num'>{dh}</td>"
                "</tr>"
            )
        return (
            "<div class='diff-table-wrap'><table class='diff-table'>"
            "<thead><tr><th>Domain</th>"
            "<th class='num'>Baseline</th><th class='num'>Current</th>"
            "<th class='num'>Δ</th></tr></thead>"
            f"<tbody>{''.join(body)}</tbody></table></div>"
        )

    g_col, l_col = st.columns(2)
    with g_col:
        st.markdown("**Newly cited** — appear in current, not baseline")
        st.markdown(_domain_table(gained, "Nothing new.", label_delta=False), unsafe_allow_html=True)
    with l_col:
        st.markdown("**Dropped** — appeared in baseline, gone in current")
        st.markdown(_domain_table(lost, "Nothing dropped.", label_delta=False), unsafe_allow_html=True)

    st.markdown("**Rank shifts** — in both runs, citation count changed")
    st.markdown(
        _domain_table(shifted, "No overlap between the two runs."),
        unsafe_allow_html=True,
    )


# ============================================================
# Footer
# ============================================================
st.markdown(
    "<div style='margin-top:56px; padding-top:16px; border-top:1px solid var(--border); "
    "color:var(--text-3); font-size:11px; font-family:Geist Mono, monospace;'>"
    "Citerion · Compare Reports · runs pulled from data/results.db"
    "</div>",
    unsafe_allow_html=True,
)
