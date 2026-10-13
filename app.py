from __future__ import annotations

import asyncio
import html
import os
import io
import json
import re
import sys
import time
import webbrowser
from collections import Counter
from pathlib import Path
from urllib.parse import urlparse

import pandas as pd
import plotly.express as px
import streamlit as st
import streamlit.components.v1 as components

from datetime import datetime

from scrapers import SCRAPERS, PLATFORMS
from scrapers.base import ScrapeResult
from core.aggregator import build_dataframes
from core.storage import save_results, load_recent, clear_all, list_runs, load_run, delete_run
from core.settings import get_tracked_terms, set_tracked_terms, parse_terms_input
from core.export import build_workbook
from core.gsheets import render_input_source_sidebar
from core.auth import require_login, logout, current_user, user_badge_html
from core.ui import (
    PLATFORM_LABEL as _UI_PLATFORM_LABEL,
    PLATFORM_ROW_ORDER as _UI_PLATFORM_ROW_ORDER,
    compute_shape as _ui_compute_shape,
    kpi_grid_html as _ui_kpi_grid_html,
    kpi_single_cell as _ui_kpi_single_cell,
    plat_pill_html as _ui_plat_pill_html,
    SHARED_CSS as _UI_SHARED_CSS,
)

STANDALONE_DASHBOARD = Path(__file__).resolve().parent / "dashboard" / "ai-citation-tracker.html"


# ============================================================
# Cached data loaders — biggest perf win. SQLite reads + the
# build_dataframes pipeline get rerun on EVERY widget interaction
# otherwise, which is most of the perceived slowness.
# ============================================================
@st.cache_data(show_spinner=False)
def _cached_load_run(ts: float):
    # `ts` is immutable for a given run, so caching by ts is safe and never
    # stale. The fresh-scrape path picks the new ts via pending_run_ts.
    return load_run(ts)


@st.cache_data(show_spinner=False)
def _cached_dataframes(rows_key: str, rows: list):
    """Cache build_dataframes by a stable string key derived from row ts."""
    return build_dataframes(rows)


# NOTE: list_runs and load_recent are NOT cached anymore. Both are
# sub-millisecond SQLite reads, and caching them across reruns created
# invalidation bugs where freshly-saved scrapes wouldn't appear in the
# Scan history dropdown until Streamlit was restarted.
def _cache_token() -> float:
    """Retained for `_cached_dataframes` cache key — bumped after writes so
    df builds re-run when underlying rows change."""
    return st.session_state.get("_data_token", 0.0)


def _bump_token() -> None:
    st.session_state["_data_token"] = st.session_state.get("_data_token", 0.0) + 1


# Playwright on Windows needs the Proactor loop policy.
if sys.platform == "win32":
    asyncio.set_event_loop_policy(asyncio.WindowsProactorEventLoopPolicy())


st.set_page_config(
    page_title="Citerion",
    page_icon="◆",
    layout="wide",
    initial_sidebar_state="collapsed",
)

# Login gate. Must come before anything else renders: require_login calls
# st.stop() when nobody is signed in, and anything drawn above it would be
# visible to a stranger. The app drives paid proxies and signed-in LLM
# accounts, so an open port 8000 is someone else spending your credits.
_user = require_login(st)

# Kill the sidebar entirely — the tool nav moved to the top bar, and the
# LLM Tracker page has no sidebar widgets left. Hides both the collapsed
# rail and the expand chevron so nothing left-side is visible.
st.markdown(
    "<style>"
    "section[data-testid='stSidebar'] { display: none !important; }"
    "[data-testid='stSidebarCollapsedControl'] { display: none !important; }"
    "</style>",
    unsafe_allow_html=True,
)

PLATFORM_LABEL = {
    "chatgpt": "ChatGPT",
    "gemini": "Gemini",
    "perplexity": "Perplexity",
    "google": "Google AIO",
    "claude": "Claude",
}
PLATFORM_LETTER = {
    "chatgpt": "G",   # the GPT marker the design uses
    "gemini": "M",
    "perplexity": "P",
    "google": "O",    # AI Overview
    "claude": "C",
}

