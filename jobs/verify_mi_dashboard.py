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
    ROOT / "dashboard.py",
    ROOT / "pages" / "10_Market_Pulse.py",
    ROOT / "pages" / "11_Macro_Overview.py",
    ROOT / "pages" / "12_Rates_Curve.py",
    ROOT / "pages" / "13_Credit_Overview.py",
    ROOT / "pages" / "14_Sector_Rotation_V2.py",
    ROOT / "pages" / "15_Data_Health.py",
    ROOT / "pages" / "16_Morning_Context.py",
    ROOT / "pages" / "17_PIT_Sector_Internals.py",
    ROOT / "pages" / "18_Order_Flow.py",
    ROOT / "pages" / "19_Fixed_Income.py",
    ROOT / "pages" / "20_Commodities.py",
    ROOT / "pages" / "21_Options_Volatility.py",
    ROOT / "pages" / "22_US_Markets.py",
    ROOT / "pages" / "23_Global_Markets.py",
    ROOT / "pages" / "24_Forex.py",
    ROOT / "pages" / "25_CFTC_COT.py",
    ROOT / "pages" / "26_Crypto.py",
]


def _texts(at) -> str:
    parts = []
    for kind in ("title", "caption", "info", "warning", "error", "markdown", "subheader"):
        for el in getattr(at, kind):
            parts.append(str(el.value))
    return "\n".join(parts)


def query_error_class(text: str) -> str | None:
    """Class name from ``Query failed (Name)``. A stopped query is a failed page."""
    marker = "Query failed ("
    start = text.find(marker)
    if start < 0:
        return None
    rest = text[start + len(marker):]
    end = rest.find(")")
    name = rest[:end] if end >= 0 else ""
    if name.isidentifier():
        return name
    return "query_failed"


def page_ok(*, text: str, exceptions: list[str], has_title: bool) -> bool:
    configured = "CONFIGURATION_REQUIRED" not in text
    return not exceptions and has_title and configured and query_error_class(text) is None


def run(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)
    if not (os.environ.get("DATABASE_READONLY_URL") or "").strip():
        print("DATABASE_READONLY_URL is not configured", file=sys.stderr)
        return 3
    from db.dashboard_engine import load_streamlit_env

    load_streamlit_env()
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
        query_error = query_error_class(text)
        ok = page_ok(text=text, exceptions=exceptions, has_title=has_title)
        if not ok:
            failed.append(path.stem)
        report["pages"].append(
            {
                "page": path.stem,
                "ok": ok,
                "configured": configured,
                "has_title": has_title,
                "has_table": len(at.dataframe) > 0,
                "exceptions": exceptions,
                "query_error": query_error,
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
