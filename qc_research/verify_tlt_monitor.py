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


def find_selectbox(at, label: str):
    """Return the named Streamlit selectbox. Do not assume widget order."""
    boxes = list(getattr(at, "selectbox", None) or [])
    for box in boxes:
        if str(getattr(box, "label", "") or "") == label:
            return box
    names = [str(getattr(box, "label", "") or "") for box in boxes]
    raise RuntimeError("Strategy Monitor missing {0} selectbox: {1}".format(label, names))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Verify TLT V0 Postgres → Monitor identity")
    parser.add_argument("--dry-run", action="store_true", help="Verify wrapped artifact only")
    parser.add_argument("--live", action="store_true", help="Require live PostgreSQL query-back")
    parser.add_argument("--apptest", action="store_true", help="Render Strategy Monitor via Streamlit AppTest")
    parser.add_argument(
        "--apptest-preview",
        action="store_true",
        help="Render the artifact-only Platform Research preview (no PostgreSQL)",
    )
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
        strategy_box = find_selectbox(at, "Strategy")
        names = list(strategy_box.options)
        chosen = None
        for name in names:
            text = str(name)
            if text == STRATEGY_ID or STRATEGY_ID in text or "TLT Duration Momentum" in text:
                chosen = name
                break
        if chosen is None:
            print("Strategy Monitor selectbox missing TLTDurationMomentum: {0}".format(names))
            return 1
        strategy_box.set_value(chosen).run()
        if at.exception:
            raise RuntimeError(at.exception)
        labels = [str(getattr(metric, "label", "") or "") for metric in at.metric]
        joined = " ".join(labels)
        texts = []
        for block in list(at.markdown) + list(at.subheader) + list(at.info) + list(at.caption):
            texts.append(str(getattr(block, "value", "") or getattr(block, "label", "") or ""))
        page = " ".join(texts)
        if "No structured rules stored for this strategy." in page:
            print("Strategy Monitor still shows empty rules_json empty-state for TLT")
            return 1
        for needle in (
            "Economic gate",
            "OOS window count",
            "Holdout accessed",
            "Research status",
            "Promotion gate",
            "Holdout status",
        ):
            if needle not in joined:
                print("Strategy Monitor missing metric {0}: {1}".format(needle, labels))
                return 1
        report["apptest_ok"] = True
        report["monitor_metrics"] = labels
    if ns.apptest_preview:
        from streamlit.testing.v1 import AppTest

        preview = AppTest.from_file(str(ROOT / "qc_research" / "preview_platform_monitor.py"), default_timeout=45)
        preview.run()
        if preview.exception:
            raise RuntimeError(preview.exception)
        labels = [str(getattr(metric, "label", "") or "") for metric in preview.metric]
        values = [str(getattr(metric, "value", "") or "") for metric in preview.metric]
        joined = " ".join(labels)
        joined_values = " ".join(values)
        for needle in (
            "Research status",
            "Economic gate",
            "Promotion gate",
            "Holdout status",
            "WFO window count",
            "Selected model",
            "Baseline",
        ):
            if needle not in joined:
                print("Platform preview missing metric {0}: {1}".format(needle, labels))
                return 1
        for needle in ("COMPLETE", "NOT_DEFINED", "HUMAN_REVIEW_REQUIRED", "LOCKED"):
            if needle not in joined_values:
                print("Platform preview missing value {0}: {1}".format(needle, values))
                return 1
        report["preview_apptest_ok"] = True
        report["preview_metrics"] = labels
    print(json.dumps(report, indent=2, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
