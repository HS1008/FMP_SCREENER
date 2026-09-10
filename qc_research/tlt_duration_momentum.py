"""TLTDurationMomentum V0 artifact wrap, registry, and query-back.

Does not launch QuantConnect. Does not alter the frozen TLT V0 contract.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from sqlalchemy import text

STRATEGY_ID = "TLTDurationMomentum"
LINEAGE_ID = "LINEAGE_TLT_DURATION_MOMENTUM_V0"
RESEARCH_KIND = "platform_research"
RESEARCH_MODE = "ML_DISCOVERY"
FAMILY_ID = "FIXED_INCOME_TREND"
ASSET_CLASS = "BOND_ETF"
SYMBOL = "TLT"
RUN_ID = "PLATFORM_TLTDurationMomentum_V0"
ECONOMIC_GATE = "NOT_DEFINED"
SELECTED_TRIAL = "elasticnet::lb120_a0p1_l10p5"
BASELINE_TRIAL = "deterministic::sma120_long_cash"
MODEL_FAMILY = "elasticnet"
RESEARCH_STATE = "COMPLETE"
PROMOTION_GATE = "HUMAN_REVIEW_REQUIRED"
HOLDOUT_STATUS = "LOCKED"
FINGERPRINT = "d8f43c83ddec8d70"
SEARCH_SPACE_HASH = "1c609a682653ee90"
FEATURE_SCHEMA_HASH = "122b102a7a402e2c"
COST_MODEL_ID = "BOND_ETF_V1"
PROJECT_ID = "36108691"
PROJECT_NAME = "PlatformResearch"
WINDOW_IDS = tuple("W{0}".format(year) for year in range(2015, 2025))

# Official attempt-1 QC IDs. Do not replace with unofficial collision IDs.
OFFICIAL_WINDOWS = {
    "W2015": {
        "contract_hash": "d620d9cea168c0d70716f1f0210279559b07e9f6d75d4cf13dbcc6a3554db4d3",
        "train_backtest_id": "42444d596c9116f1320203e896fbf0fe",
        "winner_backtest_id": "60ab8a7760249476e360d9b6a53ec06f",
        "baseline_backtest_id": "a8cb56a48a94485ed9eaf6d8fdd01c7f",
    },
    "W2016": {
        "contract_hash": "cd5fb01a3a158ca1eb1f6f3a7ba2540f733382923173db9a8fa154e0fb61007c",
        "train_backtest_id": "7312409bc89e60438047dda355ffb754",
        "winner_backtest_id": "56430c837aa342c5f7d02bc082d18d2b",
        "baseline_backtest_id": "d1e66b1a2f1a826f76b46b1e2935ee69",
    },
    "W2017": {
        "contract_hash": "3826543849d6c65cfed762de4e38d1293ee778da8e3d3b526e62c6dc4e95fe9a",
        "train_backtest_id": "427ac73ca9246ad90cd44ddb75471a1f",
        "winner_backtest_id": "faaa52dc9fe8cdd7b67474152de75330",
        "baseline_backtest_id": "44edeba1d2b0f6619b86c67f78c4cdbe",
    },
    "W2018": {
        "contract_hash": "76c82c3c8e295144566a6a2b76e3b93c7fadf210ac039ec5789b81339d07ebc9",
        "train_backtest_id": "8c6a742cb81aad1120e927c643c7a656",
        "winner_backtest_id": "afb66ac6453df7f2e4fdd73906a220ec",
        "baseline_backtest_id": "e9b7e6e16c671a984a2974168385aca9",
    },
    "W2019": {
        "contract_hash": "77a7100d902a76637e6e93989919f73c9b2ae215b59d3c759dcf4d553ed0227a",
        "train_backtest_id": "c33b078914e4187a588de7c30f013a5c",
        "winner_backtest_id": "bbb18bc4afafeb0eea14b231262a1df2",
        "baseline_backtest_id": "9bceb14ec61d649d026a63f163a01efd",
    },
    "W2020": {
        "contract_hash": "75c2ccaaf5312da1b052f5362b87ad4581e7b9a2803cb0c5d7bd066dc1665260",
        "train_backtest_id": "00f7fc452ad05b507f3bbac8c836945f",
        "winner_backtest_id": "c1ed0e0c8797d25585cdbcb543fa0414",
        "baseline_backtest_id": "b439e09be4128ac9cc8272b9b432696d",
    },
    "W2021": {
        "contract_hash": "6612316b212d1ff06eddb299efc08b3e49acf36fa86dd524e91a74ce0d8e2beb",
        "train_backtest_id": "8ba24d8329dc5e6af4b3853b5d9546a6",
        "winner_backtest_id": "0a35d6bdca65588428c1738c6ac245b7",
        "baseline_backtest_id": "3b909b82b0bf57f7c0ff31bd86b20d5f",
    },
    "W2022": {
        "contract_hash": "c76388ef5812455b4b33aeea4d40f055d2c1885e7affa44b053be5b8f521fa9b",
        "train_backtest_id": "2aaa06ba4621b36c2811b80f8652260c",
        "winner_backtest_id": "55cb101bbdacdf1f22f33e785078c817",
        "baseline_backtest_id": "8c2ed6eb24cb06432ed0ade119a3e238",
    },
    "W2023": {
        "contract_hash": "196e7a81cb5fb8a55c6b4cd2d9d487fa60b59d7be0facc517eabd4dd4ee0a0ca",
        "train_backtest_id": "59c3e50f0d093df4dc2dee9e281184b9",
        "winner_backtest_id": "bd577838e79abe2ee68c149c2646bb4d",
        "baseline_backtest_id": "4a3d97a5d6cce83383e21c3f489ffd74",
    },
    "W2024": {
        "contract_hash": "8b5e0a4668877de657b36a90ed1d4ca1a9d04861da5584824f6288aa91d44c90",
        "train_backtest_id": "377b2e086b8121fbe7dc3d28d8a1fe7b",
        "winner_backtest_id": "7817e46209101c701ca93342a4c67059",
        "baseline_backtest_id": "0c658059909cfef950c3ac2cf433ecff",
    },
}


def official_tlt_qc_backtest_ids() -> frozenset[str]:
    """Official TLT V0 QuantConnect backtest IDs. Cloud sync must not first-INSERT these."""
    found: set[str] = set()
    for window in OFFICIAL_WINDOWS.values():
        for key in ("train_backtest_id", "winner_backtest_id", "baseline_backtest_id"):
            value = str(window.get(key) or "").strip()
            if value:
                found.add(value)
    return frozenset(found)


REGISTER_STRATEGY_SQL = """
INSERT INTO strategies (
    strategy_id, name, environment, status,
    qc_research_project_id, qc_research_project_name
) VALUES (
    :strategy_id, :name, :environment, :status,
    :qc_research_project_id, :qc_research_project_name
)
ON CONFLICT (strategy_id) DO UPDATE SET
    name = EXCLUDED.name,
    qc_research_project_id = COALESCE(EXCLUDED.qc_research_project_id, strategies.qc_research_project_id),
    qc_research_project_name = COALESCE(EXCLUDED.qc_research_project_name, strategies.qc_research_project_name)