CUSTOM_CSS = """
<style>
@import url('https://fonts.googleapis.com/css2?family=Instrument+Serif:ital@0;1&family=Geist:wght@300;400;500;600;700&family=Geist+Mono:wght@400;500;600&display=swap');
@import url('https://fonts.googleapis.com/css2?family=Material+Symbols+Rounded:opsz,wght,FILL,GRAD@20..48,100..700,0..1,-50..200&family=Material+Symbols+Outlined:opsz,wght,FILL,GRAD@20..48,100..700,0..1,-50..200&display=block');
@import url('https://fonts.googleapis.com/icon?family=Material+Icons&display=block');

/* Material Symbols icon ligatures — Streamlit renders chevrons, hamburger
   toggles, expander arrows, etc. as <span> with these classes containing
   ligature text (e.g. "keyboard_double_arrow_right", "expand_more"). When
   the icon font fails to load, the raw ligature text leaks through. Pinning
   the font-family explicitly + giving spans a fixed visual size keeps the
   glyphs rendering instead of words. */
.material-symbols-rounded,
.material-symbols-outlined,
.material-icons,
[data-testid="stIconMaterial"],
span[class*="material-symbols"] {
  font-family: 'Material Symbols Rounded', 'Material Symbols Outlined', 'Material Icons' !important;
  font-weight: normal !important;
  font-style: normal !important;
  font-variation-settings: 'FILL' 0, 'wght' 400, 'GRAD' 0, 'opsz' 24 !important;
  line-height: 1 !important;
  letter-spacing: normal !important;
  text-transform: none !important;
  display: inline-block !important;
  white-space: nowrap !important;
  word-wrap: normal !important;
  direction: ltr !important;
  -webkit-font-feature-settings: 'liga' !important;
  font-feature-settings: 'liga' !important;
  -webkit-font-smoothing: antialiased !important;
  font-size: 20px !important;
  vertical-align: middle;
}

:root {
  --bg: #08080a;
  --bg-2: #0d0d10;
  --surface: rgba(255,255,255,0.025);
  --surface-2: rgba(255,255,255,0.045);
  --surface-3: rgba(255,255,255,0.07);
  --border: rgba(255,255,255,0.08);
  --border-strong: rgba(255,255,255,0.16);
  --text: #f5f5f7;
  --text-2: #a1a1aa;
  --text-3: #6b6b73;
  --accent: #8b5cf6;
  --accent-2: #06b6d4;
  --accent-gradient: linear-gradient(135deg, #8b5cf6 0%, #06b6d4 100%);
  --green: #34d399;
  --green-bg: rgba(52,211,153,0.12);
  --green-border: rgba(52,211,153,0.32);
  --red: #f87171;
  --red-bg: rgba(248,113,113,0.12);
  --red-border: rgba(248,113,113,0.32);
  --amber: #fbbf24;
  --amber-bg: rgba(251,191,36,0.12);
  --amber-border: rgba(251,191,36,0.32);
  --radius: 8px;
  --radius-lg: 14px;
  --shadow-sm: 0 1px 2px rgba(0,0,0,0.4);
  --shadow-md: 0 4px 16px rgba(0,0,0,0.4);
  --shadow-lg: 0 8px 32px rgba(0,0,0,0.55);
  --glow: 0 0 24px rgba(139,92,246,0.18);
}

/* App background — deep neutral + subtle violet/cyan glow mesh */
html, body, .stApp {
  background: var(--bg) !important;
  background-image:
    radial-gradient(at 18% 0%, rgba(139,92,246,0.10) 0px, transparent 50%),
    radial-gradient(at 82% 100%, rgba(6,182,212,0.07) 0px, transparent 50%) !important;
  background-attachment: fixed !important;
  color: var(--text) !important;
  font-family: 'Geist', -apple-system, BlinkMacSystemFont, sans-serif !important;
  -webkit-font-smoothing: antialiased;
  -moz-osx-font-smoothing: grayscale;
}
.stApp p, .stApp span, .stApp label {
  font-family: 'Geist', -apple-system, BlinkMacSystemFont, sans-serif;
  color: var(--text);
}
.stMarkdown, .stMarkdown p, .stMarkdown li { color: var(--text) !important; }
header[data-testid="stHeader"] { background: transparent; }
.block-container { padding-top: 1.4rem; max-width: 1320px; }

/* Inputs */
.stTextInput input, .stTextArea textarea {
  background: var(--surface-2) !important;
  background-color: var(--surface-2) !important;
  color: var(--text) !important;
  -webkit-text-fill-color: var(--text) !important;
  border: 1px solid var(--border) !important;
  border-radius: 8px !important;
  font-family: 'Geist', sans-serif !important;
  font-size: 13px !important;
  caret-color: var(--accent) !important;
  transition: border-color 0.15s ease, box-shadow 0.15s ease;
}
.stTextInput input:focus, .stTextArea textarea:focus {
  border-color: var(--accent) !important;
  box-shadow: 0 0 0 3px rgba(139,92,246,0.18) !important;
  outline: none !important;
}
/* Streamlit textarea wrapper (baseweb) — keep transparent so the
   inner <textarea> background shows through instead of a white shell. */
[data-testid="stTextArea"],
[data-testid="stTextArea"] > div,
[data-testid="stTextArea"] [data-baseweb="textarea"],
[data-testid="stTextArea"] [data-baseweb="base-input"] {
  background: transparent !important;
  background-color: transparent !important;
}
/* Hide Grammarly browser-extension overlays that paint the field white
   and inject badges/counters on top of the textarea. */
grammarly-extension,
grammarly-desktop-integration,
grammarly-popups,
[data-grammarly-shadow-root="true"],
[data-gramm_id],
.gr-textarea-btn,
g-button[role="button"][aria-label*="Grammarly"] {
  display: none !important;
  visibility: hidden !important;
  pointer-events: none !important;
}
.stSelectbox div[data-baseweb="select"] > div {
  background: var(--surface-2) !important;
  color: var(--text) !important;
  border: 1px solid var(--border) !important;
  border-radius: 8px !important;
}
.stSelectbox div[data-baseweb="select"] svg { color: var(--text-2) !important; fill: var(--text-2) !important; }

/* Baseweb popovers (dropdown menus) — force dark */
div[data-baseweb="popover"] [role="listbox"],
div[data-baseweb="popover"] ul {
  background: #131318 !important;
  border: 1px solid var(--border) !important;
  color: var(--text) !important;
}
div[data-baseweb="popover"] [role="option"] {
  color: var(--text) !important;
  background: transparent !important;
}
div[data-baseweb="popover"] [role="option"]:hover,
div[data-baseweb="popover"] [role="option"][aria-selected="true"] {
  background: var(--surface-3) !important;
}

/* Placeholders */
.stTextInput input::placeholder,
.stTextArea textarea::placeholder,
section[data-testid="stSidebar"] textarea::placeholder,
section[data-testid="stSidebar"] input[type="text"]::placeholder {
  color: var(--text-3) !important;
  -webkit-text-fill-color: var(--text-3) !important;
  opacity: 0.7 !important;
}

/* Checkbox / radio labels — readable on dark */
[data-testid="stCheckbox"] label,
[data-testid="stCheckbox"] label p,
[data-testid="stCheckbox"] label span,
[data-testid="stRadio"] label,
[data-testid="stRadio"] label p,
[data-testid="stRadio"] label span {
  color: var(--text) !important;
  -webkit-text-fill-color: var(--text) !important;
  font-size: 13px !important;
  font-weight: 400 !important;
}

/* Buttons */
.stButton button {
  background: linear-gradient(135deg, #8b5cf6 0%, #06b6d4 100%) !important;
  color: #ffffff !important;
  border: none !important;
  border-radius: 8px !important;
  font-family: 'Geist', sans-serif !important;
  font-size: 13px !important;
  font-weight: 500 !important;
  height: 36px;
  box-shadow: 0 4px 16px rgba(0,0,0,0.4), 0 0 24px rgba(139,92,246,0.20);
  transition: transform 0.15s ease, box-shadow 0.15s ease, filter 0.15s ease;
}
.stButton button:hover {
  transform: translateY(-1px);
  filter: brightness(1.08);
  box-shadow: 0 8px 32px rgba(0,0,0,0.55), 0 0 32px rgba(139,92,246,0.35);
}
.stButton button:active { transform: translateY(0); }
.stButton button p { color: #ffffff !important; }
.stDownloadButton button {
  background: var(--surface-2) !important;
  color: var(--text) !important;
  border: 1px solid var(--border) !important;
  box-shadow: none;
}
.stDownloadButton button:hover {
  background: var(--surface-3) !important;
  border-color: var(--border-strong) !important;
  filter: none;
  transform: none;
  box-shadow: none;
}
.stDownloadButton button p { color: var(--text) !important; }

/* File uploader */
[data-testid="stFileUploader"] section {
  background: var(--surface) !important;
  border: 1px dashed var(--border) !important;
  border-radius: 10px !important;
  padding: 12px !important;
  transition: border-color 0.15s ease, background 0.15s ease;
}
[data-testid="stFileUploader"] section:hover {
  border-color: var(--accent) !important;
  background: var(--surface-2) !important;
}
[data-testid="stFileUploader"] button {
  background: var(--surface-2) !important;
  color: var(--text) !important;
  border: 1px solid var(--border) !important;
  height: 32px !important;
  padding: 0 14px !important;
  font-size: 12px !important;
  border-radius: 6px !important;
  min-width: 100% !important;
  font-weight: 400 !important;
  box-shadow: none !important;
  text-shadow: none !important;
  -webkit-font-smoothing: antialiased !important;
}
[data-testid="stFileUploader"] button:hover {
  background: var(--surface-3) !important;
  box-shadow: none !important;
  transform: none !important;
  filter: none !important;
}
[data-testid="stFileUploader"] button * {
  font-weight: 400 !important;
  text-shadow: none !important;
  color: var(--text) !important;
}
[data-testid="stFileUploader"] button svg,
[data-testid="stFileUploader"] button [data-testid="stIconMaterial"],
[data-testid="stFileUploader"] button [data-testid*="Icon"] {
  display: none !important;
}
[data-testid="stFileUploader"] small { color: var(--text-3) !important; font-size: 11px !important; }
[data-testid="stFileUploaderDropzoneInstructions"] span { color: var(--text-2) !important; font-size: 11px !important; }

hr { border-color: var(--border) !important; }

.stExpander {
  border: 1px solid var(--border) !important;
  border-radius: 10px !important;
  background: var(--surface) !important;
  backdrop-filter: blur(8px);
}
.stExpander summary, .stExpander summary p, .stExpander summary span {
  font-family: 'Geist' !important;
  font-size: 13px !important;
  color: var(--text) !important;
}

/* Streamlit's expander chevron renders as a span containing the Material
   ligature `keyboard_arrow_right`. When the remote icon font is slow or
   blocked, the raw word leaks out and stacks against the "Danger zone"
   title. Hide the ligature text outright (font-size:0 collapses it) and
   draw our own chevron with a Unicode glyph in a CSS pseudo-element so the
   icon no longer depends on a remote font. */
.stExpander summary [data-testid="stIconMaterial"],
.stExpander summary [data-testid="stExpanderToggleIcon"],
.stExpander summary span.material-symbols-rounded,
.stExpander summary span.material-symbols-outlined,
.stExpander summary span.material-icons {
  font-size: 0 !important;
  color: transparent !important;
  line-height: 0 !important;
  position: relative;
  width: 14px;
  height: 14px;
  display: inline-flex !important;
  align-items: center;
  justify-content: center;
}
.stExpander summary [data-testid="stIconMaterial"]::before,
.stExpander summary [data-testid="stExpanderToggleIcon"]::before,
.stExpander summary span.material-symbols-rounded::before,
.stExpander summary span.material-symbols-outlined::before,
.stExpander summary span.material-icons::before {
  content: '\\203A'; /* › single right-pointing angle quote */
  font-family: 'Geist', sans-serif !important;
  font-size: 16px !important;
  font-weight: 500 !important;
  color: var(--text-2) !important;
  line-height: 1 !important;
  display: inline-block;
  transition: transform 0.15s ease, color 0.15s ease;
}
.stExpander[open] summary [data-testid="stIconMaterial"]::before,
.stExpander[open] summary [data-testid="stExpanderToggleIcon"]::before,
.stExpander[open] summary span.material-symbols-rounded::before,
.stExpander[open] summary span.material-symbols-outlined::before,
.stExpander[open] summary span.material-icons::before,
details[open].stExpander summary [data-testid="stIconMaterial"]::before,
details[open].stExpander summary [data-testid="stExpanderToggleIcon"]::before {
  transform: rotate(90deg);
  color: var(--accent) !important;
}

.stProgress > div > div > div > div {
  background: linear-gradient(90deg, #8b5cf6, #06b6d4) !important;
  box-shadow: 0 0 12px rgba(139,92,246,0.35);
}

/* Alerts / toasts */
.stAlert, [data-baseweb="notification"] {
  background: var(--surface-2) !important;
  border: 1px solid var(--border) !important;
  border-radius: 10px !important;
  color: var(--text) !important;
}
.stAlert * { color: var(--text) !important; }
[data-testid="stToast"] {
  background: #131318 !important;
  border: 1px solid var(--border) !important;
  color: var(--text) !important;
  backdrop-filter: blur(16px);
}

/* ========== TOPBAR ========== */
.topbar {
  border: 1px solid var(--border);
  background: var(--surface);
  backdrop-filter: blur(16px);
  -webkit-backdrop-filter: blur(16px);
  padding: 16px 22px;
  border-radius: 14px;
  margin: 0 0 28px;
  display: flex;
  align-items: center;
  justify-content: space-between;
  box-shadow: var(--shadow-sm);
}
.brand { display: flex; align-items: center; gap: 12px; }
.brand-mark {
  width: 32px; height: 32px; border-radius: 8px;
  background: linear-gradient(135deg, #8b5cf6 0%, #06b6d4 100%);
  color: #ffffff;
  display: flex; align-items: center; justify-content: center;
  font-family: 'Instrument Serif', serif; font-style: italic; font-size: 20px;
  box-shadow: 0 0 20px rgba(139,92,246,0.45);
}
.brand-name {
  font-family: 'Instrument Serif', serif;
  font-style: italic;
  font-size: 22px;
  letter-spacing: -0.01em;
  background: linear-gradient(180deg, #ffffff 0%, #a1a1aa 100%);
  -webkit-background-clip: text;
  -webkit-text-fill-color: transparent;
  background-clip: text;
}
.brand-name::after { content: '.'; color: var(--accent); -webkit-text-fill-color: var(--accent); }
.topbar-meta {
  font-size: 11px;
  color: var(--text-3);
  margin-left: 18px;
  padding-left: 18px;
  border-left: 1px solid var(--border);
  font-family: 'Geist Mono', monospace;
  text-transform: uppercase;
  letter-spacing: 0.06em;
}
/* Signed-in chip — who you are, who else is on, and the way out. */
.userchip {
  display: flex; align-items: center; gap: 8px;
  background: var(--surface-2);
  border: 1px solid var(--border);
  padding: 4px 10px 4px 12px;
  border-radius: 999px;
  font-size: 11px;
  font-family: 'Geist Mono', monospace;
}
.userchip-dot {
  width: 6px; height: 6px; border-radius: 50%;
  background: #22c55e;
  box-shadow: 0 0 8px rgba(34,197,94,0.8);
}
.userchip-name {
  color: var(--text-1);
  letter-spacing: 0.04em;
  text-transform: uppercase;
}
/* Hover reveals the other names via the title attribute — the chip stays
   one line wide however many people are on. */
.userchip-others {
  color: var(--text-3);
  padding-left: 8px;
  border-left: 1px solid var(--border);
  cursor: default;
}
.userchip-out {
  color: var(--text-3);
  text-decoration: none;
  padding-left: 8px;
  border-left: 1px solid var(--border);
  transition: color .15s ease;
}
.userchip-out:hover { color: var(--accent); }
.tag {
  background: var(--surface-2);
  border: 1px solid var(--border);
  padding: 4px 12px;
  border-radius: 999px;
  font-size: 11px;
  color: var(--text-2);
  font-family: 'Geist Mono', monospace;
}
.live-dot {
  display: inline-block;
  width: 7px; height: 7px;
  border-radius: 50%;
  background: var(--green);
  margin-right: 6px;
  box-shadow: 0 0 0 3px rgba(52,211,153,0.2), 0 0 12px rgba(52,211,153,0.6);
  animation: pulse 2.2s ease-in-out infinite;
}
@keyframes pulse {
  0%, 100% { opacity: 1; }
  50% { opacity: 0.55; }
}

/* ========== PAGE TITLE ========== */
.page-title {
  font-family: 'Instrument Serif', serif;
  font-weight: 400; font-size: 46px; letter-spacing: -0.025em;
  margin: 10px 0 6px; line-height: 1.05;
  background: linear-gradient(180deg, #ffffff 0%, #a1a1aa 100%);
  -webkit-background-clip: text;
  -webkit-text-fill-color: transparent;
  background-clip: text;
}
.page-title em {
  font-style: italic;
  background: linear-gradient(135deg, #8b5cf6 0%, #06b6d4 100%);
  -webkit-background-clip: text;
  -webkit-text-fill-color: transparent;
  background-clip: text;
}
.page-sub { font-size: 13px; color: var(--text-3); margin: 0 0 24px; }

/* ========== CARDS ========== */
.card {
  background: var(--surface);
  backdrop-filter: blur(16px);
  -webkit-backdrop-filter: blur(16px);
  border: 1px solid var(--border);
  border-radius: var(--radius-lg);
  padding: 20px 22px;
  margin-bottom: 20px;
  box-shadow: var(--shadow-sm);
  transition: border-color 0.15s ease;
}
.card:hover { border-color: var(--border-strong); }
.card-title { font-size: 15px; font-weight: 500; margin: 0 0 14px; display: flex; align-items: center; gap: 8px; color: var(--text); }
.card-title .ti { font-size: 17px; color: var(--text-2); }

/* ========== FLOATING TOC ========== */
html { scroll-behavior: smooth; }
/* Offset anchor scroll so the section header isn't hidden behind
   Streamlit's sticky top bar. Apply to anchored elements via [id]. */
.toc-anchor { scroll-margin-top: 90px; }

.toc-float {
  position: fixed;
  right: 14px;
  top: 50%;
  transform: translateY(-50%);
  z-index: 950;
  background: var(--surface);
  border: 1px solid var(--border);
  border-radius: 14px;
  padding: 10px 8px;
  box-shadow: var(--shadow-md);
  backdrop-filter: blur(14px);
  -webkit-backdrop-filter: blur(14px);
  max-width: 56px;
  transition: max-width 0.25s ease, padding 0.25s ease;
  overflow: hidden;
  font-family: 'Geist Mono', monospace;
}
.toc-float:hover { max-width: 280px; padding: 10px 14px; }
.toc-float ul { list-style: none; margin: 0; padding: 0; }
.toc-float li { margin: 0; }
.toc-float a {
  display: flex;
  align-items: center;
  gap: 10px;
  color: var(--text-2);
  text-decoration: none;
  padding: 7px 6px;
  font-size: 12px;
  letter-spacing: 0.01em;
  border-radius: 8px;
  transition: background 0.15s ease, color 0.15s ease;
  white-space: nowrap;
}
.toc-float a:hover { color: var(--text); background: rgba(139,92,246,0.08); }
.toc-dot {
  flex: 0 0 8px;
  height: 8px; width: 8px; border-radius: 50%;
  background: var(--border-strong);
  transition: background 0.15s ease, transform 0.15s ease;
}
.toc-float a:hover .toc-dot {
  background: linear-gradient(135deg, #8B5CF6, #EC4899);
  transform: scale(1.3);
}
.toc-label {
  opacity: 0;
  transform: translateX(-4px);
  transition: opacity 0.2s ease 0.05s, transform 0.2s ease 0.05s;
}
.toc-float:hover .toc-label { opacity: 1; transform: translateX(0); }
.toc-head {
  font-size: 9px;
  letter-spacing: 0.12em;
  color: var(--text-3);
  text-transform: uppercase;
  padding: 4px 6px 8px;
  border-bottom: 1px solid var(--border);
  margin-bottom: 6px;
  opacity: 0;
  height: 0; overflow: hidden;
  transition: opacity 0.2s ease, height 0.2s ease;
}
.toc-float:hover .toc-head { opacity: 1; height: 22px; }
@media (max-width: 1280px) { .toc-float { display: none; } }

/* ========== HERO SCORE ========== */
.hero-score {
  background: var(--surface);
  border: 1px solid var(--border);
  border-radius: var(--radius-lg);
  padding: 20px 24px 22px;
  margin: 8px 0 24px;
  position: relative;
  overflow: hidden;
}
.hero-score::before {
  content: '';
  position: absolute;
  top: 0; left: 0; right: 0;
  height: 2px;
  background: linear-gradient(90deg, rgba(139,92,246,0.0), rgba(139,92,246,0.85), rgba(139,92,246,0.0));
}
.hero-score-head {
  display: flex; align-items: baseline; justify-content: space-between;
  margin-bottom: 16px; flex-wrap: wrap; gap: 8px;
}
.hero-score-title {
  font-size: 15px; font-weight: 600; margin: 0;
  letter-spacing: -0.005em; color: var(--text);
  display: flex; align-items: center; gap: 8px;
}
.hero-score-title::before {
  content: '';
  display: inline-block;
  width: 8px; height: 8px; border-radius: 50%;
  background: linear-gradient(135deg, #8B5CF6, #EC4899);
}
.hero-score-sub {
  font-size: 11px; color: var(--text-3);
  font-family: 'Geist Mono', monospace;
}
.hero-grid {
  display: grid;
  grid-template-columns: repeat(6, 1fr);
  gap: 18px;
}
@media (max-width: 1100px) { .hero-grid { grid-template-columns: repeat(3, 1fr); } }
@media (max-width: 640px)  { .hero-grid { grid-template-columns: repeat(2, 1fr); } }
.hero-metric {
  padding: 6px 12px 6px 14px;
  border-left: 2px solid var(--border);
  min-width: 0;
}
.hero-metric:first-child { border-left-color: rgba(139,92,246,0.5); }
.hero-metric-label {
  font-size: 10px;
  color: var(--text-2);
  text-transform: uppercase;
  letter-spacing: 0.08em;
  font-weight: 600;
  margin-bottom: 6px;
  font-family: 'Geist Mono', monospace;
}
.hero-metric-value {
  font-family: 'Instrument Serif', serif;
  font-size: 34px;
  font-weight: 400;
  line-height: 1;
  letter-spacing: -0.02em;
  margin: 0;
  color: var(--text);
  white-space: nowrap;
}
.hero-metric-value.green { color: var(--green); }
.hero-metric-value.red   { color: var(--red); }
.hero-metric-value .pct  { font-size: 19px; opacity: .9; }
.hero-metric-sub {
  font-size: 10px; color: var(--text-3);
  margin-top: 4px;
  font-family: 'Geist Mono', monospace;
}

/* ========== AI SHARE OF VOICE (per-keyword) ========== */
.sov-card {
  background: var(--surface);
  border: 1px solid var(--border);
  border-radius: var(--radius-lg);
  padding: 18px 22px 20px;
  margin: 0 0 24px;
}
.sov-head {
  display: flex; align-items: baseline; justify-content: space-between;
  margin-bottom: 14px; flex-wrap: wrap; gap: 8px;
}
.sov-title {
  font-size: 15px; font-weight: 600; margin: 0;
  color: var(--text);
}
.sov-sub {
  font-size: 11px; color: var(--text-3);
  font-family: 'Geist Mono', monospace;
}
.sov-table {
  display: grid; grid-template-columns: 140px 1fr 70px; gap: 12px 16px;
  align-items: center;
}
.sov-platform {
  font-size: 13px; color: var(--text);
  font-family: 'Geist Mono', monospace;
}
.sov-bar-wrap {
  position: relative;
  height: 10px;
  background: var(--surface-2, rgba(0,0,0,0.04));
  border-radius: 999px;
  overflow: hidden;
  border: 1px solid var(--border);
}
.sov-bar-fill {
  position: absolute; top: 0; left: 0; bottom: 0;
  background: linear-gradient(90deg, #8B5CF6, #EC4899);
  border-radius: 999px;
  transition: width 0.3s ease;
}
.sov-bar-fill.low  { background: linear-gradient(90deg, #94A3B8, #CBD5E1); }
.sov-pct {
  font-family: 'Instrument Serif', serif;
  font-size: 22px; line-height: 1;
  text-align: right; color: var(--text);
  letter-spacing: -0.01em;
}
.sov-pct .pct { font-size: 13px; opacity: .8; }
.sov-pct.green { color: var(--green); }
.sov-pct.muted { color: var(--text-3); }

/* SoV matrix — full table view */
.sov-tbl { margin-top: 4px; }
.sov-tbl table { width: 100%; border-collapse: collapse; }
.sov-tbl th {
  font-size: 11px; text-transform: uppercase; letter-spacing: 0.06em;
  font-weight: 600; color: var(--text-2);
  padding: 8px 10px; border-bottom: 1px solid var(--border);
  font-family: 'Geist Mono', monospace; text-align: left;
}
.sov-tbl th.col-center { text-align: center; }
.sov-tbl th.col-right  { text-align: right; }
.sov-tbl td {
  padding: 10px 10px;
  border-bottom: 1px solid var(--border);
  font-size: 13px; color: var(--text);
  vertical-align: middle;
}
.sov-tbl td:first-child {
  font-family: 'Geist Mono', monospace;
  max-width: 380px; overflow: hidden; text-overflow: ellipsis; white-space: nowrap;
}
.sov-tbl td.col-center { text-align: center; }
.sov-tbl td.col-right  { text-align: right; }
.sov-tbl tr:last-child td { border-bottom: none; }
.sov-tbl tr:hover td { background: rgba(139,92,246,0.04); }
.sov-cell {
  position: relative;
  min-width: 110px;
}
.sov-cell-empty { color: var(--text-3); font-family: 'Geist Mono', monospace; }
.sov-cell-bar {
  position: relative;
  height: 6px;
  background: var(--surface-2, rgba(0,0,0,0.04));
  border-radius: 999px;
  overflow: hidden;
  border: 1px solid var(--border);
  margin: 0 auto 4px;
  max-width: 100px;
}
.sov-cell-fill {
  position: absolute; top: 0; left: 0; bottom: 0;
  background: linear-gradient(90deg, #8B5CF6, #EC4899);
  border-radius: 999px;
}
.sov-cell-fill.low { background: linear-gradient(90deg, #94A3B8, #CBD5E1); }
.sov-cell-pct {
  font-family: 'Instrument Serif', serif;
  font-size: 15px; line-height: 1;
  color: var(--text); letter-spacing: -0.01em;
}
.sov-cell-pct.green { color: var(--green); }
.sov-cell-pct.muted { color: var(--text-3); }

/* SoV platform roll-up — count + %-of-N typography */
.sov-count {
  font-family: 'Instrument Serif', serif;
  font-size: 22px; letter-spacing: -0.01em; color: var(--text);
}
.sov-count-of {
  font-size: 12px; color: var(--text-3);
  font-family: 'Geist Mono', monospace;
}
.sov-count-pct {
  font-size: 11px; color: var(--text-2);
  font-family: 'Geist Mono', monospace;
  margin-left: 6px;
}

/* ========== KPI ========== */
.kpi-grid { display: grid; grid-template-columns: repeat(4, 1fr); gap: 14px; margin-bottom: 28px; }
.kpi {
  background: var(--surface);
  backdrop-filter: blur(16px);
  -webkit-backdrop-filter: blur(16px);
  border: 1px solid var(--border);
  border-radius: var(--radius-lg);
  padding: 18px 20px;
  position: relative;
  overflow: hidden;
  transition: transform 0.2s ease, border-color 0.2s ease, box-shadow 0.2s ease;
}
.kpi::before {
  content: '';
  position: absolute;
  top: 0; left: 0; right: 0;
  height: 1px;
  background: linear-gradient(90deg, transparent, rgba(139,92,246,0.6), transparent);
  opacity: 0.6;
}
.kpi:hover {
  border-color: var(--border-strong);
  transform: translateY(-2px);
  box-shadow: var(--shadow-md);
}
.kpi-label {
  font-size: 10px;
  color: var(--text-2);
  text-transform: uppercase;
  letter-spacing: 0.10em;
  font-weight: 600;
  margin-bottom: 8px;
  font-family: 'Geist Mono', monospace;
}
.kpi-value {
  font-family: 'Instrument Serif', serif;
  font-size: 42px;
  font-weight: 400;
  line-height: 1;
  letter-spacing: -0.025em;
  margin: 0;
  color: var(--text);
}
.kpi-value.green { color: var(--green); }
.kpi-value.red { color: var(--red); }
.kpi-sub { font-size: 11px; color: var(--text-3); margin-top: 8px; font-family: 'Geist Mono', monospace; }

/* ========== SECTION HEADERS ========== */
.section-header { display: flex; align-items: baseline; justify-content: space-between; margin: 32px 0 14px; }
.section-title {
  font-size: 16px; font-weight: 500; margin: 0;
  display: flex; align-items: center; gap: 10px;
  color: var(--text); letter-spacing: -0.01em;
}
.section-title::before {
  content: '';
  width: 3px; height: 14px;
  background: linear-gradient(135deg, #8b5cf6 0%, #06b6d4 100%);
  border-radius: 2px;
  display: inline-block;
}
.section-title .ti { font-size: 17px; color: var(--text-2); }
.section-meta {
  font-size: 11px;
  color: var(--text-3);
  font-family: 'Geist Mono', monospace;
  text-transform: uppercase;
  letter-spacing: 0.06em;
}

/* ========== TABLES ========== */
.tbl {
  background: var(--surface);
  backdrop-filter: blur(16px);
  -webkit-backdrop-filter: blur(16px);
  border: 1px solid var(--border);
  border-radius: var(--radius-lg);
  overflow: hidden;
  box-shadow: var(--shadow-sm);
}
.tbl table { width: 100%; border-collapse: collapse; }
.tbl thead th {
  background: var(--surface-2);
  text-align: left;
  padding: 12px 16px;
  font-size: 10px;
  font-weight: 600;
  color: var(--text-2);
  text-transform: uppercase;
  letter-spacing: 0.08em;
  border-bottom: 1px solid var(--border);
  font-family: 'Geist Mono', monospace;
}
.tbl tbody tr { border-bottom: 1px solid var(--border); transition: background 0.1s ease; }
.tbl tbody tr:last-child { border-bottom: none; }
.tbl tbody tr:hover { background: var(--surface-2); }
.tbl td { padding: 13px 16px; font-size: 13px; vertical-align: middle; color: var(--text); }
.tbl td a { color: var(--text); text-decoration: none; }
.tbl td a:hover { color: var(--accent); }
.tbl .gap-row { background: rgba(248,113,113,0.05); }
.tbl .gap-row:hover { background: rgba(248,113,113,0.09); }
.tbl .you-row { background: rgba(139,92,246,0.07); }
.tbl .you-row:hover { background: rgba(139,92,246,0.11); }
.tbl .col-center { text-align: center; }
.tbl .col-right { text-align: right; }

.cell-result {
  display: inline-flex;
  align-items: center;
  gap: 4px;
  padding: 4px 9px;
  border-radius: 6px;
  font-size: 11px;
  font-family: 'Geist Mono', monospace;
  font-weight: 500;
  border: 1px solid;
}
.cell-result.cited { background: var(--green-bg); color: var(--green); border-color: var(--green-border); }
.cell-result.missing { background: var(--red-bg); color: var(--red); border-color: var(--red-border); }
.score-pill { font-family: 'Geist Mono', monospace; font-size: 12px; font-weight: 500; color: var(--text); }
.score-pill.full { color: var(--green); }
.score-pill.zero { color: var(--red); }

.you-tag {
  background: linear-gradient(135deg, #8b5cf6 0%, #06b6d4 100%);
  color: #ffffff;
  font-size: 9px;
  font-weight: 600;
  padding: 2px 7px;
  border-radius: 4px;
  font-family: 'Geist Mono', monospace;
  text-transform: uppercase;
  letter-spacing: 0.08em;
  margin-left: 8px;
  box-shadow: 0 0 10px rgba(139,92,246,0.4);
}
.url-cell { display: flex; align-items: center; gap: 8px; min-width: 0; flex-wrap: wrap; }
.url-cell .domain { overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.url-paths {
  font-size: 10px;
  font-family: 'Geist Mono', monospace;
  color: var(--text-3);
  background: var(--surface-2);
  border: 1px solid var(--border);
  padding: 1px 7px;
  border-radius: 999px;
  cursor: help;
}

.llm-dots { display: inline-flex; gap: 4px; }
.llm-dot {
  width: 22px; height: 22px;
  border-radius: 5px;
  display: inline-flex;
  align-items: center;
  justify-content: center;
  font-size: 10px;
  font-weight: 600;
  font-family: 'Geist Mono', monospace;
  border: 1px solid var(--border);
  color: var(--text-3);
  background: var(--surface-2);
}
.llm-dot.on.chatgpt { background: rgba(16,185,129,0.18); color: #10b981; border-color: rgba(16,185,129,0.4); }
.llm-dot.on.perplexity { background: rgba(56,189,248,0.18); color: #38bdf8; border-color: rgba(56,189,248,0.4); }
.llm-dot.on.gemini { background: rgba(251,191,36,0.18); color: #fbbf24; border-color: rgba(251,191,36,0.4); }
.llm-dot.on.google { background: rgba(129,140,248,0.18); color: #818cf8; border-color: rgba(129,140,248,0.4); }

.count-badge {
  display: inline-flex;
  align-items: center;
  gap: 4px;
  padding: 3px 11px;
  background: var(--surface-2);
  border: 1px solid var(--border);
  border-radius: 999px;
  font-family: 'Geist Mono', monospace;
  font-size: 12px;
  font-weight: 500;
  color: var(--text);
}
.count-badge.high {
  background: linear-gradient(135deg, #8b5cf6 0%, #06b6d4 100%);
  color: #ffffff;
  border-color: transparent;
}

.rank-cell { width: 38px; font-family: 'Geist Mono', monospace; color: var(--text-3); font-size: 12px; font-weight: 500; }
.rank-cell.top { color: var(--accent); font-weight: 600; }

/* Citation matrix accordion row */
.tbl tbody tr.matrix-detail { border-bottom: 1px solid var(--border); }
.tbl tbody tr.matrix-detail:hover { background: transparent; }
.tbl tbody tr.matrix-detail > td { padding: 0 !important; background: var(--bg-2); }
.tbl tbody tr.matrix-detail-empty > td .acc-empty {
  padding: 8px 16px;
  font-size: 11px;
  color: var(--text-3);
  font-family: 'Geist Mono', monospace;
  text-transform: uppercase;
  letter-spacing: 0.06em;
  background: var(--bg-2);
}
.matrix-acc { width: 100%; }
.matrix-acc > summary {
  list-style: none;
  cursor: pointer;
  padding: 10px 16px;
  display: flex;
  align-items: center;
  gap: 10px;
  font-size: 12px;
  color: var(--text-2);
  font-family: 'Geist Mono', monospace;
  background: var(--bg-2);
  border-top: 1px dashed var(--border);
  user-select: none;
  transition: background 0.15s ease, color 0.15s ease;
}
.matrix-acc > summary::-webkit-details-marker { display: none; }
.matrix-acc > summary:hover { background: var(--surface-2); color: var(--text); }
.matrix-acc > summary:hover .acc-label { color: var(--text); }
.matrix-acc[open] > summary { background: var(--surface); color: var(--text); }
.acc-chevron {
  display: inline-block;
  color: var(--accent);
  font-size: 10px;
  transition: transform 0.15s ease;
}
.matrix-acc[open] .acc-chevron { transform: rotate(90deg); }
.acc-label {
  color: var(--text-2);
  text-transform: uppercase;
  letter-spacing: 0.08em;
  font-size: 10px;
  font-weight: 600;
}
.acc-count {
  margin-left: auto;
  background: var(--surface-2);
  border: 1px solid var(--border);
  padding: 2px 9px;
  border-radius: 999px;
  font-size: 11px;
  color: var(--text-2);
}
.matrix-acc[open] .acc-count {
  background: linear-gradient(135deg, #8b5cf6 0%, #06b6d4 100%);
  color: #ffffff;
  border-color: transparent;
}
.acc-body {
  padding: 14px 18px 16px;
  background: var(--bg-2);
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(260px, 1fr));
  gap: 16px;
  border-top: 1px solid var(--border);
}
.acc-group { min-width: 0; }
.acc-plat {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 8px;
  margin-bottom: 8px;
  padding-bottom: 8px;
  border-bottom: 1px dashed var(--border);
}
.acc-plat-count {
  font-size: 10px;
  color: var(--text-3);
  font-family: 'Geist Mono', monospace;
  text-transform: uppercase;
  letter-spacing: 0.06em;
}
.acc-urls {
  list-style: none;
  margin: 0;
  padding: 0;
  display: flex;
  flex-direction: column;
  gap: 4px;
}
.acc-urls li {
  font-size: 12px;
  font-family: 'Geist Mono', monospace;
  display: flex;
  flex-direction: column;
  gap: 2px;
  padding: 6px 10px;
  background: var(--surface);
  border: 1px solid var(--border);
  border-radius: 6px;
  transition: border-color 0.1s ease, background 0.1s ease;
}
.acc-urls li:hover { border-color: var(--border-strong); background: var(--surface-2); }
.acc-urls li a {
  color: var(--text);
  text-decoration: none;
  font-weight: 500;
  font-size: 12px;
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
}
.acc-urls li a:hover { color: var(--accent); }
.acc-url-head {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 8px;
  min-width: 0;
}
.acc-url-head > a {
  flex: 1;
  min-width: 0;
}
.acc-url-badge {
  flex: 0 0 auto;
  display: inline-flex;
  align-items: center;
  justify-content: center;
  min-width: 26px;
  height: 18px;
  padding: 0 7px;
  border-radius: 999px;
  background: var(--surface-2);
  border: 1px solid var(--border);
  font-size: 10px;
  font-weight: 600;
  font-family: 'Geist Mono', monospace;
  color: var(--text-2);
  cursor: help;
}
.acc-url-badge.acc-badge-hot {
  background: linear-gradient(135deg, #8b5cf6 0%, #06b6d4 100%);
  color: #ffffff;
  border-color: transparent;
  box-shadow: 0 0 10px rgba(139,92,246,0.4);
}
.acc-urls li a.tracked {
  background: linear-gradient(135deg, #8b5cf6 0%, #06b6d4 100%);
  -webkit-background-clip: text;
  -webkit-text-fill-color: transparent;
  background-clip: text;
  font-weight: 600;
}
.acc-url-full {
  font-size: 10px;
  color: var(--text-3);
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
}
.acc-urls li.acc-more {
  background: transparent;
  border-style: dashed;
  color: var(--text-3);
  text-align: center;
  font-style: italic;
}

/* Keywords-cited cell in the URL leaderboard */
.kw-cell { display: flex; flex-direction: column; gap: 6px; min-width: 200px; max-width: 340px; }
.kw-count {
  font-family: 'Geist Mono', monospace;
  font-size: 10px;
  color: var(--text-3);
  text-transform: uppercase;
  letter-spacing: 0.06em;
  font-weight: 600;
}
.kw-chips { display: flex; flex-wrap: wrap; gap: 4px; }
.kw-chip {
  display: inline-block;
  padding: 2px 8px;
  background: var(--surface-2);
  border: 1px solid var(--border);
  border-radius: 999px;
  font-size: 11px;
  font-family: 'Geist Mono', monospace;
  color: var(--text-2);
  white-space: nowrap;
  max-width: 220px;
  overflow: hidden;
  text-overflow: ellipsis;
}
.kw-tracked-row {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: 4px;
  margin-bottom: 4px;
}
.kw-tracked-label {
  font-family: 'Geist Mono', monospace;
  font-size: 9px;
  color: var(--text-3);
  text-transform: uppercase;
  letter-spacing: 0.10em;
  font-weight: 600;
  margin-right: 4px;
}
.kw-tracked-chip {
  display: inline-flex;
  align-items: center;
  gap: 6px;
  padding: 2px 9px;
  background: linear-gradient(135deg, rgba(139,92,246,0.18) 0%, rgba(6,182,212,0.18) 100%);
  border: 1px solid rgba(139,92,246,0.35);
  border-radius: 999px;
  font-size: 11px;
  font-family: 'Geist Mono', monospace;
  font-weight: 600;
  color: var(--text);
  cursor: help;
  max-width: 220px;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.kw-tracked-where {
  font-size: 9px;
  font-weight: 500;
  text-transform: uppercase;
  letter-spacing: 0.08em;
  color: var(--accent);
  padding: 1px 5px;
  background: rgba(139,92,246,0.12);
  border-radius: 999px;
}
.tracked-bold {
  background: linear-gradient(135deg, #8b5cf6 0%, #06b6d4 100%);
  -webkit-background-clip: text;
  -webkit-text-fill-color: transparent;
  background-clip: text;
  font-weight: 700;
}
.matrix-keyword { color: var(--text); }
.matrix-tracked {
  display: inline-flex;
  flex-wrap: wrap;
  gap: 4px;
  margin-left: 8px;
  vertical-align: middle;
}
.kw-chip-n {
  display: inline-block;
  margin-left: 5px;
  padding: 0 5px;
  background: rgba(139,92,246,0.12);
  color: var(--accent);
  border-radius: 8px;
  font-size: 9px;
  font-weight: 600;
  letter-spacing: 0.02em;
}
.kw-chip.kw-more {
  background: transparent;
  color: var(--text-3);
  border-style: dashed;
  cursor: pointer;
  list-style: none;
  user-select: none;
  transition: color 0.15s ease, border-color 0.15s ease;
}
.kw-chip.kw-more::-webkit-details-marker { display: none; }
.kw-chip.kw-more::marker { display: none; }
.kw-chip.kw-more:hover { color: var(--text); border-color: var(--border-strong); }
.kw-overflow { display: inline; }
.kw-overflow[open] > summary.kw-more {
  color: var(--accent);
  border-color: var(--accent);
  border-style: solid;
}
.kw-overflow[open] > summary.kw-more::before { content: '▾ '; }
.kw-overflow-list {
  display: flex;
  flex-wrap: wrap;
  gap: 4px;
  margin-top: 6px;
  padding: 6px 0 0;
  width: 100%;
}

/* ========== RESPONSE VIEWER ========== */
.response {
  background: var(--surface);
  backdrop-filter: blur(16px);
  -webkit-backdrop-filter: blur(16px);
  border: 1px solid var(--border);
  border-radius: var(--radius-lg);
  overflow: hidden;
  margin-bottom: 18px;
  box-shadow: var(--shadow-sm);
}
.response-header {
  background: var(--surface-2);
  padding: 14px 20px;
  display: flex;
  align-items: center;
  justify-content: space-between;
  border-bottom: 1px solid var(--border);
  gap: 12px;
}
.response-meta { display: flex; align-items: center; gap: 12px; font-size: 13px; min-width: 0; color: var(--text); }
.llm-badge {
  padding: 4px 11px;
  border-radius: 6px;
  font-size: 11px;
  font-weight: 600;
  font-family: 'Geist Mono', monospace;
  text-transform: uppercase;
  letter-spacing: 0.05em;
  border: 1px solid;
}
.llm-badge.chatgpt { background: rgba(16,185,129,0.15); color: #10b981; border-color: rgba(16,185,129,0.35); }
.llm-badge.perplexity { background: rgba(56,189,248,0.15); color: #38bdf8; border-color: rgba(56,189,248,0.35); }
.llm-badge.gemini { background: rgba(251,191,36,0.15); color: #fbbf24; border-color: rgba(251,191,36,0.35); }
.llm-badge.google { background: rgba(129,140,248,0.15); color: #818cf8; border-color: rgba(129,140,248,0.35); }
.response-meta .kw { font-weight: 500; color: var(--text); }
.response-meta .pos { color: var(--text-3); font-size: 12px; font-family: 'Geist Mono', monospace; }
.response-meta .pos.cited { color: var(--green); }
.response-meta .pos.missing { color: var(--red); }
.response-body {
  padding: 22px 24px;
  font-size: 14px;
  line-height: 1.75;
  color: var(--text);
  white-space: pre-wrap;
  max-height: 540px;
  overflow-y: auto;
}
.response-body::-webkit-scrollbar { width: 8px; }
.response-body::-webkit-scrollbar-track { background: transparent; }
.response-body::-webkit-scrollbar-thumb { background: var(--surface-3); border-radius: 4px; }
.response-body::-webkit-scrollbar-thumb:hover { background: var(--border-strong); }
.response-body .hl-target {
  background: linear-gradient(135deg, #8b5cf6 0%, #06b6d4 100%);
  color: #ffffff;
  padding: 1px 6px;
  border-radius: 4px;
  font-weight: 500;
  box-shadow: 0 0 12px rgba(139,92,246,0.35);
}
.response-sources {
  padding: 14px 24px 18px;
  border-top: 1px dashed var(--border);
  font-size: 12px;
  color: var(--text-2);
  display: flex;
  flex-wrap: wrap;
  gap: 6px;
  align-items: center;
  background: var(--surface-2);
}
.response-sources strong {
  font-weight: 600;
  margin-right: 8px;
  text-transform: uppercase;
  font-size: 10px;
  letter-spacing: 0.08em;
  color: var(--text-3);
  font-family: 'Geist Mono', monospace;
}
.response-source {
  padding: 3px 9px;
  background: var(--surface);
  border: 1px solid var(--border);
  border-radius: 4px;
  font-size: 11px;
  font-family: 'Geist Mono', monospace;
  color: var(--text-2);
}
.response-source a { color: inherit !important; text-decoration: none !important; }
.response-source.you {
  background: linear-gradient(135deg, #8b5cf6 0%, #06b6d4 100%);
  color: #ffffff;
  border-color: transparent;
}
.response-source.you a { color: #ffffff !important; }

/* ========== CHIPS ========== */
.chip {
  background: var(--surface-2);
  border: 1px solid var(--border);
  padding: 5px 11px;
  border-radius: 999px;
  font-size: 12px;
  display: inline-block;
  margin: 2px 4px 2px 0;
  font-family: 'Geist Mono', monospace;
  color: var(--text);
}
.chip.target {
  background: var(--amber-bg);
  border-color: var(--amber-border);
  color: var(--amber);
}

/* ========== EMPTY STATE ========== */
.empty {
  border: 1px dashed var(--border);
  border-radius: 16px;
  padding: 48px;
  text-align: center;
  color: var(--text-3);
  background: var(--surface);
  backdrop-filter: blur(16px);
}
.empty h4 {
  color: var(--text);
  margin-bottom: 8px;
  font-size: 1.15rem;
  font-weight: 400;
  font-family: 'Instrument Serif', serif;
  font-style: italic;
}
.empty b { color: var(--text); }

/* ========== SIDEBAR ========== */
section[data-testid="stSidebar"] {
  background: var(--bg-2) !important;
  border-right: 1px solid var(--border);
}
section[data-testid="stSidebar"] .stMarkdown h3 {
  color: var(--text-2);
  font-size: 0.70rem;
  letter-spacing: 0.12em;
  text-transform: uppercase;
  margin: 1.2rem 0 0.5rem;
  font-weight: 600;
  font-family: 'Geist Mono', monospace;
}

/* ========== TOOL SWITCHER ========== */
/* Segmented-control container + item styles live in core.ui.SHARED_CSS
   (single source of truth, no truncation). Only the Streamlit default
   multipage nav link list is hidden here. */
[data-testid="stSidebarNav"] { display: none; }

/* Plotly */
.js-plotly-plot, .js-plotly-plot * { background: transparent !important; }
.js-plotly-plot .modebar { display: none !important; }
</style>
"""

