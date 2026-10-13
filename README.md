# LLM Citation Scraper

Submits a list of prompts to ChatGPT, Claude, Gemini, and Perplexity, scrapes the responses + cited URLs, and shows a Streamlit dashboard with the most-cited domains and how many prompts reference each.

## Warning — Terms of Service

Scraping the chat UIs of ChatGPT, Claude, and Gemini **violates their Terms of Service** and can get your account banned. Perplexity public search URLs are publicly accessible but still ToS-restricted for automation. Run this against test accounts you are willing to lose. The official APIs (OpenAI / Anthropic / Gemini / Perplexity Sonar) all return citations as structured data and would be the compliant path — see `core/api_alternative.md`.

## Deploying on a server

For Windows Server -- service setup, proxies, firewall, headless
browser gotchas -- see **[DEPLOY-WINDOWS.md](DEPLOY-WINDOWS.md)**.
The steps below are for a local development machine.

## Install

```powershell
python -m venv .venv
.venv\Scripts\Activate.ps1
pip install -r requirements.txt
scrapling install                # downloads browser binaries Scrapling needs
python -m playwright install chromium
```

## Login

Log in once per platform. A real browser window opens, you sign in, and the cookies are written to `data/profiles/<platform>/` for every later run to reuse:

```powershell
python -m scrapers.login perplexity
python -m scrapers.login chatgpt
python -m scrapers.login gemini
```

**Perplexity requires it.** Anonymous search is gated — logged out it answers "Sign up and repeat your request" and returns no citations at all. Being signed in also restores the source links.

Perplexity's login goes through Camoufox (Firefox) rather than Chrome, because its scraper uses Scrapling's StealthyFetcher to clear Cloudflare and a Chromium profile's cookies are unreadable to it. That is what `scrapling install` provides.

Re-run the login for a platform whenever a scraper reports a sign-up wall; sessions expire and nothing recovers that on its own.

Gemini's anonymous mode renders only at `gemini.google.com/app?hl=en-IN` and below — the locale parameter unlocks the guest composer. In anonymous Gemini, the inline citation chips don't expose source URLs as plain anchors, so you'll see Gemini responses with `citation_count = 0` even when the model used grounding.

Claude was dropped because claude.ai has no anonymous mode.

⚠ Automating a signed-in account violates these platforms' Terms of Service. Use accounts you are willing to lose.

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
  perplexity.py       Camoufox/Scrapling on a logged-in profile
  chatgpt.py          chatgpt.com (login required, always headed)
  gemini.py           gemini.google.com (login required, headless OK)
  login.py            One-shot interactive login helper
core/
  aggregator.py       Domain extraction + counting
  storage.py          SQLite results store
data/
  results.db          Created on first run
  profiles/           Per-platform Playwright user data dirs
```
