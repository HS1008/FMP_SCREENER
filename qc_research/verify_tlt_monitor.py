#!/usr/bin/env python3
"""Query-back TLTDurationMomentum V0 from live PostgreSQL / Monitor read model.

Does not launch QuantConnect. Live query-back uses DASHBOARD_READONLY_URL only.
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


def verify_exit_code(report: dict, *, require_present: bool) -> int:
    if report.get("present") and not report.get("identity_ok"):
        print("tlt_v0=identity_refused")
        return 2
    if require_present and not report.get("present"):
        print("tlt_v0=official_run_missing")
        return 3
    if report.get("identity_ok"):
        print("tlt_v0=identity_ok")
        return 0
    print("tlt_v0=not_ingested")
    return 0


def write_live_report(
    report: dict,
    path: str,
    *,
    code_root: str = "",
) -> None:
    from datetime import datetime, timezone
    from pathlib import Path

    from jobs.audit_host_dashboard import write_facts

    write_facts(
        {
            "recorded_at": datetime.now(timezone.utc).replace(microsecond=0).isoformat(),
            "code_root": str(code_root or "").strip(),
            "present": bool(report.get("present")),
            "identity_ok": bool(report.get("identity_ok")),
            "blockers": [str(item) for item in (report.get("blockers") or [])],
            "research_run_id": report.get("research_run_id"),
            "strategy_id": report.get("strategy_id"),
            "economic_gate": report.get("economic_gate"),
            "holdout_accessed": bool(report.get("holdout_accessed")),
            "window_count": report.get("window_count"),
        },
        Path(path),
    )


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
    parser.add_argument("--out", default="")
    parser.add_argument("--code-root", default="")
    parser.add_argument(
        "--allow-missing",
        action="store_true",
        default=False,
        help="Missing official row is recorded, not a failure (everyday deploy)",
    )
    ns = parser.parse_args(argv)

    from qc_research.platform_ingest import (
        monitor_view_from_artifacts,
        normalize_platform_file,
    )
    from qc_research.tlt_duration_momentum import (
        STRATEGY_ID,
        evaluate_tlt_v0,
        verify_tlt_monitor_view,
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
    from db.dashboard_engine import (
        DashboardIdentityError,
        dashboard_database_url,
        dashboard_engine,
        load_streamlit_env,
        writer_fallback_allowed,
    )

    want_live = ns.live or (bool(dashboard_database_url()) and not ns.dry_run)
    if want_live and writer_fallback_allowed():
        print("DASHBOARD_ALLOW_WRITER_FALLBACK is not a live TLT verify path")
        return 4
    if ns.live and not dashboard_database_url():
        print("DASHBOARD_READONLY_URL unset. Live TLT query-back requires dashboard_readonly.")
        return 1
    if want_live:
        load_streamlit_env()
        try:
            engine = dashboard_engine()
        except DashboardIdentityError as exc:
            print(str(exc))
            return 1
        with engine.connect() as conn:
            live_report = evaluate_tlt_v0(conn)
        report["live"] = live_report
        report["present"] = live_report.get("present")
        report["identity_ok"] = live_report.get("identity_ok")
        report["blockers"] = live_report.get("blockers")
        report["live_identity"] = "dashboard_readonly"
        if ns.out:
            write_live_report(live_report, ns.out, code_root=ns.code_root)
            print("tlt_v0_live_written={0}".format(ns.out))
        live_rc = verify_exit_code(live_report, require_present=not ns.allow_missing)
        if live_rc != 0:
            print(json.dumps(report, indent=2, default=str))
            return live_rc
    elif not ns.dry_run:
        report["live_skipped"] = "DASHBOARD_READONLY_URL unset"
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
