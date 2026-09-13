"""Import stability after SQL/lifecycle extraction. No economics changes."""

from __future__ import annotations

from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_live_monitor_loaders_are_read_only_and_engine_optional():
    from qc_research.read_models.monitor_queries import (
        BACKTESTS_SQL,
        STRATEGIES_SQL,
        load_backtests_frame,
        load_equity_history_frame,
        load_latest_snapshot_row,
        load_research_run_row,
        load_strategies_frame,
    )

    assert "FROM strategies" in STRATEGIES_SQL
    assert "FROM backtests" in BACKTESTS_SQL
    assert "INSERT" not in STRATEGIES_SQL.upper()
    assert load_strategies_frame(None).empty
    assert load_backtests_frame(None, "SPYTrend").empty
    assert load_equity_history_frame(None, "SPYTrend").empty
    assert load_latest_snapshot_row(None, "SPYTrend") is None
    assert load_research_run_row(None, "run") is None


def test_monitor_loaders_reexport_from_ml_monitor_ui():
    from qc_research.ml_monitor_ui import (
        PLATFORM_RUN_IDS_SQL,
        load_platform_run_ids,
        load_stage2_artifact_payload,
        load_stage2_feature_diagnostics,
        load_stage2_models,
        load_stage2_run_ids,
        load_stage2_signal_points,
        load_stage2_trials,
    )
    from qc_research.read_models.monitor_queries import (
        load_platform_run_ids as query_platform,
        load_stage2_run_ids as query_stage2,
    )

    assert load_platform_run_ids is query_platform
    assert load_stage2_run_ids is query_stage2
    assert "research_artifacts" in PLATFORM_RUN_IDS_SQL
    assert load_stage2_trials(None, "x").empty
    assert load_stage2_models(None, "x").empty
    assert load_stage2_feature_diagnostics(None, "x").empty
    assert load_stage2_signal_points(None, "x").empty
    assert load_stage2_artifact_payload(None, "x", "run_summary") is None
    assert load_stage2_run_ids(None, "CrossSectionalFactorML") == []
    assert load_platform_run_ids(None, "TLTDurationMomentum") == []


def test_stage2_upsert_sql_reexport_from_object_store_sync():
    from qc_research.ingest.stage2_sql import (
        UPSERT_ARTIFACT_SQL as SRC,
        update_run_metadata as src_update,
    )
    from qc_research.object_store_sync import UPSERT_ARTIFACT_SQL, update_run_metadata

    assert UPSERT_ARTIFACT_SQL is SRC
    assert update_run_metadata is src_update
    assert "ON CONFLICT" in SRC
    assert "promotion_gate" in (ROOT / "qc_research" / "ingest" / "stage2_sql.py").read_text(encoding="utf-8")


def test_streamlit_pages_do_not_import_ingest_sql():
    monitor = (ROOT / "pages" / "strategy_monitor.py").read_text(encoding="utf-8")
    dashboard = (ROOT / "dashboard.py").read_text(encoding="utf-8")
    assert "qc_research.ingest" not in monitor
    assert "object_store_sync" not in monitor
    assert "writer_db" not in monitor
    assert "qc_research.ingest" not in dashboard
    assert "writer_db" not in dashboard
    assert "load_streamlit_env" in dashboard
    legacy = (ROOT / "legacy_fmp_dashboard.py").read_text(encoding="utf-8")
    assert "def _background_warm_enabled" in legacy
    assert "import data_loader" not in dashboard
    forbidden = (
        "writer_db",
        "object_store_sync",
        "qc_research.ingest",
        "market_intelligence.writer_db",
        "from market_intelligence.ideas import",
    )
    for path in (ROOT / "pages").glob("*.py"):
        text = path.read_text(encoding="utf-8")
        for token in forbidden:
            assert token not in text, "{0} imports {1}".format(path.name, token)
        assert "load_streamlit_env" in text, "{0} must load_streamlit_env to clear writer fallback".format(
            path.name
        )
    ui = (ROOT / "qc_research" / "ml_monitor_ui.py").read_text(encoding="utf-8")
    assert "from qc_research.read_models.monitor_queries import" in ui
    assert "INSERT INTO" not in ui
    assert "from qc_research.read_models.monitor_queries import" in monitor
    assert "INSERT INTO" not in monitor
    assert "def load_strategies():" in monitor
    assert "def load_backtests(" in monitor
    store = (ROOT / "qc_research" / "object_store_sync.py").read_text(encoding="utf-8")
    assert "from qc_research.ingest.stage2_sql import" in store
    assert "def ingest_artifact" in store
