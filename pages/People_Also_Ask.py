"""People Also Ask — standalone feature page.

Separate from the citation tracker on the main page: its own keyword input,
its own run button, its own `paa_runs` storage table and its own exports.
Scrapes Google's "People also ask" module per keyword, clicking questions
open (which also makes Google load follow-up questions) up to a configurable
cap, and captures question / answer / source for each.
"""
from __future__ import annotations

import asyncio
import html
import io
import sys
from datetime import datetime
from pathlib import Path

import pandas as pd
import streamlit as st

# Pages run as their own script — make sure the project root is importable.
ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scrapers.google_paa import scrape_paa, close_session  # noqa: E402
from core.paa_storage import (  # noqa: E402
    save_paa_results, list_paa_runs, load_paa_run, delete_paa_run, clear_paa,
)

# Playwright on Windows needs the Proactor loop policy (pages don't inherit
# module-level setup from app.py).
if sys.platform == "win32":
    asyncio.set_event_loop_policy(asyncio.WindowsProactorEventLoopPolicy())

st.set_page_config(
    page_title="People Also Ask",
    page_icon="◆",
    layout="wide",
    initial_sidebar_state="expanded",
)

# Login gate -- a page under pages/ has its own URL and is reachable
# directly, so gating app.py alone would leave this one open.
from core.auth import require_login  # noqa: E402
require_login(st)

