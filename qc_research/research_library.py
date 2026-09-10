"""Research library rows for Strategy Monitor. PostgreSQL only. No QuantConnect."""

from __future__ import annotations

from typing import Any

import pandas as pd
from sqlalchemy import bindparam, text

from qc_research.contracts.label_integrity import load_csfml_v1_label_integrity
from qc_research.lifecycle import COMPLETE, RESEARCH_COMPLETE
from qc_research.platform_presentation import (
    ASSET_LABELS,
    display_strategy_name,
    friendly_label,
)
from qc_research.research_readout import plain_status_line

UNAVAILABLE = "Unavailable / Not applicable"

SMOKE_KIND = "smoke"
HOLDOUT_ACCESSED = {"ACCESSED"}
COMPLETE_STATUSES = {COMPLETE, RESEARCH_COMPLETE, "NON_HOLDOUT_COMPLETE"}

LIBRARY_RUNS_SQL = """
SELECT DISTINCT ON (rr.strategy_id)
    rr.strategy_id,
    rr.research_run_id,
    rr.research_kind,
    rr.research_mode,
    rr.asset_class,
    rr.strategy_family_id,
    rr.run_status,
    rr.economic_gate,
    rr.promotion_gate,
    rr.holdout_status,
    rr.holdout_accessed,
    rr.delivery_status,
    rr.last_seen_at,
    rr.completed_count,
    rr.failed_count,
    rr.expected_experiment_count,
    rr.synced_experiment_count
FROM research_runs rr
WHERE rr.strategy_id IS NOT NULL
  AND rr.strategy_id <> ''
  AND COALESCE(rr.holdout_status, 'LOCKED') <> 'ACCESSED'
  AND COALESCE(rr.holdout_accessed, FALSE) IS NOT TRUE
ORDER BY rr.strategy_id,
    CASE
        WHEN COALESCE(rr.run_status, '') IN ('COMPLETE', 'RESEARCH_COMPLETE', 'NON_HOLDOUT_COMPLETE')
             AND COALESCE(rr.holdout_status, 'LOCKED') IN ('LOCKED', '')
        THEN 0
        ELSE 1
    END,
    rr.last_seen_at DESC NULLS LAST
"""

ALL_RUNS_SQL = """
SELECT
    strategy_id,
    research_run_id,
    research_kind,
    research_mode,
    asset_class,
    run_status,
    economic_gate,
    promotion_gate,
    holdout_status,
    delivery_status,
    last_seen_at,
    completed_count,
    failed_count
FROM research_runs
WHERE strategy_id = :strategy_id
ORDER BY last_seen_at DESC NULLS LAST
"""

THESIS_SQL = """
SELECT DISTINCT ON (research_run_id)
    research_run_id,
    COALESCE(
        payload_json->'payload'->>'thesis',
        payload_json->>'thesis',
        payload_json->'payload'->'strategy_definition'->>'thesis',
        payload_json->'strategy_definition'->>'thesis',
        payload_json->'payload'->'intent'->>'original_user_thesis'
    ) AS thesis,
    COALESCE(
        payload_json->'payload'->>'display_name',
        payload_json->>'display_name',
        payload_json->'payload'->'strategy_definition'->>'display_name'
    ) AS display_name,
    COALESCE(
        NULLIF(payload_json->'payload'->>'window_count', '')::int,
        NULLIF(payload_json->>'window_count', '')::int,
        jsonb_array_length(COALESCE(payload_json->'payload'->'official_windows', payload_json->'official_windows', '[]'::jsonb))
    ) AS window_count,
    COALESCE(
        payload_json->'payload'->>'metric_kind',
        payload_json->>'metric_kind'
    ) AS metric_kind
FROM research_artifacts
WHERE research_run_id IN :run_ids
  AND artifact_type IN ('run_summary', 'strategy_spec')
ORDER BY research_run_id, synced_at DESC NULLS LAST
"""


