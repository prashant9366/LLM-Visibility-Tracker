# Changelog

All notable changes to this project are recorded here.

Format based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).
Dates are `YYYY-MM-DD`.

Sections per release: `Added`, `Changed`, `Fixed`, `Removed`, `Deprecated`, `Security`.

## [Unreleased]

### Added
- **People Also Ask scraper** — a standalone feature, separate from the
  citation tracker. New Streamlit page (`pages/People_Also_Ask.py`) with its
  own keyword input (paste or CSV/XLSX upload), run button, scan history and
  Excel/CSV export. The scraper (`scrapers/google_paa.py`) reuses one headed
  Chrome tab across all keywords (same persistent-session pattern as the AI
  Overview scraper, own `google_paa` profile), clicks each PAA question open
  — which also makes Google append follow-up questions — up to a configurable
  cap (4–20, default 10), and captures question / answer / source per item.
  Results live in their own `paa_runs` table (`core/paa_storage.py`) so the
  two features never mix rows. Answer cleaning strips Google chrome: when a
  mini AI Overview renders inside an expanded PAA answer, the sources
  carousel and inline "view related links" publisher chips are hidden in the
  DOM before innerText is read, and the AIO label/footer/`+N` chip lines are
  stripped Python-side.
- `CHANGELOG.md` to track updates.
- New "Where you ARE cited" section in `app.py` — the inverse of the
  existing "Where you're not cited" gap panel. For each tracked term, lists
  every (term × keyword × platform) hit with where the match landed
  (`text` / `url` / `text+url`), the position in the source URL list, and
  the matched URL (clickable, domain shown, opens in new tab). Per-term
  summary chips at the top show `{N kw · M hits}` for each tracked term
  that fired. Three filters above the table (tracked term / platform /
  where matched) let users drill in. Table truncates at 200 rows with a
  count footer when there are more — narrow with filters.