# Trimmed-down cut of the main app's violet/cyan dark theme — just the tokens
# and widgets this page uses.
CUSTOM_CSS = """
<style>
@import url('https://fonts.googleapis.com/css2?family=Instrument+Serif:ital@0;1&family=Geist:wght@300;400;500;600;700&family=Geist+Mono:wght@400;500;600&display=swap');
@import url('https://fonts.googleapis.com/css2?family=Material+Symbols+Rounded:opsz,wght,FILL,GRAD@20..48,100..700,0..1,-50..200&family=Material+Symbols+Outlined:opsz,wght,FILL,GRAD@20..48,100..700,0..1,-50..200&display=block');
@import url('https://fonts.googleapis.com/icon?family=Material+Icons&display=block');

/* Material Symbols icon ligatures — same fix as app.py: pin the icon font so
   raw ligature text ("keyboard_double_arrow_right") never leaks through. */
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
  --surface: rgba(255,255,255,0.025);
  --surface-2: rgba(255,255,255,0.045);
  --surface-3: rgba(255,255,255,0.07);
  --border: rgba(255,255,255,0.08);
  --border-strong: rgba(255,255,255,0.16);
  --text: #f5f5f7;
  --text-2: #a1a1aa;
  --text-3: #6b6b73;
  --accent: #8b5cf6;
  --green: #34d399;
  --red: #f87171;
  --radius-lg: 14px;
}
html, body, .stApp {
  background: var(--bg) !important;
  background-image:
    radial-gradient(at 18% 0%, rgba(139,92,246,0.10) 0px, transparent 50%),
    radial-gradient(at 82% 100%, rgba(6,182,212,0.07) 0px, transparent 50%) !important;
  background-attachment: fixed !important;
  color: var(--text) !important;
  font-family: 'Geist', -apple-system, BlinkMacSystemFont, sans-serif !important;
}
.stApp p, .stApp span, .stApp label { color: var(--text); font-family: 'Geist', sans-serif; }
.stMarkdown, .stMarkdown p, .stMarkdown li { color: var(--text) !important; }
header[data-testid="stHeader"] { background: transparent; }
.block-container { padding-top: 1.4rem; max-width: 1320px; }

section[data-testid="stSidebar"] {
  background: #0d0d10 !important;
  border-right: 1px solid var(--border);
}
section[data-testid="stSidebar"] * { color: var(--text); }

.stTextInput input, .stTextArea textarea {
  background: var(--surface-2) !important;
  color: var(--text) !important;
  -webkit-text-fill-color: var(--text) !important;
  border: 1px solid var(--border) !important;
  border-radius: 8px !important;
  font-size: 13px !important;
  caret-color: var(--accent) !important;
}
.stTextInput input:focus, .stTextArea textarea:focus {
  border-color: var(--accent) !important;
  box-shadow: 0 0 0 3px rgba(139,92,246,0.18) !important;
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
.stSelectbox div[data-baseweb="select"] > div {
  background: var(--surface-2) !important;
  color: var(--text) !important;
  border: 1px solid var(--border) !important;
  border-radius: 8px !important;
}
div[data-baseweb="popover"] [role="listbox"], div[data-baseweb="popover"] ul {
  background: #131318 !important;
  border: 1px solid var(--border) !important;
}
div[data-baseweb="popover"] [role="option"] { color: var(--text) !important; }
div[data-baseweb="popover"] [role="option"]:hover,
div[data-baseweb="popover"] [role="option"][aria-selected="true"] {
  background: var(--surface-3) !important;
}

.stButton button {
  background: linear-gradient(135deg, #8b5cf6 0%, #06b6d4 100%) !important;
  color: #ffffff !important;
  border: none !important;
  border-radius: 8px !important;
  font-size: 13px !important;
  font-weight: 500 !important;
}
.stButton button p { color: #ffffff !important; }
.stDownloadButton button {
  background: var(--surface-2) !important;
  color: var(--text) !important;
  border: 1px solid var(--border) !important;
}
.stDownloadButton button p { color: var(--text) !important; }

[data-testid="stFileUploader"] section {
  background: var(--surface) !important;
  border: 1px dashed var(--border) !important;
  border-radius: 10px !important;
}
[data-testid="stFileUploader"] button {
  background: var(--surface-2) !important;
  color: var(--text) !important;
  border: 1px solid var(--border) !important;
}

.page-title {
  font-family: 'Instrument Serif', serif;
  font-weight: 400; font-size: 46px; letter-spacing: -0.025em;
  margin: 10px 0 6px; line-height: 1.05;
  background: linear-gradient(180deg, #ffffff 0%, #a1a1aa 100%);
  -webkit-background-clip: text;
  -webkit-text-fill-color: transparent;
}
.page-sub { font-size: 13px; color: var(--text-3); margin: 0 0 24px; }
.tag {
  background: var(--surface-2);
  border: 1px solid var(--border);
  padding: 4px 12px;
  border-radius: 999px;
  font-size: 11px;
  color: var(--text-2);
  font-family: 'Geist Mono', monospace;
}

.kpi-grid { display: grid; grid-template-columns: repeat(5, 1fr); gap: 12px; margin-bottom: 24px; }
.kpi {
  background: var(--surface);
  border: 1px solid var(--border);
  border-radius: var(--radius-lg);
  padding: 18px 20px;
}
.kpi-label {
  font-size: 10px; color: var(--text-2);
  text-transform: uppercase; letter-spacing: 0.10em; font-weight: 600;
  margin-bottom: 8px; font-family: 'Geist Mono', monospace;
}
.kpi-value {
  font-family: 'Instrument Serif', serif;
  font-size: 42px; line-height: 1; letter-spacing: -0.025em;
  margin: 0; color: var(--text);
}
.kpi-value.red { color: var(--red); }

.paa-kw {
  background: var(--surface);
  border: 1px solid var(--border);
  border-radius: var(--radius-lg);
  margin-bottom: 16px;
  overflow: hidden;
}
.paa-kw-head {
  background: var(--surface-2);
  padding: 13px 18px;
  display: flex; align-items: center; gap: 12px;
  list-style: none; cursor: pointer; user-select: none;
}
.paa-kw-head::-webkit-details-marker { display: none; }
.paa-kw-head:hover { background: var(--surface-3); }
.paa-kw[open] > .paa-kw-head { border-bottom: 1px solid var(--border); }
.paa-kw-name { font-size: 14px; font-weight: 500; color: var(--text); }
.paa-kw-count {
  margin-left: auto;
  font-size: 11px; color: var(--text-2);
  font-family: 'Geist Mono', monospace;
  background: var(--surface); border: 1px solid var(--border);
  padding: 2px 10px; border-radius: 999px;
}
.paa-kw-chevron { color: var(--accent); font-size: 12px; transition: transform 0.15s ease; }
.paa-kw[open] > .paa-kw-head .paa-kw-chevron { transform: rotate(90deg); }
.paa-item { border-bottom: 1px solid var(--border); }
.paa-item:last-child { border-bottom: none; }
.paa-item > summary {
  list-style: none; cursor: pointer;
  padding: 12px 18px;
  display: flex; align-items: center; gap: 12px;
  font-size: 13px; color: var(--text);
  user-select: none;
}
.paa-item > summary::-webkit-details-marker { display: none; }
.paa-item > summary:hover { background: var(--surface-2); }
.paa-item[open] > summary { background: var(--surface-2); }
.paa-pos {
  flex: 0 0 auto;
  font-family: 'Geist Mono', monospace; font-size: 11px;
  color: var(--text-3); min-width: 22px;
}
.paa-chevron { margin-left: auto; color: var(--accent); font-size: 11px; transition: transform 0.15s ease; }
.paa-item[open] .paa-chevron { transform: rotate(90deg); }
.paa-answer {
  padding: 14px 18px 16px 52px;
  font-size: 13px; line-height: 1.7; color: var(--text-2);
  white-space: pre-wrap;
}
.paa-source {
  padding: 0 18px 14px 52px;
  font-size: 12px; font-family: 'Geist Mono', monospace;
}
.paa-source a { color: var(--accent); text-decoration: none; }
.paa-source a:hover { text-decoration: underline; }
.paa-noanswer { color: var(--text-3); font-style: italic; }
/* Follow-up questions nested under the question that generated them. */
.paa-children {
  margin: 4px 0 12px 52px;
  border-left: 2px solid var(--accent);
  padding-left: 2px;
  background: var(--surface);
  border-radius: 0 8px 8px 0;
}
.paa-children-label {
  font-size: 10px; text-transform: uppercase; letter-spacing: 0.10em;
  color: var(--accent); font-family: 'Geist Mono', monospace;
  padding: 8px 14px 4px;
}
.paa-children .paa-item:last-child { border-bottom: none; }
.paa-children .paa-item > summary { padding-left: 14px; }
.paa-children .paa-answer,
.paa-children .paa-source { padding-left: 14px; }
.pasf-block {
  padding: 13px 18px 16px;
  border-top: 1px dashed var(--border);
}
.pasf-label {
  font-size: 10px; text-transform: uppercase; letter-spacing: 0.10em;
  color: var(--text-2); font-family: 'Geist Mono', monospace; margin-bottom: 10px;
}
.pasf-chips { display: flex; flex-wrap: wrap; gap: 8px; }
.pasf-chip {
  font-size: 12px; color: var(--text);
  background: var(--surface-2); border: 1px solid var(--border);
  border-radius: 999px; padding: 5px 12px;
}
.paa-error {
  border: 1px solid rgba(248,113,113,0.32);
  background: rgba(248,113,113,0.08);
  color: var(--red);
  border-radius: var(--radius-lg);
  padding: 13px 18px;
  margin-bottom: 16px;
  font-size: 13px;
}
.paa-error b { color: var(--red); }

.empty {
  border: 1px dashed var(--border);
  border-radius: 16px;
  padding: 48px;
  text-align: center;
  color: var(--text-3);
  background: var(--surface);
}
.empty h4 {
  color: var(--text); margin-bottom: 8px; font-size: 1.15rem;
  font-weight: 400; font-family: 'Instrument Serif', serif; font-style: italic;
}
.empty b { color: var(--text); }

/* Segmented-control container + item styles live in core.ui.SHARED_CSS
   (single source of truth). Only the default page-nav link list is hidden. */
[data-testid="stSidebarNav"] { display: none; }
</style>
"""
from core.ui import SHARED_CSS as _UI_SHARED_CSS  # noqa: E402
st.markdown(CUSTOM_CSS, unsafe_allow_html=True)
st.markdown(_UI_SHARED_CSS, unsafe_allow_html=True)