st.markdown(CUSTOM_CSS, unsafe_allow_html=True)
st.markdown(_UI_SHARED_CSS, unsafe_allow_html=True)

# Overview-specific overrides: type scale, sidebar nav visibility, tracked-terms
# popover, responsive max-width. Layered on top of the shared component CSS.
_OVERVIEW_CSS = """
<style>
/* One max-width container so nothing clips at 1280px. */
.block-container { max-width: 1320px !important; padding-left: 24px !important; padding-right: 24px !important; }
@media (max-width: 900px)  { .block-container { padding-left: 16px !important; padding-right: 16px !important; } }

/* Sidebar nav — inactive items are muted but full opacity (clickable-looking).
   Active item gets a left rule + filled surface. */
section[data-testid="stSidebar"] a { opacity: 1 !important; color: var(--text-2) !important; }
section[data-testid="stSidebar"] a:hover { background: var(--surface-2) !important; }
section[data-testid="stSidebar"] a[aria-current="page"],
section[data-testid="stSidebar"] a.active {
  background: var(--surface-3) !important;
  color: var(--text) !important;
  border-left: 3px solid var(--accent) !important;
  padding-left: calc(1rem - 3px) !important;
}

/* Meta line under page title — "216 queries · 72 keywords · 3 platforms" */
.run-meta {
  display: inline-flex; align-items: center; gap: 10px; flex-wrap: wrap;
  font-family: 'Geist Mono', ui-monospace, monospace;
  font-size: 11px; letter-spacing: 0.08em; text-transform: uppercase;
  color: var(--text-3);
  margin: 2px 0 14px;
}
.run-meta .dot { color: var(--text-3); opacity: 0.5; }

/* Truncated tracked-terms chip with native <details> popover. */
.tt-chip {
  display: inline-flex; align-items: center; gap: 6px;
  padding: 4px 12px; border-radius: 999px;
  background: rgba(124,92,255,0.14); border: 1px solid rgba(124,92,255,0.32);
  font-size: 11px; color: #E8DFFF;
  font-family: 'Geist Mono', ui-monospace, monospace;
  cursor: pointer; user-select: none;
}
.tt-chip::-webkit-details-marker, .tt-chip summary::-webkit-details-marker { display: none; }
.tt-chip summary { list-style: none; display: inline-flex; align-items: center; gap: 6px; }
.tt-panel {
  position: absolute; z-index: 30;
  margin-top: 8px;
  min-width: 260px; max-width: 380px;
  padding: 10px 12px;
  background: #14122C;
  border: 1px solid var(--border);
  border-radius: 10px;
  box-shadow: 0 20px 40px rgba(0,0,0,0.5);
  color: var(--text);
  font-family: 'Geist', sans-serif; font-size: 12px;
  display: flex; flex-wrap: wrap; gap: 6px;
}
.tt-panel .tt-term {
  padding: 3px 10px; border-radius: 999px;
  background: rgba(255,255,255,0.06); border: 1px solid var(--border);
  font-family: 'Geist Mono', ui-monospace, monospace; font-size: 11px;
  color: var(--text);
}

/* Responsive plot cards so the trend + platform table don't clip. */
.overview-cards { display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 16px; }
@media (max-width: 1100px) { .overview-cards { grid-template-columns: 1fr; } }

/* Hero setup card — replaces the deleted sidebar inputs. */
.hero-card { padding: 4px 2px 2px; }
.hero-block-label {
  font-family: 'Geist Mono', ui-monospace, monospace;
  font-size: 11px; letter-spacing: 0.10em; text-transform: uppercase;
  color: var(--text-3); font-weight: 600;
  margin-bottom: 8px;
}
.hero-block-gap { height: 24px; }
.hero-plat-dot {
  display: inline-block;
  width: 9px; height: 9px; border-radius: 50%;
  margin: 6px 0 -6px;
  box-shadow: 0 0 0 3px rgba(255,255,255,0.05);
}
.hero-run-summary {
  font-family: 'Geist Mono', ui-monospace, monospace;
  font-size: 13px; color: var(--text-2);
  padding-top: 8px;
}
.hero-run-summary b { color: var(--text); font-weight: 600; }
.hero-collapsed {
  font-family: 'Geist Mono', ui-monospace, monospace;
  font-size: 12px; color: var(--text-3);
  padding: 10px 0;
  letter-spacing: 0.02em;
}

/* Consistent uppercase label scale for all "meta" text. */
.section-meta, .run-meta, .kpi-flat-label {
  text-transform: uppercase !important;
  letter-spacing: 0.10em !important;
  font-size: 11px !important;
}
</style>
"""
st.markdown(_OVERVIEW_CSS, unsafe_allow_html=True)

