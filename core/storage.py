from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Iterable, List
import json
import time

DB_PATH = Path(__file__).resolve().parent.parent / "data" / "results.db"
DB_PATH.parent.mkdir(parents=True, exist_ok=True)


_SCHEMA = """
CREATE TABLE IF NOT EXISTS runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts REAL NOT NULL,
    platform TEXT NOT NULL,
    prompt TEXT NOT NULL,
    response_text TEXT,
    citations TEXT,
    source_urls TEXT,
    fanout_queries TEXT,
    error TEXT,
    raw_html_path TEXT
);
CREATE INDEX IF NOT EXISTS idx_runs_platform ON runs(platform);
CREATE INDEX IF NOT EXISTS idx_runs_ts ON runs(ts);
"""


def _conn() -> sqlite3.Connection:
    c = sqlite3.connect(DB_PATH)
    c.row_factory = sqlite3.Row
    return c


def init_db() -> None:
    with _conn() as c:
        c.executescript(_SCHEMA)
        # Migrate existing dbs that pre-date newer columns.
        cols = {r[1] for r in c.execute("PRAGMA table_info(runs)")}
        if "source_urls" not in cols:
            c.execute("ALTER TABLE runs ADD COLUMN source_urls TEXT")
        if "fanout_queries" not in cols:
            c.execute("ALTER TABLE runs ADD COLUMN fanout_queries TEXT")
        if "terms_source_config" not in cols:
            c.execute("ALTER TABLE runs ADD COLUMN terms_source_config TEXT")
        if "keyword_metadata" not in cols:
            c.execute("ALTER TABLE runs ADD COLUMN keyword_metadata TEXT")


def _coerce_list_field(val) -> str:
    if isinstance(val, str):
        return val
    return json.dumps(val or [])


def save_results(
    results: Iterable[dict],
    terms_source_config: dict | None = None,
    keyword_metadata: dict | None = None,
) -> None:
    """Persist every scrape row for this run under a shared timestamp.
    - `terms_source_config` (Manual vs Google Sheet, URL, tab, column,
      fetched_at) records how the term list was assembled.
    - `keyword_metadata` = {keyword_str: {extra_col_label: value}} — the
      Sheet/Excel-loaded metadata columns (Search Volume, Difficulty, etc.)
      that Compare Reports maps back into the Keyword Level table.
    Both are stored per row so historic runs can re-render with the same
    source/metadata that was used to build them."""
    init_db()
    now = time.time()
    ts_cfg = json.dumps(terms_source_config or {})
    kw_meta = json.dumps(keyword_metadata or {})
    with _conn() as c:
        c.executemany(
            "INSERT INTO runs ("
            "ts, platform, prompt, response_text, citations, source_urls, "
            "fanout_queries, error, raw_html_path, terms_source_config, "
            "keyword_metadata"
            ") VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            [
                (
                    now,
                    r["platform"],
                    r["prompt"],
                    r.get("response_text", ""),
                    _coerce_list_field(r.get("citations", [])),
                    _coerce_list_field(r.get("source_urls", [])),
                    _coerce_list_field(r.get("fanout_queries", [])),
                    r.get("error", ""),
                    r.get("raw_html_path", ""),
                    ts_cfg,
                    kw_meta,
                )
                for r in results
            ],
        )


_SELECT_COLS = (
    "platform, prompt, response_text, citations, source_urls, "
    "fanout_queries, error, raw_html_path, ts, "
    "terms_source_config, keyword_metadata"
)


def load_recent(limit: int = 500) -> List[dict]:
    init_db()
    with _conn() as c:
        rows = c.execute(
            f"SELECT {_SELECT_COLS} FROM runs ORDER BY ts DESC LIMIT ?",
            (limit,),
        ).fetchall()
    return [dict(r) for r in rows]


def list_runs(limit: int = 50) -> List[dict]:
    """One row per distinct run (grouped by ts), newest first."""
    init_db()
    with _conn() as c:
        rows = c.execute(
            "SELECT ts, COUNT(*) AS row_count, COUNT(DISTINCT prompt) AS prompt_count, "
            "GROUP_CONCAT(DISTINCT platform) AS platforms "
            "FROM runs GROUP BY ts ORDER BY ts DESC LIMIT ?",
            (limit,),
        ).fetchall()
    return [dict(r) for r in rows]


def load_run(ts: float) -> List[dict]:
    init_db()
    with _conn() as c:
        rows = c.execute(
            f"SELECT {_SELECT_COLS} FROM runs WHERE ts = ? ORDER BY platform, prompt",
            (ts,),
        ).fetchall()
    return [dict(r) for r in rows]


def delete_run(ts: float) -> None:
    init_db()
    with _conn() as c:
        c.execute("DELETE FROM runs WHERE ts = ?", (ts,))


def clear_all() -> None:
    init_db()
    with _conn() as c:
        c.execute("DELETE FROM runs")
