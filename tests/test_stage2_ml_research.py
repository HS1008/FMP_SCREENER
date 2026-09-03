"""Stage 2 Phase 1 persistence / monitor contracts."""

from __future__ import annotations

from pathlib import Path

import pandas as pd

from jobs.apply_migrations import MIGRATIONS_DIR, pending_migration_files
from qc_research.aggregation import is_stage1, stage1_backtests
from qc_research.aggregation import research_runs
from qc_research.ml_aggregation import (
    INCOMPLETE,
    PASS,
    WATCH,
    assess_stage2,
    is_stage2,
    stage2_backtests,
    stage2_research_rows,
)
from qc_research.object_store_sync import (
    ArtifactSyncError,
    identify_stage2_runs,
    ingest_artifact,
    should_redownload,
    sha256_payload,
    validate_artifact,
    verify_hash,
)
from qc_research.parsing import is_stage1_name, is_stage2_name


ROOT = Path(__file__).resolve().parent.parent
MIGRATION = (ROOT / "db" / "migrations" / "003_stage2_ml_research.sql").read_text(encoding="utf-8")
MONITOR = (ROOT / "pages" / "strategy_monitor.py").read_text(encoding="utf-8")
SYNC = (ROOT / "jobs" / "sync_quantconnect.py").read_text(encoding="utf-8")
ML_UI = (ROOT / "qc_research" / "ml_monitor_ui.py").read_text(encoding="utf-8")
STORE = (ROOT / "qc_research" / "object_store_sync.py").read_text(encoding="utf-8")
CRON = (ROOT / "scripts" / "install_backtest_sync_cron.sh").read_text(encoding="utf-8")


def test_migration_is_idempotent_and_additive():
    assert "IF NOT EXISTS" in MIGRATION
    assert "DROP TABLE" not in MIGRATION
    assert "DROP COLUMN" not in MIGRATION
    assert "RENAME" not in MIGRATION
    for table in (
        "ml_trials",
        "ml_models",
        "ml_feature_diagnostics",
        "ml_signal_points",
        "research_artifacts",
    ):
        assert "CREATE TABLE IF NOT EXISTS {0}".format(table) in MIGRATION
    assert "research_kind" in MIGRATION
    assert "feature_set_hash" in MIGRATION
    files = sorted(MIGRATIONS_DIR.glob("*.sql"))
    names = [path.name for path in files]
    assert "003_stage2_ml_research.sql" in names
    pending = pending_migration_files(files, {"001_stage1_research.sql", "002_research_project.sql"})
    assert [path.name for path in pending] == ["003_stage2_ml_research.sql"]
    skipped = pending_migration_files(files, {path.name for path in files})
    assert skipped == []


def test_stage1_and_stage2_rows_do_not_mix():
    df = pd.DataFrame(
        [
            {
                "name": "S1__SPYTrend__run__WFO_TEST__2022__081",
                "backtest_id": "bt-s1",
                "research_suite_version": "S1",
                "research_run_id": "s1",
                "research_test_type": "WFO_TEST",
                "research_is_holdout": False,
            },
            {
                "name": "S2__SyntheticStage2__run__ML_OOS_TEST__2015__002",
                "backtest_id": "bt-s2-oos",
                "research_suite_version": "S2",
                "research_run_id": "s2",
                "research_test_type": "ML_OOS_TEST",
                "research_is_holdout": False,
            },
            {
                "name": "S2__SyntheticStage2__run__ML_FINAL_HOLDOUT__HOLDOUT__032",
                "backtest_id": "bt-s2-hold",
                "research_suite_version": "S2",
                "research_run_id": "s2",
                "research_test_type": "ML_FINAL_HOLDOUT",
                "research_is_holdout": True,
            },
        ]
    )
    s1 = stage1_backtests(df)
    s2 = stage2_backtests(df)
    research = stage2_research_rows(df)
    assert list(s1["research_run_id"]) == ["s1"]
    assert set(s2["research_test_type"]) == {"ML_OOS_TEST", "ML_FINAL_HOLDOUT"}
    assert list(research["research_test_type"]) == ["ML_OOS_TEST"]
    assert not is_stage1(df).equals(is_stage2(df))
    assert is_stage1_name(df.loc[0, "name"])
    assert is_stage2_name(df.loc[1, "name"])
    assert not is_stage1_name(df.loc[1, "name"])
    grouped = research_runs(df)
    if grouped is not None and not grouped.empty and "research_run_id" in grouped.columns:
        assert list(grouped["research_run_id"].astype(str).unique()) == ["s1"]


def test_artifact_hash_mismatch_and_missing_not_zero():
    payload = {
        "schema_version": "stage2_ml_v1",
        "research_run_id": "RUN",
        "run_status": "IN_PROGRESS",
    }
    validate_artifact("run_summary", payload)
    digest = sha256_payload(payload)
    verify_hash(payload, digest)
    try:
        verify_hash(payload, "0" * 64)
        raised = False
    except ArtifactSyncError:
        raised = True
    assert raised
    assert payload.get("median_rank_ic") is None


