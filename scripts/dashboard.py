"""Launch either dashboard — pick the standalone HTML or the live Streamlit app.

The project ships two views of the same scrape data:

  * STANDALONE  — ``dashboard/ai-citation-tracker.html``: a self-contained,
    shareable single file with data baked in at build time
    (rebuild with ``python scripts/build_dashboard.py``). Opens in your browser,
    no server needed.
  * STREAMLIT   — ``app.py``: the live, interactive dashboard that reads
    ``data/results.db`` directly (run scans, switch runs, export).

Usage:
    python scripts/dashboard.py            # interactive menu
    python scripts/dashboard.py html       # open the standalone HTML
    python scripts/dashboard.py app        # launch the Streamlit app
"""
from __future__ import annotations

import subprocess
import sys
import webbrowser
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
HTML = ROOT / "dashboard" / "ai-citation-tracker.html"
APP = ROOT / "app.py"


def open_html() -> None:
    if not HTML.exists():
        raise SystemExit(f"Standalone dashboard not found: {HTML}\n"
                         f"Build it with: python scripts/build_dashboard.py")
    print(f"Opening standalone dashboard → {HTML.relative_to(ROOT)}")
    webbrowser.open(HTML.as_uri())


def run_streamlit() -> None:
    if not APP.exists():
        raise SystemExit(f"Streamlit app not found: {APP}")
    print("Launching Streamlit (Ctrl+C to stop)…")
    # Use the same interpreter's streamlit so the active venv is honored.
    subprocess.run([sys.executable, "-m", "streamlit", "run", str(APP)], cwd=ROOT)


def menu() -> None:
    print("Which dashboard?\n"
          "  [1] Standalone HTML  (shareable, baked data)\n"
          "  [2] Streamlit app    (live, interactive)\n"
          "  [q] quit")
    choice = input("> ").strip().lower()
    if choice in ("1", "html", "h"):
        open_html()
    elif choice in ("2", "app", "streamlit", "s"):
        run_streamlit()
    elif choice in ("q", "quit", "exit", ""):
        return
    else:
        print(f"Unrecognized: {choice!r}")


def main() -> None:
    arg = sys.argv[1].lower() if len(sys.argv) > 1 else None
    if arg in ("html", "h", "standalone"):
        open_html()
    elif arg in ("app", "streamlit", "s", "live"):
        run_streamlit()
    elif arg in (None, "menu"):
        menu()
    else:
        raise SystemExit(__doc__)


if __name__ == "__main__":
    main()
