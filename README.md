# LLM Citation Scraper

Submits a list of prompts to ChatGPT, Claude, Gemini, and Perplexity, scrapes the responses + cited URLs, and shows a Streamlit dashboard with the most-cited domains and how many prompts reference each.

## Warning — Terms of Service

Scraping the chat UIs of ChatGPT, Claude, and Gemini **violates their Terms of Service** and can get your account banned. Perplexity public search URLs are publicly accessible but still ToS-restricted for automation. Run this against test accounts you are willing to lose. The official APIs (OpenAI / Anthropic / Gemini / Perplexity Sonar) all return citations as structured data and would be the compliant path — see `core/api_alternative.md`.

## Install

```powershell
python -m venv .venv
.venv\Scripts\Activate.ps1
pip install -r requirements.txt
scrapling install                # downloads browser binaries Scrapling needs
python -m playwright install chromium
```

## Login

None of the three supported platforms (ChatGPT, Perplexity, Gemini) require login. Gemini's anonymous mode renders only at `gemini.google.com/app?hl=en-IN` and below — the locale parameter unlocks the guest composer. In anonymous Gemini, the inline citation chips don't expose source URLs as plain anchors, so you'll see Gemini responses with `citation_count = 0` even when the model used grounding. ChatGPT and Perplexity both expose real source URLs.

Claude was dropped because claude.ai has no anonymous mode.

## Run

```powershell
streamlit run app.py
```

Paste prompts (one per line) or upload a CSV with a `prompt` column, pick the platforms, click **Run**. The dashboard tabs show:

- **Results** — raw rows: prompt × platform → response text + cited URLs
- **Top domains** — bar chart of most-cited domains across all prompts
- **Domain × platform** — heatmap of how often each domain appears per platform
- **Prompts per domain** — for each domain, how many distinct prompts surfaced it

## Layout

```
app.py                Streamlit UI
scrapers/
  base.py             ScrapeResult dataclass, common helpers
  perplexity.py       Public search URL (no login)
  chatgpt.py          chatgpt.com (login required)
  claude.py           claude.ai (login required)
  gemini.py           gemini.google.com (login required)
  login.py            One-shot interactive login helper
core/
  aggregator.py       Domain extraction + counting
  storage.py          SQLite results store
data/
  results.db          Created on first run
  profiles/           Per-platform Playwright user data dirs
```
