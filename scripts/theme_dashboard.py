"""Re-skin the standalone dashboard: drop CashKaro branding, apply an "AI feel"
(violet + cyan, dark) color theme. Idempotent — safe to re-run.

The standalone HTML (``dashboard/ai-citation-tracker.html``) is a self-contained
bundle made of three parts:

  * a ``<script type="__bundler/manifest">`` JSON map of gzip+base64 assets
    (the component JSX modules, fonts, the logo PNG, the CK_DATA payload);
  * a ``<script type="__bundler/template">`` JSON *string* holding the app's
    HTML + CSS — so the theme's CSS custom properties (``--ck-blue``,
    ``--side-bg`` …) live here as literal hex values;
  * an outer loader that decodes the assets, substitutes UUIDs for blob URLs,
    and swaps in the template. The loader also paints a splash from the same
    hex literals.

Two transforms:
  1. THEME — literal hex swaps across the whole file. Because every component
     reads the same ``--ck-*`` / ``--side-*`` variables (and the splash reuses
     the same hexes), swapping the source values recolors everything at once,
     with no specificity battles. ``#`` never occurs in base64, so asset bytes
     are untouched.
  2. BRANDING — the sidebar logo + footer identity live in one JSX module; we
     decode it, replace the markup with a "Citerion" wordmark + sparkle, and
     recompress.
  3. CROSS-LINK — a "Live dashboard" link is added to the sidebar footer,
     pointing at the Streamlit app (companion to ``scripts/dashboard.py``).

All edits survive ``build_dashboard.py`` data rebuilds (that script only swaps
the CK_DATA asset). Re-run this only after re-seeding the base bundle from a
fresh download.

    python scripts/theme_dashboard.py            # apply in place
    python scripts/theme_dashboard.py --check     # report state, change nothing
"""
from __future__ import annotations

import argparse
import base64
import gzip
import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DASHBOARD = ROOT / "dashboard" / "ai-citation-tracker.html"

# ── AI-feel palette (violet + cyan, dark). old CashKaro hex -> new ──────────
HEX_MAP = {
    "#0036DA": "#6D5EFB",  # --ck-blue / --info / --link  -> electric violet-indigo
    "#2C40AA": "#4F3FD6",  # --ck-blue-deep
    "#FF6D1D": "#8B5CF6",  # --ck-orange (CTAs, avatar)   -> violet
    "#FF7E3E": "#A78BFA",  # --ck-orange-soft
    "#0B2275": "#4B2FB5",  # scoreband hero gradient (mid stop)
    "#15349E": "#7C5CFF",  # scoreband hero gradient (end stop)
    "#071542": "#0B0A1F",  # --side-bg + splash bg + scoreband start -> near-black indigo
    "#0B1E5C": "#18162E",  # --side-bg-2 (sidebar hover)
    "#C9D2F2": "#C7C5EC",  # --side-fg
    "#8390C4": "#8482B0",  # --side-fg-dim
}
TITLE_OLD = "CashKaro · AI Citation Tracker"
TITLE_NEW = "Citerion · LLM Tracker"

# ── Cross-link to the live Streamlit dashboard (companion to scripts/dashboard.py) ──
LIVE_URL = "http://localhost:8501"          # Streamlit default; edit if you change the port
LIVE_LINK_MARKER = ">Live dashboard<"        # present iff the link was already inserted
SIDEBAR_ASSET_MARKER = 'className="side-foot"'  # identifies the sidebar JSX module

# ── Branding JSX rewrites (module that contains the sidebar) ────────────────
BRAND_MARKER = "logo-cashkaro.png"  # present iff the module still has CK branding

JSX_EDITS = [
    # 1. Sidebar logo image -> "Citerion" wordmark with a violet→cyan sparkle mark
    (
        '      <div className="side-brand">\n'
        '        <img src={(window.__resources && window.__resources.logo) || "assets/logo-cashkaro.png"} alt="CashKaro" />\n'
        '      </div>',
        '      <div className="side-brand" style={{ gap: 10, paddingTop: 2, paddingBottom: 0 }}>\n'
        '        <span style={{ width: 32, height: 32, borderRadius: 9, display: "grid", placeItems: "center", flex: "0 0 auto", color: "#fff", background: "linear-gradient(135deg,#7C5CFF 0%,#22D3EE 100%)", boxShadow: "0 4px 14px rgba(124,92,255,.45)" }}><Icon n="auto_awesome" style={{ fontSize: 19 }} /></span>\n'
        '        <span style={{ fontFamily: "var(--font-display)", fontWeight: 800, fontSize: 21, letterSpacing: "-.02em", color: "#fff" }}>Citerion</span>\n'
        '      </div>',
    ),
    # 2. Subtitle block sat -26px under the tall logo; tighten now the mark is short
    (
        '<div className="side-brand" style={{ marginTop: -26, paddingTop: 0, paddingBottom: 14 }}>',
        '<div className="side-brand" style={{ marginTop: -4, paddingTop: 0, paddingBottom: 14, paddingLeft: 8 }}>',
    ),
    # 3. Footer identity: "CK / Growth team / marketing@cashkaro.com" -> neutral
    (
        '          <div className="side-avatar">CK</div>\n'
        '          <div>\n'
        '            <div className="nm side-label">Growth team</div>\n'
        '            <div className="em side-label">marketing@cashkaro.com</div>\n'
        '          </div>',
        '          <div className="side-avatar"><Icon n="auto_awesome" style={{ fontSize: 16 }} /></div>\n'
        '          <div>\n'
        '            <div className="nm side-label">Citerion</div>\n'
        '            <div className="em side-label">LLM Tracker</div>\n'
        '          </div>',
    ),
]


