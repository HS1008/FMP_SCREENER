"""Idempotent ingest of platform (non-Stage-2-V1) research artifacts.

Does not download Object Store objects. Does not launch QuantConnect.
"""

from __future__ import annotations

from typing import Any

from sqlalchemy import text

from qc_research.object_store_sync import canonical_dumps


UPSERT_TRIAL = """
INSERT INTO research_trials (
    research_run_id, trial_id, model_family, selected, rejected, inner_score, trial_json
) VALUES (
    :research_run_id, :trial_id, :model_family, :selected, :rejected, :inner_score,
    CAST(:trial_json AS JSONB)
)
ON CONFLICT (research_run_id, trial_id) DO UPDATE SET
    model_family = EXCLUDED.model_family,
    selected = EXCLUDED.selected,
    rejected = EXCLUDED.rejected,
    inner_score = EXCLUDED.inner_score,
    trial_json = EXCLUDED.trial_json
"""

UPSERT_PAIR = """
INSERT INTO research_pair_diagnostics (
    research_run_id, pair_left, pair_right, hedge_ratio_method, hedge_ratio,
    correlation, half_life, selection_used_oos, diagnostics_json
) VALUES (
    :research_run_id, :pair_left, :pair_right, :hedge_ratio_method, :hedge_ratio,
    :correlation, :half_life, :selection_used_oos, CAST(:diagnostics_json AS JSONB)
)
ON CONFLICT (research_run_id, pair_left, pair_right) DO UPDATE SET
    hedge_ratio_method = EXCLUDED.hedge_ratio_method,
    hedge_ratio = EXCLUDED.hedge_ratio,
    correlation = EXCLUDED.correlation,
    half_life = EXCLUDED.half_life,
    selection_used_oos = EXCLUDED.selection_used_oos,
    diagnostics_json = EXCLUDED.diagnostics_json
"""

UPSERT_FI = """
INSERT INTO research_fixed_income_metrics (
    research_run_id, metric_name, metric_value, available, source
) VALUES (
    :research_run_id, :metric_name, :metric_value, :available, :source
)
ON CONFLICT (research_run_id, metric_name) DO UPDATE SET
    metric_value = EXCLUDED.metric_value,
    available = EXCLUDED.available,
    source = EXCLUDED.source
"""

UPSERT_SPEC = """
INSERT INTO strategy_specs (
    strategy_spec_hash, strategy_id, strategy_family_id, research_lineage_id,
    research_mode, asset_class, spec_json, git_sha
) VALUES (
    :strategy_spec_hash, :strategy_id, :strategy_family_id, :research_lineage_id,
    :research_mode, :asset_class, CAST(:spec_json AS JSONB), :git_sha
)
ON CONFLICT (strategy_spec_hash) DO UPDATE SET
    spec_json = EXCLUDED.spec_json
"""


def _inner(payload: dict[str, Any]) -> dict[str, Any]:
    inner = payload.get("payload")
    return inner if isinstance(inner, dict) else payload


def ingest_platform_payload(conn, *, kind: str, payload: dict[str, Any]) -> None:
    inner = _inner(payload)
    run_id = str(payload.get("research_run_id") or inner.get("research_run_id") or "")
    if kind == "trials":
        selected = inner.get("selected_trial_id")
        for row in list(inner.get("candidates") or []) + list(inner.get("rejected") or []):
            conn.execute(
                text(UPSERT_TRIAL),
                {
                    "research_run_id": run_id,
                    "trial_id": row.get("trial_id"),
                    "model_family": row.get("model_family"),
                    "selected": row.get("trial_id") == selected,
                    "rejected": bool(row.get("rejected")),
                    "inner_score": row.get("inner_score"),
                    "trial_json": canonical_dumps(row),
                },
            )
    elif kind == "pair_diagnostics":
        pair = inner.get("pair") or []
        if len(pair) >= 2:
            conn.execute(
                text(UPSERT_PAIR),
                {
                    "research_run_id": run_id,
                    "pair_left": pair[0],
                    "pair_right": pair[1],
                    "hedge_ratio_method": inner.get("hedge_ratio_method"),
                    "hedge_ratio": inner.get("hedge_ratio"),
                    "correlation": inner.get("correlation"),
                    "half_life": inner.get("half_life"),
                    "selection_used_oos": bool(inner.get("selection_used_oos")),
                    "diagnostics_json": canonical_dumps(inner),
                },
            )
    elif kind == "fixed_income_risk":
        for name, value in (inner.get("metrics") or {"gross_dv01": inner.get("gross_dv01")}).items():
            conn.execute(
                text(UPSERT_FI),
                {
                    "research_run_id": run_id,
                    "metric_name": name,
                    "metric_value": value if isinstance(value, (int, float)) else None,
                    "available": value is not None,
                    "source": inner.get("source") or "platform_artifact",
                },
            )
    elif kind == "strategy_spec":
        spec = inner if inner.get("identity") else payload
        identity = spec.get("identity") or {}
        conn.execute(
            text(UPSERT_SPEC),
            {
                "strategy_spec_hash": identity.get("config_fingerprint") or payload.get("config_fingerprint") or "",
                "strategy_id": identity.get("strategy_id") or payload.get("strategy_id") or "",
                "strategy_family_id": identity.get("strategy_family_id"),
                "research_lineage_id": identity.get("research_lineage_id"),
                "research_mode": identity.get("research_mode"),
                "asset_class": identity.get("asset_class"),
                "spec_json": canonical_dumps(spec),
                "git_sha": identity.get("git_sha"),
            },
        )
