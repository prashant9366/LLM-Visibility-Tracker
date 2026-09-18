"""Tiny persistent settings store — currently just the list of tracked
brand/keyword terms used by the Mentions report on the dashboard. Written
as JSON so a human can hand-edit `data/settings.json` if needed."""
from __future__ import annotations

import json
from pathlib import Path
from typing import List


SETTINGS_PATH = Path(__file__).resolve().parent.parent / "data" / "settings.json"
SETTINGS_PATH.parent.mkdir(parents=True, exist_ok=True)


def _load() -> dict:
    if not SETTINGS_PATH.exists():
        return {}
    try:
        return json.loads(SETTINGS_PATH.read_text(encoding="utf-8")) or {}
    except Exception:
        return {}


def _save(data: dict) -> None:
    SETTINGS_PATH.write_text(json.dumps(data, indent=2), encoding="utf-8")


def get_tracked_terms() -> List[str]:
    data = _load()
    terms = data.get("tracked_terms", [])
    if not isinstance(terms, list):
        return []
    out: List[str] = []
    seen = set()
    for t in terms:
        s = str(t).strip()
        if s and s.lower() not in seen:
            seen.add(s.lower())
            out.append(s)
    return out


def set_tracked_terms(terms: List[str]) -> None:
    data = _load()
    cleaned: List[str] = []
    seen = set()
    for t in terms:
        s = str(t).strip()
        if s and s.lower() not in seen:
            seen.add(s.lower())
            cleaned.append(s)
    data["tracked_terms"] = cleaned
    _save(data)


def _coerce_sheet_col(v) -> list[str]:
    """Normalise sheet_col to a list of strings. Handles legacy string values
    and modern list values from the multi-column selector."""
    if v is None:
        return []
    if isinstance(v, list):
        return [str(x) for x in v if str(x).strip()]
    s = str(v).strip()
    return [s] if s else []


def get_terms_source_config() -> dict:
    """Return the last saved terms-source config (mode/url/tab/col/fetched_at).
    `sheet_col` is a list of column labels (may be empty). Legacy single-string
    values are auto-migrated to a one-element list."""
    data = _load()
    src = data.get("terms_source", {}) or {}
    return {
        "source":     str(src.get("source", "Manual")),
        "sheet_url":  str(src.get("sheet_url", "")),
        "sheet_tab":  str(src.get("sheet_tab", "")),
        "sheet_col":  _coerce_sheet_col(src.get("sheet_col")),
        "fetched_at": str(src.get("fetched_at", "")),
    }


def set_terms_source_config(cfg: dict) -> None:
    """Persist the terms-source config so re-runs can re-pull without the
    user re-entering the sheet URL. Only known keys are stored."""
    data = _load()
    data["terms_source"] = {
        "source":     str(cfg.get("source", "Manual")),
        "sheet_url":  str(cfg.get("sheet_url", "")),
        "sheet_tab":  str(cfg.get("sheet_tab", "")),
        "sheet_col":  _coerce_sheet_col(cfg.get("sheet_col")),
        "fetched_at": str(cfg.get("fetched_at", "")),
    }
    _save(data)


def parse_terms_input(raw: str) -> List[str]:
    """Accept either newline- or comma-separated input. Empty parts dropped,
    leading/trailing whitespace stripped, case-insensitive dedupe."""
    if not raw:
        return []
    # Split on newlines and commas.
    parts: List[str] = []
    for line in raw.splitlines():
        for chunk in line.split(","):
            parts.append(chunk)
    out: List[str] = []
    seen = set()
    for p in parts:
        s = p.strip()
        if s and s.lower() not in seen:
            seen.add(s.lower())
            out.append(s)
    return out
