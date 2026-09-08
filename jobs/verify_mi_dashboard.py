"""Live AppTest of Market Intelligence pages against the host read-only role.

    python -m jobs.verify_mi_dashboard --json

Never prints observation values, restricted credit numbers, or credentials.
Exit 0 ok, 2 verification failed, 3 configuration.
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

from market_intelligence.nulls import strict_dumps

ROOT = Path(__file__).resolve().parents[1]
PAGES = [
    ROOT / "pages" / "10_Market_Pulse.py",
    ROOT / "pages" / "11_Macro_Overview.py",
    ROOT / "pages" / "12_Rates_Curve.py",
    ROOT / "pages" / "13_Credit_Overview.py",
    ROOT / "pages" / "14_Sector_Rotation_V2.py",
    ROOT / "pages" / "15_Data_Health.py",
    ROOT / "pages" / "16_Morning_Context.py",
    ROOT / "pages" / "17_PIT_Sector_Internals.py",
]


def _texts(at) -> str:
    parts = []
    for kind in ("title", "caption", "info", "warning", "error", "markdown", "subheader"):
        for el in getattr(at, kind):
            parts.append(str(el.value))
    return "\n".join(parts)


def run(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)
    if not (os.environ.get("DATABASE_READONLY_URL") or "").strip():
        print("DATABASE_READONLY_URL is not configured", file=sys.stderr)
        return 3
    from streamlit.testing.v1 import AppTest

    report: dict[str, object] = {"pages": []}
    failed = []
    for path in PAGES:
        from market_intelligence.ui import clear_read_cache

        clear_read_cache()
        at = AppTest.from_file(str(path), default_timeout=90)
        at.run()
        text = _texts(at)
        exceptions = [str(e.value.__class__.__name__) for e in at.exception]
        configured = "CONFIGURATION_REQUIRED" not in text
        has_title = bool(at.title)
        page_ok = not exceptions and has_title and configured
        if not page_ok:
            failed.append(path.stem)
        report["pages"].append(
            {
                "page": path.stem,
                "ok": page_ok,
                "configured": configured,
                "has_title": has_title,
                "has_table": len(at.dataframe) > 0,
                "exceptions": exceptions,
                "unconfigured": "CONFIGURATION_REQUIRED" in text,
                "empty_like": any(w in text for w in ("No ", "not been published", "No sources")),
            }
        )
    report["failed"] = failed
    report["status"] = "PASSED" if not failed else "FAILED"
    if args.json:
        print(strict_dumps(report))
    return 0 if not failed else 2


def main() -> int:  # pragma: no cover
    return run()


if __name__ == "__main__":
    sys.exit(main())
