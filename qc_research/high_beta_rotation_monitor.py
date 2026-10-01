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


REQUIRED_COMPLETE_EVIDENCE = (
    "strategy_spec",
    "run_manifest",
    "run_summary",
    "selection_ledger",
    "event_ledger",
    "fill_ledger",
    "risk_diagnostics",
    "rotation_diagnostics",
    "signal_health",
    "benchmark_diagnostics",
    "annual_results",
    "cost_stress",
    "assessment",
)
_COMPLETE_TOKENS = {"COMPLETE", "RESEARCH_COMPLETE", "NON_HOLDOUT_COMPLETE"}


def _stamp_text(value: object) -> str:
    if value is None:
        return ""
    text = str(value).strip()
    if text in {"", "NaT", "None", "nan"}:
        return ""
    return text


def select_hbr_run(rows: list[Mapping[str, Any]] | None) -> Mapping[str, Any] | None:
    """Latest research attempt. Lexical run id is only a stable tie-break.

    Precedence is last_seen_at, then first_seen_at, then the smallest
    research_run_id. A newer blocked run stays ahead of an older complete run.
    """
    candidates = [
        row
        for row in (rows or [])
        if str(row.get("research_run_id") or "").strip()
    ]
    if not candidates:
        return None
    ordered = sorted(candidates, key=lambda row: str(row.get("research_run_id") or ""))
    return max(
        ordered,
        key=lambda row: (_stamp_text(row.get("last_seen_at")), _stamp_text(row.get("first_seen_at"))),
    )


def evidence_status_for_run(run_status: str, artifacts: Mapping[str, Any] | None = None) -> str:
    """Honest research state. Complete requires the stored evidence contract."""
    text = str(run_status or "").strip().upper()
    indexed = artifacts or {}
    if text == "BLOCKED_TRANSPORT":
        return "blocked_incomplete"
    if text == "HUMAN_REVIEW_REQUIRED":
        return "human_review_required"
    if text in _COMPLETE_TOKENS:
        if all(kind in indexed for kind in REQUIRED_COMPLETE_EVIDENCE):
            return "complete"
        return "incomplete"
    if text in {"", "INCOMPLETE", "FAILED", "ERROR"}:
        return "incomplete"
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


def _payload_of(row: Mapping[str, Any]) -> dict[str, Any] | None:
    payload = row.get("payload") if isinstance(row.get("payload"), dict) else row
    if not isinstance(payload, dict):
        return None
    return dict(payload)


def _row_sha(row: Mapping[str, Any], payload: Mapping[str, Any]) -> str:
    digest = row.get("sha256") or payload.get("artifact_sha256") or ""
    return str(digest)


def select_hbr_artifacts(
    artifacts: list[Mapping[str, Any]] | None,
    *,
    versions: Mapping[str, str] | None = None,
) -> tuple[dict[str, Mapping[str, Any]], list[str]]:
    """Pick one payload per kind. Duplicates need an explicit sha, not file order."""
    grouped: dict[str, list[tuple[Mapping[str, Any], dict[str, Any]]]] = {}
    order: list[str] = []
    for row in artifacts or []:
        payload = _payload_of(row)
        if payload is None:
            continue
        kind = str(row.get("artifact_type") or payload.get("artifact_type") or "")
        if not kind:
            continue
        if kind not in grouped:
            order.append(kind)
            grouped[kind] = []
        grouped[kind].append((row, payload))
    chosen: dict[str, Mapping[str, Any]] = {}
    ambiguous: list[str] = []
    requested = {str(kind): str(digest) for kind, digest in dict(versions or {}).items()}
    for kind in order:
        rows = grouped[kind]
        if kind in requested:
            wanted = requested[kind]
            matches = [payload for row, payload in rows if _row_sha(row, payload) == wanted]
            if len(matches) == 1:
                chosen[kind] = matches[0]
            else:
                ambiguous.append(kind)
            continue
        if len(rows) == 1:
            chosen[kind] = rows[0][1]
            continue
        ambiguous.append(kind)
    return chosen, ambiguous


def build_hbr_monitor_view(
    run: Mapping[str, Any] | None,
    artifacts: list[Mapping[str, Any]] | None = None,
    *,
    versions: Mapping[str, str] | None = None,
) -> dict[str, Any]:
    """Compact historical summary. Nulls stay null. Zero stays zero."""
    record = dict(run or {})
    indexed, ambiguous = select_hbr_artifacts(artifacts, versions=versions)
    summary = indexed.get("run_summary") or {}
    assessment = indexed.get("assessment") or {}
    run_status = str(record.get("run_status") or summary.get("run_status") or "")
    evidence_status = evidence_status_for_run(run_status, indexed)
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
        "ambiguous_artifacts": ambiguous,
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