def _read_sql(engine, sql: str, params: dict[str, Any] | None = None, *, expanding: tuple[str, ...] = ()) -> pd.DataFrame:
    if engine is None:
        return pd.DataFrame()
    stmt = text(sql)
    for name in expanding:
        stmt = stmt.bindparams(bindparam(name, expanding=True))
    return pd.read_sql(stmt, engine, params=params or {})


def _first_sentence(text: Any) -> str | None:
    raw = str(text or "").strip()
    if not raw:
        return None
    for sep in (". ", ".\n"):
        if sep in raw:
            return raw.split(sep, 1)[0].strip() + "."
    return raw if len(raw) <= 220 else raw[:217].rstrip() + "…"


def _stage_label(kind: Any, mode: Any) -> str:
    kind_text = str(kind or "").lower()
    if kind_text == "platform_research":
        return "Platform · {0}".format(friendly_label(mode))
    if kind_text in {"stage2_ml", "stage2"}:
        return "Stage 2"
    if kind_text in {"stage1", "stage1_research"}:
        return "Stage 1"
    if kind_text == SMOKE_KIND or "smoke" in kind_text:
        return "Smoke test"
    if kind_text:
        return friendly_label(kind)
    return "Research"


def _oos_text(window_count: Any, completed: Any, expected: Any) -> str:
    if window_count not in {None, ""}:
        try:
            return "{0} OOS windows".format(int(window_count))
        except (TypeError, ValueError):
            pass
    if completed not in {None, ""} or expected not in {None, ""}:
        if completed not in {None, ""} and expected not in {None, ""}:
            return "{0} / {1} experiments".format(completed, expected)
        return "{0} completed experiments".format(completed if completed not in {None, ""} else "—")
    return UNAVAILABLE


def default_run_id(runs: pd.DataFrame) -> str | None:
    """Latest completed eligible non-holdout run. Never the highest-performing run."""
    if runs is None or runs.empty:
        return None
    work = runs.copy()
    work["_holdout"] = work.get("holdout_status", pd.Series([""] * len(work))).fillna("").astype(str).str.upper()
    work["_status"] = work.get("run_status", pd.Series([""] * len(work))).fillna("").astype(str).str.upper()
    work["_kind"] = work.get("research_kind", pd.Series([""] * len(work))).fillna("").astype(str).str.lower()
    if "holdout_accessed" in work.columns:
        flagged = work["holdout_accessed"].map(
            lambda value: value is True or value in {1, "1", "true", "True", "yes", "YES"}
        )
        work = work[~flagged.fillna(False)]
    eligible = work[~work["_holdout"].isin(HOLDOUT_ACCESSED)]
    if eligible.empty:
        eligible = work
    complete = eligible[eligible["_status"].isin(COMPLETE_STATUSES)]
    pool = complete if not complete.empty else eligible
    smoke = pool[pool["_kind"].eq(SMOKE_KIND) | pool["_kind"].str.contains("smoke", na=False)]
    official = pool[~pool.index.isin(smoke.index)]
    chosen = official if not official.empty else pool
    if "last_seen_at" in chosen.columns:
        chosen = chosen.sort_values("last_seen_at", ascending=False, na_position="last")
    return str(chosen.iloc[0]["research_run_id"])