# ============================================================
# Helpers
# ============================================================
def collect_keywords(csv_file, text_blob: str) -> list[str]:
    """Same shape as the main page's collect_prompts: upload + paste, deduped."""
    kws: list[str] = []
    if csv_file is not None:
        try:
            df = pd.read_csv(io.BytesIO(csv_file.getvalue()))
            col = "keyword" if "keyword" in df.columns else df.columns[0]
            kws.extend([str(x).strip() for x in df[col].dropna()])
        except Exception:
            try:
                df = pd.read_excel(io.BytesIO(csv_file.getvalue()))
                col = "keyword" if "keyword" in df.columns else df.columns[0]
                kws.extend([str(x).strip() for x in df[col].dropna()])
            except Exception:
                st.error("Could not parse uploaded file as CSV or Excel.")
    kws.extend([ln.strip() for ln in text_blob.splitlines() if ln.strip()])
    seen, out = set(), []
    for k in kws:
        if k not in seen:
            seen.add(k); out.append(k)
    return out


async def _scrape_batch(keywords: list[str], max_q: int, progress, status) -> list[dict]:
    """Serial — the scraper reuses ONE Chrome tab across every keyword, so
    keywords are typed into the same search box one after another."""
    rows: list[dict] = []
    try:
        for i, kw in enumerate(keywords, start=1):
            status.markdown(f"**Keyword {i}/{len(keywords)}** — `{kw[:80]}`")
            res = await scrape_paa(kw, max_questions=max_q)
            rows.extend(res.to_rows())
            progress.progress(i / len(keywords), text=f"{i}/{len(keywords)} keywords done")
    finally:
        # One shared Chrome for the whole batch — close it now.
        await close_session()
    return rows


