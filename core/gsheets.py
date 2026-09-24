"""Google Sheet → list-of-terms fetcher.

Fetch runs server-side (this is a Streamlit backend module, called from a
button handler) so there's no CORS to worry about. The sheet must be shared
"Anyone with the link → Viewer" for the CSV export endpoint to serve data
instead of an HTML login page.

Public API:
    parse_sheet_url(url)               -> spreadsheet_id | None
    column_letter_to_index("AB")       -> 27
    fetch_google_sheet_terms(url, tab, column) -> (terms, error_message)

All error paths are returned as (terms=[], error="human-readable string") so
callers can render the error inline without exception handling.
"""
from __future__ import annotations

import csv
import io
import re
import urllib.error
import urllib.parse
import urllib.request

# Regex over any Google Sheets URL (edit, viewer, export, embedded, etc.).
# The ID is the segment right after "/d/", 25+ alphanumeric / dash / underscore chars.
_SHEET_ID_RE = re.compile(r"/d/([a-zA-Z0-9_-]{15,})")

_TIMEOUT_SECONDS = 15
_HEADER_HINTS = {
    "term", "terms",
    "keyword", "keywords",
    "brand", "brands",
    "domain", "domains",
    "url", "urls",
    "name", "label", "prompt", "prompts",
}


def parse_sheet_url(url: str) -> str | None:
    """Extract the spreadsheet ID from any Google Sheets URL variant.
    Returns None if the URL doesn't look like a Google Sheet."""
    if not url or "docs.google.com" not in url:
        return None
    m = _SHEET_ID_RE.search(url)
    return m.group(1) if m else None


def column_letter_to_index(col: str) -> int:
    """A → 0, B → 1, Z → 25, AA → 26, AB → 27. Empty / bad input → 0."""
    if not col:
        return 0
    col = col.strip().upper()
    idx = 0
    for ch in col:
        if not ("A" <= ch <= "Z"):
            return 0
        idx = idx * 26 + (ord(ch) - ord("A") + 1)
    return idx - 1


def _looks_like_header(value: str) -> bool:
    """Heuristic: is the first row a header row rather than a real term?
    Matches common header keywords or ultra-short strings ('#')."""
    if not value:
        return True
    v = value.strip().lower()
    return v in _HEADER_HINTS or len(v) <= 1


def _keep_duplicates() -> bool:
    """True when repeated keywords should be preserved (load testing).

    Set by the "Keep duplicate keywords" checkbox, or ALLOW_DUPLICATE_KEYWORDS=1
    for headless/CI runs. Off by default: a normal scan should not scrape the same
    keyword twice.
    """
    import os
    try:
        # streamlit is imported per-function in this module, not at module level —
        # referencing a bare `st` here would raise NameError, and the except below
        # would swallow it, silently ignoring the checkbox.
        import streamlit as _st
        if _st.session_state.get("allow_duplicate_keywords"):
            return True
    except Exception:
        pass
    return os.getenv("ALLOW_DUPLICATE_KEYWORDS", "").strip().lower() in (
        "1", "true", "yes", "on")


def _dedupe_case_insensitive(values: list[str]) -> list[str]:
    """Keep first-seen casing; drop later duplicates that only differ in case."""
    seen: set[str] = set()
    out: list[str] = []
    for v in values:
        key = v.lower()
        if key in seen:
            continue
        seen.add(key)
        out.append(v)
    return out


def _build_csv_url(sheet_id: str, tab: str) -> str:
    params = {"tqx": "out:csv"}
    if tab:
        params["sheet"] = tab
    return (
        f"https://docs.google.com/spreadsheets/d/{sheet_id}"
        f"/gviz/tq?{urllib.parse.urlencode(params)}"
    )


