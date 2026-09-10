"""Strategy Monitor library and investment-question layout (no QuantConnect)."""

from __future__ import annotations

from pathlib import Path

import pytest
from qc_research.research_library import (
    OfficialResearchIdentityError,
    filter_library,
    official_research_identity_blockers,
    refuse_official_research_identity,
)
import pandas as pd


ROOT = Path(__file__).resolve().parents[1]
MONITOR = (ROOT / "pages" / "strategy_monitor.py").read_text(encoding="utf-8")
PLATFORM = (ROOT / "qc_research" / "ml_monitor_ui.py").read_text(encoding="utf-8")
UI = (ROOT / "qc_research" / "monitor_ui.py").read_text(encoding="utf-8")


def test_monitor_has_library_filters_and_stable_selection():
    assert "Research library" in MONITOR
    assert 'key="strategy_monitor_selected_strategy"' in MONITOR
    assert 'key="strategy_monitor_asset_class"' in MONITOR
    assert 'key="strategy_monitor_research_status"' in MONITOR
    assert 'key="strategy_monitor_include_smoke"' in MONITOR
    assert "latest completed eligible non-holdout" in MONITOR
    assert "highest-performing" in MONITOR
    assert "strategy_monitor_last_strategy" in MONITOR
    assert "This page does not launch backtests" in MONITOR
    assert "dashboard_engine" in MONITOR
    assert "from db.dashboard_engine import DashboardIdentityError, dashboard_engine, load_streamlit_env" in MONITOR
    assert "load_streamlit_env()" in MONITOR
    assert "strip_writer_database_env()" not in MONITOR
    assert "DASHBOARD_ALLOW_WRITER_FALLBACK" in MONITOR
    assert "st.stop()" in MONITOR
    assert "arrow_safe_frame" in MONITOR
    assert "UNAVAILABLE" in MONITOR
    assert "will not treat a query failure as an empty research library" in MONITOR


def test_monitor_does_not_open_or_launch_holdout():
    assert "authorize final holdout" not in MONITOR.lower()
    assert "Open holdout" not in MONITOR
    assert "launch holdout" not in MONITOR.lower()
    assert "create_backtest" not in MONITOR


def test_platform_view_uses_investment_tabs_and_readout():
    assert '["Summary", "Performance", "Robustness / OOS", "Research Details"]' in PLATFORM
    assert "build_readout" in PLATFORM
    assert "comparison_table" in PLATFORM
    assert "Completed research is not labeled approved" in PLATFORM
    assert "percentage points, not alpha" in PLATFORM
    library = (ROOT / "qc_research" / "research_library.py").read_text(encoding="utf-8")
    assert "csfml_v1_historical_impact_for_run" in library
    assert "label_integrity" in PLATFORM
    assert "official_csfml_v1_identity_blockers" in PLATFORM
    assert "official_tlt_v0_identity_blockers" in PLATFORM
    assert "This is not an economic PASS/WATCH/FAIL" in PLATFORM
    assert "official_stage1_identity_blockers" in UI
    assert "This is not an economic PASS/WATCH/FAIL" in UI
    assert "engine=engine" in MONITOR.split("render_stage1_section(", 1)[1]
    assert "OfficialResearchIdentityError" in MONITOR
    assert "official_research_identity_blockers" in (ROOT / "qc_research" / "research_library.py").read_text(
        encoding="utf-8"
    )


def test_filter_library_keeps_failed_and_hides_smoke_by_default():
    frame = pd.DataFrame(
        [
            {"strategy_id": "A", "asset_class": "Equity", "run_status": "COMPLETE", "failed": False, "is_smoke": False},
            {"strategy_id": "B", "asset_class": "Equity", "run_status": "FAILED", "failed": True, "is_smoke": False},
            {"strategy_id": "C", "asset_class": "Bond ETF", "run_status": "COMPLETE", "failed": False, "is_smoke": True},
        ]
    )
    default = filter_library(frame)
    assert set(default["strategy_id"]) == {"A", "B"}
    failed = filter_library(frame, research_status="Failed")
    assert list(failed["strategy_id"]) == ["B"]
    smoke = filter_library(frame, include_smoke=True)
    assert set(smoke["strategy_id"]) == {"A", "B", "C"}