# Disable browser spell-check + Grammarly on every Streamlit textarea so the
# fields stay on-theme (no red squiggles, no white overlay, no Grammarly
# badges). Runs once on load and re-applies on every Streamlit DOM patch via
# a MutationObserver against the parent document.
components.html(
    """
    <script>
    (function () {
      const doc = window.parent.document;
      function patch() {
        doc.querySelectorAll('textarea, input[type="text"]').forEach((el) => {
          el.setAttribute('spellcheck', 'false');
          el.setAttribute('autocorrect', 'off');
          el.setAttribute('autocapitalize', 'off');
          el.setAttribute('data-gramm', 'false');
          el.setAttribute('data-gramm_editor', 'false');
          el.setAttribute('data-enable-grammarly', 'false');
        });
      }
      patch();
      const obs = new MutationObserver(patch);
      obs.observe(doc.body, { childList: true, subtree: true });
    })();
    </script>
    """,
    height=0,
    width=0,
)


# ============================================================
# Top bar
# ============================================================
st.markdown(
    f"""
    <div class="topbar">
      <div style="display:flex; align-items:center;">
        <div class="brand">
          <div class="brand-mark">C</div>
          <span class="brand-name">Citerion</span>
        </div>
        <div class="topbar-meta">LLM Tracker · production</div>
      </div>
      <div style="display:flex; align-items:center; gap:10px;">
        <div class="tag"><span class="live-dot"></span>Live</div>
        {user_badge_html(st)}
      </div>
    </div>
    """,
    unsafe_allow_html=True,
)


# ============================================================
# Helpers
# ============================================================
def collect_prompts(csv_file, text_blob: str) -> list[str]:
    prompts: list[str] = []
    if csv_file is not None:
        try:
            df = pd.read_csv(io.BytesIO(csv_file.getvalue()))
            col = "prompt" if "prompt" in df.columns else df.columns[0]
            prompts.extend([str(x).strip() for x in df[col].dropna()])
        except Exception:
            try:
                # Try Excel
                df = pd.read_excel(io.BytesIO(csv_file.getvalue()))
                col = "prompt" if "prompt" in df.columns else df.columns[0]
                prompts.extend([str(x).strip() for x in df[col].dropna()])
            except Exception:
                st.error("Could not parse uploaded file as CSV or Excel.")
    prompts.extend([ln.strip() for ln in text_blob.splitlines() if ln.strip()])
    # Duplicates are dropped so the same keyword isn't scraped twice by accident.
    # For LOAD TESTING that's the wrong behaviour — pasting one keyword 500 times
    # would run a single scrape — so keep them when the UI checkbox is ticked, or
    # ALLOW_DUPLICATE_KEYWORDS=1 is set (useful for headless/CI runs).
    _keep = bool(st.session_state.get("allow_duplicate_keywords"))
    if not _keep:
        _keep = os.getenv("ALLOW_DUPLICATE_KEYWORDS", "").strip().lower() in (
            "1", "true", "yes", "on")
    if _keep:
        return prompts
    seen, out = set(), []
    for p in prompts:
        if p not in seen:
            seen.add(p); out.append(p)
    return out


async def _one(plat: str, prm: str, headless: bool) -> dict:
    fn = SCRAPERS[plat]
    _t0 = time.monotonic()
    try:
        r = await fn(prm, headless=headless)
    except Exception as e:
        r = ScrapeResult(platform=plat, prompt=prm, error=f"{type(e).__name__}: {e}")
    # measured here so every platform is timed identically, failures included
    r.duration_s = round(time.monotonic() - _t0, 1)
    return r.to_row()


def _default_concurrency() -> int:
    """Prompts processed at once. Override with SCRAPE_CONCURRENCY=N.

    Left at 4 by default because RAM scales with it — Gemini and Perplexity each
    launch their own browser per prompt, so N=4 can mean ~8 live Chromium
    processes. Raise it on a machine with headroom; that is the single biggest
    throughput lever here, since per-prompt time is dominated by the model
    streaming its answer, which no amount of tuning shortens.
    """
    import os
    try:
        return max(1, int(os.getenv("SCRAPE_CONCURRENCY", "4")))
    except ValueError:
        return 4


async def _scrape_async(prompts: list[str], platforms: list[str], headless: bool,
                        progress, status, concurrency: int | None = None) -> list[dict]:
    """Process prompts in parallel (up to `concurrency` prompts at once).
    Within each prompt, all platforms still run in parallel.

    Note: Google + ChatGPT share a single browser session and serialise via
    an internal lock regardless of this knob — the win is on Gemini and
    Perplexity, which each spin up their own context per prompt. RAM cost
    scales roughly linearly with `concurrency`.
    """
    if concurrency is None:
        concurrency = _default_concurrency()
    results: list[dict] = []
    total = len(prompts) * len(platforms)
    done_count = [0]
    started_count = [0]
    sem = asyncio.Semaphore(concurrency)

    async def _process_prompt(idx: int, prm: str) -> None:
        async with sem:
            started_count[0] += 1
            status.markdown(
                f"**Prompt {idx}/{len(prompts)}** started — `{prm[:80]}` · "
                f"{started_count[0]} in flight"
            )
            coros = [_one(p, prm, headless) for p in platforms]
            for coro in asyncio.as_completed(coros):
                row = await coro
                results.append(row)
                done_count[0] += 1
                progress.progress(
                    done_count[0] / total,
                    text=f"{done_count[0]}/{total} · {row['platform']} done",
                )

    tasks = [_process_prompt(i + 1, p) for i, p in enumerate(prompts)]
    try:
        await asyncio.gather(*tasks)
    finally:
        # Google AND ChatGPT each reuse ONE Chrome across every keyword in the
        # batch (the tab is never closed mid-scan). Close them now that the
        # batch is done.
        if "google" in platforms:
            from scrapers.google import close_session as _close_google
            await _close_google()
        if "chatgpt" in platforms:
            from scrapers.chatgpt import close_session as _close_chatgpt
            await _close_chatgpt()
    return results


def run_scrape(prompts: list[str], platforms: list[str], headless: bool) -> list[dict]:
    progress = st.progress(0.0, text="Starting…")
    status = st.empty()
    try:
        results = asyncio.run(_scrape_async(prompts, platforms, headless, progress, status))
    finally:
        progress.empty()
        status.empty()
    return results


def domain_of(url: str) -> str:
    try:
        return urlparse(url).netloc.lower()
    except Exception:
        return ""


def highlight_terms(text: str, terms: list[str]) -> str:
    out = html.escape(text)
    for t in terms:
        if not t:
            continue
        out = re.sub(
            re.escape(t),
            lambda m: f"<span class='hl-target'>{m.group(0)}</span>",
            out, flags=re.IGNORECASE,
        )
    return out


def bold_tracked(text: str, terms: list[str]) -> str:
    """Return HTML-escaped `text` with any tracked-term substring wrapped
    in <span class='tracked-bold'> — used in narrow cells (matrix keyword,
    leaderboard kw chips) where the heavy `.hl-target` pill would dominate
    the row. CSS gives the span a gradient tint + bold weight so matches
    stay visible without taking extra space."""
    out = html.escape(text)
    for t in terms:
        if not t:
            continue
        out = re.sub(
            re.escape(html.escape(t)),
            lambda m: f"<span class='tracked-bold'>{m.group(0)}</span>",
            out, flags=re.IGNORECASE,
        )
    return out


def is_tracked_url(url: str, terms: list[str]) -> bool:
    if not url or not terms:
        return False
    u = url.lower()
    return any(t.lower() in u for t in terms if t)


def row_citation_position(row, terms: list[str]) -> tuple[bool, int | None, str]:
    """For a result row + tracked terms, return (cited?, position_or_None, where).
    Position is 1-indexed position in the combined source URL list of the first
    URL that contains any tracked term. If only matched in text, position is None.
    """
    if not terms:
        return False, None, ""
    text = (row.get("response_text") or "").lower()
    cit_list = list(row.get("citations_list", []) or [])
    src_list = list(row.get("source_urls_list", []) or [])
    combined: list[str] = []
    seen = set()
    for u in cit_list + src_list:
        if u and u not in seen:
            seen.add(u); combined.append(u)

    # Position in URL list
    for i, u in enumerate(combined, start=1):
        if is_tracked_url(u, terms):
            in_text = any(t.lower() in text for t in terms)
            return True, i, ("text+url" if in_text else "url")

    # Text-only mention
    if any(t.lower() in text for t in terms):
        return True, None, "text"
    return False, None, ""


# ============================================================
# Sidebar deleted — the tool nav lives in the top bar, every input in
# the hero setup card. Widgets below need `csv` / `text` / `chosen` /
# `run_btn` in scope before the hero renders; seed the fallbacks here.
# ============================================================
csv = None
text = ""
chosen: dict[str, bool] = {}
headless = True
run_btn = False
_keywords: list[str] = []
_effective_terms: list[str] = []


# ============================================================
# Run handler — moved below the hero setup card. See the `if run_btn:`
# block right after the hero closes; here we just declare the fallback so
# downstream reads never fault before the hero renders.
# ============================================================


# ============================================================
# Run picker state — default to NOTHING SELECTED, so the dashboard
# stays empty unless the user just scraped OR explicitly picks a run.
# ============================================================
runs = list_runs(limit=50)


def _run_label(r: dict) -> str:
    when = datetime.fromtimestamp(r["ts"]).strftime("%d %b · %H:%M")
    return f"{when} · {r['prompt_count']} kw × {r['row_count']} calls · {r['platforms']}"


PLACEHOLDER_OPTION = {"ts": None, "label": "— Select a run to view —"}
ALL_OPTION = {"ts": -1, "label": "All runs combined"}
historic_options = [{"ts": r["ts"], "label": _run_label(r)} for r in runs]
run_options = (
    [PLACEHOLDER_OPTION]
    + historic_options
    + ([ALL_OPTION] if historic_options else [])
)

# After a scrape just completed, auto-pick that run.
if "pending_run_ts" in st.session_state:
    ts = st.session_state.pop("pending_run_ts")
    match = next((o for o in run_options if o.get("ts") == ts), None)
    if match:
        st.session_state["run_picker"] = match

# Read the picker (default to the placeholder "nothing selected").
sel = st.session_state.get("run_picker", PLACEHOLDER_OPTION)
# Guard against stale state (e.g. the picked run was deleted).
if isinstance(sel, dict) and sel.get("ts") not in (None, -1):
    if not any(o.get("ts") == sel.get("ts") for o in run_options):
        sel = PLACEHOLDER_OPTION
        st.session_state["run_picker"] = PLACEHOLDER_OPTION

# Load data based on what the user picked (cached).
if sel.get("ts") is None:
    rows = []
    run_label_text = ""
elif sel.get("ts") == -1:
    rows = load_recent(limit=5000)
    run_label_text = "All runs combined"
else:
    rows = _cached_load_run(sel["ts"])
    run_label_text = sel["label"]

dfs = _cached_dataframes(f"{sel.get('ts')}::{_cache_token()}::{len(rows)}", rows)
results_df = dfs["results"]
tracked_terms = get_tracked_terms()


# ============================================================
# Top nav — three tools horizontally, above the page title.
# ============================================================
_top_nav = st.segmented_control(
    "Tool", ["LLM Tracker", "People Also Ask", "Report"],
    default="LLM Tracker", label_visibility="collapsed",
    key="nav_top_app",
)
if _top_nav == "People Also Ask":
    st.switch_page("pages/People_Also_Ask.py")