def fetch_google_sheet_terms(
    url: str,
    tab: str = "",
    column: str = "A",
) -> tuple[list[str], str]:
    """Fetch and parse a Google Sheet as a list of terms.

    Returns (terms, error_message). One of them will be empty:
    - Success: (["cashkaro", "grabck", ...], "")
    - Failure: ([], "That doesn't look like a Google Sheet URL")

    Never raises. Callers render `error` inline when non-empty.
    """
    sheet_id = parse_sheet_url(url)
    if not sheet_id:
        return [], "That doesn't look like a Google Sheet URL"

    export_url = _build_csv_url(sheet_id, tab)

    try:
        req = urllib.request.Request(
            export_url,
            headers={"User-Agent": "Mozilla/5.0 Citerion/1.0"},
        )
        with urllib.request.urlopen(req, timeout=_TIMEOUT_SECONDS) as resp:
            content_type = (resp.headers.get("Content-Type") or "").lower()
            raw = resp.read()
    except urllib.error.HTTPError as e:
        # Google returns 302/401/403/404 or an HTML page when the sheet
        # isn't shared publicly. Fold all "can't view" cases together.
        if e.code in (401, 403, 404):
            return [], (
                "Sheet isn't publicly viewable. Set sharing to "
                "'Anyone with the link → Viewer' and try again."
            )
        return [], "Couldn't reach Google Sheets. Try again."
    except (urllib.error.URLError, TimeoutError):
        return [], "Couldn't reach Google Sheets. Try again."
    except Exception:
        return [], "Couldn't reach Google Sheets. Try again."

    # gviz returns text/csv on success. If sharing is wrong, Google redirects
    # to an HTML login page (text/html). Distinguish here.
    if "text/csv" not in content_type and "application/csv" not in content_type:
        return [], (
            "Sheet isn't publicly viewable. Set sharing to "
            "'Anyone with the link → Viewer' and try again."
        )

    try:
        body = raw.decode("utf-8", errors="replace")
    except Exception:
        return [], "Couldn't read the Google Sheet response."

    col_idx = column_letter_to_index(column) if column else 0
    col_label = (column or "A").strip().upper() or "A"
    tab_label = tab or "first tab"

    try:
        rows = list(csv.reader(io.StringIO(body)))
    except csv.Error:
        return [], f"No terms found in column {col_label} of tab {tab_label}"

    if not rows:
        return [], f"No terms found in column {col_label} of tab {tab_label}"

    # Pull the target column, trim, drop empties later.
    values: list[str] = []
    for row in rows:
        if col_idx < len(row):
            values.append(row[col_idx].strip())

    # Drop a plausible header row.
    if values and _looks_like_header(values[0]):
        values = values[1:]

    values = [v for v in values if v]
    values = _dedupe_case_insensitive(values)

    if not values:
        return [], f"No terms found in column {col_label} of tab {tab_label}"

    return values, ""


# ============================================================
# Sidebar helper — renders the Manual|Google Sheet toggle + inputs and
# returns the effective terms list. Keeps both sources in session state so
# the user can switch without losing either.
# ============================================================
def _index_to_letter(i: int) -> str:
    """0 → 'A', 25 → 'Z', 26 → 'AA'."""
    n = i + 1
    label = ""
    while n > 0:
        n, r = divmod(n - 1, 26)
        label = chr(ord("A") + r) + label
    return label


def _column_labels_from_first_row(first_row: list[str], max_cols: int) -> list[str]:
    """Build dropdown labels like 'A · keyword' / 'B · description' using the
    first row as the header hint. If the first row has no useful text at a
    given index, fall back to just the column letter."""
    labels: list[str] = []
    for i in range(max_cols):
        letter = _index_to_letter(i)
        cell = first_row[i].strip() if i < len(first_row) else ""
        if cell and not cell.startswith("#"):
            labels.append(f"{letter} · {cell}")
        else:
            labels.append(letter)
    return labels