def test_official_research_identity_blockers_are_run_scoped():
    from qc_research.contracts.label_integrity import load_csfml_v1_label_integrity

    pin = load_csfml_v1_label_integrity()
    other = pd.DataFrame(
        [
            {
                "strategy_id": "SPYTrend",
                "research_run_id": "STAGE1_SPYTrend_other",
                "git_commit": "0" * 40,
                "run_status": "COMPLETE",
            }
        ]
    )
    assert official_research_identity_blockers(other) == []
    official_stage1 = pd.DataFrame(
        [
            {
                "strategy_id": "SPYTrend",
                "research_run_id": "STAGE1_SPYTrend_c04553d8",
                "git_commit": "f04dbfb1a936c753a42a1389d9181f7c22f551a3",
                "run_status": "COMPLETE",
                "expected_experiment_count": 81,
                "completed_count": 81,
                "failed_count": 0,
                "skipped_count": 0,
                "holdout_accessed": False,
                "holdout_access_count": 0,
            }
        ]
    )
    assert official_research_identity_blockers(official_stage1) == []
    drifted = official_research_identity_blockers(
        pd.DataFrame(
            [
                {
                    "strategy_id": "SPYTrend",
                    "research_run_id": "STAGE1_SPYTrend_c04553d8",
                    "git_commit": "0" * 40,
                    "run_status": "COMPLETE",
                    "expected_experiment_count": 81,
                    "completed_count": 1,
                    "failed_count": 0,
                    "skipped_count": 0,
                    "holdout_accessed": False,
                    "holdout_access_count": 0,
                }
            ]
        )
    )
    assert any("git_commit" in item for item in drifted)
    assert any("completed_count" in item for item in drifted)
    official_csfml = pd.DataFrame(
        [
            {
                "strategy_id": pin["strategy_id"],
                "research_run_id": pin["full_suite_run_id"],
                "git_commit": pin["authoritative_csfml_v1_qc_sha"],
                "holdout_accessed": False,
                "holdout_access_count": 0,
                "economic_gate": "NOT_DEFINED",
            }
        ]
    )
    assert official_research_identity_blockers(official_csfml) == []
    with pytest.raises(OfficialResearchIdentityError, match="This is not an economic PASS/WATCH/FAIL"):
        refuse_official_research_identity(
            pd.DataFrame(
                [
                    {
                        "strategy_id": pin["strategy_id"],
                        "research_run_id": pin["full_suite_run_id"],
                        "git_commit": "0" * 40,
                        "holdout_accessed": False,
                        "holdout_access_count": 0,
                        "economic_gate": "NOT_DEFINED",
                    }
                ]
            )
        )
    tlt = official_research_identity_blockers(
        pd.DataFrame(
            [
                {
                    "strategy_id": "TLTDurationMomentum",
                    "research_run_id": "PLATFORM_TLTDurationMomentum_V0",
                }
            ]
        )
    )
    assert any("identity_query_failed" in item for item in tlt)


def test_load_research_library_refuses_drifted_official_stage1(monkeypatch):
    from qc_research import research_library as lib

    official = pd.DataFrame(
        [
            {
                "strategy_id": "SPYTrend",
                "research_run_id": "STAGE1_SPYTrend_c04553d8",
                "research_kind": "stage1",
                "research_mode": None,
                "asset_class": "Equity",
                "run_status": "COMPLETE",
                "economic_gate": "NOT_DEFINED",
                "promotion_gate": None,
                "holdout_status": "LOCKED",
                "holdout_accessed": False,
                "holdout_access_count": 0,
                "delivery_status": None,
                "last_seen_at": None,
                "git_commit": "0" * 40,
                "completed_count": 1,
                "failed_count": 0,
                "skipped_count": 0,
                "expected_experiment_count": 81,
                "synced_experiment_count": 1,
            }
        ]
    )

    def _read(_engine, sql, params=None, expanding=()):
        if "FROM research_artifacts" in sql:
            return pd.DataFrame()
        return official

    monkeypatch.setattr(lib, "_read_sql", _read)
    with pytest.raises(OfficialResearchIdentityError, match="git_commit"):
        lib.load_research_library(object())