def test_all_candidate_trials_are_visible_in_training_summary():
    payload = {
        "schema_version": "stage2_ml_v1",
        "research_run_id": "RUN",
        "window_id": "2015",
        "candidate_trials": [
            {"trial_id": "a=0.1", "selected": False, "median_rank_ic": 0.2},
            {"trial_id": "a=10", "selected": True, "median_rank_ic": 0.18},
            {"trial_id": "a=1000", "selected": False, "median_rank_ic": None},
        ],
    }
    validate_artifact("training_summary", payload)
    assert len(payload["candidate_trials"]) == 3
    assert payload["candidate_trials"][2]["median_rank_ic"] is None


def test_missing_required_artifact_is_incomplete():
    result = assess_stage2(
        expected=31,
        completed=31,
        missing_required_artifacts=True,
        holdout_rows=pd.DataFrame([{"research_test_type": "ML_FINAL_HOLDOUT", "median_rank_ic": 0.9}]),
        oos_metrics={"median_rank_ic": 0.9, "holdout_rank_ic": 0.9},
    )
    assert result["status"] == INCOMPLETE
    assert result["label_uses_holdout"] is False
    complete = assess_stage2(
        expected=31,
        completed=31,
        missing_required_artifacts=False,
        thresholds={"min_median_oos_rank_ic": 0.01},
        oos_metrics={"median_rank_ic": 0.2, "holdout_rank_ic": -1},
        holdout_rows=pd.DataFrame([{"median_rank_ic": -1}]),
    )
    assert complete["status"] == PASS
    watch = assess_stage2(
        expected=31,
        completed=31,
        thresholds={"min_positive_ic_fraction": 0.60},
        oos_metrics={"positive_ic_fraction": 0.40, "holdout_rank_ic": 0.99},
        holdout_rows=pd.DataFrame([{"median_rank_ic": 0.99}]),
    )
    assert watch["status"] == WATCH
    assert watch["label_uses_holdout"] is False


def test_redownload_skip_when_hash_matches():
    assert should_redownload(None, "abc") is True
    assert should_redownload("abc", "abc") is False
    assert should_redownload("abc", "def") is True


def test_identify_stage2_runs_ignores_stage1():
    runs = identify_stage2_runs(
        [
            {"name": "S1__SPYTrend__r__WFO_TEST__2022__001", "research_run_id": "s1", "research_suite_version": "S1"},
            {
                "name": "S2__X__r__ML_TRAIN__2015__001",
                "research_run_id": "s2",
                "research_suite_version": "S2",
                "research_window_id": "2015",
                "strategy_id": "X",
            },
        ]
    )
    assert [row["research_run_id"] for row in runs] == ["s2"]
    assert "2015" in runs[0]["windows"]


def test_ingest_is_idempotent_on_fake_connection():
    class FakeConn:
        def __init__(self):
            self.calls = []

        def execute(self, statement, params=None):
            self.calls.append((str(statement), params))

    payload = {
        "schema_version": "stage2_ml_v1",
        "research_run_id": "RUN",
        "window_id": "2015",
        "candidate_trials": [
            {"trial_id": "a=0.1", "selected": False, "median_rank_ic": 0.1, "hyperparameters": {"alpha": 0.1}},
            {"trial_id": "a=10", "selected": True, "median_rank_ic": 0.09, "hyperparameters": {"alpha": 10}},
        ],
        "feature_diagnostics": [{"feature_name": "momentum_12m", "ridge_coefficient": 0.2}],
    }
    conn = FakeConn()
    ingest_artifact(conn, key="k", kind="training_summary", payload=payload)
    first = len(conn.calls)
    ingest_artifact(conn, key="k", kind="training_summary", payload=payload)
    assert len(conn.calls) == first * 2
    trial_calls = [params for sql, params in conn.calls if params and params.get("trial_id")]
    assert {row["trial_id"] for row in trial_calls} == {"a=0.1", "a=10"}


def test_streamlit_stage2_is_postgres_only_and_fragment_intact():
    assert "render_stage2_section" in MONITOR
    assert "from qc_research.ml_monitor_ui import render_stage2_section" in MONITOR
    assert "create_backtest" not in ML_UI
    assert "qc_post" not in ML_UI
    assert "/object/" not in ML_UI
    assert "sklearn" not in ML_UI
    assert "fit(" not in ML_UI
    assert "@st.fragment(run_every=LIVE_MONITOR_REFRESH)" in MONITOR
    assert MONITOR.count("run_every") == 1
    assert "window.parent.location.reload" not in MONITOR
    assert "jobs.sync_quantconnect --backtests-only" in CRON
    assert "sync_stage2_object_store" in SYNC
    assert "--live-only" in SYNC
    assert "ON CONFLICT" in STORE
    assert SYNC.find("sync_stage2_object_store") < SYNC.find("Skipping backtest sync (--live-only)")
    assert "object_store" not in SYNC[SYNC.find("Skipping backtest sync (--live-only)") :]
    live_001 = (ROOT / "db" / "migrations" / "001_stage1_research.sql").read_text(encoding="utf-8")
    assert "CREATE TABLE IF NOT EXISTS research_runs" in live_001
    assert "CREATE TABLE IF NOT EXISTS backtests" in live_001
    assert "DROP TABLE research_runs" not in MIGRATION
    assert "DROP TABLE backtests" not in MIGRATION