"""


def is_tlt_duration_momentum_record(payload: dict[str, Any]) -> bool:
    if not isinstance(payload, dict):
        return False
    if str(payload.get("schema_version") or "") in {"platform_artifact_v1", "platform_v1", "stage2_ml_v1"}:
        return False
    strategy = str(payload.get("strategy_id") or "")
    lineage = str(payload.get("research_lineage_id") or "")
    family = str(payload.get("family") or "")
    if strategy == STRATEGY_ID or lineage == LINEAGE_ID or family == "tlt_duration_momentum":
        return True
    return bool(payload.get("official_windows")) and strategy == STRATEGY_ID


def _merge_windows(record: dict[str, Any]) -> list[dict[str, Any]]:
    by_id: dict[str, dict[str, Any]] = {}
    aggregate = record.get("aggregate") if isinstance(record.get("aggregate"), dict) else {}
    nested = aggregate.get("windows") if isinstance(aggregate, dict) else None
    for row in list(nested or []) + list(record.get("official_windows") or []):
        if not isinstance(row, dict):
            continue
        window_id = str(row.get("window_id") or "")
        if not window_id:
            continue
        current = dict(by_id.get(window_id) or {})
        current.update(row)
        pinned = OFFICIAL_WINDOWS.get(window_id) or {}
        for key, value in pinned.items():
            current.setdefault(key, value)
        current["window_id"] = window_id
        current["selected_trial_id"] = current.get("selected_trial_id") or SELECTED_TRIAL
        current.setdefault("start", current.get("oos_start"))
        current.setdefault("end", current.get("oos_end"))
        by_id[window_id] = current
    missing = [window_id for window_id in WINDOW_IDS if window_id not in by_id]
    if missing:
        raise ValueError("TLT V0 artifact missing official windows: {0}".format(", ".join(missing)))
    extra = [window_id for window_id in by_id if window_id not in WINDOW_IDS]
    if extra:
        raise ValueError("TLT V0 artifact has unexpected windows: {0}".format(", ".join(sorted(extra))))
    windows = [by_id[window_id] for window_id in WINDOW_IDS]
    for window in windows:
        end = str(window.get("oos_end") or window.get("end") or "")
        if end.startswith("2025") or end > "2024-12-31":
            raise ValueError("TLT V0 window {0} touches sealed 2025+ holdout".format(window["window_id"]))
        pinned = OFFICIAL_WINDOWS[window["window_id"]]
        for key, value in pinned.items():
            if str(window.get(key) or "") != value:
                raise ValueError(
                    "TLT V0 {0} {1} is {2}, expected official {3}".format(
                        window["window_id"], key, window.get(key), value
                    )
                )
    return windows


def wrap_tlt_duration_momentum_record(record: dict[str, Any]) -> list[tuple[str, dict[str, Any]]]:
    """Turn the official 10-window TLT V0 JSON into hashed Monitor artifacts."""
    from qc_research.platform_ingest import _hashed_envelope, refuse_tainted_source

    refuse_tainted_source(record)
    if str(record.get("strategy_id") or "") not in {"", STRATEGY_ID}:
        raise ValueError("TLT V0 artifact strategy_id must be {0}".format(STRATEGY_ID))
    if str(record.get("research_lineage_id") or "") not in {"", LINEAGE_ID}:
        raise ValueError("TLT V0 artifact lineage must be {0}".format(LINEAGE_ID))
    windows = _merge_windows(record)
    aggregate = record.get("aggregate") if isinstance(record.get("aggregate"), dict) else {}
    means = aggregate.get("aggregate") if isinstance(aggregate.get("aggregate"), dict) else {}
    ml_mean = means.get("ml") if isinstance(means.get("ml"), dict) else {}
    provenance = str(record.get("provenance") or "REAL_QC")
    summary = {
        "research_run_id": RUN_ID,
        "strategy_id": STRATEGY_ID,
        "research_lineage_id": LINEAGE_ID,
        "research_kind": RESEARCH_KIND,
        "research_mode": RESEARCH_MODE,
        "strategy_family_id": FAMILY_ID,
        "asset_class": record.get("asset_class") or ASSET_CLASS,
        "symbol": SYMBOL,
        "run_status": record.get("research_status") or RESEARCH_STATE,
        "research_status": record.get("research_status") or RESEARCH_STATE,
        "research_state": record.get("research_status") or RESEARCH_STATE,
        "promotion_gate": record.get("promotion_gate") or PROMOTION_GATE,
        "holdout_status": record.get("holdout_status") or HOLDOUT_STATUS,
        "thesis": record.get("thesis"),
        "display_name": record.get("display_name") or "TLT Duration Momentum",
        "delivery_status": record.get("delivery_status") or "DELIVERED",
        "strategy_definition": record.get("strategy_definition") or {
            "display_name": "TLT Duration Momentum",
            "thesis": record.get("thesis"),
            "instrument": SYMBOL,
            "universe": [SYMBOL],
            "portfolio_behavior": "Hold TLT when the frozen model's signal is positive. Otherwise hold cash.",
            "features": ["ret_1", "sma_gap", "vol"],
            "lookback": 120,
            "target": "21-session forward TLT return",
            "models_searched": ["deterministic", "ridge", "elasticnet"],
            "winner": {"trial_id": SELECTED_TRIAL, "model_family": MODEL_FAMILY, "alpha": 0.1, "l1_ratio": 0.5},
            "baseline": {"trial_id": BASELINE_TRIAL, "label": "120-day SMA long/cash"},
            "validation": {
                "train_years": 5,
                "oos_years": 1,
                "first_oos_year": 2015,
                "last_oos_year": 2024,
                "inner_folds": 3,
                "embargo_sessions": 5,
            },
            "execution": {
                "signal_timing": record.get("signal_timing"),
                "fill_assumptions": record.get("fill_assumptions"),
            },
            "cost_model_id": record.get("cost_model_id") or COST_MODEL_ID,
            "metric_kind": "mean_across_windows",
        },
        "metric_kind": "mean_across_windows",
        "economic_gate": record.get("economic_gate") or ECONOMIC_GATE,
        "economic_pass": None,
        "holdout_accessed": False,
        "holdout_locked": True if record.get("holdout_locked") is None else bool(record.get("holdout_locked")),
        "thresholds_defined": False,
        "window_count": len(windows),
        "model_family": MODEL_FAMILY,
        "selected_candidate": SELECTED_TRIAL,
        "selected_trial_id": SELECTED_TRIAL,
        "baseline_trial_id": BASELINE_TRIAL,
        "search_space_hash": record.get("search_space_hash") or SEARCH_SPACE_HASH,
        "feature_schema_hash": record.get("feature_schema_hash") or FEATURE_SCHEMA_HASH,
        "config_fingerprint": record.get("fingerprint") or FINGERPRINT,
        "strategy_spec_hash": record.get("fingerprint") or FINGERPRINT,
        "cost_model_id": record.get("cost_model_id") or COST_MODEL_ID,
        "adapter_id": record.get("adapter_id"),
        "fill_assumptions": record.get("fill_assumptions"),
        "signal_timing": record.get("signal_timing"),
        "history_provider": "qc_cloud",
        "training_layer": record.get("training_layer") or "qc_cloud",
        "data_read_used": bool(record.get("data_read_used")),
        "official_windows": [
            {
                "window_id": window["window_id"],
                "contract_hash": window.get("contract_hash"),
                "train_backtest_id": window.get("train_backtest_id"),
                "winner_backtest_id": window.get("winner_backtest_id"),
                "baseline_backtest_id": window.get("baseline_backtest_id"),
                "selected_trial_id": window.get("selected_trial_id") or SELECTED_TRIAL,
            }
            for window in windows
        ],
        "provenance": provenance,
        "observation_provenance": record.get("observation_provenance") or "REAL_HISTORICAL_PRE_2025",
        "qc_creates_official": record.get("qc_creates_official") or 30,
        "project_id": record.get("project_id") or int(PROJECT_ID),
        "project_name": PROJECT_NAME,
        "ml_metrics": ml_mean,
        "baseline_metrics": means.get("baseline") if isinstance(means.get("baseline"), dict) else {},
        "ml_minus_baseline": means.get("ml_minus_baseline") if isinstance(means.get("ml_minus_baseline"), dict) else {},
        "selected_model_stability": aggregate.get("selected_model_stability"),
        "note": record.get("note"),
    }
    oos = {
        "research_run_id": RUN_ID,
        "strategy_id": STRATEGY_ID,
        "windows": windows,
        "window_count": len(windows),
        "sharpe_ratio": ml_mean.get("sharpe_ratio"),
        "holdout_excluded": True,
        "holdout_accessed": False,
        "latest_oos_end": windows[-1].get("oos_end") or windows[-1].get("end"),
        "provenance": provenance,
        "ml": ml_mean,
        "baseline": means.get("baseline") if isinstance(means.get("baseline"), dict) else {},
        "ml_minus_baseline": means.get("ml_minus_baseline") if isinstance(means.get("ml_minus_baseline"), dict) else {},
        "aggregate": means,
        "selected_model_stability": aggregate.get("selected_model_stability"),
        "positive_return_consistency": aggregate.get("positive_return_consistency"),
        "regime_dependence": aggregate.get("regime_dependence"),
    }
    trials = {
        "research_run_id": RUN_ID,
        "strategy_id": STRATEGY_ID,
        "selected_trial_id": SELECTED_TRIAL,
        "trial_count": 2,
        "candidates": [
            {
                "trial_id": SELECTED_TRIAL,
                "model_family": MODEL_FAMILY,
                "alpha": 0.1,
                "rejected": False,
            },
            {
                "trial_id": BASELINE_TRIAL,
                "model_family": "deterministic",
                "rejected": False,
            },
        ],
        "provenance": provenance,
    }
    experiments = {
        "research_run_id": RUN_ID,
        "strategy_id": STRATEGY_ID,
        "experiments": [
            {
                "experiment_id": window["window_id"],
                "contract_hash": window.get("contract_hash"),
                "train_backtest_id": window.get("train_backtest_id"),
                "winner_backtest_id": window.get("winner_backtest_id"),
                "baseline_backtest_id": window.get("baseline_backtest_id"),
                "selected_trial_id": window.get("selected_trial_id") or SELECTED_TRIAL,
                "oos_start": window.get("oos_start") or window.get("start"),
                "oos_end": window.get("oos_end") or window.get("end"),
            }
            for window in windows
        ],
        "provenance": provenance,
    }
    return [
        (
            "run_summary",
            _hashed_envelope(
                "run_summary",
                RUN_ID,
                summary,
                strategy_id=STRATEGY_ID,
                provenance=provenance,
                research_mode=RESEARCH_MODE,
            ),
        ),
        (
            "oos_aggregate",
            _hashed_envelope("oos_aggregate", RUN_ID, oos, strategy_id=STRATEGY_ID, provenance=provenance),
        ),
        (
            "trials",
            _hashed_envelope("trials", RUN_ID, trials, strategy_id=STRATEGY_ID, provenance=provenance),
        ),
        (
            "experiment_manifest",
            _hashed_envelope(
                "experiment_manifest",
                RUN_ID,
                experiments,
                strategy_id=STRATEGY_ID,
                provenance=provenance,
            ),
        ),
    ]


def register_tlt_monitor_strategy(conn) -> None:
    """Idempotent research-only Strategy Monitor row. No execution project."""
    conn.execute(
        text(REGISTER_STRATEGY_SQL),
        {
            "strategy_id": STRATEGY_ID,
            "name": "TLT Duration Momentum",
            "environment": "research",
            "status": RESEARCH_STATE,
            "qc_research_project_id": PROJECT_ID,
            "qc_research_project_name": PROJECT_NAME,
        },
    )


def platform_oos_window_frame(windows: list[dict[str, Any]] | None):
    from qc_research.ml_monitor_ui import platform_oos_window_frame as generic_frame

    return generic_frame(windows, selected_trial_fallback=SELECTED_TRIAL)


def assert_tlt_identity(payload: dict[str, Any]) -> None:
    if str(payload.get("strategy_id") or "") != STRATEGY_ID:
        raise ValueError("strategy_id is {0}, expected {1}".format(payload.get("strategy_id"), STRATEGY_ID))
    if str(payload.get("research_lineage_id") or "") != LINEAGE_ID:
        raise ValueError(
            "research_lineage_id is {0}, expected {1}".format(payload.get("research_lineage_id"), LINEAGE_ID)
        )
    if str(payload.get("research_kind") or RESEARCH_KIND) != RESEARCH_KIND:
        raise ValueError("research_kind is {0}, expected {1}".format(payload.get("research_kind"), RESEARCH_KIND))
    if str(payload.get("economic_gate") or "") != ECONOMIC_GATE:
        raise ValueError("economic_gate is {0}, expected {1}".format(payload.get("economic_gate"), ECONOMIC_GATE))
    if payload.get("economic_pass") is not None:
        raise ValueError("economic_pass must be NULL")
    if payload.get("holdout_accessed") not in {False, 0, "false", None}:
        raise ValueError("holdout_accessed must be false")


def query_tlt_identity(conn) -> dict[str, Any]:
    run = conn.execute(
        text(
            """
            SELECT
                research_run_id, strategy_id, research_lineage_id, research_kind,
                research_mode, asset_class, strategy_family_id, holdout_accessed,
                holdout_access_count, run_status
            FROM research_runs
            WHERE strategy_id = :strategy_id
               OR research_lineage_id = :lineage
               OR research_run_id = :run_id
            ORDER BY last_seen_at DESC NULLS LAST
            LIMIT 1
            """
        ),
        {"strategy_id": STRATEGY_ID, "lineage": LINEAGE_ID, "run_id": RUN_ID},
    ).mappings().first()
    if not run:
        raise ValueError("TLTDurationMomentum research_runs row is missing")
    return dict(run)


def query_tlt_windows(conn, research_run_id: str) -> list[dict[str, Any]]:
    rows = conn.execute(
        text(
            """
            SELECT outer_window_id, oos_start, oos_end, metrics_json
            FROM research_oos_windows
            WHERE research_run_id = :research_run_id
            ORDER BY outer_window_id
            """
        ),
        {"research_run_id": research_run_id},
    ).mappings().all()
    return [dict(row) for row in rows]


def query_tlt_experiments(conn, research_run_id: str) -> list[dict[str, Any]]:
    rows = conn.execute(
        text(
            """
            SELECT experiment_id, metadata_json
            FROM research_experiments
            WHERE research_run_id = :research_run_id
            ORDER BY experiment_id
            """
        ),
        {"research_run_id": research_run_id},
    ).mappings().all()
    return [dict(row) for row in rows]


def _as_dict(value: Any) -> dict[str, Any]:
    if value is None:
        return {}
    if isinstance(value, dict):
        return value
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
        except ValueError:
            return {}
        return parsed if isinstance(parsed, dict) else {}
    return {}


def verify_tlt_postgres(conn) -> dict[str, Any]:
    """Fail-closed query-back of the official 10-window identity."""
    identity = query_tlt_identity(conn)
    if str(identity.get("strategy_id") or "") != STRATEGY_ID:
        raise ValueError("persisted strategy_id is {0}".format(identity.get("strategy_id")))
    if str(identity.get("research_lineage_id") or "") != LINEAGE_ID:
        raise ValueError("persisted lineage is {0}".format(identity.get("research_lineage_id")))
    if str(identity.get("research_kind") or "") != RESEARCH_KIND:
        raise ValueError("persisted research_kind is {0}".format(identity.get("research_kind")))
    if identity.get("holdout_accessed") not in {False, 0}:
        raise ValueError("holdout_accessed must be false")
    if int(identity.get("holdout_access_count") or 0) != 0:
        raise ValueError("holdout_access_count must be 0")
    run_id = str(identity.get("research_run_id") or "")
    windows = query_tlt_windows(conn, run_id)
    found = {str(row.get("outer_window_id") or "") for row in windows}
    if found != set(WINDOW_IDS):
        raise ValueError("persisted windows are {0}, expected {1}".format(sorted(found), list(WINDOW_IDS)))
    for row in windows:
        metrics = _as_dict(row.get("metrics_json"))
        window_id = str(row.get("outer_window_id") or "")
        pinned = OFFICIAL_WINDOWS[window_id]
        for key, value in pinned.items():
            if str(metrics.get(key) or "") != value:
                raise ValueError("persisted {0} {1} is {2}".format(window_id, key, metrics.get(key)))
        end = str(row.get("oos_end") or metrics.get("oos_end") or "")
        if end.startswith("2025"):
            raise ValueError("{0} oos_end is in the sealed holdout".format(window_id))
    experiments = query_tlt_experiments(conn, run_id)
    exp_ids = {str(row.get("experiment_id") or "") for row in experiments}
    if not set(WINDOW_IDS).issubset(exp_ids):
        raise ValueError("research_experiments missing windows: {0}".format(sorted(set(WINDOW_IDS) - exp_ids)))
    summary_row = conn.execute(
        text(
            """
            SELECT payload_json
            FROM research_artifacts
            WHERE research_run_id = :research_run_id
              AND artifact_type = 'run_summary'
            ORDER BY synced_at DESC NULLS LAST
            LIMIT 1
            """
        ),
        {"research_run_id": run_id},
    ).mappings().first()
    if not summary_row:
        raise ValueError("run_summary artifact is missing")
    payload = _as_dict(summary_row.get("payload_json"))
    inner = payload.get("payload") if isinstance(payload.get("payload"), dict) else payload
    assert_tlt_identity(
        {
            "strategy_id": inner.get("strategy_id") or payload.get("strategy_id"),
            "research_lineage_id": inner.get("research_lineage_id"),
            "research_kind": inner.get("research_kind") or RESEARCH_KIND,
            "economic_gate": inner.get("economic_gate"),
            "economic_pass": inner.get("economic_pass"),
            "holdout_accessed": inner.get("holdout_accessed"),
        }
    )
    return {
        "research_run_id": run_id,
        "strategy_id": STRATEGY_ID,
        "research_lineage_id": LINEAGE_ID,
        "research_kind": RESEARCH_KIND,
        "economic_gate": ECONOMIC_GATE,
        "economic_pass": None,
        "holdout_accessed": False,
        "window_ids": list(WINDOW_IDS),
        "window_count": len(windows),
    }


def official_tlt_v0_identity_blockers(
    *,
    strategy_id: str | None,
    research_run_id: str | None,
    engine: Any = None,
) -> list[str]:
    """Blockers when the selected run is official TLT V0. Empty otherwise.

    Query failures fail closed. This is not an economic PASS/WATCH/FAIL.
    """
    if str(research_run_id or "").strip() != RUN_ID:
        return []
    if strategy_id and str(strategy_id) != STRATEGY_ID:
        return ["strategy_id_mismatch"]
    if engine is None:
        return ["identity_query_failed"]
    try:
        with engine.connect() as conn:
            verify_tlt_postgres(conn)
    except Exception as exc:
        text = str(exc).strip() or "identity_refused"
        return [text]
    return []


def verify_tlt_monitor_view(view: dict[str, Any] | None) -> dict[str, Any]:
    from qc_research.ml_monitor_ui import UNAVAILABLE
    from qc_research.platform_ingest import verify_monitor_view

    checked = verify_monitor_view(view)
    if str(checked.get("strategy_id") or "") != STRATEGY_ID:
        raise ValueError("monitor strategy_id is {0}".format(checked.get("strategy_id")))
    if str(checked.get("lineage") or "") != LINEAGE_ID:
        raise ValueError("monitor lineage is {0}".format(checked.get("lineage")))
    if str(checked.get("economic_gate") or "") != ECONOMIC_GATE:
        raise ValueError("monitor economic_gate is {0}".format(checked.get("economic_gate")))
    if checked.get("economic_pass") is not None:
        raise ValueError("monitor economic_pass must be NULL")
    if str(checked.get("research_status") or checked.get("research_state") or "") != RESEARCH_STATE:
        raise ValueError("monitor research_status is {0}".format(checked.get("research_status") or checked.get("research_state")))
    if str(checked.get("promotion_gate") or "") != PROMOTION_GATE:
        raise ValueError("monitor promotion_gate is {0}".format(checked.get("promotion_gate")))
    if str(checked.get("holdout_status") or "") != HOLDOUT_STATUS:
        raise ValueError("monitor holdout_status is {0}".format(checked.get("holdout_status")))
    if str(checked.get("strategy_family") or "") != FAMILY_ID:
        raise ValueError("monitor family is {0}".format(checked.get("strategy_family")))
    if str(checked.get("asset_class_label") or "") not in {ASSET_CLASS, "Bond ETF"}:
        raise ValueError("monitor asset is {0}".format(checked.get("asset_class_label")))
    if str(checked.get("model_family") or "").lower() != MODEL_FAMILY:
        raise ValueError("monitor model is {0}".format(checked.get("model_family")))
    if str(checked.get("selected_candidate") or "") != SELECTED_TRIAL:
        raise ValueError("monitor selected model is {0}".format(checked.get("selected_candidate")))
    if str(checked.get("baseline") or "") != BASELINE_TRIAL:
        raise ValueError("monitor baseline is {0}".format(checked.get("baseline")))
    windows = checked.get("oos_windows")
    if windows is None or windows == UNAVAILABLE or not isinstance(windows, list) or len(windows) != 10:
        raise ValueError("monitor must show 10 OOS windows")
    ids = [str(row.get("window_id") or "") for row in windows]
    if ids != list(WINDOW_IDS):
        raise ValueError("monitor windows are {0}".format(ids))
    for row in windows:
        end = str(row.get("oos_end") or row.get("end") or "")
        if end.startswith("2025"):
            raise ValueError("monitor includes sealed 2025+ window {0}".format(row.get("window_id")))
    if checked.get("holdout_accessed") not in {False, 0, "no", "false"}:
        raise ValueError("monitor holdout_accessed must be false")
    return checked


def default_artifact_path() -> Path:
    return Path(__file__).resolve().parent / "platform_artifacts" / "tlt_duration_momentum.json"


def sibling_quant_strategies_artifact() -> Path | None:
    here = Path(__file__).resolve()
    for parent in here.parents:
        candidate = parent / "quant-strategies" / "research" / "platform_smokes" / "tlt_duration_momentum.json"
        if candidate.is_file():
            return candidate
        candidate = parent / "repos" / "quant-strategies" / "research" / "platform_smokes" / "tlt_duration_momentum.json"
        if candidate.is_file():
            return candidate
    return None
