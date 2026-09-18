# If/when you want to switch off scraping

All four vendors return citations as structured data through official APIs. The dashboard layer (`core/aggregator.py`, `app.py`) does not care where citations come from — only that each `ScrapeResult` has a `citations: list[str]`. Swapping in API calls means replacing the four files under `scrapers/` with API-backed equivalents.

| Platform | Endpoint | Field with URLs |
|---|---|---|
| Perplexity | POST `https://api.perplexity.ai/chat/completions`, model `sonar` or `sonar-pro` | response `citations[]` |
| OpenAI | POST `https://api.openai.com/v1/responses`, tools=`[{type:"web_search"}]` | annotations of type `url_citation` |
| Gemini | `generateContent` with `tools=[{googleSearch:{}}]` | `groundingMetadata.groundingChunks[].web.uri` |
| Anthropic | Messages API with `tools=[{type:"web_search_20250305", name:"web_search"}]` | `citations[]` blocks on each text block |

Cost is roughly $0.01–0.05 per prompt across all four. No bans, no broken selectors, no logins.