def run_paa_scan(keywords: list[str], max_q: int) -> list[dict]:
    progress = st.progress(0.0, text="Starting…")
    status = st.empty()
    try:
        rows = asyncio.run(_scrape_batch(keywords, max_q, progress, status))
    finally:
        progress.empty()
        status.empty()
    return rows


def _excel_bytes(df: pd.DataFrame) -> bytes:
    """Two sheets: PAA questions/answers, and the related-search terms."""
    buf = io.BytesIO()
    q = df[df["kind"] == "question"][
        ["keyword", "position", "question", "parent", "answer",
         "source_title", "source_url", "error"]
    ].rename(columns={"parent": "parent_question"})
    rel = (
        df[df["kind"] == "related"][["keyword", "position", "question"]]
        .rename(columns={"position": "rank", "question": "related_search"})
    )
    with pd.ExcelWriter(buf, engine="openpyxl") as xw:
        q.to_excel(xw, sheet_name="People Also Ask", index=False)
        if not rel.empty:
            rel.to_excel(xw, sheet_name="People Also Search For", index=False)
    return buf.getvalue()


def _question_items_html(grp: pd.DataFrame) -> str:
    """Nested accordion HTML for one keyword's question rows. A row whose
    `parent` matches another question's text renders nested inside that parent
    (the follow-ups Google appended when it was expanded); the rest are roots."""
    recs = grp.to_dict("records")
    by_text = {str(r["question"]).strip().lower(): r for r in recs}
    children: dict = {}
    roots = []
    for r in recs:
        key = str(r["question"]).strip().lower()
        parent = str(r.get("parent") or "").strip().lower()
        if parent and parent in by_text and parent != key:
            children.setdefault(parent, []).append(r)
        else:
            roots.append(r)

    def render(r, seen: set) -> str:
        key = str(r["question"]).strip().lower()
        if key in seen:  # guard against any accidental parent cycle
            return ""
        seen.add(key)
        ans = (
            f"<div class='paa-answer'>{html.escape(str(r['answer']))}</div>"
            if r["answer"] else
            "<div class='paa-answer paa-noanswer'>No answer captured.</div>"
        )
        src = ""
        if r["source_url"]:
            label = r["source_title"] or r["source_url"]
            src = (
                f"<div class='paa-source'>Source: "
                f"<a href='{html.escape(str(r['source_url']))}' target='_blank' rel='noopener'>"
                f"{html.escape(str(label)[:120])}</a></div>"
            )
        kids = children.get(key, [])
        kids_html = ""
        if kids:
            inner = "".join(render(k, seen) for k in kids)
            kids_html = (
                "<div class='paa-children'>"
                "<div class='paa-children-label'>Follow-up questions</div>"
                f"{inner}</div>"
            )
        return (
            f"<details class='paa-item'>"
            f"<summary><span class='paa-pos'>{int(r['position'])}</span>"
            f"{html.escape(str(r['question']))}"
            f"<span class='paa-chevron'>▶</span></summary>"
            f"{ans}{src}{kids_html}</details>"
        )

    seen: set = set()
    return "".join(render(r, seen) for r in roots)


# ============================================================
# Sidebar — inputs
# ============================================================
with st.sidebar:
    # Tool switcher moved out of the sidebar — see the top nav below the
    # main content's page title.
    st.markdown("### Keywords")
    csv = st.file_uploader("Upload CSV/XLSX", type=["csv", "xlsx", "xls"],
                           label_visibility="collapsed", key="paa_upload")
    text = st.text_area(
        "Paste keywords (one per line)",
        height=180,
        placeholder="best PTE coaching India\nIELTS voucher discount\nduolingo english test fee",
        label_visibility="collapsed",
        key="paa_keywords",
    )

    st.markdown("### Options")
    max_q = st.slider("Questions to expand per keyword", min_value=4, max_value=50, value=10,
                      help="How many questions to click open. Each reveals an "
                           "answer and makes Google append follow-up questions, "
                           "which are captured and nested under their parent — so "
                           "the total questions per keyword is usually 2-4× this. "
                           "Higher = slower and more bot-detection exposure.")
    st.caption("Runs in a visible Chrome window (same as the AI Overview scraper).")

    st.markdown(" ")
    run_btn = st.button("Run PAA scan", type="primary", use_container_width=True)

    with st.expander("Danger zone"):
        if st.button("Clear all PAA history", use_container_width=True):
            clear_paa()
            st.toast("PAA history cleared")
            st.rerun()