def _manifest_span(html: str) -> tuple[int, int]:
    open_tag = '<script type="__bundler/manifest">'
    i = html.index(open_tag) + len(open_tag)
    j = html.index("</script>", i)
    return i, j


def _decode(entry: dict) -> bytes:
    raw = base64.b64decode(entry["data"])
    if entry.get("compressed"):
        raw = gzip.decompress(raw)
    return raw


def _encode(entry: dict, data: bytes) -> None:
    comp = gzip.compress(data, mtime=0)  # mtime=0 -> reproducible
    entry["data"] = base64.b64encode(comp).decode("ascii")
    entry["compressed"] = True


def rebrand(html: str, check: bool) -> tuple[str, bool]:
    """Rewrite the sidebar branding inside its JSX module. Returns (html, changed)."""
    i, j = _manifest_span(html)
    manifest = json.loads(html[i:j])

    target, text = None, None
    for uuid, entry in manifest.items():
        if "javascript" not in entry.get("mime", ""):
            continue
        try:
            t = _decode(entry).decode("utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        if BRAND_MARKER in t:
            target, text = uuid, t
            break

    if target is None:
        return html, False  # already de-branded (or marker moved)
    if check:
        return html, True

    new_text = text
    for old, new in JSX_EDITS:
        if old not in new_text:
            raise SystemExit(
                "Branding rewrite failed: expected JSX block not found — the "
                "base bundle markup changed. Block starts:\n  "
                + old.splitlines()[0]
            )
        new_text = new_text.replace(old, new, 1)

    _encode(manifest[target], new_text.encode("utf-8"))
    new_manifest = json.dumps(manifest, ensure_ascii=False)
    if "</script>" in new_manifest:
        raise SystemExit("Refusing to write: regenerated manifest contains </script>.")
    return html[:i] + new_manifest + html[j:], True


def add_live_link(html: str, check: bool = False) -> tuple[str, bool]:
    """Insert a "Live dashboard" link into the sidebar footer pointing at the
    Streamlit app. Idempotent — skips if already present."""
    i, j = _manifest_span(html)
    manifest = json.loads(html[i:j])

    target, text = None, None
    for uuid, entry in manifest.items():
        if "javascript" not in entry.get("mime", ""):
            continue
        try:
            t = _decode(entry).decode("utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        if SIDEBAR_ASSET_MARKER in t:
            target, text = uuid, t
            break

    if target is None or LIVE_LINK_MARKER in (text or ""):
        return html, False  # sidebar module missing, or link already there
    if check:
        return html, True

    anchor = '      <div className="side-foot">\n        <div className="row">'
    if anchor not in text:
        raise SystemExit("Live-link insert failed: sidebar footer markup changed.")
    link = (
        '      <div className="side-foot">\n'
        '        <a className="side-item" href="' + LIVE_URL + '" target="_blank" rel="noopener" '
        'style={{ marginBottom: 8, textDecoration: "none" }}>\n'
        '          <Icon n="open_in_new" />\n'
        '          <span className="side-label">Live dashboard</span>\n'
        '        </a>\n'
        '        <div className="row">'
    )
    new_text = text.replace(anchor, link, 1)
    _encode(manifest[target], new_text.encode("utf-8"))
    new_manifest = json.dumps(manifest, ensure_ascii=False)
    if "</script>" in new_manifest:
        raise SystemExit("Refusing to write: regenerated manifest contains </script>.")
    return html[:i] + new_manifest + html[j:], True


def recolor(html: str) -> tuple[str, int]:
    """Swap brand hexes + the window title. Returns (html, swaps_applied)."""
    swaps = 0
    for old, new in HEX_MAP.items():
        html, n = re.subn(re.escape(old), new, html, flags=re.IGNORECASE)
        swaps += n
    if TITLE_OLD in html:
        html = html.replace(TITLE_OLD, TITLE_NEW)
        swaps += 1
    return html, swaps


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--file", type=Path, default=DASHBOARD, help="Dashboard HTML to theme (default: the bundled file)")
    ap.add_argument("--check", action="store_true", help="Report whether branding/theme are present; change nothing")
    args = ap.parse_args()

    if not args.file.exists():
        raise SystemExit(f"Not found: {args.file}")
    html = args.file.read_text(encoding="utf-8")

    has_brand = BRAND_MARKER in html or any(h in html for h in HEX_MAP) or TITLE_OLD in html
    if args.check:
        print(f"{args.file.relative_to(ROOT)}")
        print(f"  CashKaro branding present : {'yes' if BRAND_MARKER in html else 'no'}")
        present = [h for h in HEX_MAP if re.search(re.escape(h), html, re.IGNORECASE)]
        print(f"  original brand hexes left  : {len(present)}/{len(HEX_MAP)}  {present}")
        print(f"  old window title           : {'yes' if TITLE_OLD in html else 'no'}")
        _, link_needed = add_live_link(html, check=True)
        print(f"  live-dashboard link        : {'missing' if link_needed else 'present'}")
        return

    html, rebranded = rebrand(html, check=False)
    html, linked = add_live_link(html)
    html, swaps = recolor(html)

    if not rebranded and not linked and swaps == 0:
        print("Already customized — nothing to do.")
        return

    args.file.write_text(html, encoding="utf-8")
    print(f"Customized {args.file.relative_to(ROOT)}")
    print(f"  branding rewritten : {'yes (Citerion wordmark + sparkle)' if rebranded else 'already done'}")
    print(f"  color swaps applied: {swaps} (violet + cyan, dark)")
    print(f"  live link added    : {'yes (-> ' + LIVE_URL + ')' if linked else 'already present'}")


if __name__ == "__main__":
    main()