elif _top_nav == "Report":
    st.switch_page("pages/Compare_Reports.py")


# ============================================================
# Hero — page title
# ============================================================
# Run scale meta line: queries · keywords · platforms — pure context, not KPIs.
_pre_n_queries = len(results_df)
_pre_n_prompts = int(results_df["prompt"].nunique()) if not results_df.empty else 0
_pre_n_platforms = int(results_df["platform"].nunique()) if not results_df.empty else 0
_meta_html = ""
if not results_df.empty:
    _meta_html = (
        "<div class='run-meta'>"
        f"<span>{_pre_n_queries:,} queries</span>"
        "<span class='dot'>·</span>"
        f"<span>{_pre_n_prompts:,} keywords</span>"
        "<span class='dot'>·</span>"
        f"<span>{_pre_n_platforms} platforms</span>"
        "</div>"
    )

# Tracked-terms chip — count + primary brand only, with <details> popover.
_tt_chip = ""
if tracked_terms:
    _primary_term = tracked_terms[0]
    _extra = len(tracked_terms) - 1
    _summary_txt = (
        f"{html.escape(_primary_term)}"
        + (f" + {_extra} term{'s' if _extra != 1 else ''}" if _extra > 0 else "")
    )
    _terms_html = "".join(
        f"<span class='tt-term'>{html.escape(t)}</span>" for t in tracked_terms
    )
    _tt_chip = (
        "<details class='tt-chip'>"
        f"<summary>◆ {_summary_txt} ▾</summary>"
        f"<div class='tt-panel'>{_terms_html}</div>"
        "</details>"
    )

st.markdown(
    f"""
    {_meta_html}
    <div style="display:flex; align-items:center; gap:12px; margin: 4px 0 18px; flex-wrap:wrap;">
      {_tt_chip}
    </div>
    """,
    unsafe_allow_html=True,
)


# ============================================================
# HERO SETUP CARD — replaces the deleted sidebar inputs.
# Four blocks: Input Source · Tracking Terms · Platforms · Run.
# Collapses to a summary line once results are on screen; the user can
# expand it back via the disclosure to edit and re-run.
# ============================================================
_ORDERED_PLATFORMS = ["chatgpt", "gemini", "google", "perplexity"]
_PLAT_DOT_COLORS = {
    "chatgpt":    "#10A37F",
    "gemini":     "#4385F5",
    "google":     "#EA4335",
    "perplexity": "#20808D",
}

# Seed platform-checkbox session state on first render — all four checked.
for _p in _ORDERED_PLATFORMS:
    _k = f"chk_{_p}"
    if _k not in st.session_state:
        st.session_state[_k] = True

_hero_expanded = st.session_state.get("_hero_expanded", results_df.empty)
if not results_df.empty and not st.session_state.get("_hero_expanded_seen"):
    _hero_expanded = False
    st.session_state["_hero_expanded"] = False
    st.session_state["_hero_expanded_seen"] = True

if _hero_expanded:
    with st.container(border=True):
        st.markdown("<div class='hero-card'>", unsafe_allow_html=True)

        # ---------- BLOCK 1 — Input Source ----------
        st.markdown("<div class='hero-block-label'>◆ Input Source</div>",
                    unsafe_allow_html=True)
        _keywords = render_input_source_sidebar(
            "app_kw",
            manual_label="One keyword per line",
            manual_placeholder="One keyword per line",
            manual_height=120,
        )
        csv = None
        text = "\n".join(_keywords)

        # Repeated keywords are dropped by default so a scan never pays twice for
        # the same query. That is wrong for load testing — pasting one keyword 500
        # times would run a single scrape — so it can be turned off here.
        _dupe_n = len(_keywords) - len(set(_keywords))
        st.checkbox(
            f"Keep duplicate keywords{f'  ({_dupe_n} repeat lines)' if _dupe_n else ''}",
            key="allow_duplicate_keywords",
            help="Off: repeats are collapsed to one scrape each (normal scans). "
                 "On: every line is scraped, including repeats — for load testing.",
        )

        # ---------- BLOCK 2 — Tracking Terms ----------
        st.markdown(
            "<div class='hero-block-gap'></div>"
            "<div class='hero-block-label'>◆ Tracking Terms</div>",
            unsafe_allow_html=True,
        )
        saved_terms = get_tracked_terms()
        tracked_raw = st.text_area(
            "Brands or domains to match",
            value=st.session_state.get(
                "tracked_terms_input", "\n".join(saved_terms)
            ),
            height=100,
            placeholder="Brands or domains to match — one per line",
            label_visibility="collapsed",
            key="tracked_terms_input",
        )
        _typed_terms = parse_terms_input(tracked_raw)
        # Defensive: only overwrite settings.json when the user actually
        # provides terms. Empty textarea never wipes a saved list.
        if _typed_terms:
            if _typed_terms != saved_terms:
                set_tracked_terms(_typed_terms)
                saved_terms = _typed_terms
            _effective_terms = _typed_terms
        else:
            _effective_terms = saved_terms
        st.caption(
            "Runs are scored on-the-fly against these terms, so historic "
            "scans can be re-analysed against a new brand list."
        )

        # ---------- BLOCK 3 — Platforms ----------
        # Session-state modifications happen INSIDE on_click / on_change
        # callbacks. Streamlit forbids touching a widget's state key from
        # the main script body after the widget has been instantiated,
        # so we can never call `st.session_state[chk_...] = True`
        # inline — that raises StreamlitAPIException.
        def _plat_select_all() -> None:
            for _p in _ORDERED_PLATFORMS:
                st.session_state[f"chk_{_p}"] = True

        def _plat_clear() -> None:
            # At-least-one guard — first platform stays on.
            for _p in _ORDERED_PLATFORMS:
                st.session_state[f"chk_{_p}"] = False
            st.session_state[f"chk_{_ORDERED_PLATFORMS[0]}"] = True

        def _plat_toggle_guard(p: str) -> None:
            """on_change hook — if the user's toggle leaves zero platforms
            selected, re-check the one they just clicked."""
            if not any(
                st.session_state.get(f"chk_{_x}", False)
                for _x in _ORDERED_PLATFORMS
            ):
                st.session_state[f"chk_{p}"] = True
                st.session_state["_plat_warn"] = True

        st.markdown(
            "<div class='hero-block-gap'></div>"
            "<div class='hero-block-label'>Platforms</div>"
            "<div class='hero-platform-row-anchor'></div>",
            unsafe_allow_html=True,
        )
        _plat_cols = st.columns([1, 1, 1, 1, 1])
        for _i, _p in enumerate(_ORDERED_PLATFORMS):
            with _plat_cols[_i]:
                # Dot rendered as a colored HTML span above the checkbox row.
                st.markdown(
                    f"<span class='hero-plat-dot' "
                    f"style='background:{_PLAT_DOT_COLORS[_p]};'></span>",
                    unsafe_allow_html=True,
                )
                chosen[_p] = st.checkbox(
                    PLATFORM_LABEL.get(_p, _p),
                    key=f"chk_{_p}",
                    on_change=_plat_toggle_guard,
                    args=(_p,),
                )
        with _plat_cols[4]:
            _sa1, _sa2 = st.columns(2)
            with _sa1:
                st.button(
                    "Select all",
                    key="plat_sel_all",
                    on_click=_plat_select_all,
                    use_container_width=True,
                )
            with _sa2:
                st.button(
                    "Clear",
                    key="plat_clear",
                    on_click=_plat_clear,
                    use_container_width=True,
                )

        # Show the "pick at least one platform" hint if the on_change guard
        # had to re-check a platform on the previous interaction.
        if st.session_state.pop("_plat_warn", False):
            st.warning("Pick at least one platform.")

        # ---------- BLOCK 4 — Run ----------
        st.markdown("<div class='hero-block-gap'></div>",
                    unsafe_allow_html=True)
        _n_kw = len(_keywords)
        _n_pl = sum(1 for v in chosen.values() if v)
        _n_calls = _n_kw * _n_pl
        _sum_col, _cta_col = st.columns([3, 1])
        with _sum_col:
            st.markdown(
                "<div class='hero-run-summary'>"
                f"<b>{_n_kw:,}</b> keywords × <b>{_n_pl}</b> platforms · "
                f"<b>{_n_calls:,}</b> calls"
                "</div>",
                unsafe_allow_html=True,
            )
        with _cta_col:
            # Disabled unless we have ≥1 keyword AND ≥1 tracking term AND
            # ≥1 platform. Tooltip names the first unmet condition.
            _can_run = _n_kw > 0 and _n_pl > 0 and bool(_effective_terms)
            _run_tip = None
            if _n_kw == 0:
                _run_tip = "Add at least one keyword above."
            elif not _effective_terms:
                _run_tip = "Add at least one tracking term."
            elif _n_pl == 0:
                _run_tip = "Pick at least one platform."
            run_btn = st.button(
                "Run scan",
                type="primary",
                disabled=not _can_run,
                help=_run_tip,
                use_container_width=True,
                key="run_scan_btn",
            )

        st.markdown("</div>", unsafe_allow_html=True)
else:
    # ---------- Collapsed hero — one summary row + Edit setup ----------
    _saved_now = get_tracked_terms()
    _effective_terms = _saved_now
    saved_terms = _saved_now
    _keywords = []
    text = ""
    csv = None
    for _p in _ORDERED_PLATFORMS:
        chosen[_p] = st.session_state.get(f"chk_{_p}", True)
    _plats_txt = ", ".join(
        PLATFORM_LABEL.get(p, p)
        for p in _ORDERED_PLATFORMS if st.session_state.get(f"chk_{p}", True)
    )
    _last_ts = runs[0]["ts"] if runs else None
    _last_txt = (
        datetime.fromtimestamp(_last_ts).strftime("%d %b · %H:%M")
        if _last_ts else "—"
    )
    _sum_col, _btn_col = st.columns([4, 1])
    with _sum_col:
        st.markdown(
            "<div class='hero-collapsed'>"
            f"<span>{len(_saved_now)} tracked terms · "
            f"{_plats_txt} · last run {_last_txt}</span>"
            "</div>",
            unsafe_allow_html=True,
        )
    with _btn_col:
        if st.button("Edit setup", key="hero_edit_btn",
                     use_container_width=True):
            st.session_state["_hero_expanded"] = True
            st.rerun()
    run_btn = False


# Refresh tracked_terms after the hero: the Tracking Terms textarea may
# have persisted new terms via set_tracked_terms; downstream sections
# (Overview KPIs, Platform breakdown, Citation matrix, etc.) read this.
tracked_terms = get_tracked_terms()


# ============================================================
# Run handler — runs AFTER the hero setup card so it can read the
# current values of `run_btn`, `csv`, `text`, `chosen`, `headless`.
# ============================================================
if run_btn:
    prompts = collect_prompts(csv, text)
    platforms = [p for p, on in chosen.items() if on]
    if not prompts:
        st.error("No keywords provided.")
    elif not platforms:
        st.error("Pick at least one platform.")
    else:
        _scan_t0 = time.monotonic()
        with st.spinner(f"Scanning {len(prompts)} keywords × {len(platforms)} platforms…"):
            rows_out = run_scrape(prompts, platforms, headless)
        _scan_s = time.monotonic() - _scan_t0
        # Snapshot the input-source config + any metadata columns pulled
        # from the Sheet/Excel (Search Volume, Difficulty, etc.). Metadata
        # gets persisted with the run so Compare Reports can add columns
        # to the Keyword Level table.
        from core.gsheets import (  # noqa: E402
            snapshot_terms_source_config, get_keyword_metadata,
        )
        from core.settings import set_terms_source_config  # noqa: E402
        _src_cfg = snapshot_terms_source_config("app_kw")
        _md_cols, _md_by_kw = get_keyword_metadata("app_kw")
        set_terms_source_config(_src_cfg)
        _run_metadata_payload = {
            "columns": _md_cols,
            "by_keyword": _md_by_kw,
        } if _md_cols else None
        save_results(
            rows_out,
            terms_source_config=_src_cfg,
            keyword_metadata=_run_metadata_payload,
        )
        # Bust caches so we see the fresh run.
        _bump_token()
        # After save_results, the rows we just stored are tagged with a fresh
        # `ts`. Find that ts and signal the next rerun to make it the active
        # view — so the user sees ONLY the data they just scraped.
        latest = list_runs(limit=1)
        if latest:
            st.session_state["pending_run_ts"] = latest[0]["ts"]
        # Stash timing for the post-rerun view: st.rerun() below wipes anything
        # written here, so the summary has to survive in session_state.
        _by_plat: dict[str, list[float]] = {}
        for _r in rows_out:
            _by_plat.setdefault(_r.get("platform", "?"), []).append(
                float(_r.get("duration_s") or 0.0)
            )
        st.session_state["last_scan_timing"] = {
            "total_s": round(_scan_s, 1),
            "rows": len(rows_out),
            "prompts": len(prompts),
            "platforms": {
                p: {
                    "n": len(v),
                    "avg": round(sum(v) / len(v), 1) if v else 0.0,
                    "max": round(max(v), 1) if v else 0.0,
                    "total": round(sum(v), 1),
                }
                for p, v in sorted(_by_plat.items())
            },
        }
        st.success(f"Done. {len(rows_out)} responses captured.")
        st.rerun()


# ============================================================
# Empty state — shown when no run is selected (the default)
# ============================================================
if results_df.empty:
    has_history = bool(historic_options)
    primary_cta = (
        "Fill in the setup above and click <b>Run scan</b>."
    )
    secondary_cta = (
        "Or pick a previous run from <b>Scan history</b> at the bottom of the page."
        if has_history else ""
    )
    st.markdown(
        f"""
        <div class="empty">
          <h4>Run a scan to populate</h4>
          <div style="margin-bottom:8px;">{primary_cta}</div>
          <div style="color:var(--text-3); font-size:12px;">{secondary_cta}</div>
        </div>
        """,
        unsafe_allow_html=True,
    )