def fetch_google_sheet_data(
    url: str,
    tab: str = "",
) -> tuple[list[list[str]], list[str], str]:
    """Fetch entire sheet, return (data_rows, column_labels, error).
    - data_rows: list-of-list-of-str (header stripped if the first row looks
      like a header).
    - column_labels: dropdown-ready labels ('A · keyword', 'B', …).
    """
    sheet_id = parse_sheet_url(url)
    if not sheet_id:
        return [], [], "That doesn't look like a Google Sheet URL"

    export_url = _build_csv_url(sheet_id, tab)
    try:
        req = urllib.request.Request(
            export_url,
            headers={"User-Agent": "Mozilla/5.0 Citerion/1.0"},
        )
        with urllib.request.urlopen(req, timeout=_TIMEOUT_SECONDS) as resp:
            content_type = (resp.headers.get("Content-Type") or "").lower()
            raw = resp.read()
    except urllib.error.HTTPError as e:
        if e.code in (401, 403, 404):
            return [], [], (
                "Sheet isn't publicly viewable. Set sharing to "
                "'Anyone with the link → Viewer' and try again."
            )
        return [], [], "Couldn't reach Google Sheets. Try again."
    except (urllib.error.URLError, TimeoutError):
        return [], [], "Couldn't reach Google Sheets. Try again."
    except Exception:
        return [], [], "Couldn't reach Google Sheets. Try again."

    if "text/csv" not in content_type and "application/csv" not in content_type:
        return [], [], (
            "Sheet isn't publicly viewable. Set sharing to "
            "'Anyone with the link → Viewer' and try again."
        )
    body = raw.decode("utf-8", errors="replace")
    try:
        rows = list(csv.reader(io.StringIO(body)))
    except csv.Error:
        return [], [], "Couldn't parse the sheet response."
    if not rows:
        return [], [], f"No data found in tab {tab or 'first'}"

    max_cols = max(len(r) for r in rows)
    header_row = rows[0]
    header_looks_like_header = bool(header_row) and (
        any(cell.strip().lower() in _HEADER_HINTS for cell in header_row)
        or all(cell.strip() and not cell.strip()[:1].isdigit() for cell in header_row if cell)
    )
    if header_looks_like_header:
        labels = _column_labels_from_first_row(header_row, max_cols)
        data_rows = rows[1:]
    else:
        labels = [_index_to_letter(i) for i in range(max_cols)]
        data_rows = rows

    return data_rows, labels, ""


def parse_excel_data(
    file_bytes: bytes,
    tab: str = "",
) -> tuple[list[list[str]], list[str], str]:
    """Excel counterpart to fetch_google_sheet_data. Returns
    (data_rows, column_labels, error)."""
    if not file_bytes:
        return [], [], "Upload an Excel file to load data."
    try:
        import pandas as pd
    except Exception:
        return [], [], "Excel support isn't available on this server."

    try:
        df = pd.read_excel(
            io.BytesIO(file_bytes),
            sheet_name=(tab or 0),
            header=None,
            dtype=str,
        )
    except ValueError:
        return [], [], f"Tab '{tab}' not found in this Excel file."
    except Exception:
        return [], [], "Couldn't read that Excel file — is it a valid .xlsx?"

    # Convert to plain list-of-list-of-str, coerce NaN → "".
    rows = df.fillna("").astype(str).values.tolist()
    if not rows:
        return [], [], f"No data found in tab {tab or 'first'}"

    max_cols = max(len(r) for r in rows)
    header_row = rows[0]
    header_looks_like_header = bool(header_row) and (
        any(cell.strip().lower() in _HEADER_HINTS for cell in header_row)
        or all(cell.strip() and not cell.strip()[:1].isdigit() for cell in header_row if cell)
    )
    if header_looks_like_header:
        labels = _column_labels_from_first_row(header_row, max_cols)
        data_rows = rows[1:]
    else:
        labels = [_index_to_letter(i) for i in range(max_cols)]
        data_rows = rows

    return data_rows, labels, ""


def extract_column_terms(rows: list[list[str]], col_idx: int) -> list[str]:
    """Trim, drop blanks, dedupe case-insensitive keeping first casing."""
    if col_idx < 0:
        return []
    values: list[str] = []
    for row in rows:
        if col_idx < len(row):
            v = str(row[col_idx]).strip()
            if v:
                values.append(v)
    return _dedupe_case_insensitive(values)


def parse_excel_terms(
    file_bytes: bytes,
    tab: str = "",
    column: str = "A",
) -> tuple[list[str], str]:
    """Parse an uploaded .xlsx file and extract terms from the given column.
    Returns (terms, error_message). Same shape as fetch_google_sheet_terms."""
    if not file_bytes:
        return [], "Upload an Excel file to load terms."
    try:
        import pandas as pd
    except Exception:
        return [], "Excel support isn't available on this server."

    col_idx = column_letter_to_index(column) if column else 0
    col_label = (column or "A").strip().upper() or "A"
    tab_label = tab or "first tab"

    try:
        df = pd.read_excel(
            io.BytesIO(file_bytes),
            sheet_name=(tab or 0),
            header=None,
            dtype=str,
        )
    except ValueError as e:
        # sheet_name not found → ValueError from pandas / openpyxl.
        return [], f"Tab '{tab}' not found in this Excel file."
    except Exception:
        return [], "Couldn't read that Excel file — is it a valid .xlsx?"

    if col_idx >= len(df.columns):
        return [], f"No terms found in column {col_label} of tab {tab_label}"

    values = [
        str(v).strip()
        for v in df.iloc[:, col_idx].tolist()
        if v is not None and str(v).strip()
    ]
    if values and _looks_like_header(values[0]):
        values = values[1:]
    values = _dedupe_case_insensitive([v for v in values if v])
    if not values:
        return [], f"No terms found in column {col_label} of tab {tab_label}"
    return values, ""


