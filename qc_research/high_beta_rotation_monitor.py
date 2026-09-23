"""Read-only HighBetaRotation Strategy Monitor view.

Builds a display model from already stored research rows. It does not query
QuantConnect, launch a backtest, train a model, or write strategy state.
"""

from __future__ import annotations

from typing import Any, Mapping

RESEARCH_KIND = "high_beta_rotation_rule_v1"
HISTORICAL_LABEL = (
    "Historical through 2024-12-31. These observations are not current trading recommendations."
)
MAIN_METRICS = (
    "cagr",
    "max_drawdown",
    "sortino",
    "calmar",
    "ex_ante_beta",
    "turnover",
    "cash_shortfall",
    "constraint_shortfall",
)


def _status_for_run(run_status: str) -> str:
    text = run_status.upper()
    if text == "BLOCKED_TRANSPORT":
        return "blocked_transport"
    if text in {"INCOMPLETE", "FAILED", "ERROR"}:
        return "incomplete"
    if text in {"COMPLETE", "RESEARCH_COMPLETE", "NON_HOLDOUT_COMPLETE"}:
        return "observed"
    return "unavailable"


def _metric(container: Mapping[str, Any] | None, key: str, *, evidence_status: str) -> dict[str, Any]:
    if container is None or key not in container:
        return {"value": None, "status": "unavailable", "reason": None}
    value = container.get(key)
    if value is None:
        reason = container.get("{0}_reason".format(key))
        status = "undefined"
        if evidence_status == "incomplete":
            status = "incomplete"
        return {"value": None, "status": status, "reason": reason}
    return {"value": value, "status": "observed", "reason": None}


def _series(value: Any, *, evidence_status: str) -> dict[str, Any]:
    if value is None:
        status = "undefined" if evidence_status != "unavailable" else "unavailable"
        if evidence_status == "incomplete":
            status = "incomplete"
        return {"rows": None, "status": status}
    if isinstance(value, list):
        return {"rows": value, "status": "observed" if value else "unavailable"}
    return {"rows": value, "status": "observed"}


def _artifacts_by_type(artifacts: list[Mapping[str, Any]] | None) -> dict[str, Mapping[str, Any]]:
    indexed: dict[str, Mapping[str, Any]] = {}
    for row in artifacts or []:
        payload = row.get("payload") if isinstance(row.get("payload"), dict) else row
        if not isinstance(payload, dict):
            continue
        kind = str(row.get("artifact_type") or payload.get("artifact_type") or "")
        if kind:
            indexed[kind] = payload
    return indexed


def build_hbr_monitor_view(
    run: Mapping[str, Any] | None,
    artifacts: list[Mapping[str, Any]] | None = None,
) -> dict[str, Any]:
    """Compact historical summary. Nulls stay null. Zero stays zero."""
    record = dict(run or {})
    indexed = _artifacts_by_type(artifacts)
    summary = indexed.get("run_summary") or {}
    assessment = indexed.get("assessment") or {}
    run_status = str(record.get("run_status") or summary.get("run_status") or "")
    evidence_status = _status_for_run(run_status)
    if not record and not indexed:
        evidence_status = "unavailable"
    variants = summary.get("variants")
    main = variants.get("HBR_MAIN") if isinstance(variants, dict) else None
    if not isinstance(main, dict):
        main = None
    metrics = {key: _metric(main, key, evidence_status=evidence_status) for key in MAIN_METRICS}
    return {
        "strategy_id": record.get("strategy_id") or summary.get("strategy_id") or "HighBetaRotationV1",
        "research_kind": RESEARCH_KIND,
        "research_run_id": record.get("research_run_id") or summary.get("research_run_id"),
        "historical_label": HISTORICAL_LABEL,
        "economic_rating": assessment.get("economic_rating") or summary.get("economic_rating") or "UNRATED",
        "economic_gate": record.get("economic_gate") or summary.get("economic_gate") or "NOT_DEFINED",
        "thresholds": assessment.get("thresholds") or "THRESHOLDS_NOT_PREDEFINED",
        "run_status": run_status or None,
        "evidence_status": evidence_status,
        "provenance": summary.get("provenance") or record.get("provenance"),
        "metrics": metrics,
        "beta_control_comparison": _metric(
            summary if "beta_control_comparison" in summary else None,
            "beta_control_comparison",
            evidence_status=evidence_status,
        ),
        "cost_stress": {
            key: _metric(
                summary.get("cost_stress_bps") if isinstance(summary.get("cost_stress_bps"), dict) else None,
                key,
                evidence_status=evidence_status,
            )
            for key in ("0", "10", "20")
        },
        "yearly_returns": _series(summary.get("yearly_returns"), evidence_status=evidence_status),
        "rolling_beta": _series(summary.get("rolling_beta"), evidence_status=evidence_status),
        "exposures": _series(summary.get("exposures"), evidence_status=evidence_status),
        "rotation_efficacy": _series(summary.get("rotation_efficacy"), evidence_status=evidence_status),
        "signal_health": _series(summary.get("signal_health"), evidence_status=evidence_status),
        "read_only": True,
        "launches_backtests": False,
        "writes_state": False,
    }


def format_hbr_metric(metric: Mapping[str, Any] | None) -> str:
    """Show a status word for missing values. A stored zero remains 0."""
    row = dict(metric or {})
    status = str(row.get("status") or "unavailable")
    if status != "observed":
        return status
    value = row.get("value")
    if value is None:
        return "undefined"
    if isinstance(value, float):
        return "{0:.6g}".format(value)
    return str(value)