else:
    # ============================================================
    # Scrape timing — how long the last scan took, overall and per platform.
    # Shown once, right after a scan (survives the st.rerun via session_state).
    # ============================================================
    _timing = st.session_state.get("last_scan_timing")
    if _timing:
        def _dur(s: float) -> str:
            s = float(s or 0)
            if s < 60:
                return f"{s:.1f}s"
            m, sec = divmod(int(round(s)), 60)
            return f"{m}m {sec:02d}s"

        _plats = _timing.get("platforms") or {}
        # Wall-clock beats the sum of per-scrape times because prompts run in
        # parallel — say so, or the two numbers look contradictory.
        _serial = sum(p["total"] for p in _plats.values())
        with st.expander(
            f"⏱  Last scan: {_dur(_timing['total_s'])} "
            f"· {_timing['rows']} responses "
            f"· {_timing['prompts']} keywords",
            expanded=False,
        ):
            if _plats:
                _cols = st.columns(len(_plats))
                for _c, (_p, _st) in zip(_cols, _plats.items()):
                    _c.metric(
                        _p,
                        _dur(_st["avg"]),
                        help=(f"average per scrape · slowest {_dur(_st['max'])} "
                              f"· {_st['n']} scrapes · {_dur(_st['total'])} total"),
                    )
            st.caption(
                f"Wall clock {_dur(_timing['total_s'])} vs {_dur(_serial)} of "
                f"combined scrape time — the gap is the parallelism "
                f"(SCRAPE_CONCURRENCY, currently {_default_concurrency()} prompts "
                f"at a time). Per-scrape times are in the results table's "
                f"`duration_s` column."
            )

    # ============================================================
    # Compute per-row tracking status & citation matrix
    # ============================================================
    annotated = results_df.copy()
    annotated["_cited"] = False
    annotated["_pos"] = None
    annotated["_where"] = ""
    for idx, row in annotated.iterrows():
        cited, pos, where = row_citation_position(row, tracked_terms)
        annotated.at[idx, "_cited"] = cited
        annotated.at[idx, "_pos"] = pos if pos is not None else (0 if cited else -1)
        annotated.at[idx, "_where"] = where

    # ============================================================
    # KPI grid
    # ============================================================
    # Hoisted once — used by the "Where you ARE cited" section, the matrix,
    # and the URL leaderboard. Defining it later caused a NameError when the
    # appearances section tried to build its platform filter.
    platforms_in_data = sorted(annotated["platform"].unique().tolist())

    n_queries = len(annotated)
    n_prompts_unique = annotated["prompt"].nunique()
    n_platforms_unique = annotated["platform"].nunique()
    n_cited = int(annotated["_cited"].sum())
    citation_rate = (n_cited / n_queries * 100) if n_queries else 0.0
    cited_with_pos = annotated[(annotated["_cited"]) & (annotated["_pos"].apply(lambda x: isinstance(x, int) and x > 0))]
    avg_pos = cited_with_pos["_pos"].mean() if not cited_with_pos.empty else None

    # Gap keywords = prompts where no platform cited a tracked term.
    if tracked_terms:
        gap_prompts = (
            annotated.groupby("prompt")["_cited"].max()
            .reset_index(name="any_cited")
        )
        n_gap = int((~gap_prompts["any_cited"]).sum())
    else:
        n_gap = 0

    avg_pos_label = f"#{avg_pos:.1f}" if avg_pos is not None else "—"
    citation_rate_color = "green" if citation_rate >= 40 else ("red" if citation_rate < 20 and tracked_terms else "")
    gap_color = "red" if n_gap > 0 else "green"

    # ============================================================
    # Floating TOC — right rail, click to jump to a section.
    # Collapsed by default (just dots), expands on hover to show labels.
    # Hidden on viewports < 1280px so it doesn't crowd the content.
    # ============================================================
    _toc_items = [
        ("ai-visibility-score", "Summary"),
        ("ai-share-of-voice",   "Platform breakdown"),
        ("gaps",                "Gap keywords"),
        ("appearances",         "Where you're cited"),
        ("top-sources",         "Top sources"),
        ("citation-matrix",     "Citation matrix"),
        ("url-leaderboard",     "URL leaderboard"),
        ("fanout-queries",      "Fanout queries"),
        ("full-response",       "Full response"),
        ("scan-history",        "Scan history"),
    ]
    _toc_html = (
        "<nav class='toc-float' aria-label='Section navigation'>"
        "<div class='toc-head'><span class='toc-label'>Jump to</span></div>"
        "<ul>"
        + "".join(
            f"<li><a href='#{slug}'>"
            f"<span class='toc-dot'></span>"
            f"<span class='toc-label'>{html.escape(label)}</span>"
            f"</a></li>"
            for slug, label in _toc_items
        )
        + "</ul></nav>"
    )
    st.markdown(_toc_html, unsafe_allow_html=True)

    # ============================================================
    # Overview KPI row — single shared component, one summary layer only.
    # Delta is computed vs the previous run and shown as a tooltip on the pill.
    # ============================================================
    # Convert once to dicts so compute_shape can iterate cheaply per platform.
    _annotated_rows: list[dict] = [row.to_dict() for _, row in annotated.iterrows()]
    curr_shape = _ui_compute_shape(_annotated_rows, tracked_terms)

    # Per-platform shapes for the merged platform table below.
    curr_by_plat: dict[str, dict] = {}
    for _p in sorted({r.get("platform", "") for r in _annotated_rows}):
        curr_by_plat[_p] = _ui_compute_shape(
            [r for r in _annotated_rows if r.get("platform") == _p],
            tracked_terms,
        )

    # Previous run (for KPI deltas). Skipped in the "All runs combined" view.
    prev_shape: dict | None = None
    _prev_date_txt: str | None = None
    if tracked_terms and sel.get("ts") not in (None, -1):
        _older = [r for r in runs if r.get("ts") and r["ts"] < sel["ts"]]
        if _older:
            _prev_run = _older[0]
            try:
                _prev_raw_rows = _cached_load_run(_prev_run["ts"])
                _prev_dfs = _cached_dataframes(
                    f"{_prev_run['ts']}::{_cache_token()}::{len(_prev_raw_rows)}",
                    _prev_raw_rows,
                )
                _prev_results = _prev_dfs["results"]
                _prev_rows_dicts = [row.to_dict() for _, row in _prev_results.iterrows()]
                prev_shape = _ui_compute_shape(_prev_rows_dicts, tracked_terms)
                _prev_date_txt = datetime.fromtimestamp(_prev_run["ts"]).strftime("%d %b")
            except Exception:
                prev_shape = None

    _delta_tt = f"vs {_prev_date_txt}" if _prev_date_txt else None

    # Anchor kept so the TOC still links to the KPI row.
    st.markdown(
        "<div class='toc-anchor' id='ai-visibility-score'></div>",
        unsafe_allow_html=True,
    )
    st.markdown(
        _ui_kpi_grid_html(prev_shape, curr_shape, mode="current",
                          delta_tooltip=_delta_tt),
        unsafe_allow_html=True,
    )

    # Retained for downstream sections that still reference these locals.
    total_citations = curr_shape["n_urls_total"]
    tracked_citation_count = curr_shape["n_urls_tracked"]
    share_of_voice = curr_shape["sov"]

    # ============================================================
    # Platform breakdown — merged single table.
    # Replaces the old "AI Share of Voice" table + "Citation rate by LLM"
    # stacked bar. Columns: Citation rate | Mention rate | Avg position | SoV.
    # Bar visualization only in the SoV column, as per the design.
    # ============================================================
    if tracked_terms:
        _plats_present = list(curr_by_plat.keys())
        _ordered_plats = [p for p in _UI_PLATFORM_ROW_ORDER if p in _plats_present]
        for _p in sorted(_plats_present):
            if _p not in _ordered_plats:
                _ordered_plats.append(_p)

        _body_rows: list[str] = []
        for _p in _ordered_plats:
            _pm = curr_by_plat.get(_p)
            _body_rows.append(
                "<tr>"
                f"<td>{_ui_plat_pill_html(_p)}</td>"
                + _ui_kpi_single_cell(_pm, "citation_rate")
                + _ui_kpi_single_cell(_pm, "mention_rate")
                + _ui_kpi_single_cell(_pm, "keyword_position")
                + _ui_kpi_single_cell(_pm, "share_of_voice", show_bar=True)
                + "</tr>"
            )

        st.markdown(
            """
            <div class="section-header toc-anchor" id="ai-share-of-voice">
              <h2 class="section-title">Platform breakdown</h2>
              <span class="section-meta">◆ One row per LLM · this run</span>
            </div>
            """,
            unsafe_allow_html=True,
        )
        st.markdown(
            "<div class='diff-table-wrap'><table class='diff-table'>"
            "<thead><tr>"
            "<th>Platform</th>"
            "<th class='num'>Citation rate</th>"
            "<th class='num'>Mention rate</th>"
            "<th class='num'>Avg position</th>"
            "<th class='num'>Share of voice</th>"
            "</tr></thead>"
            f"<tbody>{''.join(_body_rows)}</tbody></table></div>",
            unsafe_allow_html=True,
        )

        # CSV download for the merged platform table.
        _csv_records = []
        for _p in _ordered_plats:
            _pm = curr_by_plat.get(_p) or {}
            _csv_records.append({
                "Platform": _UI_PLATFORM_LABEL.get(_p, _p),
                "Citation rate %": f"{_pm.get('url_cited_rate', 0):.0f}%",
                "Mention rate %": f"{_pm.get('mention_rate', 0):.0f}%",
                "Avg position": (
                    f"{_pm['avg_position']:.1f}" if _pm.get("avg_position") is not None else "—"
                ),
                "Share of voice %": f"{_pm.get('sov', 0):.0f}%",
                "URLs cited": _pm.get("n_urls_tracked", 0),
                "URLs total": _pm.get("n_urls_total", 0),
                "Mentions": _pm.get("mention_count", 0),
                "Keywords ranked": _pm.get("keywords_ranked", 0),
            })
        if _csv_records:
            _csv_bytes = pd.DataFrame(_csv_records).to_csv(index=False).encode("utf-8")
            _dl_l, _dl_r = st.columns([3, 1])
            with _dl_r:
                st.download_button(
                    "Download platform CSV",
                    data=_csv_bytes,
                    file_name=f"platforms_{int(time.time())}.csv",
                    mime="text/csv",
                    key="platform_csv_download",
                    use_container_width=True,
                )

    # Anchor kept so the TOC "Overview KPIs" link still works.
    st.markdown(
        "<div class='toc-anchor' id='overview'></div>",
        unsafe_allow_html=True,
    )

    # If no tracked terms set, show a hint banner.
    if not tracked_terms:
        st.markdown(
            "<div style='background:var(--amber-bg); color:var(--amber); border:1px solid #E6D9A8; "
            "border-radius:8px; padding:10px 14px; font-size:13px; margin-bottom:20px;'>"
            "Add brands or domains to <b>Track these</b> in the sidebar to unlock the platform breakdown."
            "</div>",
            unsafe_allow_html=True,
        )

    # ============================================================
    # Excel download
    # ============================================================
    dl_col1, dl_col2 = st.columns([3, 1])
    with dl_col2:
        xlsx_bytes = build_workbook(rows, tracked_terms)
        ts_label = datetime.now().strftime("%Y%m%d_%H%M")
        st.download_button(
            "⬇ Download Excel report",
            data=xlsx_bytes,
            file_name=f"citerion_{ts_label}.xlsx",
            mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            use_container_width=True,
        )

    # ============================================================
    # "Where you're NOT cited" panel — most prominent gap visualisation
    # ============================================================
    if tracked_terms:
        gap_kw_list = (
            annotated.groupby("prompt")["_cited"].max()
            .reset_index(name="any_cited")
        )
        gap_kws = gap_kw_list[~gap_kw_list["any_cited"]]["prompt"].tolist()
        partial_kw_df = (
            annotated.groupby("prompt").agg(
                cited_count=("_cited", "sum"),
                total=("_cited", "count"),
            ).reset_index()
        )
        partial_kws = partial_kw_df[
            (partial_kw_df["cited_count"] > 0) &
            (partial_kw_df["cited_count"] < partial_kw_df["total"])
        ]["prompt"].tolist()
        full_kws = partial_kw_df[
            partial_kw_df["cited_count"] == partial_kw_df["total"]
        ]["prompt"].tolist()

        gap_chips = "".join(
            f"<span style='display:inline-block;background:var(--red-bg);color:var(--red);"
            f"border:1px solid var(--red-border);padding:4px 11px;border-radius:999px;font-size:12px;"
            f"margin:3px 4px 3px 0;font-family:Geist Mono, monospace;'>{html.escape(k)}</span>"
            for k in gap_kws[:60]
        )
        partial_chips = "".join(
            f"<span style='display:inline-block;background:var(--amber-bg);color:var(--amber);"
            f"border:1px solid var(--amber-border);padding:4px 11px;border-radius:999px;font-size:12px;"
            f"margin:3px 4px 3px 0;font-family:Geist Mono, monospace;'>{html.escape(k)}</span>"
            for k in partial_kws[:60]
        )

        st.markdown(
            f"""
            <div class="card toc-anchor" id="gaps" style="border-top:3px solid var(--red); margin-top:18px;">
              <div style="display:flex;align-items:baseline;justify-content:space-between;margin-bottom:10px;">
                <h3 style="margin:0;font-size:15px;font-weight:600;">Where you're <span style="color:var(--red);">not cited</span></h3>
                <span class="section-meta">{len(gap_kws)} gap · {len(partial_kws)} partial · {len(full_kws)} full coverage</span>
              </div>
              <div style="display:flex;gap:30px;flex-wrap:wrap;align-items:flex-start;">
                <div style="flex:1;min-width:260px;">
                  <div style="font-size:11px;color:var(--text-2);text-transform:uppercase;letter-spacing:.05em;margin-bottom:6px;font-weight:500;">
                    No mentions ({len(gap_kws)})
                  </div>
                  <div>{gap_chips if gap_chips else "<i style='color:var(--green);font-size:12px;'>Every keyword is cited on at least one platform.</i>"}</div>
                </div>
                <div style="flex:1;min-width:260px;">
                  <div style="font-size:11px;color:var(--text-2);text-transform:uppercase;letter-spacing:.05em;margin-bottom:6px;font-weight:500;">
                    Partial — missing on some LLMs ({len(partial_kws)})
                  </div>
                  <div>{partial_chips if partial_chips else "<i style='color:var(--green);font-size:12px;'>Every keyword is cited on every platform present.</i>"}</div>
                </div>
              </div>
            </div>
            """,
            unsafe_allow_html=True,
        )

    # ============================================================
    # Tracked-term appearances — inverse of the "not cited" panel.
    # Shows every (tracked-term × keyword × platform) hit with where the
    # match landed (text vs URL position) and the matched URL. Lets the
    # user audit exactly when their brand surfaced and through which LLM.
    # ============================================================
    if tracked_terms:
        appearances: list[dict] = []
        for _, row in annotated.iterrows():
            text = (row.get("response_text") or "").lower()
            cit_list = list(row.get("citations_list", []) or [])
            src_list = list(row.get("source_urls_list", []) or [])
            combined: list[str] = []
            seen_u: set[str] = set()
            for u in cit_list + src_list:
                if u and u not in seen_u:
                    seen_u.add(u); combined.append(u)

            for term in tracked_terms:
                if not term:
                    continue
                tl = term.lower()
                in_text = tl in text
                matched_urls = [(i, u) for i, u in enumerate(combined, start=1)
                                if tl in u.lower()]
                if not in_text and not matched_urls:
                    continue
                if matched_urls:
                    for pos, u in matched_urls:
                        appearances.append({
                            "term": term,
                            "prompt": row["prompt"],
                            "platform": row["platform"],
                            "where": "text+url" if in_text else "url",
                            "position": pos,
                            "url": u,
                        })
                else:
                    appearances.append({
                        "term": term,
                        "prompt": row["prompt"],
                        "platform": row["platform"],
                        "where": "text",
                        "position": None,
                        "url": "",
                    })

        # Per-term summary header.
        term_counts = Counter(a["term"] for a in appearances)
        unique_kws_per_term: dict[str, set[str]] = {}
        for a in appearances:
            unique_kws_per_term.setdefault(a["term"], set()).add(a["prompt"])

        st.markdown(
            f"""
            <div class="section-header" style="margin-top:28px;">
              <h2 class="section-title toc-anchor" id="appearances">Where you ARE cited</h2>
              <span class="section-meta">{len(appearances)} appearance{'s' if len(appearances) != 1 else ''} · {len(term_counts)} term{'s' if len(term_counts) != 1 else ''} matched</span>
            </div>
            """,
            unsafe_allow_html=True,
        )

        if not appearances:
            st.markdown(
                "<div class='empty' style='padding:24px;'>"
                "<div style='color:var(--text-3); font-size:13px;'>"
                "No tracked-term appearances yet. Your brand didn't surface in any "
                "response text or cited URL for this run."
                "</div></div>",
                unsafe_allow_html=True,
            )
        else:
            # Roll appearances up to one row per tracked term, with all
            # cited prompts collapsed into a single chip cell — same
            # pattern as the URL leaderboard's keyword cell.
            per_term: dict[str, dict] = {}
            for a in appearances:
                bucket = per_term.setdefault(a["term"], {
                    "prompts": Counter(),       # prompt → hit count
                    "platforms": set(),
                    "url_hits": 0,
                    "text_hits": 0,
                    "textplus_hits": 0,
                    "sample_urls": {},          # prompt → first cited URL
                })
                bucket["prompts"][a["prompt"]] += 1
                bucket["platforms"].add(a["platform"])
                if a["where"] == "url":
                    bucket["url_hits"] += 1
                elif a["where"] == "text":
                    bucket["text_hits"] += 1
                elif a["where"] == "text+url":
                    bucket["textplus_hits"] += 1
                if a["url"] and a["prompt"] not in bucket["sample_urls"]:
                    bucket["sample_urls"][a["prompt"]] = a["url"]

            # Filters: term substring + platform.
            af_col1, af_col2 = st.columns([2, 1])
            with af_col1:
                appear_term_search = st.text_input(
                    "Filter terms…",
                    value="",
                    placeholder="filter tracked terms",
                    label_visibility="collapsed",
                    key="appear_term_search",
                )
            with af_col2:
                plat_options_appear = ["All platforms"] + [PLATFORM_LABEL.get(p, p) for p in platforms_in_data]
                appear_plat = st.selectbox(
                    "Platform",
                    options=plat_options_appear,
                    label_visibility="collapsed",
                    key="appear_plat_filter",
                )
            appear_plat_key = (
                None if appear_plat == "All platforms"
                else next((p for p, lbl in PLATFORM_LABEL.items() if lbl == appear_plat), appear_plat)
            )

            q = appear_term_search.strip().lower()

            # Build rows. Re-aggregate with the platform filter applied if
            # a specific platform is picked, so the per-row counts match
            # the filter.
            def _build_rows() -> list[str]:
                rows_html: list[str] = []
                ordered_terms = sorted(
                    per_term.keys(),
                    key=lambda t: (-len(per_term[t]["prompts"]), t.lower()),
                )
                for term in ordered_terms:
                    if q and q not in term.lower():
                        continue
                    if appear_plat_key:
                        # Recompute per-platform projection.
                        sub_prompts: Counter = Counter()
                        url_n = text_n = textplus_n = 0
                        sample_urls: dict = {}
                        for a in appearances:
                            if a["term"] != term:
                                continue
                            if a["platform"] != appear_plat_key:
                                continue
                            sub_prompts[a["prompt"]] += 1
                            if a["where"] == "url": url_n += 1
                            elif a["where"] == "text": text_n += 1
                            elif a["where"] == "text+url": textplus_n += 1
                            if a["url"] and a["prompt"] not in sample_urls:
                                sample_urls[a["prompt"]] = a["url"]
                        if not sub_prompts:
                            continue
                        prompts_ctr = sub_prompts
                        platforms = {appear_plat_key}
                        url_hits, text_hits, textplus_hits = url_n, text_n, textplus_n
                    else:
                        b = per_term[term]
                        prompts_ctr = b["prompts"]
                        platforms = b["platforms"]
                        url_hits = b["url_hits"]
                        text_hits = b["text_hits"]
                        textplus_hits = b["textplus_hits"]
                        sample_urls = b["sample_urls"]

                    sorted_prompts = sorted(
                        prompts_ctr.items(), key=lambda kv: kv[1], reverse=True,
                    )
                    visible = sorted_prompts[:3]
                    hidden = sorted_prompts[3:]

                    def _kw_chip(p: str, n: int) -> str:
                        truncated = p[:50] + ("…" if len(p) > 50 else "")
                        sample = sample_urls.get(p, "")
                        tip_parts = [p, f"cited {n}×"]
                        if sample:
                            tip_parts.append(domain_of(sample) or sample)
                        return (
                            f"<span class='kw-chip' title='{html.escape(' · '.join(tip_parts))}'>"
                            f"{bold_tracked(truncated, tracked_terms)}"
                            + (f"<span class='kw-chip-n'>×{n}</span>" if n > 1 else "")
                            + "</span>"
                        )

                    kw_chips = "".join(_kw_chip(p, n) for p, n in visible)
                    if hidden:
                        hidden_chips = "".join(_kw_chip(p, n) for p, n in hidden)
                        kw_chips += (
                            f"<details class='kw-overflow'>"
                            f"<summary class='kw-chip kw-more' "
                            f"title='Show {len(hidden)} more'>+{len(hidden)}</summary>"
                            f"<div class='kw-overflow-list'>{hidden_chips}</div>"
                            f"</details>"
                        )

                    plat_pills = "".join(
                        f"<span class='llm-badge {p}' style='margin-right:4px;'>"
                        f"{html.escape(PLATFORM_LABEL.get(p, p))}</span>"
                        for p in sorted(platforms)
                    )

                    total_hits = url_hits + text_hits + textplus_hits
                    breakdown_bits: list[str] = []
                    if url_hits:
                        breakdown_bits.append(f"{url_hits} url")
                    if text_hits:
                        breakdown_bits.append(f"{text_hits} text")
                    if textplus_hits:
                        breakdown_bits.append(f"{textplus_hits} both")
                    breakdown = (
                        f"<div style='font-size:10px;color:var(--text-3);"
                        f"font-family:Geist Mono,monospace;margin-top:3px;'>"
                        f"{' · '.join(breakdown_bits)}</div>"
                        if breakdown_bits else ""
                    )

                    rows_html.append(
                        f"<tr>"
                        f"<td style='width:160px;'>"
                        f"<span class='kw-tracked-chip'>{html.escape(term)}</span>"
                        f"</td>"
                        f"<td>"
                        f"<div class='kw-cell'>"
                        f"<span class='kw-count'>{len(sorted_prompts)} kw</span>"
                        f"<div class='kw-chips'>{kw_chips}</div>"
                        f"</div>"
                        f"</td>"
                        f"<td class='col-center' style='width:170px;'>{plat_pills}</td>"
                        f"<td class='col-right' style='width:110px;'>"
                        f"<span class='count-badge high'>{total_hits}</span>"
                        f"{breakdown}"
                        f"</td>"
                        f"</tr>"
                    )
                return rows_html

            body_rows = _build_rows()
            if not body_rows:
                st.markdown(
                    "<div class='tbl'><table><tbody>"
                    "<tr><td colspan='4' style='padding:24px; text-align:center; color:var(--text-3);'>"
                    "No tracked terms match this filter.</td></tr>"
                    "</tbody></table></div>",
                    unsafe_allow_html=True,
                )
            else:
                st.markdown(
                    "<div class='tbl'><table>"
                    "<thead><tr>"
                    "<th style='width:160px;'>Tracked term</th>"
                    "<th>Keywords cited on</th>"
                    "<th class='col-center' style='width:170px;'>Platforms</th>"
                    "<th class='col-right' style='width:110px;'>Hits</th>"
                    "</tr></thead>"
                    f"<tbody>{''.join(body_rows)}</tbody>"
                    "</table></div>",
                    unsafe_allow_html=True,
                )

    # ============================================================
    # Top sources cited — bubble chart. The stacked "citation rate by LLM"
    # bar was removed: the merged platform table above already exposes that.
    # ============================================================
    if tracked_terms and not annotated.empty:
        st.markdown(
            "<div class='section-header toc-anchor' id='top-sources' style='margin:24px 0 8px;'>"
            "<h2 class='section-title'>Top sources cited</h2>"
            "<span class='section-meta'>◆ Top 20 URLs by mention volume · yours vs competitors</span>"
            "</div>"
            "<div class='toc-anchor' id='citation-rate'></div>",
            unsafe_allow_html=True,
        )
        url_counts = Counter()
        for _, rr in annotated.iterrows():
            for u in (list(rr.get("citations_list", []) or []) +
                      list(rr.get("source_urls_list", []) or [])):
                if u:
                    url_counts[u] += 1
        if url_counts:
            bub_data = []
            for u, cnt in url_counts.most_common(20):
                bub_data.append({
                    "domain": (urlparse(u).netloc or u)[:32],
                    "mentions": cnt,
                    "tracked": "Yours" if is_tracked_url(u, tracked_terms) else "Competitors",
                    "url": u,
                })
            df_bub = pd.DataFrame(bub_data)
            fig2 = px.scatter(
                df_bub, x="mentions", y="domain", size="mentions",
                color="tracked",
                color_discrete_map={"Yours": "#8b5cf6", "Competitors": "#6b6b73"},
                hover_data=["url"],
                size_max=36,
            )
            fig2.update_layout(
                height=max(220, 22 * len(df_bub)),
                margin=dict(l=4, r=4, t=8, b=4),
                paper_bgcolor="rgba(0,0,0,0)", plot_bgcolor="rgba(0,0,0,0)",
                legend=dict(
                    orientation="h", yanchor="bottom", y=1.02, xanchor="right", x=1,
                    font=dict(color="#a1a1aa"),
                ),
                font=dict(family="Geist, sans-serif", color="#f5f5f7", size=12),
                xaxis=dict(title="mentions", showgrid=True, gridcolor="rgba(255,255,255,0.06)", color="#a1a1aa"),
                yaxis=dict(title="", showgrid=False, autorange="reversed", color="#f5f5f7"),
            )
            st.plotly_chart(fig2, use_container_width=True, config={"displayModeBar": False})
        else:
            st.caption("Run a scan to populate this chart.")

    # ============================================================
    # Citation matrix
    # ============================================================
    st.markdown(
        f"""
        <div class="section-header">
          <h2 class="section-title toc-anchor" id="citation-matrix">Citation matrix</h2>
          <span class="section-meta">{n_prompts_unique} keywords · {n_platforms_unique} platforms</span>
        </div>
        """,
        unsafe_allow_html=True,
    )

    mf_col1, mf_col2 = st.columns([1, 1])
    with mf_col1:
        matrix_filter = st.radio(
            "filter",
            options=["All", "Cited only", "Gaps only"],
            horizontal=True,
            label_visibility="collapsed",
            key="matrix_filter",
        )
    with mf_col2:
        matrix_search = st.text_input(
            "Search keywords",
            placeholder="Search keywords…",
            label_visibility="collapsed",
            key="matrix_search",
        )

    matrix_search_norm = (matrix_search or "").strip().lower()
    all_prompts = annotated["prompt"].unique().tolist()
    prompts_sorted = sorted(
        p for p in all_prompts
        if not matrix_search_norm or matrix_search_norm in p.lower()
    )
    # platforms_in_data is hoisted earlier (just after annotated is built).

    def _matrix_html() -> str:
        head_cols = "".join(
            f"<th class='col-center'>{html.escape(PLATFORM_LABEL.get(p, p))}</th>"
            for p in platforms_in_data
        )
        total_cols = len(platforms_in_data) + 2  # Keyword + score + N platforms
        rows_html = []
        for prm in prompts_sorted:
            sub = annotated[annotated["prompt"] == prm]
            cited_count = int(sub["_cited"].sum())
            total = len(sub)
            if matrix_filter == "Cited only" and cited_count == 0:
                continue
            if matrix_filter == "Gaps only" and cited_count > 0:
                continue
            row_cls = "gap-row" if cited_count == 0 else ""
            gap_icon = (
                "<span style='color:var(--red); margin-right:6px; font-family:Geist Mono;'>!</span>"
                if cited_count == 0 else ""
            )
            cell_cells = []
            for p in platforms_in_data:
                r = sub[sub["platform"] == p]
                if r.empty:
                    cell_cells.append("<td class='col-center' style='color:var(--text-3);font-size:11px;'>—</td>")
                    continue
                rr = r.iloc[0]
                if rr["_cited"]:
                    pos = rr["_pos"]
                    pos_label = f"#{pos}" if isinstance(pos, int) and pos > 0 else "text"
                    cell_cells.append(
                        f"<td class='col-center'>"
                        f"<span class='cell-result cited'>✓ {pos_label}</span>"
                        f"</td>"
                    )
                elif rr.get("error"):
                    # Collapse whitespace BEFORE escaping: newlines/tabs in the
                    # title attribute survive html.escape but break Streamlit's
                    # markdown rendering (newlines inside unsafe_allow_html
                    # HTML get treated as paragraph breaks, splitting the <td>
                    # so the raw tag leaks into the visible cell).
                    err_msg = " ".join(str(rr.get("error") or "").split())[:300]
                    cell_cells.append(
                        f"<td class='col-center' title='{html.escape(err_msg)}'>"
                        f"<span class='cell-result missing'>! error</span>"
                        f"</td>"
                    )
                else:
                    cell_cells.append(
                        "<td class='col-center'>"
                        "<span class='cell-result missing'>✗ missing</span>"
                        "</td>"
                    )
            score_cls = "full" if cited_count == total else ("zero" if cited_count == 0 else "")

            # First pass: collect per-platform URL lists AND compute the
            # cross-platform count for each URL (= how many platforms cited
            # it for this prompt). High counts = "aggressively cited" =
            # surfaces in tooltip and as a badge next to the domain.
            per_plat_urls: dict[str, list[str]] = {}
            url_plat_set: dict[str, set[str]] = {}
            for p in platforms_in_data:
                r = sub[sub["platform"] == p]
                if r.empty:
                    continue
                rr = r.iloc[0]
                merged: list[str] = []
                seen: set[str] = set()
                for u in (list(rr.get("citations_list", []) or [])
                          + list(rr.get("source_urls_list", []) or [])):
                    if u and u not in seen:
                        seen.add(u); merged.append(u)
                if merged:
                    per_plat_urls[p] = merged
                    for u in merged:
                        url_plat_set.setdefault(u, set()).add(p)

            total_urls = sum(len(v) for v in per_plat_urls.values())
            unique_urls = len(url_plat_set)
            max_platforms = len(per_plat_urls) or 1

            def _url_badge(u: str) -> str:
                # "3×" if cited by 3 platforms; visually heated when the URL
                # is cited by every platform that ran for this prompt.
                cnt = len(url_plat_set.get(u, set()))
                heat_cls = "acc-badge-hot" if cnt >= max_platforms and cnt > 1 else ""
                plural = "s" if cnt != 1 else ""
                return (
                    f"<span class='acc-url-badge {heat_cls}' "
                    f"title='Cited by {cnt} platform{plural} for this keyword'>"
                    f"{cnt}×</span>"
                )

            # Build per-platform groups, ordering URLs within each group by
            # cross-platform count desc — heavy hitters surface at the top.
            acc_groups: list[str] = []
            for p in platforms_in_data:
                merged = per_plat_urls.get(p)
                if not merged:
                    continue
                ordered = sorted(
                    merged,
                    key=lambda u: (-len(url_plat_set.get(u, set())), merged.index(u)),
                )
                url_items = "".join(
                    f"<li>"
                    f"<div class='acc-url-head'>"
                    f"<a href='{html.escape(u)}' target='_blank' "
                    f"class='{('tracked' if is_tracked_url(u, tracked_terms) else '')}'"
                    f">{html.escape(domain_of(u) or u)}</a>"
                    f"{_url_badge(u)}"
                    f"</div>"
                    f"<span class='acc-url-full'>{html.escape(u)}</span>"
                    f"</li>"
                    for u in ordered[:50]
                )
                more = (
                    f"<li class='acc-more'>+{len(merged) - 50} more</li>"
                    if len(merged) > 50 else ""
                )
                acc_groups.append(
                    f"<div class='acc-group'>"
                    f"<div class='acc-plat acc-plat-{p}'>"
                    f"<span class='llm-badge {p}'>{html.escape(PLATFORM_LABEL.get(p, p))}</span>"
                    f"<span class='acc-plat-count'>{len(merged)} URL{'s' if len(merged) != 1 else ''}</span>"
                    f"</div>"
                    f"<ul class='acc-urls'>{url_items}{more}</ul>"
                    f"</div>"
                )

            # Tracked-term matches for THIS keyword: in the keyword text
            # itself, or in any URL that any platform cited for it. Surface
            # the matched terms as small chips after the keyword so the user
            # sees exactly which tracking word fired for the row.
            kw_tracked_hits: list[tuple[str, list[str]]] = []
            for t in tracked_terms:
                if not t:
                    continue
                tl = t.lower()
                where: list[str] = []
                if tl in prm.lower():
                    where.append("keyword")
                if any(tl in u.lower() for u in url_plat_set):
                    where.append("url")
                if where:
                    kw_tracked_hits.append((t, where))

            tracked_chips_html = ""
            if kw_tracked_hits:
                parts: list[str] = []
                for t, where in kw_tracked_hits:
                    where_full = ", ".join(where)
                    where_short = ",".join(w[0] for w in where)
                    parts.append(
                        f"<span class='kw-tracked-chip' title='Matches in: {where_full}'>"
                        f"{html.escape(t)}"
                        f"<span class='kw-tracked-where'>{where_short}</span>"
                        f"</span>"
                    )
                tracked_chips_html = (
                    f"<span class='matrix-tracked'>{''.join(parts)}</span>"
                )

            rows_html.append(
                f"<tr class='{row_cls}'>"
                f"<td>{gap_icon}<span class='matrix-keyword'>{bold_tracked(prm, tracked_terms)}</span>{tracked_chips_html}</td>"
                + "".join(cell_cells) +
                f"<td class='col-right'><span class='score-pill {score_cls}'>{cited_count}/{total}</span></td>"
                f"</tr>"
            )
            # Accordion row directly below — uses <details> for native toggle,
            # no JS required. Hidden inside a single colspan'd cell.
            if acc_groups:
                # "X unique · Y total" makes it obvious when the same URL is
                # being cited across multiple platforms — i.e. an aggressive
                # source. Single line keeps the row dense.
                unique_label = f"{unique_urls} unique"
                total_label = (
                    f" · {total_urls} total"
                    if total_urls != unique_urls else ""
                )
                summary_label = (
                    f"<span class='acc-chevron'>▸</span>"
                    f"<span class='acc-label'>View URLs</span>"
                    f"<span class='acc-count'>{unique_label}{total_label}</span>"
                )
                rows_html.append(
                    f"<tr class='matrix-detail'>"
                    f"<td colspan='{total_cols}' style='padding:0;'>"
                    f"<details class='matrix-acc'>"
                    f"<summary>{summary_label}</summary>"
                    f"<div class='acc-body'>{''.join(acc_groups)}</div>"
                    f"</details>"
                    f"</td>"
                    f"</tr>"
                )
            else:
                rows_html.append(
                    f"<tr class='matrix-detail matrix-detail-empty'>"
                    f"<td colspan='{total_cols}' style='padding:0;'>"
                    f"<div class='acc-empty'>No URLs captured for this keyword</div>"
                    f"</td>"
                    f"</tr>"
                )
        if not rows_html:
            rows_html.append(
                f"<tr><td colspan='{total_cols}' style='padding:24px; text-align:center; color:var(--text-3);'>"
                "No rows match this filter.</td></tr>"
            )
        return (
            "<div class='tbl'><table>"
            f"<thead><tr><th>Keyword</th>{head_cols}<th class='col-right'>Score</th></tr></thead>"
            f"<tbody>{''.join(rows_html)}</tbody>"
            "</table></div>"
        )

    st.markdown(_matrix_html(), unsafe_allow_html=True)

    # ============================================================
    # URL leaderboard
    # ============================================================
    # Platform toggle FIRST — it scopes everything below.
    plat_options = ["All platforms"] + [PLATFORM_LABEL.get(p, p) for p in platforms_in_data]
    plat_label_to_key = {PLATFORM_LABEL.get(p, p): p for p in platforms_in_data}

    st.markdown(
        f"""
        <div class="section-header" style="margin-top:32px;">
          <h2 class="section-title toc-anchor" id="url-leaderboard">URL leaderboard</h2>
        </div>
        """,
        unsafe_allow_html=True,
    )

    leaderboard_platform = st.radio(
        "leaderboard_platform",
        options=plat_options,
        horizontal=True,
        label_visibility="collapsed",
        key="leaderboard_platform",
    )
    selected_plat_key = plat_label_to_key.get(leaderboard_platform)  # None when "All platforms"

    # Aggregate by DOMAIN (not full URL) — multiple paths under the same
    # domain (e.g. several reddit threads) collapse into a single row so the
    # leaderboard shows distinct sources, not duplicate domain entries.
    domain_counts: dict[str, int] = Counter()
    domain_per_platform: dict[str, dict[str, int]] = {}
    domain_per_prompt: dict[str, dict[str, int]] = {}
    domain_urls: dict[str, set[str]] = {}
    domain_sample_url: dict[str, str] = {}

    for _, row in results_df.iterrows():
        plat = row["platform"]
        if selected_plat_key and plat != selected_plat_key:
            continue
        prm = row.get("prompt", "")
        for u in (list(row.get("citations_list", []) or [])
                  + list(row.get("source_urls_list", []) or [])):
            if not u:
                continue
            d = domain_of(u) or u
            domain_counts[d] += 1
            domain_per_platform.setdefault(d, {})[plat] = domain_per_platform.get(d, {}).get(plat, 0) + 1
            if prm:
                domain_per_prompt.setdefault(d, {})[prm] = domain_per_prompt.get(d, {}).get(prm, 0) + 1
            domain_urls.setdefault(d, set()).add(u)
            domain_sample_url.setdefault(d, u)

    domain_rows = sorted(domain_counts.items(), key=lambda kv: kv[1], reverse=True)

    lf_col1, lf_col2 = st.columns([1, 1])
    with lf_col1:
        leaderboard_filter = st.radio(
            "leaderboard_filter",
            options=["All", "Your URLs", "Competitors"],
            horizontal=True,
            label_visibility="collapsed",
            key="leaderboard_filter",
        )
    with lf_col2:
        leaderboard_search = st.text_input(
            "Search URLs",
            placeholder="Search URL, domain, or keyword cited…",
            label_visibility="collapsed",
            key="leaderboard_search",
        )

    leaderboard_search_norm = (leaderboard_search or "").strip().lower()

    def _is_tracked_domain(d: str) -> bool:
        if not tracked_terms:
            return False
        if any(t.lower() in d.lower() for t in tracked_terms if t):
            return True
        # Fall back: any URL under this domain matches a tracked term.
        return any(is_tracked_url(u, tracked_terms) for u in domain_urls.get(d, ()))

    def _leaderboard_html() -> str:
        head_cols = "".join(
            f"<th class='col-center' style='width:46px;'>{PLATFORM_LETTER.get(p, '?')}</th>"
            for p in platforms_in_data
        )
        filtered = []
        for d, cnt in domain_rows:
            you = _is_tracked_domain(d)
            if leaderboard_filter == "Your URLs" and not you:
                continue
            if leaderboard_filter == "Competitors" and you:
                continue
            if leaderboard_search_norm:
                # Match domain, any URL path under it, or any keyword.
                hit = leaderboard_search_norm in d.lower()
                if not hit:
                    hit = any(leaderboard_search_norm in u.lower() for u in domain_urls.get(d, ()))
                if not hit:
                    kws = domain_per_prompt.get(d, {})
                    hit = any(leaderboard_search_norm in k.lower() for k in kws)
                if not hit:
                    continue
            filtered.append((d, cnt, you))
        if not filtered:
            return (
                "<div class='tbl'><table><tbody>"
                f"<tr><td colspan='{5 + len(platforms_in_data)}' style='padding:24px; text-align:center; color:var(--text-3);'>"
                "No URLs match this filter.</td></tr>"
                "</tbody></table></div>"
            )
        rows_html = []
        for i, (d, cnt, you) in enumerate(filtered[:50], start=1):
            sample_url = domain_sample_url.get(d, d)
            link_href = f"https://{d}" if not d.startswith(("http://", "https://")) else d
            n_paths = len(domain_urls.get(d, ()))
            you_tag = "<span class='you-tag'>you</span>" if you else ""
            you_cls = "you-row" if you else ""
            per_plat = domain_per_platform.get(d, {})
            plat_cells = []
            for p in platforms_in_data:
                n = per_plat.get(p, 0)
                on_cls = f"on {p}" if n > 0 else ""
                plat_cells.append(
                    f"<td class='col-center'><span class='llm-dot {on_cls}'>{PLATFORM_LETTER.get(p, '?')}</span></td>"
                )
            count_cls = "high" if cnt >= 5 else ""
            rank_cls = "top" if i <= 3 else ""

            # Keywords that cited any URL under this domain — sorted by
            # per-keyword frequency. First 3 always visible; the rest live
            # behind a <details> toggle.
            prompt_freq = domain_per_prompt.get(d, {})
            sorted_prompts = sorted(prompt_freq.items(), key=lambda kv: kv[1], reverse=True)
            visible = sorted_prompts[:3]
            hidden = sorted_prompts[3:]

            # Tracked-term matches for THIS row — which tracking words from
            # the sidebar appear in the domain, in any URL path under it, or
            # in any keyword that cited it. Surfaced as a small chip row
            # above the keyword chips so users can see *why* a row is
            # flagged "you" and *which* tracked word is being matched
            # without hovering or guessing.
            tracked_hits: list[tuple[str, list[str]]] = []
            for t in tracked_terms:
                if not t:
                    continue
                tl = t.lower()
                where: list[str] = []
                if tl in d.lower():
                    where.append("domain")
                if any(tl in u.lower() for u in domain_urls.get(d, ())):
                    where.append("url")
                if any(tl in k.lower() for k in prompt_freq):
                    where.append("keyword")
                if where:
                    tracked_hits.append((t, where))

            def _kw_chip(p: str, n: int) -> str:
                truncated = p[:40] + ('…' if len(p) > 40 else '')
                return (
                    f"<span class='kw-chip' title='{html.escape(p)} · cited {n}×'>"
                    f"{bold_tracked(truncated, tracked_terms)}</span>"
                )

            kw_chips = "".join(_kw_chip(p, n) for p, n in visible)
            if hidden:
                hidden_chips = "".join(_kw_chip(p, n) for p, n in hidden)
                kw_chips += (
                    f"<details class='kw-overflow'>"
                    f"<summary class='kw-chip kw-more' "
                    f"title='Show {len(hidden)} more keyword{'s' if len(hidden) != 1 else ''}'>"
                    f"+{len(hidden)}</summary>"
                    f"<div class='kw-overflow-list'>{hidden_chips}</div>"
                    f"</details>"
                )
            kw_count_label = (
                f"<span class='kw-count'>{len(sorted_prompts)} kw</span>"
                if sorted_prompts else "<span class='kw-count' style='opacity:0.4;'>—</span>"
            )
            tracked_row = ""
            if tracked_hits:
                pill_parts = []
                for t, where in tracked_hits:
                    where_full = ", ".join(where)
                    where_short = ",".join(w[0] for w in where)
                    pill_parts.append(
                        f"<span class='kw-tracked-chip' "
                        f"title='Matches in: {where_full}'>"
                        f"{html.escape(t)}"
                        f"<span class='kw-tracked-where'>{where_short}</span>"
                        f"</span>"
                    )
                tracked_pills = "".join(pill_parts)
                tracked_row = (
                    f"<div class='kw-tracked-row'>"
                    f"<span class='kw-tracked-label'>Tracking</span>"
                    f"{tracked_pills}"
                    f"</div>"
                )
            kw_cell = (
                f"<td><div class='kw-cell'>"
                f"{tracked_row}"
                f"{kw_count_label}"
                f"<div class='kw-chips'>{kw_chips}</div>"
                f"</div></td>"
            )

            paths_label = (
                f"<span class='url-paths' title='{n_paths} distinct URL paths under this domain'>"
                f"{n_paths} path{'s' if n_paths != 1 else ''}</span>"
                if n_paths > 1 else ""
            )

            rows_html.append(
                f"<tr class='{you_cls}'>"
                f"<td class='rank-cell {rank_cls}'>{i}</td>"
                f"<td><div class='url-cell'>"
                f"<span class='domain'><a href='{html.escape(link_href)}' target='_blank' style='color:inherit; text-decoration:none;'>{html.escape(d)}</a></span>"
                f"{paths_label}"
                f"{you_tag}</div></td>"
                + kw_cell
                + "".join(plat_cells) +
                f"<td class='col-right'><span class='count-badge {count_cls}'>{cnt}×</span></td>"
                f"</tr>"
            )
        return (
            "<div class='tbl'><table>"
            f"<thead><tr><th style='width:42px;'>#</th><th>Domain</th><th>Cited for</th>{head_cols}<th class='col-right'>Cites</th></tr></thead>"
            f"<tbody>{''.join(rows_html)}</tbody>"
            "</table></div>"
        )

    st.markdown(
        f"<div class='section-meta' style='margin:-6px 0 10px;'>"
        f"{len(domain_counts)} unique domains · {sum(domain_counts.values())} total citations"
        f"{' · ' + html.escape(leaderboard_platform) if selected_plat_key else ''}"
        f"</div>",
        unsafe_allow_html=True,
    )
    st.markdown(_leaderboard_html(), unsafe_allow_html=True)

    # ============================================================
    # Fanout queries — what the model ACTUALLY searched for under the
    # hood (different from the user prompt). Captured from each
    # platform's backend SSE payload, so this is the rephrased /
    # expanded query the model issued to the search tool.
    # ============================================================
    fanout_df = dfs.get("fanout", pd.DataFrame())
    if not fanout_df.empty:
        st.markdown(
            """
            <div class="section-header" style="margin-top:32px;">
              <h2 class="section-title toc-anchor" id="fanout-queries">Fanout queries</h2>
              <span class="section-meta">What the model actually searched for</span>
            </div>
            """,
            unsafe_allow_html=True,
        )

        fan_plat_options = ["All platforms"] + [
            PLATFORM_LABEL.get(p, p)
            for p in sorted(fanout_df["platform"].unique().tolist())
        ]
        fan_plat_label_to_key = {
            PLATFORM_LABEL.get(p, p): p
            for p in fanout_df["platform"].unique().tolist()
        }
        fan_col1, fan_col2 = st.columns([2, 3])
        with fan_col1:
            fan_platform = st.radio(
                "fanout_platform",
                options=fan_plat_options,
                horizontal=True,
                label_visibility="collapsed",
                key="fanout_platform",
            )
        with fan_col2:
            fan_search = st.text_input(
                "Search fanout queries…",
                value="",
                placeholder="filter by keyword or query text",
                label_visibility="collapsed",
                key="fanout_search",
            )

        fan_view = fanout_df.copy()
        fan_plat_key = fan_plat_label_to_key.get(fan_platform)
        if fan_plat_key:
            fan_view = fan_view[fan_view["platform"] == fan_plat_key]
        if fan_search.strip():
            q = fan_search.strip().lower()
            fan_view = fan_view[
                fan_view["fanout_query"].str.lower().str.contains(q, na=False)
                | fan_view["prompt"].str.lower().str.contains(q, na=False)
            ]

        # Friendly platform label in the display.
        fan_display = fan_view.assign(
            platform=fan_view["platform"].map(
                lambda p: PLATFORM_LABEL.get(p, p)
            )
        ).rename(columns={
            "platform": "Platform",
            "prompt": "Keyword",
            "fanout_query": "Fanout query",
        })

        meta_cols = st.columns([3, 1])
        with meta_cols[0]:
            unique_queries = fan_view["fanout_query"].nunique()
            st.markdown(
                f"<div class='section-meta' style='margin:-4px 0 8px;'>"
                f"{len(fan_view)} rows · {unique_queries} unique queries"
                f"{' · ' + html.escape(fan_platform) if fan_plat_key else ''}"
                f"</div>",
                unsafe_allow_html=True,
            )
        with meta_cols[1]:
            csv_bytes = fan_display.to_csv(index=False).encode("utf-8")
            st.download_button(
                "Download CSV",
                data=csv_bytes,
                file_name=(
                    f"fanout_queries_"
                    f"{(fan_plat_key or 'all')}"
                    f"_{int(time.time())}.csv"
                ),
                mime="text/csv",
                key="fanout_csv_download",
                use_container_width=True,
            )

        if fan_view.empty:
            st.info("No fanout queries match the current filter.")
        else:
            st.dataframe(
                fan_display,
                use_container_width=True,
                hide_index=True,
                height=min(420, 56 + 35 * min(len(fan_display), 12)),
            )

    # ============================================================
    # Full response viewer
    # ============================================================
    st.markdown(
        """
        <div class="section-header" style="margin-top:32px;">
          <h2 class="section-title toc-anchor" id="full-response">Full response</h2>
          <span class="section-meta">Pick a keyword + platform</span>
        </div>
        """,
        unsafe_allow_html=True,
    )

    pick_col1, pick_col2 = st.columns([3, 1])
    with pick_col1:
        selected_prompt = st.selectbox(
            "Keyword",
            options=prompts_sorted,
            label_visibility="collapsed",
            key="resp_prompt_picker",
        )
    with pick_col2:
        plat_options = sorted(
            annotated[annotated["prompt"] == selected_prompt]["platform"].unique().tolist()
        ) if selected_prompt else []
        selected_plat = st.selectbox(
            "Platform",
            options=plat_options,
            format_func=lambda p: PLATFORM_LABEL.get(p, p),
            label_visibility="collapsed",
            key="resp_plat_picker",
        )

    if selected_prompt and selected_plat:
        sub = annotated[
            (annotated["prompt"] == selected_prompt) &
            (annotated["platform"] == selected_plat)
        ]
        if not sub.empty:
            row = sub.iloc[0]
            full_text = row.get("response_text") or ""
            cits = list(row.get("citations_list", []) or [])
            srcs = list(row.get("source_urls_list", []) or [])
            combined: list[str] = []
            seen = set()
            for u in cits + srcs:
                if u and u not in seen:
                    seen.add(u); combined.append(u)

            cited, pos, _ = row_citation_position(row, tracked_terms)
            err = row.get("error") or ""

            if cited and isinstance(pos, int) and pos > 0:
                pos_label = f"cited at #{pos}"
                pos_cls = "cited"
            elif cited:
                pos_label = "cited in text"
                pos_cls = "cited"
            else:
                pos_label = "not cited"
                pos_cls = "missing"

            body_html = highlight_terms(full_text, tracked_terms) if full_text else "<i>No response captured.</i>"

            sources_html_parts = []
            for i, u in enumerate(combined[:12], start=1):
                cls = "response-source you" if is_tracked_url(u, tracked_terms) else "response-source"
                domain = domain_of(u) or u
                sources_html_parts.append(
                    f"<span class='{cls}'>[{i}] <a href='{html.escape(u)}' target='_blank' style='color:inherit; text-decoration:none;'>{html.escape(domain)}</a></span>"
                )
            sources_html = (
                "<div class='response-sources'><strong>Sources</strong>"
                + "".join(sources_html_parts)
                + "</div>"
            ) if sources_html_parts else ""

            st.markdown(
                f"""
                <div class="response">
                  <div class="response-header">
                    <div class="response-meta">
                      <span class="llm-badge {selected_plat}">{html.escape(PLATFORM_LABEL.get(selected_plat, selected_plat))}</span>
                      <span class="kw">{html.escape(selected_prompt)}</span>
                      <span class="pos {pos_cls}">· {pos_label}</span>
                    </div>
                  </div>
                  <div class="response-body">{body_html}</div>
                  {sources_html}
                </div>
                """,
                unsafe_allow_html=True,
            )
            if err:
                st.error(f"Scrape error: {err}")

    # Tracked-terms detail lives in the header chip's popover now — no
    # duplicate summary at the bottom.


