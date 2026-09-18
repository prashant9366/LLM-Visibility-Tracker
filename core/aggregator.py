from __future__ import annotations

from collections import Counter, defaultdict
from typing import Iterable, List
from urllib.parse import urlparse
import json

import pandas as pd
import tldextract


_EXTRACT = tldextract.TLDExtract(suffix_list_urls=None)  # offline; ships its own snapshot

# Self-references and tracker domains we want to exclude from "external citations".
_SKIP_DOMAINS = {
    "chatgpt.com", "openai.com", "chat.openai.com",
    "claude.ai", "anthropic.com",
    "gemini.google.com", "bard.google.com", "google.com",
    "perplexity.ai",
    "googleusercontent.com",
}


def domain_of(url: str) -> str:
    try:
        host = urlparse(url).netloc.lower()
        if not host:
            return ""
        ext = _EXTRACT(host)
        if ext.domain and ext.suffix:
            return f"{ext.domain}.{ext.suffix}"
        return host
    except Exception:
        return ""


def _to_list(val) -> List[str]:
    if isinstance(val, list):
        return val
    if isinstance(val, str) and val:
        try:
            parsed = json.loads(val)
            return parsed if isinstance(parsed, list) else []
        except json.JSONDecodeError:
            return []
    return []


def build_dataframes(results: Iterable[dict]) -> dict:
    """Returns a dict of pandas DataFrames ready for the dashboard."""
    rows = list(results)
    if not rows:
        return {
            "results": pd.DataFrame(),
            "top_domains": pd.DataFrame(),
            "domain_x_platform": pd.DataFrame(),
            "prompts_per_domain": pd.DataFrame(),
            "fanout": pd.DataFrame(),
        }

    results_df = pd.DataFrame(rows)
    results_df["citations_list"] = results_df["citations"].apply(_to_list)
    results_df["source_urls_list"] = (
        results_df["source_urls"].apply(_to_list)
        if "source_urls" in results_df.columns
        else [[] for _ in range(len(results_df))]
    )
    results_df["fanout_list"] = (
        results_df["fanout_queries"].apply(_to_list)
        if "fanout_queries" in results_df.columns
        else [[] for _ in range(len(results_df))]
    )

    # Combined URL set per row for domain analysis: visible citations + backend-extracted source_urls.
    def _combined(row) -> list[str]:
        merged, seen = [], set()
        for u in list(row["citations_list"]) + list(row["source_urls_list"]):
            if u and u not in seen:
                seen.add(u)
                merged.append(u)
        return merged

    results_df["all_urls"] = results_df.apply(_combined, axis=1)
    results_df["citation_count"] = results_df["all_urls"].apply(len)
    results_df["fanout_count"] = results_df["fanout_list"].apply(len)

    # Flatten (platform, prompt, url, domain) across the combined set.
    flat = []
    for r in results_df.itertuples(index=False):
        for url in r.all_urls:
            d = domain_of(url)
            if not d or d in _SKIP_DOMAINS:
                continue
            flat.append({
                "platform": r.platform,
                "prompt": r.prompt,
                "url": url,
                "domain": d,
            })
    flat_df = pd.DataFrame(flat)

    # Flatten fanout queries for inspection.
    fanout_rows = []
    for r in results_df.itertuples(index=False):
        for q in r.fanout_list:
            if not isinstance(q, str) or not q.strip():
                continue
            fanout_rows.append({
                "platform": r.platform,
                "prompt": r.prompt,
                "fanout_query": q.strip(),
            })
    fanout_df = pd.DataFrame(fanout_rows)

    if flat_df.empty:
        return {
            "results": results_df,
            "top_domains": pd.DataFrame(columns=["domain", "citations"]),
            "domain_x_platform": pd.DataFrame(),
            "prompts_per_domain": pd.DataFrame(columns=["domain", "prompts"]),
            "flat": flat_df,
            "fanout": fanout_df,
        }

    top_domains = (
        flat_df.groupby("domain").size().reset_index(name="citations")
        .sort_values("citations", ascending=False)
    )

    domain_x_platform = (
        flat_df.groupby(["domain", "platform"]).size().reset_index(name="citations")
        .pivot(index="domain", columns="platform", values="citations").fillna(0).astype(int)
    )
    domain_x_platform["total"] = domain_x_platform.sum(axis=1)
    domain_x_platform = domain_x_platform.sort_values("total", ascending=False).drop(columns="total")

    prompts_per_domain = (
        flat_df.groupby("domain")["prompt"].nunique().reset_index(name="prompts")
        .sort_values("prompts", ascending=False)
    )

    return {
        "results": results_df,
        "top_domains": top_domains,
        "domain_x_platform": domain_x_platform,
        "prompts_per_domain": prompts_per_domain,
        "flat": flat_df,
        "fanout": fanout_df,
    }
