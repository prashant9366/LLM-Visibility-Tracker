#!/usr/bin/env python3
"""
debug_google.py — why is the Google AI Overview scrape slow, and why are its source
links missing? Runs one real search with per-phase timings, then compares what the
card extractor sees against every anchor actually on the page.

    python3 debug_google.py "best running shoes 2026"

Opens a real Chrome window (the google scraper is always headed by design).
"""
import asyncio
import sys
import time

from scrapers import google as G


PROBE_JS = r"""
() => {
  const norm = s => (s || '').replace(/\s+/g, ' ').trim();
  // 1. locate the AI Overview label exactly like _EXTRACT_JS does
  const cands = Array.from(document.querySelectorAll('h1,h2,h3,div,span,[role="heading"]'));
  let label = null;
  for (const el of cands) {
    const t = norm(el.innerText || el.textContent);
    if (/^AI Overview\b/i.test(t) && t.length < 40) { label = el; break; }
  }
  const out = {label_found: !!label, walk: [], card_links: [], page_links: [],
               card_text_len: 0, card_tag: null};
  if (label) {
    // 2. record the whole ancestor walk so we can see WHERE it stops
    let node = label, card = label;
    for (let i = 0; i < 12 && node; i++) {
      const len = (node.innerText || '').length;
      const anchors = node.querySelectorAll('a[href]').length;
      out.walk.push({i, tag: node.tagName, cls: (node.className||'').toString().slice(0,40),
                     text_len: len, anchors});
      if (len > 250) { card = node; break; }
      card = node;
      node = node.parentElement;
    }
    out.card_tag = card.tagName;
    out.card_text_len = (card.innerText || '').length;
    out.card_links = Array.from(card.querySelectorAll('a[href]'))
      .map(a => a.href).filter(h => /^https?:/i.test(h));
  }
  // 3. every external anchor anywhere on the page, for comparison
  out.page_links = Array.from(document.querySelectorAll('a[href]'))
    .map(a => a.href).filter(h => /^https?:/i.test(h));
  return out;
}
"""


def host(u):
    try:
        return u.split("/", 3)[2].lower()
    except Exception:
        return "?"


async def main(prompt):
    t0 = time.monotonic()
    def lap(tag, since):
        print(f"  [{time.monotonic()-t0:6.1f}s] {tag:<28} (+{time.monotonic()-since:.1f}s)")

    t = time.monotonic()
    sess = await G._ensure_session(False)
    page = sess["page"]
    lap("browser session ready", t)

    t = time.monotonic(); await G._run_search(page, prompt);      lap("search submitted", t)
    t = time.monotonic(); await G._dismiss_consent(page);         lap("consent dismissed", t)

    if "/sorry/" in page.url:
        print("\n  ✗ CAPTCHA wall — solve it in the window, then re-run."); return

    t = time.monotonic()
    data = await G._await_ai_overview(page)
    lap("AI Overview settled", t)

    if not data:
        print("\n  ✗ No AI Overview rendered for this query.")
    else:
        print(f"\n  card text: {len(data.get('text') or '')} chars")
        print(f"  card links (what the scraper sees): {len(data.get('links') or [])}")

    probe = await page.evaluate(PROBE_JS)
    print(f"\n  label found: {probe['label_found']}   card tag: {probe['card_tag']}"
          f"   card text: {probe['card_text_len']} chars")
    print("\n  ANCESTOR WALK (where the card boundary lands):")
    print(f"    {'i':>2}  {'tag':<10} {'text_len':>8} {'anchors':>8}  class")
    for w in probe["walk"]:
        print(f"    {w['i']:>2}  {w['tag']:<10} {w['text_len']:>8} {w['anchors']:>8}  {w['cls']}")

    cl, pl = probe["card_links"], probe["page_links"]
    print(f"\n  anchors INSIDE card : {len(cl)}")
    print(f"  anchors ON PAGE     : {len(pl)}")
    print("\n  RAW card hrefs (pre-unwrap) — what _unwrap has to cope with:")
    for u in cl[:18]:
        print(f"      {u[:190]}")

    # what survives the scraper's own filters
    kept, dropped = [], []
    for h in cl:
        u = G._unwrap(h)
        (kept if G._is_source(u) else dropped).append(u)
    print(f"\n  after _unwrap/_is_source: {len(kept)} kept, {len(dropped)} dropped")
    if kept:
        print("    kept:");    [print("      ", u[:100]) for u in kept[:10]]
    if dropped:
        print("    dropped (host in _SKIP_HOSTS):")
        seen = {}
        for u in dropped:
            seen[host(u)] = seen.get(host(u), 0) + 1
        for h_, n in sorted(seen.items(), key=lambda x: -x[1])[:10]:
            print(f"      {n:>3}  {h_}")

    # links on the page that the CARD missed but look like real sources
    missed = [u for u in pl if u not in cl and G._is_source(G._unwrap(u))]
    print(f"\n  external links on page but OUTSIDE the card: {len(missed)}")
    seen = {}
    for u in missed:
        seen[host(G._unwrap(u))] = seen.get(host(G._unwrap(u)), 0) + 1
    for h_, n in sorted(seen.items(), key=lambda x: -x[1])[:15]:
        print(f"      {n:>3}  {h_}")

    print(f"\n  TOTAL {time.monotonic()-t0:.1f}s")
    await G.close_session()


if __name__ == "__main__":
    q = sys.argv[1] if len(sys.argv) > 1 else "best running shoes 2026"
    print(f"query: {q!r}\n")
    asyncio.run(main(q))
