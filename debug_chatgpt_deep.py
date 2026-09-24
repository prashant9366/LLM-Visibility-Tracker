#!/usr/bin/env python3
"""
debug_chatgpt_deep.py — the earlier hunt only searched the LIGHT dom. ChatGPT's
octane renderer uses custom elements, so citation links may live in a shadow root
or an iframe. This walks shadow roots recursively, enumerates frames, and reports
every external anchor / URL-bearing attribute it finds anywhere.

    python3 debug_chatgpt_deep.py "best cashback site in india"
"""
import asyncio
import sys

from scrapers import chatgpt as C

# Recursive walk: light dom + every open shadowRoot.
DEEP_JS = r"""
() => {
  const SKIP = /(^|\.)(chatgpt|openai|oaistatic|oaiusercontent)\.com/i;
  const out = {anchors: [], attrs: [], shadow_hosts: 0, roots: 1,
               clickables: [], iframes: []};

  const visit = (root) => {
    let els;
    try { els = root.querySelectorAll('*'); } catch (e) { return; }
    for (const el of els) {
      // anchors
      if (el.tagName === 'A' && el.href && /^https?:/i.test(el.href)) {
        if (!SKIP.test(new URL(el.href).hostname)) out.anchors.push(el.href);
      }
      // any attribute holding a URL
      for (const at of el.attributes || []) {
        const v = at.value || '';
        const m = v.match(/https?:\/\/[^\s"'<>)]+/g);
        if (m) for (const u of m) {
          try { if (!SKIP.test(new URL(u).hostname))
                  out.attrs.push(el.tagName + '@' + at.name + ' = ' + u); }
          catch (e) {}
        }
      }
      // citation-looking clickables inside the assistant turn
      if (el.closest && el.closest('[data-message-role="assistant"]')) {
        const t = (el.innerText || '').trim();
        if (t && t.length <= 32 &&
            (el.tagName === 'BUTTON' || el.tagName === 'A' ||
             el.getAttribute('role') === 'button' || el.hasAttribute('data-citation'))) {
          const at = {};
          for (const a of el.attributes || []) at[a.name] = (a.value || '').slice(0, 70);
          out.clickables.push({tag: el.tagName, text: t, attrs: at});
        }
      }
      if (el.tagName === 'IFRAME') out.iframes.push(el.src || '(no src)');
      if (el.shadowRoot) { out.shadow_hosts++; out.roots++; visit(el.shadowRoot); }
    }
  };
  visit(document);
  return out;
}
"""


async def main(prompt: str):
    await C.close_session()
    sess = await C._ensure_session()
    page = sess["page"]

    await page.goto("https://chatgpt.com/", wait_until="domcontentloaded", timeout=60_000)
    await C._solve_turnstile(page)
    await C._dismiss_modals(page)
    await page.wait_for_timeout(1500)
    loc = page.locator("textarea").first
    await loc.click()
    await loc.fill(prompt)
    await page.keyboard.press("Enter")
    print("submitted; waiting 45s for the answer + citation chips…\n", flush=True)
    await page.wait_for_timeout(45_000)

    d = await page.evaluate(DEEP_JS)
    print(f"shadow roots walked : {d['roots']} (hosts: {d['shadow_hosts']})")
    print(f"iframes             : {len(d['iframes'])}")
    for s in d["iframes"][:8]:
        print("     ", str(s)[:100])

    print(f"\nEXTERNAL anchors (deep): {len(d['anchors'])}")
    for u in d["anchors"][:20]:
        print("     ", u[:110])

    print(f"\nEXTERNAL urls in attributes (deep): {len(d['attrs'])}")
    for s in d["attrs"][:20]:
        print("     ", s[:130])

    print(f"\ncitation-ish clickables in the assistant turn: {len(d['clickables'])}")
    for c in d["clickables"][:12]:
        print(f"     <{c['tag']}> {c['text']!r}")
        for k, v in c["attrs"].items():
            print(f"         @{k}={v!r}")

    # Playwright sees frames even if the JS walk missed them
    print(f"\nplaywright frames: {len(page.frames)}")
    for f in page.frames:
        print("     ", (f.url or "")[:100])

    await C.close_session()


if __name__ == "__main__":
    asyncio.run(main(sys.argv[1] if len(sys.argv) > 1 else "best cashback site in india"))
