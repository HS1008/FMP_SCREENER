"""Holdout exposure labels that persist across Git commits of a lineage.

A new commit does not restore holdout innocence. Legacy backtests that
already overlap the configured holdout are marked EXPOSED_PRIOR_TO_STAGE1
and are neither deleted, hidden, nor re-run.
"""

from __future__ import annotations

from datetime import date, datetime
from typing import Any

from qc_research.dates import parse_qc_datetime


PRISTINE = "Pristine"
EXPOSED_PRIOR = "Exposed prior to Stage 1"
ACCESSED_ONCE = "Accessed once"
REPEATEDLY_ACCESSED = "Repeatedly accessed"

STATUS_EXPOSED_PRIOR_TO_STAGE1 = "EXPOSED_PRIOR_TO_STAGE1"
STATUS_ACCESSED_ONCE = "ACCESSED_ONCE"
STATUS_REPEATEDLY_ACCESSED = "REPEATEDLY_ACCESSED"
STATUS_PRISTINE = "PRISTINE"


def _as_date(value: Any) -> date | None:
    if value is None or value == "":
        return None
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    parsed = parse_qc_datetime(value)
    if parsed is not None:
        return parsed.date()
    try:
        return date.fromisoformat(str(value)[:10])
    except ValueError:
        return None


def periods_overlap(
    start_a: Any,
    end_a: Any,
    start_b: Any,
    end_b: Any,
) -> bool:
    a0 = _as_date(start_a)
    a1 = _as_date(end_a)
    b0 = _as_date(start_b)
    b1 = _as_date(end_b)
    if None in {a0, a1, b0, b1}:
        return False
    return a0 <= b1 and b0 <= a1


def simulation_window(row: dict[str, Any]) -> tuple[Any, Any]:
    start = (
        row.get("backtest_start")
        or row.get("backtestStart")
        or row.get("test_start")
        or row.get("start_date")
        or (row.get("parameters") or {}).get("start_date")
        or (row.get("parameters_json") or {}).get("start_date")
    )
    end = (
        row.get("backtest_end")
        or row.get("backtestEnd")
        or row.get("test_end")
        or row.get("end_date")
        or (row.get("parameters") or {}).get("end_date")
        or (row.get("parameters_json") or {}).get("end_date")
    )
    return start, end


def is_stage1_final_holdout(row: dict[str, Any]) -> bool:
    test_type = str(row.get("research_test_type") or "")
    name = str(row.get("name") or "")
    return test_type == "FINAL_HOLDOUT" or "FINAL_HOLDOUT" in name


def is_legacy_or_non_stage1(row: dict[str, Any]) -> bool:
    suite = str(row.get("research_suite_version") or "")
    run_id = row.get("research_run_id")
    name = str(row.get("name") or "")
    if name.startswith("S1__") or suite.startswith("S1"):
        return False
    if run_id:
        return False
    return True


def holdout_exposure_status(
    *,
    stage1_final_holdout_count: int,
    legacy_overlap: bool,
) -> str:
    if stage1_final_holdout_count >= 2:
        return STATUS_REPEATEDLY_ACCESSED
    if stage1_final_holdout_count == 1:
        return STATUS_ACCESSED_ONCE
    if legacy_overlap:
        return STATUS_EXPOSED_PRIOR_TO_STAGE1
    return STATUS_PRISTINE


def holdout_exposure_label(status: str | None) -> str:
    mapping = {
        STATUS_PRISTINE: PRISTINE,
        STATUS_EXPOSED_PRIOR_TO_STAGE1: EXPOSED_PRIOR,
        STATUS_ACCESSED_ONCE: ACCESSED_ONCE,
        STATUS_REPEATEDLY_ACCESSED: REPEATEDLY_ACCESSED,
        PRISTINE: PRISTINE,
        EXPOSED_PRIOR: EXPOSED_PRIOR,
        ACCESSED_ONCE: ACCESSED_ONCE,
        REPEATEDLY_ACCESSED: REPEATEDLY_ACCESSED,
    }
    return mapping.get(str(status or ""), PRISTINE)


def classify_rows(
    rows: list[dict[str, Any]],
    *,
    holdout_start: Any,
    holdout_end: Any,
    strategy_id: str | None = None,
    research_lineage_id: str | None = None,
) -> dict[str, Any]:
    """Audit holdout exposure across ALL git commits of a lineage."""
    overlapping = []
    stage1_holdouts = []
    for row in rows:
        if strategy_id and row.get("strategy_id") not in {None, strategy_id}:
            continue
        lineage = row.get("research_lineage_id") or row.get("strategy_id")
        if research_lineage_id and lineage not in {None, research_lineage_id, strategy_id}:
            if not is_legacy_or_non_stage1(row):
                continue
        start, end = simulation_window(row)
        overlaps = periods_overlap(start, end, holdout_start, holdout_end)
        if is_stage1_final_holdout(row):
            stage1_holdouts.append(row)
        elif overlaps:
            overlapping.append(row)

    status = holdout_exposure_status(
        stage1_final_holdout_count=len(stage1_holdouts),
        legacy_overlap=bool(overlapping),
    )
    return {
        "status": status,
        "label": holdout_exposure_label(status),
        "stage1_final_holdout_count": len(stage1_holdouts),
        "legacy_overlap_count": len(overlapping),
        "legacy_overlap_backtests": overlapping,
        "stage1_holdout_backtests": stage1_holdouts,
        "research_lineage_id": research_lineage_id,
        "holdout_start": holdout_start,
        "holdout_end": holdout_end,
        "note": (
            "Holdout exposure is tracked by strategy_id + research_lineage_id "
            "+ holdout window across all Git commits. A new commit does not "
            "reset holdout innocence. Legacy overlapping backtests are kept "
            "visible as EXPOSED_PRIOR_TO_STAGE1 and are not re-run."
        ),
    }