def render_terms_source_sidebar(
    key_prefix: str,
    manual_terms: list[str],
    label: str = "Source",
) -> list[str]:
    """Render the source toggle + Google Sheet / Excel inputs in the current
    Streamlit context. Returns the effective terms list based on selection.

    `key_prefix` namespaces every session-state key so multiple sidebars can
    coexist without clobbering each other (e.g. 'app', 'cmp').
    `manual_terms` is whatever the caller parsed from the manual textarea.
    """
    # Import here so this module stays importable without Streamlit installed.
    import streamlit as st
    from datetime import datetime
    from core.settings import get_terms_source_config

    src_key   = f"{key_prefix}_terms_source"
    url_key   = f"{key_prefix}_gs_url"
    tab_key   = f"{key_prefix}_gs_tab"
    col_key   = f"{key_prefix}_gs_col"
    xl_tab_key = f"{key_prefix}_xl_tab"
    xl_col_key = f"{key_prefix}_xl_col"
    xl_upload_key = f"{key_prefix}_xl_upload"
    terms_key = f"{key_prefix}_gs_terms"
    fetched_key = f"{key_prefix}_gs_fetched_at"
    error_key = f"{key_prefix}_gs_error"

    # Seed non-widget state from persisted config so URL / tab / column /
    # fetched_at survive process restarts.
    persisted = get_terms_source_config()
    if src_key not in st.session_state:
        st.session_state[src_key]   = persisted["source"] or "Manual"
    if url_key not in st.session_state:
        st.session_state[url_key]   = persisted["sheet_url"]
    if tab_key not in st.session_state:
        st.session_state[tab_key]   = persisted["sheet_tab"]
    if col_key not in st.session_state:
        st.session_state[col_key]   = persisted["sheet_col"]
    if fetched_key not in st.session_state:
        st.session_state[fetched_key] = persisted["fetched_at"]

    # No `default=`: src_key is always seeded in session_state just above, and
    # Streamlit warns ("created with a default value but also had its value set
    # via the Session State API") when a widget is given both.
    source = st.segmented_control(
        label,
        ["Manual", "Google Sheet", "Excel"],
        label_visibility="collapsed",
        key=src_key,
    ) or "Manual"

    if source == "Manual":
        return manual_terms

    if source == "Google Sheet":
        st.text_input(
            "Paste Google Sheet URL",
            key=url_key,
            placeholder="https://docs.google.com/spreadsheets/d/…",
        )
        c1, c2 = st.columns(2)
        with c1:
            st.text_input("Tab name", key=tab_key, placeholder="Sheet1")
        with c2:
            st.text_input("Column", key=col_key, placeholder="A")

        fetch_clicked = st.button(
            "Fetch",
            key=f"{key_prefix}_gs_fetch",
            use_container_width=True,
        )
        if fetch_clicked:
            url = (st.session_state.get(url_key) or "").strip()
            if not url:
                st.session_state[error_key] = "Paste a Google Sheet URL first."
                st.session_state[terms_key] = []
            else:
                with st.spinner("Fetching Google Sheet…"):
                    terms, err = fetch_google_sheet_terms(
                        url,
                        (st.session_state.get(tab_key) or "").strip(),
                        (st.session_state.get(col_key) or "A").strip() or "A",
                    )
                if err:
                    st.session_state[error_key] = err
                    st.session_state[terms_key] = []
                else:
                    st.session_state[error_key] = ""
                    st.session_state[terms_key] = terms
                    st.session_state[fetched_key] = datetime.now().isoformat(
                        timespec="seconds"
                    )
    else:
        # Excel upload mode.
        st.file_uploader(
            "Upload .xlsx",
            type=["xlsx"],
            key=xl_upload_key,
            label_visibility="collapsed",
        )
        c1, c2 = st.columns(2)
        with c1:
            st.text_input("Tab name", key=xl_tab_key, placeholder="Sheet1")
        with c2:
            st.text_input("Column", key=xl_col_key, placeholder="A")
        fetch_clicked = st.button(
            "Load from Excel",
            key=f"{key_prefix}_xl_load",
            use_container_width=True,
        )
        if fetch_clicked:
            uploaded = st.session_state.get(xl_upload_key)
            if not uploaded:
                st.session_state[error_key] = "Upload an Excel file first."
                st.session_state[terms_key] = []
            else:
                with st.spinner("Reading Excel…"):
                    terms, err = parse_excel_terms(
                        uploaded.getvalue(),
                        (st.session_state.get(xl_tab_key) or "").strip(),
                        (st.session_state.get(xl_col_key) or "A").strip() or "A",
                    )
                if err:
                    st.session_state[error_key] = err
                    st.session_state[terms_key] = []
                else:
                    st.session_state[error_key] = ""
                    st.session_state[terms_key] = terms
                    st.session_state[fetched_key] = datetime.now().isoformat(
                        timespec="seconds"
                    )

    err = st.session_state.get(error_key, "")
    terms = st.session_state.get(terms_key, [])

    if err:
        st.error(err)

    if terms:
        fetched_at = st.session_state.get(fetched_key, "")
        when = ""
        if fetched_at:
            try:
                when = datetime.fromisoformat(fetched_at).strftime("%d %b · %H:%M")
            except ValueError:
                when = fetched_at
        st.caption(
            f"— {len(terms)} term{'s' if len(terms) != 1 else ''} loaded"
            + (f" · loaded {when}" if when else "")
        )
        st.text_area(
            "Preview (read-only)",
            value="\n".join(terms),
            height=140,
            disabled=True,
            key=f"{key_prefix}_gs_preview",
            label_visibility="collapsed",
        )
        st.caption("Switch to Manual to edit.")

    # Effective terms = last successful fetch/upload (or empty until then).
    return terms