def load_research_library(engine) -> pd.DataFrame:
    """Compact starting view: one default run per strategy. Failed research stays visible."""
    runs = _read_sql(engine, LIBRARY_RUNS_SQL)
    if runs is None or runs.empty:
        return pd.DataFrame()
    run_ids = [str(value) for value in runs["research_run_id"].dropna().astype(str).tolist()]
    extras = pd.DataFrame()
    if run_ids:
        extras = _read_sql(engine, THESIS_SQL, {"run_ids": run_ids}, expanding=("run_ids",))
    if extras is not None and not extras.empty:
        runs = runs.merge(extras, on="research_run_id", how="left")
    else:
        runs["thesis"] = None
        runs["display_name"] = None
        runs["window_count"] = None
        runs["metric_kind"] = None
    rows = []
    for _, row in runs.iterrows():
        strategy_id = str(row.get("strategy_id") or "")
        kind = str(row.get("research_kind") or "")
        smoke = kind.lower() == SMOKE_KIND or "smoke" in kind.lower()
        rows.append(
            {
                "strategy_id": strategy_id,
                "name": display_strategy_name(strategy_id, row.get("display_name")),
                "description": _first_sentence(row.get("thesis")) or "Canonical one-sentence description is not stored for this run.",
                "asset_class": friendly_label(row.get("asset_class"), ASSET_LABELS),
                "asset_class_raw": row.get("asset_class"),
                "research_stage": _stage_label(kind, row.get("research_mode")),
                "research_kind": kind,
                "research_mode": row.get("research_mode"),
                "latest_run": row.get("research_run_id"),
                "oos_coverage": _oos_text(row.get("window_count"), row.get("completed_count"), row.get("expected_experiment_count")),
                "review_status": plain_status_line(
                    research_status=row.get("run_status"),
                    economic_gate=row.get("economic_gate"),
                    promotion_gate=row.get("promotion_gate"),
                    holdout_status=row.get("holdout_status"),
                    delivery_status=row.get("delivery_status"),
                    label_integrity=(
                        load_csfml_v1_label_integrity()["historical_v1_impact"]
                        if strategy_id == "CrossSectionalFactorML"
                        else None
                    ),
                ),
                "run_status": row.get("run_status"),
                "economic_gate": row.get("economic_gate"),
                "promotion_gate": row.get("promotion_gate"),
                "holdout_status": row.get("holdout_status"),
                "last_seen_at": row.get("last_seen_at"),
                "is_smoke": smoke,
                "failed": str(row.get("run_status") or "").upper() in {"FAILED", "ERROR"}
                or (row.get("failed_count") not in {None, 0, "0"} and str(row.get("run_status") or "").upper() not in COMPLETE_STATUSES),
            }
        )
    return pd.DataFrame(rows)


def load_strategy_runs(engine, strategy_id: str) -> pd.DataFrame:
    return _read_sql(engine, ALL_RUNS_SQL, {"strategy_id": strategy_id})


def library_display_frame(library: pd.DataFrame, *, include_smoke: bool = False) -> pd.DataFrame:
    if library is None or library.empty:
        return pd.DataFrame()
    work = library.copy()
    if not include_smoke and "is_smoke" in work.columns:
        official = work[~work["is_smoke"].fillna(False)]
        if not official.empty:
            work = official
    return pd.DataFrame(
        {
            "Strategy": work["name"],
            "What it does": work["description"],
            "Asset class": work["asset_class"],
            "Stage": work["research_stage"],
            "Latest completed run": work["latest_run"].astype(str).map(lambda value: value if len(value) <= 28 else value[:25] + "…"),
            "OOS coverage": work["oos_coverage"],
            "Review status": work["review_status"],
        }
    )


def filter_library(
    library: pd.DataFrame,
    *,
    strategy_id: str | None = None,
    asset_class: str | None = None,
    research_status: str | None = None,
    include_smoke: bool = False,
) -> pd.DataFrame:
    if library is None or library.empty:
        return library if library is not None else pd.DataFrame()
    work = library.copy()
    if not include_smoke and "is_smoke" in work.columns:
        official = work[~work["is_smoke"].fillna(False)]
        work = official if not official.empty else work
    if strategy_id:
        work = work[work["strategy_id"].astype(str) == str(strategy_id)]
    if asset_class and asset_class != "All":
        work = work[work["asset_class"].astype(str) == str(asset_class)]
    if research_status and research_status != "All":
        if research_status == "Complete":
            work = work[work["run_status"].astype(str).str.upper().isin(COMPLETE_STATUSES)]
        elif research_status == "Failed":
            work = work[work["failed"].fillna(False) | work["run_status"].astype(str).str.upper().isin({"FAILED", "ERROR"})]
        elif research_status == "Incomplete":
            work = work[~work["run_status"].astype(str).str.upper().isin(COMPLETE_STATUSES | {"FAILED", "ERROR"})]
    return work
