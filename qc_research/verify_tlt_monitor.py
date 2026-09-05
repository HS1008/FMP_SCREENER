#!/usr/bin/env python3
"""Query-back TLTDurationMomentum V0 from live PostgreSQL / Monitor read model.

Does not launch QuantConnect. Does not invent DATABASE_URL.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Verify TLT V0 Postgres → Monitor identity")
    parser.add_argument("--dry-run", action="store_true", help="Verify wrapped artifact only")
    parser.add_argument("--live", action="store_true", help="Require live PostgreSQL query-back")
    parser.add_argument("--apptest", action="store_true", help="Render Strategy Monitor via Streamlit AppTest")
    parser.add_argument(
        "--root",
        default=str(ROOT / "qc_research" / "platform_artifacts" / "tlt_duration_momentum.json"),
        help="Official TLT V0 JSON",
    )
    ns = parser.parse_args(argv)

    from qc_research.platform_ingest import (
        live_postgres_configured,
        monitor_view_from_artifacts,
        normalize_platform_file,
        postgres_engine,
    )
    from qc_research.tlt_duration_momentum import (
        STRATEGY_ID,
        verify_tlt_monitor_view,
        verify_tlt_postgres,
    )

    artifacts = normalize_platform_file(Path(ns.root))
    view = verify_tlt_monitor_view(monitor_view_from_artifacts(artifacts))
    report = {
        "wrapped_ok": True,
        "strategy_id": view.get("strategy_id"),
        "lineage": view.get("lineage"),
        "economic_gate": view.get("economic_gate"),
        "economic_pass": view.get("economic_pass"),
        "window_count": view.get("window_count"),
        "selected_candidate": view.get("selected_candidate"),
        "baseline": view.get("baseline"),
        "research_state": view.get("research_state"),
        "holdout_accessed": view.get("holdout_accessed"),
    }
    want_live = ns.live or (live_postgres_configured() and not ns.dry_run)
    if ns.live and not live_postgres_configured():
        print("DATABASE_URL / DB_* unset. Live TLT query-back cannot run. Do not invent credentials.")
        return 1
    if want_live:
        engine = postgres_engine()
        with engine.begin() as conn:
            report["live"] = verify_tlt_postgres(conn)
    elif not ns.dry_run:
        report["live_skipped"] = "DATABASE_URL / DB_* unset"
    if ns.apptest:
        from streamlit.testing.v1 import AppTest

        at = AppTest.from_file(str(ROOT / "pages" / "strategy_monitor.py"), default_timeout=45)
        at.run()
        if at.exception:
            raise RuntimeError(at.exception)
        names = list(at.selectbox[0].options) if at.selectbox else []
        if STRATEGY_ID not in names:
            print("Strategy Monitor selectbox missing TLTDurationMomentum: {0}".format(names))
            return 1
        at.selectbox[0].set_value(STRATEGY_ID).run()
        if at.exception:
            raise RuntimeError(at.exception)
        labels = [str(getattr(metric, "label", "") or "") for metric in at.metric]
        joined = " ".join(labels)
        for needle in ("Economic gate", "OOS window count", "Holdout accessed"):
            if needle not in joined:
                print("Strategy Monitor missing metric {0}: {1}".format(needle, labels))
                return 1
        report["apptest_ok"] = True
        report["monitor_metrics"] = labels
    print(json.dumps(report, indent=2, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
