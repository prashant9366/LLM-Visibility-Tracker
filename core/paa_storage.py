"""SQLite store for the People-also-ask feature.

Lives in the same results.db as the citation tracker but in its own
`paa_runs` table — the two features never mix rows. One row per scraped
question; a keyword that failed (no PAA / CAPTCHA) stores a single row with
an empty question and the error message so failures stay visible.
"""
from __future__ import annotations

import sqlite3
import time
from pathlib import Path
from typing import Iterable, List

DB_PATH = Path(__file__).resolve().parent.parent / "data" / "results.db"
DB_PATH.parent.mkdir(parents=True, exist_ok=True)


_SCHEMA = """
CREATE TABLE IF NOT EXISTS paa_runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts REAL NOT NULL,
    keyword TEXT NOT NULL,
    position INTEGER,
    question TEXT,
    answer TEXT,
    source_title TEXT,
    source_url TEXT,
    error TEXT,
    kind TEXT DEFAULT 'question',
    parent TEXT DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_paa_runs_ts ON paa_runs(ts);
CREATE INDEX IF NOT EXISTS idx_paa_runs_keyword ON paa_runs(keyword);
"""


def _conn() -> sqlite3.Connection:
    c = sqlite3.connect(DB_PATH)
    c.row_factory = sqlite3.Row
    return c


def init_db() -> None:
    with _conn() as c:
        c.executescript(_SCHEMA)
        # Migrate pre-existing tables: rows of kind 'question' (PAA) or
        # 'related' ("People also search for"). Old rows default to 'question'.
        cols = {r[1] for r in c.execute("PRAGMA table_info(paa_runs)")}
        if "kind" not in cols:
            c.execute("ALTER TABLE paa_runs ADD COLUMN kind TEXT DEFAULT 'question'")
        if "parent" not in cols:
            c.execute("ALTER TABLE paa_runs ADD COLUMN parent TEXT DEFAULT ''")


def save_paa_results(rows: Iterable[dict]) -> float:
    """Insert one batch under a single ts (the run id). Returns that ts."""
    init_db()
    now = time.time()
    with _conn() as c:
        c.executemany(
            "INSERT INTO paa_runs ("
            "ts, keyword, position, question, answer, source_title, source_url, error, kind, parent"
            ") VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            [
                (
                    now,
                    r["keyword"],
                    r.get("position", 0),
                    r.get("question", ""),
                    r.get("answer", ""),
                    r.get("source_title", ""),
                    r.get("source_url", ""),
                    r.get("error", ""),
                    r.get("kind", "question"),
                    r.get("parent", ""),
                )
                for r in rows
            ],
        )
    return now


_SELECT_COLS = "ts, keyword, position, question, answer, source_title, source_url, error, kind, parent"


def list_paa_runs(limit: int = 50) -> List[dict]:
    """One row per distinct run (grouped by ts), newest first."""
    init_db()
    with _conn() as c:
        rows = c.execute(
            "SELECT ts, COUNT(DISTINCT keyword) AS keyword_count, "
            "SUM(CASE WHEN kind = 'question' AND question != '' THEN 1 ELSE 0 END) AS question_count, "
            "SUM(CASE WHEN kind = 'related' THEN 1 ELSE 0 END) AS related_count "
            "FROM paa_runs GROUP BY ts ORDER BY ts DESC LIMIT ?",
            (limit,),
        ).fetchall()
    return [dict(r) for r in rows]


def load_paa_run(ts: float) -> List[dict]:
    init_db()
    with _conn() as c:
        rows = c.execute(
            f"SELECT {_SELECT_COLS} FROM paa_runs WHERE ts = ? ORDER BY keyword, position",
            (ts,),
        ).fetchall()
    return [dict(r) for r in rows]


def delete_paa_run(ts: float) -> None:
    init_db()
    with _conn() as c:
        c.execute("DELETE FROM paa_runs WHERE ts = ?", (ts,))


def clear_paa() -> None:
    init_db()
    with _conn() as c:
        c.execute("DELETE FROM paa_runs")