# ============================================================
# Run handler
# ============================================================
if run_btn:
    keywords = collect_keywords(csv, text)
    if not keywords:
        st.error("No keywords provided.")
    else:
        with st.spinner(f"Scraping People Also Ask for {len(keywords)} keywords…"):
            rows_out = run_paa_scan(keywords, max_q)
        ts = save_paa_results(rows_out)
        st.session_state["paa_pending_ts"] = ts
        n_q = sum(1 for r in rows_out if r.get("kind", "question") == "question" and r["question"])
        n_rel = sum(1 for r in rows_out if r.get("kind") == "related")
        st.success(
            f"Done. {n_q} questions and {n_rel} related searches "
            f"captured across {len(keywords)} keywords."
        )
        st.rerun()


# ============================================================
# Run picker — default to nothing selected, like the main page
# ============================================================
runs = list_paa_runs(limit=50)


def _run_label(r: dict) -> str:
    when = datetime.fromtimestamp(r["ts"]).strftime("%d %b · %H:%M")
    rel = r.get("related_count") or 0
    tail = f" · {rel} related" if rel else ""
    return f"{when} · {r['keyword_count']} kw · {r['question_count']} questions{tail}"


PLACEHOLDER = {"ts": None, "label": "— Select a run to view —"}
run_options = [PLACEHOLDER] + [{"ts": r["ts"], "label": _run_label(r)} for r in runs]

if "paa_pending_ts" in st.session_state:
    ts = st.session_state.pop("paa_pending_ts")
    match = next((o for o in run_options if o.get("ts") == ts), None)
    if match:
        st.session_state["paa_run_picker"] = match

sel = st.session_state.get("paa_run_picker", PLACEHOLDER)
if isinstance(sel, dict) and sel.get("ts") is not None:
    if not any(o.get("ts") == sel.get("ts") for o in run_options):
        sel = PLACEHOLDER
        st.session_state["paa_run_picker"] = PLACEHOLDER

rows = load_paa_run(sel["ts"]) if sel.get("ts") else []


# ============================================================
# Top nav — three tools horizontally, above the page title.
# ============================================================
_top_nav = st.segmented_control(
    "Tool", ["LLM Tracker", "People Also Ask", "Report"],
    default="People Also Ask", label_visibility="collapsed",
    key="nav_top_paa",
)
if _top_nav == "LLM Tracker":
    st.switch_page("app.py")
elif _top_nav == "Report":
    st.switch_page("pages/Compare_Reports.py")


# ============================================================
# Header
# ============================================================
showing_pill = (
    f"<span class='tag'>Showing: {html.escape(sel['label'])}</span>"
    if sel.get("ts") else ""
)
st.markdown(
    f"""
    <h1 class="page-title">People Also Ask</h1>
    <div style="display:flex; align-items:baseline; gap:14px; margin:-2px 0 18px; flex-wrap:wrap;">
      {showing_pill}
      <span class="page-sub" style="margin:0;">Scrape Google's PAA box per keyword — questions expanded, answers and sources captured, plus "People also search for" terms when present.</span>
    </div>
    """,
    unsafe_allow_html=True,
)


# ============================================================
# Body
# ============================================================
if not rows:
    st.markdown(
        """
        <div class="empty">
          <h4>Nothing loaded</h4>
          <div style="margin-bottom:8px;">Paste keywords in the sidebar and click <b>Run PAA scan</b>.</div>
          <div style="color:var(--text-3); font-size:12px;">Or pick a previous run from the dropdown below.</div>
        </div>
        """,
        unsafe_allow_html=True,
    )
