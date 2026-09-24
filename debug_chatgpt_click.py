#!/usr/bin/env python3
"""
debug_chatgpt_click.py — ChatGPT's inline citations render as chips ("C CashKaro +1")
that reveal their URL only on hover/click. This finds those chips inside the assistant
turn, hovers and clicks each, and reports any external URL that appears as a result.

    python3 debug_chatgpt_click.py "best cashback site in india"
"""
import asyncio
import sys

from scrapers import chatgpt as C

# chips live inside the assistant turn and are short, clickable, non-anchor elements
CHIP_JS = r"""
() => {
  const turn = document.querySelector('[data-message-role="assistant"]') || document.body;
  const els = Array.from(turn.querySelectorAll(
    'button, [role="button"], a, span[tabindex], [data-citation], [class*="citation"]'
  ));
  const out = [];
  els.forEach((el, i) => {
    const t = (el.innerText || '').trim();
    if (!t || t.length > 40) return;
    el.setAttribute('data-probe-idx', String(i));
    out.push({idx: i, tag: el.tagName, text: t});
  });
  return out;
}
"""

SNAPSHOT_JS = r"""
() => {
  const SKIP = /(^|\.)(chatgpt|openai|oaistatic|oaiusercontent)\.com$/i;
  const urls = new Set();
  const grab = (root) => {
    let els; try { els = root.querySelectorAll('*'); } catch (e) { return; }
    for (const el of els) {
      if (el.tagName === 'A' && el.href && /^https?:/i.test(el.href)) {
        try { if (!SKIP.test(new URL(el.href).hostname)) urls.add(el.href); } catch (e) {}
      }
      for (const at of el.attributes || []) {
        const m = (at.value || '').match(/https?:\/\/[^\s"'<>)]+/g);
        if (m) for (const u of m) {
          try { if (!SKIP.test(new URL(u).hostname)) urls.add(u); } catch (e) {}
        }
      }
      if (el.shadowRoot) grab(el.shadowRoot);
    }
  };
  grab(document);
  return Array.from(urls);
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
    print("submitted; waiting 45s…\n", flush=True)
    await page.wait_for_timeout(45_000)

    before = set(await page.evaluate(SNAPSHOT_JS))
    print(f"external URLs before any interaction: {len(before)}")
    for u in list(before)[:10]:
        print("     ", u[:110])

    chips = await page.evaluate(CHIP_JS)
    print(f"\nchips found in the assistant turn: {len(chips)}")
    for c in chips[:20]:
        print(f"     [{c['idx']}] <{c['tag']}> {c['text']!r}")

    found = {}
    for c in chips[:15]:
        sel = f'[data-probe-idx="{c["idx"]}"]'
        try:
            el = page.locator(sel).first
            if not await el.count():
                continue
            await el.hover(timeout=2500)
            await page.wait_for_timeout(700)
            after_hover = set(await page.evaluate(SNAPSHOT_JS)) - before
            await el.click(timeout=2500)
            await page.wait_for_timeout(1200)
            after_click = set(await page.evaluate(SNAPSHOT_JS)) - before
            new = after_hover | after_click
            if new:
                found[c["text"]] = sorted(new)
                print(f"\n  chip {c['text']!r} revealed {len(new)} url(s):")
                for u in sorted(new)[:8]:
                    print("       ", u[:110])
            await page.keyboard.press("Escape")
            await page.wait_for_timeout(300)
        except Exception as e:
            print(f"  chip {c['text']!r}: {type(e).__name__}")

    print(f"\n=== chips that revealed URLs: {len(found)} ===")
    if not found:
        print("  none — hovering/clicking exposes no URL either.")
    await C.close_session()


if __name__ == "__main__":
    asyncio.run(main(sys.argv[1] if len(sys.argv) > 1 else "best cashback site in india"))