def render_input_source_sidebar(
    key_prefix: str,
    manual_label: str = "Paste keywords (one per line)",
    manual_placeholder: str = "best PTE coaching India\nIELTS voucher discount",
    manual_height: int = 180,
) -> list[str]:
    """Render the "Input Source" section (Manual | Google Sheet | Excel) with
    a column dropdown populated from fetched data. Returns the effective list.

    Session-state keys are namespaced by `key_prefix` so multiple input
    sources can coexist (e.g. 'app_kw', 'cmp_kw', 'app_terms').
    """
    import streamlit as st
    from datetime import datetime
    from core.settings import get_terms_source_config

    # Session-state keys.
    src_key         = f"{key_prefix}_src"
    manual_key      = f"{key_prefix}_manual"
    url_key         = f"{key_prefix}_url"
    tab_key         = f"{key_prefix}_tab"
    gs_rows_key     = f"{key_prefix}_gs_rows"
    gs_cols_key     = f"{key_prefix}_gs_cols"
    gs_col_sel_key  = f"{key_prefix}_gs_col_sel"
    xl_up_key       = f"{key_prefix}_xl_upload"
    xl_tab_key      = f"{key_prefix}_xl_tab"
    xl_rows_key     = f"{key_prefix}_xl_rows"
    xl_cols_key     = f"{key_prefix}_xl_cols"
    xl_col_sel_key  = f"{key_prefix}_xl_col_sel"
    fetched_key     = f"{key_prefix}_fetched_at"
    error_key       = f"{key_prefix}_error"

    # Seed non-widget state from persisted config on first render.
    persisted = get_terms_source_config()
    if src_key not in st.session_state:
        st.session_state[src_key] = persisted["source"] or "Manual"
    if url_key not in st.session_state:
        st.session_state[url_key] = persisted["sheet_url"]
    if tab_key not in st.session_state:
        st.session_state[tab_key] = persisted["sheet_tab"]

    # No `default=` — see the note on the other segmented_control: src_key is
    # already seeded in session_state, and passing both triggers Streamlit's warning.
    source = st.segmented_control(
        "Input Source",
        ["Manual", "Google Sheet", "Excel"],
        label_visibility="collapsed",
        key=src_key,
    ) or "Manual"

    # ---------- Manual ----------
    if source == "Manual":
        manual = st.text_area(
            manual_label,
            height=manual_height,
            placeholder=manual_placeholder,
            key=manual_key,
            label_visibility="collapsed",
        )
        parts: list[str] = []
        for line in (manual or "").splitlines():
            for chunk in line.split(","):
                s = chunk.strip()
                if s:
                    parts.append(s)
        # Repeats are normally collapsed so a scan never pays twice for one query.
        # The "Keep duplicate keywords" checkbox turns that off for load testing —
        # honoured HERE because this is where the manual box is actually deduped;
        # anything downstream never sees the repeats.
        if _keep_duplicates():
            return parts
        return _dedupe_case_insensitive(parts)

    # ---------- Google Sheet ----------
    if source == "Google Sheet":
        st.text_input(
            "Paste Google Sheet URL",
            key=url_key,
            placeholder="https://docs.google.com/spreadsheets/d/…",
        )
        st.text_input("Tab name (optional)", key=tab_key, placeholder="Sheet1")

        fetch_clicked = st.button(
            "Fetch",
            key=f"{key_prefix}_gs_fetch",
            use_container_width=True,
        )
        if fetch_clicked:
            url = (st.session_state.get(url_key) or "").strip()
            tab = (st.session_state.get(tab_key) or "").strip()
            if not url:
                st.session_state[error_key] = "Paste a Google Sheet URL first."
                st.session_state[gs_rows_key] = []
                st.session_state[gs_cols_key] = []
            else:
                with st.spinner("Fetching Google Sheet…"):
                    rows, cols, err = fetch_google_sheet_data(url, tab)
                if err:
                    st.session_state[error_key] = err
                    st.session_state[gs_rows_key] = []
                    st.session_state[gs_cols_key] = []
                else:
                    st.session_state[error_key] = ""
                    st.session_state[gs_rows_key] = rows
                    st.session_state[gs_cols_key] = cols
                    st.session_state[fetched_key] = datetime.now().isoformat(
                        timespec="seconds"
                    )
                    # Default selection = every column whose header hits a
                    # known keyword-column word. Falls back to the first
                    # column if no header matches.
                    defaults = [
                        c for c in cols
                        if any(h in c.lower() for h in _HEADER_HINTS)
                    ] or ([cols[0]] if cols else [])
                    st.session_state[gs_col_sel_key] = defaults

        err = st.session_state.get(error_key, "")
        cols = st.session_state.get(gs_cols_key, [])
        rows = st.session_state.get(gs_rows_key, [])

        if err:
            st.error(err)

        if cols:
            # Migrate legacy single-string state (from the pre-multiselect UI).
            cur = st.session_state.get(gs_col_sel_key)
            if isinstance(cur, list) and cur:
                # First entry becomes keyword col, rest become metadata cols.
                st.session_state[f"{key_prefix}_gs_kw_col"] = (
                    cur[0] if cur[0] in cols else cols[0]
                )
                st.session_state[f"{key_prefix}_gs_extra_cols"] = [
                    c for c in cur[1:] if c in cols
                ]
                del st.session_state[gs_col_sel_key]
            elif isinstance(cur, str) and cur:
                st.session_state[f"{key_prefix}_gs_kw_col"] = (
                    cur if cur in cols else cols[0]
                )
                del st.session_state[gs_col_sel_key]

            kw_col_key = f"{key_prefix}_gs_kw_col"
            extra_key  = f"{key_prefix}_gs_extra_cols"
            if kw_col_key not in st.session_state:
                # Default keyword column = first header-hint column.
                default_kw = next(
                    (c for c in cols
                     if any(h in c.lower() for h in _HEADER_HINTS)),
                    cols[0],
                )
                st.session_state[kw_col_key] = default_kw

            st.selectbox(
                "Keyword column",
                cols,
                key=kw_col_key,
                help="Column whose rows become the keywords to scan.",
            )
            # Extra columns default to none. Excludes the keyword column.
            extra_choices = [c for c in cols if c != st.session_state[kw_col_key]]
            cur_extra = st.session_state.get(extra_key) or []
            # Drop any previously selected column that's now the keyword col.
            st.session_state[extra_key] = [
                c for c in cur_extra if c in extra_choices
            ]
            st.multiselect(
                "Extra columns",
                extra_choices,
                key=extra_key,
                help=(
                    "Pulled through as extra metadata (Search Volume, Difficulty, "
                    "etc.). Appear as columns in the Keyword Level table."
                ),
            )

            kw_col   = st.session_state[kw_col_key]
            extras   = st.session_state.get(extra_key) or []
            kw_idx   = cols.index(kw_col) if kw_col in cols else 0
            extra_idxs = [(c, cols.index(c)) for c in extras if c in cols]

            # Build keyword list + metadata map in a single pass.
            terms: list[str] = []
            seen: set[str] = set()
            metadata: dict[str, dict[str, str]] = {}
            for r in rows:
                if kw_idx >= len(r):
                    continue
                kw = str(r[kw_idx]).strip()
                if not kw or kw.lower() in seen:
                    continue
                seen.add(kw.lower())
                terms.append(kw)
                metadata[kw] = {
                    label: (str(r[idx]).strip() if idx < len(r) else "")
                    for label, idx in extra_idxs
                }

            # Publish to session state so Compare Reports can read metadata.
            st.session_state[f"{key_prefix}_metadata"] = metadata
            st.session_state[f"{key_prefix}_metadata_columns"] = [c for c, _ in extra_idxs]

            _render_preview(st, key_prefix, terms, st.session_state.get(fetched_key, ""))
            return terms
        return []

    # ---------- Excel ----------
    st.file_uploader(
        "Upload .xlsx",
        type=["xlsx"],
        key=xl_up_key,
        label_visibility="collapsed",
    )
    st.text_input("Tab name (optional)", key=xl_tab_key, placeholder="Sheet1")

    load_clicked = st.button(
        "Load from Excel",
        key=f"{key_prefix}_xl_load",
        use_container_width=True,
    )
    if load_clicked:
        uploaded = st.session_state.get(xl_up_key)
        if not uploaded:
            st.session_state[error_key] = "Upload an Excel file first."
            st.session_state[xl_rows_key] = []
            st.session_state[xl_cols_key] = []
        else:
            with st.spinner("Reading Excel…"):
                rows, cols, err = parse_excel_data(
                    uploaded.getvalue(),
                    (st.session_state.get(xl_tab_key) or "").strip(),
                )
            if err:
                st.session_state[error_key] = err
                st.session_state[xl_rows_key] = []
                st.session_state[xl_cols_key] = []
            else:
                st.session_state[error_key] = ""
                st.session_state[xl_rows_key] = rows
                st.session_state[xl_cols_key] = cols
                st.session_state[fetched_key] = datetime.now().isoformat(
                    timespec="seconds"
                )
                defaults = [
                    c for c in cols
                    if any(h in c.lower() for h in _HEADER_HINTS)
                ] or ([cols[0]] if cols else [])
                st.session_state[xl_col_sel_key] = defaults

    err = st.session_state.get(error_key, "")
    cols = st.session_state.get(xl_cols_key, [])
    rows = st.session_state.get(xl_rows_key, [])
    if err:
        st.error(err)
    if cols:
        # Legacy migration → keyword col + extra cols keys.
        cur = st.session_state.get(xl_col_sel_key)
        if isinstance(cur, list) and cur:
            st.session_state[f"{key_prefix}_xl_kw_col"] = (
                cur[0] if cur[0] in cols else cols[0]
            )
            st.session_state[f"{key_prefix}_xl_extra_cols"] = [
                c for c in cur[1:] if c in cols
            ]
            del st.session_state[xl_col_sel_key]
        elif isinstance(cur, str) and cur:
            st.session_state[f"{key_prefix}_xl_kw_col"] = (
                cur if cur in cols else cols[0]
            )
            del st.session_state[xl_col_sel_key]

        kw_col_key = f"{key_prefix}_xl_kw_col"
        extra_key  = f"{key_prefix}_xl_extra_cols"
        if kw_col_key not in st.session_state:
            default_kw = next(
                (c for c in cols
                 if any(h in c.lower() for h in _HEADER_HINTS)),
                cols[0],
            )
            st.session_state[kw_col_key] = default_kw

        st.selectbox(
            "Keyword column",
            cols,
            key=kw_col_key,
            help="Column whose rows become the keywords to scan.",
        )
        extra_choices = [c for c in cols if c != st.session_state[kw_col_key]]
        cur_extra = st.session_state.get(extra_key) or []
        st.session_state[extra_key] = [c for c in cur_extra if c in extra_choices]
        st.multiselect(
            "Extra columns",
            extra_choices,
            key=extra_key,
            help=(
                "Pulled through as extra metadata (Search Volume, Difficulty, "
                "etc.). Appear as columns in the Keyword Level table."
            ),
        )

        kw_col     = st.session_state[kw_col_key]
        extras     = st.session_state.get(extra_key) or []
        kw_idx     = cols.index(kw_col) if kw_col in cols else 0
        extra_idxs = [(c, cols.index(c)) for c in extras if c in cols]

        terms: list[str] = []
        seen: set[str] = set()
        metadata: dict[str, dict[str, str]] = {}
        for r in rows:
            if kw_idx >= len(r):
                continue
            kw = str(r[kw_idx]).strip()
            if not kw or kw.lower() in seen:
                continue
            seen.add(kw.lower())
            terms.append(kw)
            metadata[kw] = {
                label: (str(r[idx]).strip() if idx < len(r) else "")
                for label, idx in extra_idxs
            }

        st.session_state[f"{key_prefix}_metadata"] = metadata
        st.session_state[f"{key_prefix}_metadata_columns"] = [c for c, _ in extra_idxs]

        _render_preview(st, key_prefix, terms, st.session_state.get(fetched_key, ""))
        return terms
    return []


