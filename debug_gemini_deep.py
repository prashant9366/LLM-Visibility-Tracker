#!/usr/bin/env python3
"""
debug_gemini_deep.py — same hunt that found ChatGPT's citations in a
`data-assistant-sources-payload` attribute, applied to Gemini.

Walks light DOM + every open shadow root, collects EXTERNAL urls only (Google's own
chrome filtered out BEFORE printing — printing the first N unfiltered is what made
me wrongly call ChatGPT's citations missing), and groups them by the attribute they
came from, so the right extraction hook is obvious.

    python3 debug_gemini_deep.py "best cashback site in india"
"""
import asyncio
import sys

from scrapers import gemini as G
from scrapers._browser import new_page, wait_quiet, make_response_capturer

DEEP_JS = r"""
() => {
  const SKIP = /(^|\.)(google|googleusercontent|gstatic|googletagmanager|doubleclick|google-analytics|youtube|ytimg|schema|w3|ogp)\.(com|org|me|be)$/i;
  const isExt = (u) => {
    try { return /^https?:/i.test(u) && !SKIP.test(new URL(u).hostname); }
    catch (e) { return false; }
  };
  const out = {anchors: [], by_attr: {}, shadow_hosts: 0, text_urls: [], iframes: []};

  const visit = (root) => {
    let els; try { els = root.querySelectorAll('*'); } catch (e) { return; }
    for (const el of els) {
      if (el.tagName === 'A' && el.href && isExt(el.href)) out.anchors.push(el.href);
      for (const at of el.attributes || []) {
        const m = (at.value || '').match(/https?:\/\/[^\s"'<>)\\]+/g);
        if (!m) continue;
        for (const u of m) {
          if (!isExt(u)) continue;
          const key = el.tagName + '@' + at.name;
          (out.by_attr[key] = out.by_attr[key] || []).push(u);
        }
      }
      if (el.tagName === 'IFRAME') out.iframes.push(el.src || '(no src)');
      if (el.shadowRoot) { out.shadow_hosts++; visit(el.shadowRoot); }
    }
  };
  visit(document);

  const root = document.querySelector('model-response, message-content') || document.body;
  const m = (root.innerText || '').match(/https?:\/\/[^\s"'<>)\\]+/g);
  if (m) out.text_urls = m.filter(isExt);
  return out;
}
"""


async def main(prompt: str):
    async with new_page("gemini", headless=True) as page:
        handler, queries, urls = make_response_capturer(
            G._is_gemini_backend, skip_hosts=G._SKIP_HOSTS,
            debug_platform="gemini", debug_host_match="gemini.google.com",
        )
        page.on("response", handler)

        await page.goto("https://gemini.google.com/app?hl=en-IN",
                        wait_until="domcontentloaded", timeout=60_000)
        await wait_quiet(page, idle_ms=250, net_idle=False)

        composer = None
        for sel in ("rich-textarea div[contenteditable='true']",
                    "div.ql-editor[contenteditable='true']",
                    "[role='textbox'][contenteditable='true']", "textarea"):
            try:
                await page.wait_for_selector(sel, timeout=15_000, state="visible")
                loc = page.locator(sel).first
                if await loc.count():
                    composer = loc
                    break
            except Exception:
                continue
        if composer is None:
            print("no composer rendered — cannot test"); return

        await composer.click()
        await composer.fill(prompt)
        await page.keyboard.press("Enter")
        print("submitted; waiting for the answer + sources…\n", flush=True)
        await G._wait_for_stable_gemini_text(page, idle_ms=1200, max_ms=75_000,
                                             min_chars=60)

        # open the Sources panel — that's where grounding chips render
        try:
            btn = page.locator("sources-list sources-sidebar-button button").first
            if await btn.count():
                await btn.click(timeout=3000)
                print("clicked the Sources button")
        except Exception:
            print("no Sources button found")
        await page.wait_for_timeout(3000)

        d = await page.evaluate(DEEP_JS)

        print(f"\nshadow hosts walked : {d['shadow_hosts']}")
        print(f"iframes             : {len(d['iframes'])}")
        print(f"\nEXTERNAL anchors    : {len(d['anchors'])}")
        for u in d["anchors"][:15]:
            print("     ", u[:110])
        print(f"\nEXTERNAL urls in the answer text: {len(d['text_urls'])}")
        for u in d["text_urls"][:10]:
            print("     ", u[:110])

        print(f"\nEXTERNAL urls BY ATTRIBUTE ({len(d['by_attr'])} distinct attrs):")
        if not d["by_attr"]:
            print("   none")
        for k, v in sorted(d["by_attr"].items(), key=lambda x: -len(x[1])):
            uniq = list(dict.fromkeys(v))
            print(f"   {k}  ({len(uniq)} unique)")
            for u in uniq[:8]:
                print(f"       {u[:110]}")

        print(f"\n--- network capture ---")
        print(f"source_urls    : {len(urls)}")
        for u in list(urls)[:15]:
            print("     ", str(u)[:110])
        print(f"fanout_queries : {len(queries)}")
        for q in list(queries)[:8]:
            print("     ", str(q)[:90])

        page.remove_listener("response", handler)


if __name__ == "__main__":
    asyncio.run(main(sys.argv[1] if len(sys.argv) > 1 else "best cashback site in india"))
