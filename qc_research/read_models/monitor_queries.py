"""PostgreSQL SELECT loaders used by Strategy Monitor.

Read-only. Empty frame on missing engine or query errors.
"""

from __future__ import annotations

import json
from typing import Any

import pandas as pd
from sqlalchemy import text


def read_sql(engine, sql: str, params: dict[str, Any] | None = None) -> pd.DataFrame:
    if engine is None:
        return pd.DataFrame()
    try:
        return pd.read_sql(text(sql), engine, params=params or {})
    except Exception:
        return pd.DataFrame()


def as_payload(value: Any) -> dict[str, Any] | None:
    if value is None:
        return None
    if isinstance(value, dict):
        return value
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
        except ValueError:
            return None
        return parsed if isinstance(parsed, dict) else None
    return None


PLATFORM_RUN_IDS_SQL = """
        SELECT DISTINCT a.research_run_id
        FROM research_artifacts a
        LEFT JOIN research_runs r ON r.research_run_id = a.research_run_id
        WHERE COALESCE(r.research_kind, '') IS DISTINCT FROM 'stage2_ml'
          AND (
                r.strategy_id = :strategy_id
             OR r.research_lineage_id = :strategy_id
             OR r.research_lineage_id = :lineage
             OR a.payload_json->>'strategy_id' = :strategy_id
             OR a.payload_json->'payload'->>'strategy_id' = :strategy_id
             OR a.payload_json->'identity'->>'strategy_id' = :strategy_id
             OR a.payload_json->'payload'->'identity'->>'strategy_id' = :strategy_id
             OR a.payload_json->>'research_lineage_id' = :strategy_id
             OR a.payload_json->'payload'->>'research_lineage_id' = :strategy_id
             OR a.research_run_id LIKE :run_prefix
          )
        ORDER BY 1
        """


def load_platform_run_ids(engine, strategy_id: str) -> list[str]:
    if not strategy_id:
        return []
    lineage_rows = read_sql(
        engine,
        """
        SELECT research_lineage_id
        FROM research_runs
        WHERE strategy_id = :strategy_id
          AND research_kind = 'platform_research'
        ORDER BY last_seen_at DESC NULLS LAST
        LIMIT 1
        """,
        {"strategy_id": strategy_id},
    )
    lineage = strategy_id
    if lineage_rows is not None and not lineage_rows.empty:
        value = lineage_rows.iloc[0].get("research_lineage_id")
        if value:
            lineage = str(value)
    rows = read_sql(
        engine,
        PLATFORM_RUN_IDS_SQL,
        {
            "strategy_id": strategy_id,
            "lineage": lineage,
            "run_prefix": "PLATFORM_{0}_%".format(strategy_id),
        },
    )
    if rows is None or rows.empty:
        return []
    return [str(value) for value in rows["research_run_id"].dropna().astype(str).tolist() if value]


def load_stage2_trials(engine, research_run_id: str) -> pd.DataFrame:
    return read_sql(
        engine,
        """
        SELECT *
        FROM ml_trials
        WHERE research_run_id = :research_run_id
        ORDER BY outer_window_id, trial_id
        """,
        {"research_run_id": research_run_id},
    )


def load_stage2_models(engine, research_run_id: str) -> pd.DataFrame:
    return read_sql(
        engine,
        """
        SELECT *
        FROM ml_models
        WHERE research_run_id = :research_run_id
        ORDER BY outer_window_id
        """,
        {"research_run_id": research_run_id},
    )


def load_stage2_feature_diagnostics(engine, research_run_id: str) -> pd.DataFrame:
    return read_sql(
        engine,
        """
        SELECT *
        FROM ml_feature_diagnostics
        WHERE research_run_id = :research_run_id
        ORDER BY outer_window_id, coefficient_rank NULLS LAST
        """,
        {"research_run_id": research_run_id},
    )


def load_stage2_signal_points(engine, research_run_id: str) -> pd.DataFrame:
    return read_sql(
        engine,
        """
        SELECT *
        FROM ml_signal_points
        WHERE research_run_id = :research_run_id
        ORDER BY timestamp
        """,
        {"research_run_id": research_run_id},
    )


def load_stage2_run_ids(engine, strategy_id: str) -> list[str]:
    if engine is None:
        return []
    rows = read_sql(
        engine,
        """
        SELECT DISTINCT research_run_id
        FROM research_runs
        WHERE research_kind = 'stage2_ml'
          AND strategy_id = :strategy_id
        UNION
        SELECT DISTINCT research_run_id
        FROM research_artifacts
        WHERE research_run_id LIKE :prefix
        ORDER BY 1
        """,
        {
            "strategy_id": strategy_id,
            "prefix": "STAGE2_{0}_%".format(strategy_id),
        },
    )
    if rows is None or rows.empty:
        return []
    return [str(value) for value in rows["research_run_id"].dropna().astype(str).tolist() if value]


def load_stage2_artifact_payload(engine, research_run_id: str, artifact_type: str) -> dict[str, Any] | None:
    rows = read_sql(
        engine,
        """
        SELECT payload_json
        FROM research_artifacts
        WHERE research_run_id = :research_run_id
          AND artifact_type = :artifact_type
        ORDER BY synced_at DESC NULLS LAST
        LIMIT 1
        """,
        {"research_run_id": research_run_id, "artifact_type": artifact_type},
    )
    if rows is None or rows.empty:
        return None
    return as_payload(rows.iloc[0].get("payload_json"))
