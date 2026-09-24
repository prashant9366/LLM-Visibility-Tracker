from __future__ import annotations

from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import List
import json

PLATFORMS = ("chatgpt", "gemini", "perplexity", "google")

DATA_DIR = Path(__file__).resolve().parent.parent / "data"
PROFILES_DIR = DATA_DIR / "profiles"
PROFILES_DIR.mkdir(parents=True, exist_ok=True)


@dataclass
class ScrapeResult:
    platform: str
    prompt: str
    response_text: str = ""
    citations: List[str] = field(default_factory=list)
    # URLs the model browsed under the hood (from backend SSE/JSON payloads),
    # which may be broader than the visible citations.
    source_urls: List[str] = field(default_factory=list)
    # Hidden search queries the model issued internally
    # (e.g. ChatGPT search_model_queries / search("...") tool calls,
    # Gemini grounded search queries).
    fanout_queries: List[str] = field(default_factory=list)
    error: str = ""
    raw_html_path: str = ""
    # Wall-clock seconds this single prompt x platform scrape took. Filled in by the
    # caller (app._one) so every platform is measured the same way.
    duration_s: float = 0.0

    def to_row(self) -> dict:
        d = asdict(self)
        d["citations"] = json.dumps(self.citations)
        d["source_urls"] = json.dumps(self.source_urls)
        d["fanout_queries"] = json.dumps(self.fanout_queries)
        return d


def profile_dir(platform: str) -> Path:
    p = PROFILES_DIR / platform
    p.mkdir(parents=True, exist_ok=True)
    return p