def _render_preview(st, key_prefix: str, terms: list[str], fetched_at: str) -> None:
    """Small status caption + read-only preview textarea shared by both
    Google Sheet and Excel branches."""
    from datetime import datetime
    when = ""
    if fetched_at:
        try:
            when = datetime.fromisoformat(fetched_at).strftime("%d %b · %H:%M")
        except ValueError:
            when = fetched_at
    st.caption(
        f"— {len(terms)} value{'s' if len(terms) != 1 else ''} loaded"
        + (f" · loaded {when}" if when else "")
    )
    if terms:
        st.text_area(
            "Preview (read-only)",
            value="\n".join(terms),
            height=140,
            disabled=True,
            key=f"{key_prefix}_preview",
            label_visibility="collapsed",
        )
        st.caption("Switch to Manual to edit.")


def get_keyword_metadata(key_prefix: str) -> tuple[list[str], dict[str, dict[str, str]]]:
    """Read the metadata columns + keyword→metadata map published by the last
    fetch under `key_prefix`. Returns ([], {}) if nothing loaded."""
    import streamlit as st
    cols = st.session_state.get(f"{key_prefix}_metadata_columns") or []
    md   = st.session_state.get(f"{key_prefix}_metadata") or {}
    return list(cols), dict(md)


def snapshot_terms_source_config(key_prefix: str) -> dict:
    """Snapshot the current input-source config so callers can persist it
    with a run record. sheet_col may now be a list (multi-column select)."""
    import streamlit as st
    ss = st.session_state
    if f"{key_prefix}_src" in ss:
        gs_sel = ss.get(f"{key_prefix}_gs_col_sel")
        xl_sel = ss.get(f"{key_prefix}_xl_col_sel")
        sheet_col = gs_sel or xl_sel or ""
        return {
            "source": ss.get(f"{key_prefix}_src", "Manual"),
            "sheet_url": ss.get(f"{key_prefix}_url", ""),
            "sheet_tab": ss.get(f"{key_prefix}_tab", ""),
            "sheet_col": sheet_col,
            "fetched_at": ss.get(f"{key_prefix}_fetched_at", ""),
        }
    return {
        "source": ss.get(f"{key_prefix}_terms_source", "Manual"),
        "sheet_url": ss.get(f"{key_prefix}_gs_url", ""),
        "sheet_tab": ss.get(f"{key_prefix}_gs_tab", ""),
        "sheet_col": ss.get(f"{key_prefix}_gs_col", ""),
        "fetched_at": ss.get(f"{key_prefix}_gs_fetched_at", ""),
    }