else:
    df = pd.DataFrame(rows)
    for col in ["keyword", "position", "question", "answer",
                "source_title", "source_url", "error", "kind", "parent"]:
        if col not in df.columns:
            df[col] = ""
    # Old runs (pre-migration) have no kind → treat as questions.
    df["kind"] = df["kind"].fillna("question").replace("", "question")
    df["parent"] = df["parent"].fillna("")
    df = df[["keyword", "position", "question", "answer",
             "source_title", "source_url", "error", "kind", "parent"]]

    q_rows = df[(df["kind"] == "question") & (df["question"] != "")]
    rel_rows = df[df["kind"] == "related"]
    err_rows = df[(df["kind"] == "question") & (df["question"] == "") & (df["error"] != "")]

    n_kw = df["keyword"].nunique()
    n_q = len(q_rows)
    n_ans = int((q_rows["answer"] != "").sum())
    n_rel = len(rel_rows)
    n_err = len(err_rows)

    st.markdown(
        f"""
        <div class="kpi-grid">
          <div class="kpi"><div class="kpi-label">Keywords</div><p class="kpi-value">{n_kw}</p></div>
          <div class="kpi"><div class="kpi-label">Questions</div><p class="kpi-value">{n_q}</p></div>
          <div class="kpi"><div class="kpi-label">With answer</div><p class="kpi-value">{n_ans}</p></div>
          <div class="kpi"><div class="kpi-label">Related searches</div><p class="kpi-value">{n_rel}</p></div>
          <div class="kpi"><div class="kpi-label">Failed keywords</div><p class="kpi-value{' red' if n_err else ''}">{n_err}</p></div>
        </div>
        """,
        unsafe_allow_html=True,
    )

    # Downloads
    dl1, dl2, _sp = st.columns([1, 1, 4])
    stamp = datetime.fromtimestamp(sel["ts"]).strftime("%Y%m%d_%H%M") if sel.get("ts") else "export"
    with dl1:
        st.download_button(
            "⬇ Excel", data=_excel_bytes(df),
            file_name=f"paa_{stamp}.xlsx",
            mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            use_container_width=True,
        )
    with dl2:
        st.download_button(
            "⬇ CSV", data=df.to_csv(index=False).encode("utf-8-sig"),
            file_name=f"paa_{stamp}.csv", mime="text/csv",
            use_container_width=True,
        )

    # Failed keywords first so they don't get buried under the Q&A blocks.
    for _, r in err_rows.iterrows():
        st.markdown(
            f"<div class='paa-error'><b>{html.escape(r['keyword'])}</b> — "
            f"{html.escape(r['error'])}</div>",
            unsafe_allow_html=True,
        )

    # One card per keyword: question rows (click-to-expand) plus a "People also
    # search for" chip strip when related terms were captured. A keyword may
    # have related terms without questions, so iterate the union of both.
    shown = list(dict.fromkeys(list(q_rows["keyword"]) + list(rel_rows["keyword"])))
    for kw in shown:
        q_grp = q_rows[q_rows["keyword"] == kw]
        r_grp = rel_rows[rel_rows["keyword"] == kw]

        items_html = _question_items_html(q_grp)

        pasf_html = ""
        if len(r_grp):
            chips = "".join(
                f"<span class='pasf-chip'>{html.escape(str(t))}</span>"
                for t in r_grp["question"]
            )
            pasf_html = (
                "<div class='pasf-block'>"
                "<div class='pasf-label'>People also search for</div>"
                f"<div class='pasf-chips'>{chips}</div></div>"
            )

        count_label = " · ".join(
            p for p in (
                f"{len(q_grp)} questions" if len(q_grp) else "",
                f"{len(r_grp)} related" if len(r_grp) else "",
            ) if p
        )
        st.markdown(
            f"""
            <details class="paa-kw">
              <summary class="paa-kw-head">
                <span class="paa-kw-name">{html.escape(str(kw))}</span>
                <span class="paa-kw-count">{count_label}</span>
                <span class="paa-kw-chevron">▶</span>
              </summary>
              {items_html}
              {pasf_html}
            </details>
            """,
            unsafe_allow_html=True,
        )


# ============================================================
# Run history
# ============================================================
st.markdown("---")
hist_col, del_col = st.columns([4, 1])
with hist_col:
    st.selectbox(
        "Scan history",
        options=run_options,
        format_func=lambda o: o["label"],
        key="paa_run_picker",
    )
with del_col:
    st.markdown("<div style='height:28px'></div>", unsafe_allow_html=True)
    current_pick = st.session_state.get("paa_run_picker")
    if current_pick and current_pick.get("ts") and st.button(
        "Delete run", use_container_width=True, key="paa_del_run_btn"
    ):
        delete_paa_run(current_pick["ts"])
        st.session_state.pop("paa_run_picker", None)
        st.toast("Run deleted")
        st.rerun()