# ============================================================
# Scan history — list of past runs (bottom)
# ============================================================
st.markdown(
    """
    <div class="section-header" style="margin-top:32px;">
      <h2 class="section-title toc-anchor" id="scan-history">Scan history</h2>
    </div>
    """,
    unsafe_allow_html=True,
)
sel_col1, sel_col2 = st.columns([4, 1])
with sel_col1:
    st.selectbox(
        "Run",
        options=run_options,
        format_func=lambda o: o["label"],
        key="run_picker",
        label_visibility="collapsed",
    )
with sel_col2:
    current_pick = st.session_state.get("run_picker")
    if current_pick and current_pick.get("ts") != -1 and st.button(
        "Delete run", use_container_width=True, key="del_run_btn"
    ):
        delete_run(current_pick["ts"])
        st.session_state.pop("run_picker", None)
        st.toast("Run deleted")
        st.rerun()


# ============================================================
# Footer
# ============================================================
st.markdown(
    "<div style='margin-top:48px; padding-top:16px; border-top:1px solid var(--border); "
    "color:var(--text-3); font-size:11px; font-family:Geist Mono, monospace;'>"
    "Citerion · ChatGPT, Gemini and Perplexity scraped via headless browser · "
    "Scraping chat UIs violates each vendor's ToS — use throwaway accounts."
    "</div>",
    unsafe_allow_html=True,
)
