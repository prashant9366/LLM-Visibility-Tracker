#!/usr/bin/env python3
"""
debug_chatgpt_links.py — ChatGPT shows source chips but the scraper returns no
citations. This submits a prompt, then hunts for where the URLs actually live:
plain anchors, any attribute containing a URL, the old citation-pill testid, and
the backend SSE/JSON payloads (make_response_capturer).

    python3 debug_chatgpt_links.py "best cashback site in india"
"""
import asyncio
import sys

from scrapers import chatgpt as C
from scrapers._browser import make_response_capturer

HUNT = r"""
() => {
  const URL_RE = /https?:\/\/[^\s"'<>)]+/g;
  const out = {anchors: [], attr_urls: [], pills: 0, chips: [], text_urls: []};

  document.querySelectorAll('a[href]').forEach(a => {
    if (/^https?:/.test(a.href)) out.anchors.push(a.href);
  });

  // any attribute anywhere holding a URL (that's where Google hid them)
  document.querySelectorAll('*').forEach(el => {
    for (const at of el.attributes) {
      const m = (at.value || '').match(URL_RE);
      if (m) m.forEach(u => out.attr_urls.push(el.tagName + '@' + at.name + ' = ' + u));
    }
  });

  out.pills = document.querySelectorAll('[data-testid="webpage-citation-pill"]').length;

  // citation chips render as small elements next to the answer; capture their
  // tag/attrs so we can find a stable hook
  const cand = Array.from(document.querySelectorAll(
    '[data-testid*="citation"], [class*="citation"], [data-citation], ' +
    'button, span[role="button"], [aria-label]'
  ));
  for (const el of cand.slice(0, 400)) {
    const t = (el.innerText || '').trim();
    if (!t || t.length > 40) continue;
    const at = {};
    for (const a of el.attributes) at[a.name] = (a.value || '').slice(0, 90);
    if (Object.keys(at).length) out.chips.push({tag: el.tagName, text: t, attrs: at});
  }

  // URLs sitting in the rendered text itself
  const root = document.querySelector('[data-message-role="assistant"]') || document.body;
  const m = (root.innerText || '').match(URL_RE);
  if (m) out.text_urls = m;
  return out;
}
"""


async def main(prompt):
    sess = await C._ensure_session()
    page = sess["page"]

    handler, queries, urls = make_response_capturer(
        C._is_chatgpt_backend, skip_hosts=C._SKIP_HOSTS,
        debug_platform="chatgpt", debug_host_match="chatgpt.com",
    )
    page.on("response", handler)

    await page.goto("https://chatgpt.com/", wait_until="domcontentloaded", timeout=60_000)
    await C._solve_turnstile(page)
    await C._dismiss_modals(page)
    await page.wait_for_timeout(1500)
    loc = page.locator("textarea").first
    await loc.click()
    await loc.fill(prompt)
    await page.keyboard.press("Enter")
    print("submitted; waiting 40s for the answer + citations…\n", flush=True)
    await page.wait_for_timeout(40_000)

    d = await page.evaluate(HUNT)

    print(f"plain anchors            : {len(d['anchors'])}")
    for u in d["anchors"][:10]:
        print("    ", u[:110])
    print(f"\nold citation-pill testid : {d['pills']}")
    print(f"\nURLs inside attributes   : {len(d['attr_urls'])}")
    for s in d["attr_urls"][:15]:
        print("    ", s[:140])
    print(f"\nURLs in the answer text  : {len(d['text_urls'])}")
    for u in d["text_urls"][:10]:
        print("    ", u[:110])

    print(f"\nchip-like elements       : {len(d['chips'])}")
    for c in d["chips"][:12]:
        print(f"    <{c['tag']}> {c['text']!r}")
        for k, v in c["attrs"].items():
            print(f"        @{k}={v!r}")

    print(f"\n--- network capture ---")
    print(f"captured source_urls     : {len(urls)}")
    for u in list(urls)[:20]:
        print("    ", str(u)[:110])
    print(f"captured fanout_queries  : {len(queries)}")
    for q in list(queries)[:10]:
        print("    ", str(q)[:100])

    page.remove_listener("response", handler)
    await C.close_session()


if __name__ == "__main__":
    asyncio.run(main(sys.argv[1] if len(sys.argv) > 1 else "best cashback site in india"))