### Changed
- Sidebar tool switcher. The default multipage nav links ("app" / "People
  Also Ask") are hidden on both pages and replaced by a themed segmented
  toggle (Citation Tracker ⇄ People Also Ask) at the top of the sidebar,
  wired to `st.switch_page`. The PAA page also gained the Material-icon
  font pinning block from `app.py` so Streamlit chrome icons can't leak as
  raw ligature text.
- Citation matrix surfaces tracked-term matches too. Tracked-term
  substrings inside the keyword text are now bold + gradient-tinted, and a
  chip row appended after the keyword shows which tracked terms hit (with
  `k`/`u` indicator: keyword text vs cited URL). Extracted the highlight
  logic into a shared `bold_tracked(text, terms)` helper so the matrix
  keyword cell and URL leaderboard's keyword chips share the same
  rendering. New `.tracked-bold` CSS class applies the violet→cyan text
  gradient.
- URL leaderboard's "Cited for" column now surfaces tracked-term matches.
  A new chip row above the keyword pills shows every term from the sidebar
  "Track these" list that hits anywhere relevant for that row — the domain
  itself, any URL path under it, or any keyword that cited it. Each chip
  is rendered in the violet→cyan gradient and carries a small mono
  `d`/`u`/`k` indicator (and `title=` tooltip) telling you WHERE the match
  hit. Inside the keyword chips themselves, any tracked-term substring is
  now bold + gradient-tinted so brand mentions inside long keywords pop.
- Downloadable Excel report (`core/export.py`) restructured for
  pivotability. Previous version cram-packed lists into multi-line cells
  (`cited_for_keywords`, `all_paths`) which looked messy in Excel and
  couldn't be pivoted. Replaced with a "narrow summary + long companion"
  layout:
  - **Citation matrix** sheet kept the wide keyword × platform layout but
    dropped the multi-line `urls_cited` column; it now only carries
    `unique_urls`, `total_url_citations`, `score`.
  - New **Matrix URLs** sheet (long): one row per (keyword, URL) pair
    with `domain`, `platforms_count`, `platforms`, `is_tracked`.
    Pivot keyword in rows × platforms_count in values to reproduce the
    matrix accordion.
  - **URL leaderboard** sheet now narrow and numeric: `rank`, `domain`,
    `is_tracked`, `total_citations`, `unique_paths`, `keyword_count`,
    `platforms_count`, plus a `cites_<platform>` column per LLM. No
    multi-line cells.
  - New **Domain x Keyword** sheet (long): one row per (domain, keyword)
    with `citations` count. Pivot in Excel by dropping `domain` in rows
    and `keyword` in columns to get a full domain-vs-keyword heatmap.
  - New **Domain paths** sheet (long): one row per (domain, URL path) for
    inspecting which specific reddit threads / YouTube videos / etc.
    folded into a domain row.
  - Header row frozen on every sheet via `freeze_panes = "A2"`; column
    widths tuned per-column by name (not by letter, so they survive
    column reordering).
- URL leaderboard now aggregates by **domain** instead of by full URL —
  e.g. `www.reddit.com` appears as a single row even when LLMs cited five
  different reddit threads. Each row shows a small "N paths" pill next to
  the domain (only when >1) so you still know how many distinct URL paths
  fold into the row. Header column renamed `URL` → `Domain`. The "Cited for"
  keyword column now reflects every keyword that cited *any* URL under that
  domain, deduped and frequency-sorted.
- URL leaderboard gets a platform toggle row above the existing filter +
  search controls. Pick "All platforms" or any single LLM (ChatGPT /
  Gemini / Perplexity); the entire table — counts, keywords, platform
  dots, ranking — recomputes against just that platform's citations. The
  section meta line below the header also reflects the active platform.
- Citation matrix and URL leaderboard each get a search box next to their
  existing radio filter. Matrix search filters prompts by case-insensitive
  substring; leaderboard search matches the URL itself, its domain, OR any
  keyword that cited it — so searching for a brand surfaces both that
  brand's URLs and any competitor URLs cited under brand-related keywords.
  Both searches compose with the radio filter (All/Cited only/Gaps only for
  matrix; All/Your URLs/Competitors for leaderboard).
- Citation matrix rows are now expandable. Each prompt row in `app.py` is
  followed by a native `<details>` accordion (no JavaScript) that, when
  opened, shows every URL that LLM cited for that keyword — grouped by
  platform, with a per-platform count badge, the domain as the primary
  link, and the full URL underneath. Tracked URLs render in the violet→cyan
  gradient so brand citations stand out. Capped at 50 URLs per platform with
  a "+N more" tail. Rows with no captured URLs show a muted "No URLs
  captured for this keyword" hint instead of a clickable accordion. Each
  URL inside the accordion now carries an `Nx` cross-platform count badge
  (how many LLMs cited it for that keyword); URLs cited by *every*
  platform that ran for the prompt light up with the gradient "hot" badge.
  URLs within each platform group are reordered by that count desc so the
  most aggressively cited sources surface first, and the summary line now
  reads `X unique · Y total` so cross-platform overlap is visible without
  opening the panel.
- The "+N" overflow chip in the URL leaderboard's "Cited for" column is now
  click-to-expand (was a hover-only tooltip). Wrapped the overflow chips in
  a native `<details>` element whose `<summary>` is styled as the dashed
  `+N` chip — clicking it reveals the remaining keyword chips inline below
  the first three. The summary turns solid violet when open and grows a `▾`
  glyph; no JavaScript required.
- URL leaderboard in `app.py` now shows which keywords cited each URL. New
  `Cited for` column between URL and the per-LLM dots: monospace pill chips
  for the first 3 prompts (sorted by per-keyword citation frequency), a
  dashed `+N` overflow chip when there are more (with the full list in the
  tooltip), and a `N kw` count label above the chips. The existing `Cites`
  column still shows the total citations across the whole run.
- Full visual overhaul of `app.py` to a dark "next-gen AI" theme: deep neutral
  background with subtle violet→cyan glow mesh, glassmorphic cards with
  `backdrop-filter`, gradient accent (`#8b5cf6` → `#06b6d4`) used for primary
  button, progress bar, brand mark, highlights, and "you" pills. Typography
  keeps Instrument Serif for headings and Geist / Geist Mono for body and
  labels; KPI numbers and page title now use a white→grey vertical text
  gradient. Plotly charts (citation rate bar + top-sources bubble) updated to
  the dark palette so axis labels and bars stay legible. Streamlit base
  widgets (inputs, selectbox dropdown popovers, file uploader, expander,
  toast, alerts, scrollbar) re-skinned for the dark surface so text contrast
  is preserved everywhere.

### Fixed
- Raw Material Symbols ligature text (e.g. `keyboard_double_arrow_right`,
  `keyboard_arrow_right`, `expand_more`) was leaking through where Streamlit
  renders icon buttons — the sidebar collapse hamburger and the "Danger
  zone" expander chevron both showed the icon's name as plain text instead
  of a glyph. First attempt imported Material Symbols fonts from Google
  Fonts with `display=block`, but the "Danger zone" expander chevron kept
  leaking on slower loads. Final fix: stopped relying on a remote font for
  the expander chevron altogether — hid the ligature text via
  `font-size: 0` and drew the chevron with a CSS pseudo-element using the
  Unicode `›` (U+203A) glyph in our local `Geist` font, with a rotate
  animation on `details[open]` so it still flips visually. Targets all the
  testid/class variants Streamlit uses for the icon span.
- Freshly-saved scrapes did not appear in the "Scan history" dropdown even
  though they were correctly persisted to `data/results.db`. Removed the
  `@st.cache_data` wrappers around `list_runs` and `load_recent` in
  `app.py` — both are sub-millisecond SQLite reads, and caching them across
  reruns kept producing stale-cache bugs (the earlier `_token`→`token` rename
  was a symptom of the same class of issue). Call sites now invoke
  `list_runs(limit=50)` / `load_recent(limit=5000)` directly. The
  `_cache_token()` mechanism is retained only for `_cached_dataframes`,
  where the rebuild cost is non-trivial.
- Gemini responses showed "No response captured" in the UI even when the
  model produced an answer on screen. Two-layer fix in `scrapers/gemini.py`:
  (1) replaced the brittle CSS-selector chain with an in-browser
  `page.evaluate` scan that picks the **text-heaviest element** among ~10
  candidate selectors — so when Gemini renames its Angular components we
  don't go blind, we just pick the one that actually has text. (2) Replaced
  the `wait_for_stable_text` call with `_wait_for_stable_gemini_text` which
  polls using the same text-heaviest scan, so the wait can no longer stall
  on an empty Angular shell while the real answer streams into a different
  element. Plus a `<main>`-wide largest-block fallback and an opt-in
  `SCRAPER_DEBUG_DOM=1` env var that dumps the page HTML to
  `data/debug/gemini_empty_<ts>.html` on extraction failure for diagnosis.
- Dashboard reverted to the empty pre-scrape state after a run completed. The
  `_cached_list_runs` / `_cached_load_recent` helpers in `app.py` took a `_token`
  argument; Streamlit's `st.cache_data` excludes underscore-prefixed args from
  the cache key, so `_bump_token()` never invalidated the cache. Renamed to
  `token` so the freshly-saved run actually appears in `run_options` and the
  `pending_run_ts` auto-selection finds it.

### Removed
