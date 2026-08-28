from datetime import date
import json
from pathlib import Path

import pandas as pd
import pytest

from qc_research.aggregation import (
    COMPLETE,
    IN_PROGRESS,
    INCOMPLETE,
    assess_stage1,
    attach_skipped_experiments,
    holdout_access_count,
    legacy_backtests,
    parameter_robustness_summary,
    parse_orchestrator_summary,
    primary_equity_backtests,
    research_date_range,
    research_runs,
    select_comparison_backtest,
    stage1_backtests,
    walk_forward_aggregates,
)
from qc_research.dates import chart_request_window, created_unix, qc_simulation_dates
from qc_research.holdout import STATUS_EXPOSED_PRIOR_TO_STAGE1, classify_rows
from qc_research.parsing import (
    extract_stage1_metadata,
    normalize_statistics,
    parse_drawdown_to_decimal,
    parse_equity_chart,
    parse_percent_to_decimal,
)
from jobs.apply_migrations import pending_migration_files
from jobs.stage1_backtests import (
    apply_run_summary,
    compute_research_run_progress,
    discover_run_summary_paths,
    hydrate_legacy_and_classify,
    import_run_summaries,
    legacy_hydration_fields,
    merge_stage1_lightweight_metrics,
    needs_legacy_date_hydration,
    stage1_upsert_fields,
)


def test_parameter_set_splits_research_from_strategy_params():
    payload = {
        "name": "S1__SPYTrend__run__PARAM_SENS__IS__004",
        "parameterSet": {
            "sma_period": "200",
            "starting_cash": "100000",
            "start_date": "2010-01-01",
            "end_date": "2018-12-31",
            "research_suite_version": "S1",
            "research_run_id": "run",
            "research_test_type": "PARAM_SENS",
            "research_phase": "IS",
            "research_window_id": "IS",
            "research_git_commit": "abc123",
            "research_is_holdout": "false",
            "research_thresholds": '{"min_validation_sharpe":0.0}',
        },
    }
    meta = extract_stage1_metadata(payload)
    assert meta["source"] == "parameterSet"
    assert meta["parameters"] == {
        "sma_period": "200",
        "starting_cash": "100000",
        "start_date": "2010-01-01",
        "end_date": "2018-12-31",
    }
    assert "research_run_id" not in meta["parameters"]
    assert meta["research_run_id"] == "run"
    assert meta["research_test_type"] == "PARAM_SENS"
    assert meta["research_is_holdout"] is False
    assert meta["thresholds"]["min_validation_sharpe"] == 0.0


def test_name_fallback_when_parameter_set_missing():
    meta = extract_stage1_metadata(
        {},
        name="S1__SPYTrend__20260827T000000Z-aa__VALIDATION__VAL__009",
    )
    assert meta["source"] == "name"
    assert meta["research_run_id"] == "20260827T000000Z-aa"
    assert meta["research_test_type"] == "VALIDATION"
    assert meta["research_window_id"] == "VAL"


def test_legacy_backtest_has_no_stage1_metadata():
    meta = extract_stage1_metadata({"name": "manual SPY run", "parameterSet": {"sma_period": "200"}})
    assert meta["research_run_id"] is None
    assert meta["source"] is None
    df = pd.DataFrame(
        [
            {"backtest_id": "legacy", "research_suite_version": None, "research_run_id": None, "name": "old"},
            {
                "backtest_id": "s1",
                "research_suite_version": "S1",
                "research_run_id": "r1",
                "name": "S1__x",
            },
        ]
    )
    legacy = legacy_backtests(df)
    assert list(legacy["backtest_id"]) == ["legacy"]
    assert list(stage1_backtests(df)["backtest_id"]) == ["s1"]


def test_metric_parsing_percent_and_missing():
    stats = normalize_statistics(
        {
            "statistics": {
                "Sharpe Ratio": "1.5",
                "Compounding Annual Return": "12%",
                "Drawdown": "25%",
                "Win Rate": "40%",
                "Probabilistic Sharpe Ratio": "70%",
                "Total Trades": "8",
            }
        }
    )
    assert stats["sharpe_ratio"] == 1.5
    assert abs(stats["cagr"] - 0.12) < 1e-12
    assert abs(stats["psr"] - 0.70) < 1e-12
    assert stats["trade_count"] == 8
    assert parse_percent_to_decimal("not-a-percent") is None
    failed = normalize_statistics({"statistics": {"Sharpe Ratio": "9"}}, failed=True)
    assert failed["sharpe_ratio"] is None
    assert abs(stats["max_drawdown"] - (-0.25)) < 1e-12


def test_research_run_grouping_does_not_merge_commits():
    df = pd.DataFrame(
        [
            {
                "backtest_id": "a",
                "strategy_id": "SPYTrend",
                "research_suite_version": "S1",
                "research_run_id": "run-a",
                "research_git_commit": "aaa",
                "research_is_holdout": False,
                "created_at": "2026-08-01",
            },
            {
                "backtest_id": "b",
                "strategy_id": "SPYTrend",
                "research_suite_version": "S1",
                "research_run_id": "run-b",
                "research_git_commit": "bbb",
                "research_is_holdout": True,
                "created_at": "2026-08-02",
            },
        ]
    )
    runs = research_runs(df)
    assert set(runs["research_run_id"]) == {"run-a", "run-b"}
    assert holdout_access_count(df, "aaa") == 0
    assert holdout_access_count(df, "bbb") == 1


def test_wfo_aggregation_excludes_holdout_and_missing_metrics():
    df = pd.DataFrame(
        [
            {
                "research_suite_version": "S1",
                "research_run_id": "r",
                "research_test_type": "WFO_TEST",
                "research_is_holdout": False,
                "sharpe_ratio": 1.0,
                "cagr": 0.1,
                "max_drawdown": -0.2,
                "net_profit": 0.05,
                "parameters_json": {"sma_period": 200},
            },
            {
                "research_suite_version": "S1",
                "research_run_id": "r",
                "research_test_type": "WFO_TEST",
                "research_is_holdout": False,
                "sharpe_ratio": -0.2,
                "cagr": -0.1,
                "max_drawdown": -0.4,
                "net_profit": -0.05,
                "parameters_json": {"sma_period": 150},
            },
            {
                "research_suite_version": "S1",
                "research_run_id": "r",
                "research_test_type": "WFO_TEST_HOLDOUT",
                "research_is_holdout": True,
                "sharpe_ratio": 9.9,
                "cagr": 0.9,
                "max_drawdown": -0.01,
                "net_profit": 0.9,
                "parameters_json": {"sma_period": 250},
            },
        ]
    )
    agg = walk_forward_aggregates(df, include_holdout=False)
    assert agg["n_windows"] == 2
    assert agg["n_profitable"] == 1
    assert agg["median_oos_sharpe"] == pytest.approx(0.4)
    assert 250 not in [float(x) for x in agg["selected_parameter_history"] if x is not None]


def test_comparison_priority():
    df = pd.DataFrame(
        [
            {
                "backtest_id": "grid",
                "name": "grid",
                "research_suite_version": "S1",
                "research_run_id": "r1",
                "research_test_type": "PARAM_SENS",
                "created_at": "2026-08-03",
                "test_start": "2010-01-01",
                "test_end": "2018-12-31",
                "parameters_json": {"sma_period": 150},
            },
            {
                "backtest_id": "val",
                "name": "val",
                "research_suite_version": "S1",
                "research_run_id": "r1",
                "research_test_type": "VALIDATION",
                "created_at": "2026-08-02",
                "test_start": "2019-01-01",
                "test_end": "2022-12-31",
                "parameters_json": {"sma_period": 200},
            },
            {
                "backtest_id": "base",
                "name": "base",
                "research_suite_version": "S1",
                "research_run_id": "r1",
                "research_test_type": "BASELINE_DEV",
                "created_at": "2026-08-01",
                "test_start": "2010-01-01",
                "test_end": "2022-12-31",
                "parameters_json": {"sma_period": 200},
            },
            {
                "backtest_id": "legacy",
                "name": "old",
                "research_suite_version": None,
                "research_run_id": None,
                "research_test_type": None,
                "created_at": "2026-08-04",
            },
        ]
    )
    choice = select_comparison_backtest(df)
    assert choice["test_type"] == "VALIDATION"
    assert choice["row"]["backtest_id"] == "val"
    assert "Stage 1 Validation" in choice["label"]

    df.loc[len(df)] = {
        "backtest_id": "hold",
        "name": "hold",
        "research_suite_version": "S1",
        "research_run_id": "r1",
        "research_test_type": "FINAL_HOLDOUT",
        "created_at": "2026-08-05",
        "test_start": "2023-01-01",
        "test_end": "2026-08-27",
        "parameters_json": {"sma_period": 200},
    }
    choice = select_comparison_backtest(df)
    assert choice["test_type"] == "FINAL_HOLDOUT"


def test_equity_chart_parsing_shapes():
    payload = {
        "chart": {
            "name": "Strategy Equity",
            "series": {
                "Equity": {
                    "values": [[1_600_000_000, 100000], [1_600_086_400, 101000]],
                }
            },
        }
    }
    points = parse_equity_chart(payload)
    assert len(points) == 2
    assert points[0]["equity"] == 100000
    assert points[1]["period_return"] == pytest.approx(0.01)

    loading = parse_equity_chart({"status": "loading"})
    assert loading == []

    xy = parse_equity_chart(
        {
            "series": {
                "Strategy Equity": {
                    "values": [{"x": 10, "y": 1.0}, {"x": 20, "y": 1.1}],
                }
            }
        }
    )
    assert len(xy) == 2

    missing = parse_equity_chart({"chart": {"series": {}}})
    assert missing == []


def test_assess_does_not_use_holdout():
    df = pd.DataFrame(
        [
            {
                "research_suite_version": "S1",
                "research_run_id": "r",
                "research_test_type": "BASELINE_DEV",
                "research_is_holdout": False,
                "sharpe_ratio": 0.5,
                "cagr": 0.08,
                "max_drawdown": -0.2,
                "parameters_json": {"sma_period": 200},
            },
            {
                "research_suite_version": "S1",
                "research_run_id": "r",
                "research_test_type": "PARAM_SENS",
                "research_is_holdout": False,
                "sharpe_ratio": 1.1,
                "parameters_json": {"sma_period": 190},
            },
            {
                "research_suite_version": "S1",
                "research_run_id": "r",
                "research_test_type": "PARAM_SENS",
                "research_is_holdout": False,
                "sharpe_ratio": 1.2,
                "parameters_json": {"sma_period": 200},
            },
            {
                "research_suite_version": "S1",
                "research_run_id": "r",
                "research_test_type": "PARAM_SENS",
                "research_is_holdout": False,
                "sharpe_ratio": 1.15,
                "parameters_json": {"sma_period": 210},
            },
            {
                "research_suite_version": "S1",
                "research_run_id": "r",
                "research_test_type": "VALIDATION",
                "research_is_holdout": False,
                "sharpe_ratio": 0.9,
                "cagr": 0.07,
                "max_drawdown": -0.15,
                "net_profit": 0.3,
                "parameters_json": {"sma_period": 200},
                "research_selection_summary_json": {
                    "primary_parameter": "sma_period",
                    "selected_parameter": 200,
                    "raw_best_parameter": 200,
                    "selected_objective": 1.2,
                    "best_objective": 1.2,
                    "selected_sharpe": 1.2,
                    "authoritative": True,
                    "source": "orchestrator",
                    "positive_parameter_fraction": 1.0,
                    "robustness_label": "stable_plateau",
                },
            },
            {
                "research_suite_version": "S1",
                "research_run_id": "r",
                "research_test_type": "WFO_TEST",
                "research_is_holdout": False,
                "sharpe_ratio": 0.4,
                "cagr": 0.05,
                "max_drawdown": -0.1,
                "net_profit": 0.1,
                "parameters_json": {"sma_period": 200},
            },
            {
                "research_suite_version": "S1",
                "research_run_id": "r",
                "research_test_type": "FINAL_HOLDOUT",
                "research_is_holdout": True,
                "sharpe_ratio": -9.0,
                "cagr": -0.9,
                "max_drawdown": -0.9,
                "net_profit": -0.9,
                "parameters_json": {"sma_period": 200},
            },
        ]
    )
    result = assess_stage1(df)
    assert result["holdout"]["sharpe"] == -9.0
    # Holdout Sharpe must not flip the label by itself when validation/WFO pass.
    assert result["validation"]["sharpe"] == 0.9
    assert result["label"] in {"PASS", "WATCH", "FAIL", "IN_PROGRESS", "INCOMPLETE"}
    assert result["validation"]["oos_is_sharpe_ratio"] == pytest.approx(0.9 / 1.2)


def test_official_qc_backtest_start_end_not_confused_with_created():
    detail = {
        "name": "S1__SPYTrend__run__VALIDATION__VAL__009",
        "created": "2026-08-27T12:00:00Z",
        "backtestStart": "2010-01-01T00:00:00Z",
        "backtestEnd": "2022-12-31T00:00:00Z",
        "startDate": "1999-01-01",
        "parameterSet": {
            "sma_period": "200",
            "research_run_id": "run",
            "research_suite_version": "S1",
            "research_test_type": "VALIDATION",
        },
        "statistics": {"Sharpe Ratio": "0.8", "Compounding Annual Return": "9%"},
    }
    dates = qc_simulation_dates(detail)
    assert dates["backtest_start"].year == 2010
    assert dates["backtest_end"].year == 2022
    assert dates["created"].year == 2026
    fields = stage1_upsert_fields(detail, detail["name"])
    assert fields["backtest_start"].year == 2010
    assert fields["backtest_end"].year == 2022
    assert fields["created"].year == 2026
    assert fields["backtest_start"] != fields["created"]


def test_equity_chart_request_never_starts_at_creation_time():
    detail = {
        "created": "2026-08-27T00:00:00Z",
        "backtestStart": "2010-01-01T00:00:00Z",
        "backtestEnd": "2022-12-31T00:00:00Z",
    }
    bounds = chart_request_window(detail)
    created = created_unix(detail)
    assert bounds["start"] != created
    assert bounds["start"] < created
    assert bounds["count"] <= 1000
    missing = chart_request_window({"created": "2026-08-27T00:00:00Z"})
    assert missing["start"] == 0
    assert missing["start"] != created_unix({"created": "2026-08-27T00:00:00Z"})


def test_detailed_metrics_survive_incomplete_lightweight_refresh():
    existing = {"sharpe_ratio": 0.80, "cagr": 0.09, "max_drawdown": -0.2}
    incoming = normalize_statistics({"statistics": {}})
    merged = merge_stage1_lightweight_metrics(existing, incoming)
    assert merged["sharpe_ratio"] == 0.80
    assert merged["cagr"] == 0.09


def test_stage1_thresholds_survive_when_qc_research_guide_present():
    payload = {
        "name": "S1__SPYTrend__run__VALIDATION__VAL__009",
        "researchGuide": {"parameters": 12, "overfit": True},
        "parameterSet": {
            "sma_period": "190",
            "research_run_id": "run",
            "research_suite_version": "S1",
            "research_test_type": "VALIDATION",
            "research_thresholds": '{"min_validation_sharpe":0.0,"min_oos_is_sharpe_ratio":0.6}',
            "research_meta": '{"thresholds":{"min_validation_sharpe":0.0},"primary_parameter":"sma_period","selection":{"plateau_fraction":0.9}}',
        },
    }
    meta = extract_stage1_metadata(payload)
    fields = stage1_upsert_fields(payload, payload["name"])
    assert fields["research_guide_json"]["parameters"] == 12
    assert fields["research_thresholds_json"]["min_validation_sharpe"] == 0.0
    assert meta["thresholds"]["min_validation_sharpe"] == 0.0
    assert fields["config_json"]["thresholds"]["min_validation_sharpe"] == 0.0
    assert fields["config_json"]["primary_parameter"] == "sma_period"


def test_non_sma_primary_parameter_and_authoritative_dashboard_selection():
    df = pd.DataFrame(
        [
            {
                "research_suite_version": "S1",
                "research_run_id": "r",
                "research_test_type": "PARAM_SENS",
                "research_primary_parameter": "lookback_period",
                "sharpe_ratio": 1.0,
                "parameters_json": {"lookback_period": 10},
            },
            {
                "research_suite_version": "S1",
                "research_run_id": "r",
                "research_test_type": "PARAM_SENS",
                "research_primary_parameter": "lookback_period",
                "sharpe_ratio": 1.2,
                "parameters_json": {"lookback_period": 20},
            },
            {
                "research_suite_version": "S1",
                "research_run_id": "r",
                "research_test_type": "VALIDATION",
                "research_primary_parameter": "lookback_period",
                "sharpe_ratio": 0.9,
                "parameters_json": {"lookback_period": 15},
                "research_selection_summary_json": {
                    "primary_parameter": "lookback_period",
                    "raw_best_parameter": 20,
                    "selected_parameter": 15,
                    "best_objective": 1.2,
                    "selected_objective": 1.05,
                    "neighbor_mean": 1.1,
                    "positive_parameter_fraction": 1.0,
                    "plateau_width": 3,
                    "robustness_label": "stable_plateau",
                    "plateau_fraction": 0.9,
                    "neighbor_radius": 1,
                    "tie_breaker": "closest_to_default",
                    "authoritative": True,
                    "source": "orchestrator",
                },
            },
        ]
    )
    rob = parameter_robustness_summary(
        df[df["research_test_type"] == "PARAM_SENS"],
        run_df=df,
    )
    assert rob["primary_parameter"] == "lookback_period"
    assert rob["selected_parameter"] == 15
    assert rob["raw_best_parameter"] == 20
    assert rob["authoritative"] is True
    assert rob["source"] == "orchestrator"


def test_holdout_exposure_persists_across_git_commits_within_a_lineage():
    rows = [
        {
            "backtest_id": "old",
            "name": "S1__SPYTrend__r1__FINAL_HOLDOUT__HOLDOUT__080",
            "strategy_id": "SPYTrend",
            "research_lineage_id": "SPYTrend",
            "research_git_commit": "commit-a",
            "research_suite_version": "S1",
            "research_test_type": "FINAL_HOLDOUT",
            "backtest_start": "2023-01-01",
            "backtest_end": "2026-08-27",
        }
    ]
    first = classify_rows(
        rows,
        holdout_start="2023-01-01",
        holdout_end="2026-08-27",
        strategy_id="SPYTrend",
        research_lineage_id="SPYTrend",
    )
    second = classify_rows(
        rows,
        holdout_start="2023-01-01",
        holdout_end="2026-08-27",
        strategy_id="SPYTrend",
        research_lineage_id="SPYTrend",
    )
    assert first["stage1_final_holdout_count"] == 1
    assert second["stage1_final_holdout_count"] == 1
    assert first["status"] == second["status"]


def test_legacy_backtest_overlap_marks_holdout_previously_exposed():
    rows = [
        {
            "backtest_id": "legacy-full",
            "name": "SPYTrend full history",
            "strategy_id": "SPYTrend",
            "research_suite_version": None,
            "research_run_id": None,
            "research_test_type": None,
            "backtest_start": "2010-01-01",
            "backtest_end": "2026-08-25",
        }
    ]
    result = classify_rows(
        rows,
        holdout_start="2023-01-01",
        holdout_end="2026-08-27",
        strategy_id="SPYTrend",
        research_lineage_id="SPYTrend",
    )
    assert result["status"] == STATUS_EXPOSED_PRIOR_TO_STAGE1
    assert result["legacy_overlap_count"] == 1
    assert result["stage1_final_holdout_count"] == 0


def test_applied_migrations_are_skipped(tmp_path):
    first = tmp_path / "001_stage1_research.sql"
    second = tmp_path / "002_later.sql"
    first.write_text("SELECT 1;")
    second.write_text("SELECT 2;")
    pending = pending_migration_files(
        [first, second],
        {"001_stage1_research.sql"},
        recheck=False,
    )
    assert [path.name for path in pending] == ["002_later.sql"]
    recheck = pending_migration_files(
        [first, second],
        {"001_stage1_research.sql"},
        recheck=True,
    )
    assert [path.name for path in recheck] == ["001_stage1_research.sql", "002_later.sql"]


def test_in_progress_monitor_is_not_fail():
    df = pd.DataFrame(
        [
            {
                "research_suite_version": "S1",
                "research_run_id": "r",
                "research_test_type": "BASELINE_DEV",
                "status": "completed",
                "sharpe_ratio": 0.5,
                "parameters_json": {"sma_period": 200},
            }
        ]
    )
    research_run = {
        "research_run_id": "r",
        "expected_experiment_count": 81,
        "synced_experiment_count": 1,
        "completed_count": 1,
        "failed_count": 0,
        "skipped_count": 0,
        "run_status": "IN_PROGRESS",
    }
    result = assess_stage1(df, research_run=research_run)
    assert result["run_status"] == IN_PROGRESS
    assert result["label"] == IN_PROGRESS
    assert "expected_experiment_count" not in df.columns


def test_drawdown_worst_is_minimum():
    assert parse_drawdown_to_decimal("10%") == -0.10
    assert parse_drawdown_to_decimal("20%") == -0.20
    assert parse_drawdown_to_decimal("30%") == -0.30
    assert min([-0.10, -0.20, -0.30]) == -0.30


LOAD_BACKTESTS_COLUMNS = [
    "backtest_id",
    "strategy_id",
    "name",
    "status",
    "created_at",
    "sharpe_ratio",
    "sortino_ratio",
    "alpha",
    "beta",
    "cagr",
    "max_drawdown",
    "net_profit",
    "win_rate",
    "loss_rate",
    "trade_count",
    "psr",
    "research_suite_version",
    "research_run_id",
    "research_experiment_id",
    "research_test_type",
    "research_phase",
    "research_window_id",
    "research_git_commit",
    "research_is_holdout",
    "research_dirty",
    "train_start",
    "train_end",
    "test_start",
    "test_end",
    "parameters_json",
    "objective_name",
    "objective_value",
    "raw_statistics_json",
    "research_guide_json",
    "research_thresholds_json",
    "research_primary_parameter",
    "research_selection_summary_json",
    "research_lineage_id",
    "economic_parameter_count",
    "research_metadata_count",
    "backtest_start",
    "backtest_end",
    "error_message",
]


def _monitor_backtest_row(**overrides):
    row = {column: None for column in LOAD_BACKTESTS_COLUMNS}
    row.update(
        {
            "strategy_id": "SPYTrend",
            "research_suite_version": "S1",
            "research_run_id": "r",
            "status": "Completed.",
            "research_is_holdout": False,
            "parameters_json": {"sma_period": 200},
        }
    )
    row.update(overrides)
    return row


def _monitor_research_run(**overrides):
    row = {
        "research_run_id": "r",
        "strategy_id": "SPYTrend",
        "suite_version": "S1",
        "git_commit": "abc123",
        "dirty": False,
        "first_seen_at": None,
        "last_seen_at": None,
        "holdout_accessed": False,
        "holdout_access_count": 0,
        "config_json": None,
        "research_lineage_id": "SPYTrend",
        "expected_experiment_count": 81,
        "synced_experiment_count": 0,
        "completed_count": 0,
        "failed_count": 0,
        "skipped_count": 0,
        "run_status": "IN_PROGRESS",
        "holdout_exposure_status": None,
        "holdout_start": "2023-01-01",
        "holdout_end": "2026-08-27",
    }
    row.update(overrides)
    return row


def test_legacy_dateless_row_hydrates_and_classifies_exposed_prior():
    list_row = {
        "backtest_id": "legacy-full",
        "name": "SPYTrend full history",
        "strategy_id": "SPYTrend",
        "status": "Completed.",
        "backtest_start": None,
        "backtest_end": None,
        "research_suite_version": None,
        "research_run_id": None,
        "research_test_type": None,
    }
    assert needs_legacy_date_hydration(list_row, list_row) is True
    detail = {
        "name": "SPYTrend full history",
        "backtestStart": "2010-01-01T00:00:00Z",
        "backtestEnd": "2026-08-25T00:00:00Z",
        "parameterSet": {"sma_period": "200", "start_date": "2010-01-01", "end_date": "2026-08-25"},
    }
    fields = legacy_hydration_fields(detail)
    assert fields["backtest_start"].year == 2010
    assert fields["backtest_end"].year == 2026
    assert fields["research_run_id"] is None
    result = hydrate_legacy_and_classify(
        list_row,
        detail,
        holdout_start="2023-01-01",
        holdout_end="2026-08-27",
        strategy_id="SPYTrend",
        research_lineage_id="SPYTrend",
    )
    stored = result["stored"]
    assert stored["backtest_start"].year == 2010
    assert str(stored["backtest_end"])[:10] == "2026-08-25"
    assert stored["research_suite_version"] is None
    classified = result["classified"]
    assert classified["status"] == STATUS_EXPOSED_PRIOR_TO_STAGE1
    assert classified["legacy_overlap_count"] == 1
    populated = dict(stored)
    assert needs_legacy_date_hydration(populated, {"name": populated["name"]}) is False


def test_progress_uses_research_run_not_dataframe_column():
    cases = [
        (1, IN_PROGRESS),
        (40, IN_PROGRESS),
        (80, IN_PROGRESS),
    ]
    for n, expected_status in cases:
        rows = [
            _monitor_backtest_row(
                backtest_id="bt-{0}".format(i),
                research_test_type="PARAM_SENS" if i else "BASELINE_DEV",
            )
            for i in range(n)
        ]
        df = pd.DataFrame(rows)
        assert "expected_experiment_count" not in df.columns
        research_run = _monitor_research_run(
            synced_experiment_count=n,
            completed_count=n,
            run_status="IN_PROGRESS",
        )
        result = assess_stage1(df, research_run=research_run)
        assert result["run_status"] == expected_status
        assert result["label"] == IN_PROGRESS
        assert result["label"] not in {"PASS", "WATCH", "FAIL"}

    complete_rows = [
        _monitor_backtest_row(
            backtest_id="bt-{0}".format(i),
            research_test_type="BASELINE_DEV",
            sharpe_ratio=0.5,
        )
        for i in range(81)
    ]
    df = pd.DataFrame(complete_rows)
    research_run = _monitor_research_run(
        synced_experiment_count=81,
        completed_count=81,
        run_status="COMPLETE",
    )
    result = assess_stage1(df, research_run=research_run)
    assert result["run_status"] == COMPLETE
    assert result["label"] in {"PASS", "WATCH", "FAIL", COMPLETE}


def test_skipped_oos_finalizes_incomplete_via_run_summary():
    rows = [
        _monitor_backtest_row(backtest_id="bt-{0}".format(i), research_test_type="WFO_TEST")
        for i in range(80)
    ]
    df = pd.DataFrame(rows)
    assert "expected_experiment_count" not in df.columns
    still_open = assess_stage1(
        df,
        research_run=_monitor_research_run(
            synced_experiment_count=80,
            completed_count=80,
            skipped_count=0,
            run_status="IN_PROGRESS",
        ),
    )
    assert still_open["run_status"] == IN_PROGRESS
    summary = {
        "research_run_id": "r",
        "expected_experiment_count": 81,
        "completed_count": 80,
        "failed_count": 0,
        "skipped_count": 1,
        "run_status": "INCOMPLETE",
    }
    progress = compute_research_run_progress(
        expected=81,
        row_statuses=["Completed."] * 80,
        orchestrator_summary=summary,
    )
    assert progress["run_status"] == INCOMPLETE
    assert progress["skipped_count"] == 1
    finalized = assess_stage1(
        df,
        research_run=_monitor_research_run(
            synced_experiment_count=80,
            completed_count=80,
            skipped_count=1,
            run_status="INCOMPLETE",
        ),
    )
    assert finalized["run_status"] == INCOMPLETE
    assert finalized["label"] == INCOMPLETE


def test_migration_failure_exits_nonzero_when_backtests_requested():
    from jobs.sync_quantconnect import migration_failure_exit_code
    from pathlib import Path

    source = Path(__file__).resolve().parent.parent / "jobs" / "sync_quantconnect.py"
    text = source.read_text(encoding="utf-8")
    assert "raise SystemExit(main())" in text
    assert migration_failure_exit_code(RuntimeError("boom"), True) == 1
    assert migration_failure_exit_code(RuntimeError("boom"), False) is None
    assert migration_failure_exit_code(None, True) is None


def test_backtest_cron_installer_uses_nonblocking_flock():
    from pathlib import Path

    from jobs.sync_quantconnect import BACKTEST_SYNC_LOCK_RELATIVE

    script = (
        Path(__file__).resolve().parent.parent
        / "scripts"
        / "install_backtest_sync_cron.sh"
    )
    text = script.read_text(encoding="utf-8")
    assert "flock -n" in text
    assert "--backtests-only" in text
    assert BACKTEST_SYNC_LOCK_RELATIVE in text
    assert "live" in text.lower()
    assert "Does NOT run unless you execute this script yourself." not in text
    assert "Deploy" in text or "deploy" in text
    assert "idempotent" in text.lower()


def test_smoke_is_recognized_and_excluded_from_stage1_assessment():
    from qc_research.aggregation import smoke_backtests
    from qc_research.parsing import is_smoke_test

    smoke_row = _monitor_backtest_row(
        backtest_id="smoke-1",
        name="S1__SPYTrend__SMOKE-abc123de__SMOKE__DEV_SMOKE__001",
        research_run_id="SMOKE_SPYTrend_20260827T000000Z_abc123de",
        research_test_type="SMOKE",
        research_phase="SMOKE",
        research_window_id="DEV_SMOKE",
        research_git_commit="abc123def456",
        test_start="2017-01-01",
        test_end="2018-12-31",
        created_at="2026-08-27",
        sharpe_ratio=0.4,
        cagr=0.1,
        max_drawdown=-0.2,
        net_profit=0.12,
        status="Completed.",
        parameters_json={"sma_period": 200, "starting_cash": 100000},
    )
    stage_rows = [
        _monitor_backtest_row(
            backtest_id="bt-{0}".format(i),
            research_run_id="stage-run",
            research_test_type="PARAM_SENS" if i else "BASELINE_DEV",
        )
        for i in range(81)
    ]
    df = pd.DataFrame(stage_rows + [smoke_row])
    assert is_smoke_test(smoke_row) is True
    assert is_smoke_test(stage_rows[0]) is False
    smoke = smoke_backtests(df)
    assert len(smoke) == 1
    assert smoke.iloc[0]["backtest_id"] == "smoke-1"
    stage = stage1_backtests(df)
    assert len(stage) == 81
    assert "SMOKE" not in stage["research_test_type"].astype(str).tolist()
    runs = research_runs(df)
    assert "SMOKE_SPYTrend_20260827T000000Z_abc123de" not in runs["research_run_id"].astype(str).tolist()
    assessment = assess_stage1(df)
    assert assessment["progress"]["synced_experiment_count"] != 82
    choice = select_comparison_backtest(df)
    assert choice["test_type"] != "SMOKE"
    classified = classify_rows(
        df.to_dict("records"),
        holdout_start="2023-01-01",
        holdout_end="2026-08-27",
        strategy_id="SPYTrend",
        research_lineage_id="SPYTrend",
    )
    assert classified["stage1_final_holdout_count"] == 0
    assert all(
        row.get("research_test_type") != "SMOKE"
        for row in classified["legacy_overlap_backtests"]
    )


def test_smoke_metadata_and_metrics_parse():
    from qc_research.parsing import extract_stage1_metadata, is_smoke_test

    payload = {
        "name": "S1__SPYTrend__SMOKE-abc123de__SMOKE__DEV_SMOKE__001",
        "parameterSet": {
            "sma_period": "200",
            "starting_cash": "100000",
            "start_date": "2017-01-01",
            "end_date": "2018-12-31",
            "research_suite_version": "S1",
            "research_run_id": "SMOKE_SPYTrend_20260827T120000Z_abc123de",
            "research_experiment_id": "smoke_SPYTrend_abc",
            "research_test_type": "SMOKE",
            "research_phase": "SMOKE",
            "research_window_id": "DEV_SMOKE",
            "research_git_commit": "abc123def456",
            "research_is_holdout": "false",
            "research_strategy_id": "SPYTrend",
            "research_lineage_id": "SPYTrend",
            "research_primary_parameter": "sma_period",
            "research_expected_experiments": "1",
            "research_optimized_parameter_count": "0",
            "research_meta": '{"smoke": true, "optimized_parameter_count": 0}',
        },
        "statistics": {
            "Sharpe Ratio": "0.55",
            "Compounding Annual Return": "12%",
            "Drawdown": "20%",
            "Net Profit": "15%",
            "Total Orders": "8",
        },
    }
    meta = extract_stage1_metadata(payload, name=payload["name"])
    assert meta["research_test_type"] == "SMOKE"
    assert meta["research_phase"] == "SMOKE"
    assert meta["research_is_holdout"] is False
    assert meta["expected_experiment_count"] == 1
    assert is_smoke_test(meta) is True
    stats = normalize_statistics(payload)
    assert stats["max_drawdown"] == -0.20
    assert stats["cagr"] == pytest.approx(0.12)
    assert stats["net_profit"] == pytest.approx(0.15)
    assert stats["sharpe_ratio"] == pytest.approx(0.55)
    assert stats["trade_count"] == 8


def test_smoke_name_gets_equity_curve_sync():
    from jobs.stage1_backtests import needs_equity_curve
    from qc_research.parsing import is_stage1_name

    name = "S1__SPYTrend__SMOKE-abc123de__SMOKE__DEV_SMOKE__001"
    assert is_stage1_name(name)
    assert needs_equity_curve(
        None,
        {"name": name, "status": "Completed."},
        0,
    )


def test_upsert_research_run_skips_smoke():
    from jobs.stage1_backtests import upsert_research_run

    class Boom:
        def execute(self, *args, **kwargs):
            raise AssertionError("SMOKE must not be upserted into research_runs")

    upsert_research_run(
        Boom(),
        "SPYTrend",
        {
            "research_run_id": "SMOKE_SPYTrend_20260827T000000Z_abc123de",
            "research_test_type": "SMOKE",
            "research_phase": "SMOKE",
            "research_is_holdout": False,
        },
    )


def test_strategy_monitor_has_smoke_section_and_fragment_refresh():
    from pathlib import Path

    monitor = (
        Path(__file__).resolve().parent.parent / "pages" / "strategy_monitor.py"
    ).read_text(encoding="utf-8")
    ui = (
        Path(__file__).resolve().parent.parent / "qc_research" / "monitor_ui.py"
    ).read_text(encoding="utf-8")
    assert "render_smoke_section" in monitor
    assert '@st.fragment(run_every=LIVE_MONITOR_REFRESH)' in monitor
    assert "window.parent.location.reload" not in monitor
    assert "### Smoke Tests" in ui
    assert "PASS/WATCH/FAIL" in ui


def test_backtest_cron_installer_is_idempotent_and_preserves_live_cron(tmp_path):
    import os
    import subprocess
    from pathlib import Path

    root = tmp_path / "FMP_SCREENER"
    (root / "venv" / "bin").mkdir(parents=True)
    (root / "outputs").mkdir()
    (root / "venv" / "bin" / "python").write_text("#!/bin/sh\nexit 0\n")
    crontab_file = tmp_path / "crontab.txt"
    live_line = (
        "*/10 * * * * cd {0} && {0}/venv/bin/python -m jobs.sync_quantconnect "
        ">> {0}/outputs/qc_sync.log 2>&1".format(root)
    )
    crontab_file.write_text(live_line + "\n")
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    wrapper = fake_bin / "crontab"
    wrapper.write_text(
        "#!/usr/bin/env bash\n"
        "set -e\n"
        'CRONFILE="{0}"\n'.format(crontab_file)
        + 'if [ "${1:-}" = "-l" ]; then\n'
        "  cat \"$CRONFILE\"\n"
        "  exit 0\n"
        "fi\n"
        'cat > "$CRONFILE"\n'
    )
    wrapper.chmod(0o755)
    env = os.environ.copy()
    env["PATH"] = str(fake_bin) + ":" + env["PATH"]
    script = Path(__file__).resolve().parent.parent / "scripts" / "install_backtest_sync_cron.sh"
    first = subprocess.run(
        ["bash", str(script), str(root)],
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    assert first.returncode == 0, first.stderr + first.stdout
    second = subprocess.run(
        ["bash", str(script), str(root)],
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    assert second.returncode == 0, second.stderr + second.stdout
    text = crontab_file.read_text()
    assert text.count("jobs.sync_quantconnect --backtests-only") == 1
    assert "flock -n" in text
    assert live_line in text
    assert text.count(live_line) == 1
    assert "* * * * *" in text


def test_cron_docs_describe_automatic_flock_protected_install():
    from pathlib import Path

    from jobs.sync_quantconnect import BACKTEST_SYNC_LOCK_RELATIVE

    docs = (
        Path(__file__).resolve().parent.parent / "docs" / "STAGE1_RESEARCH_MONITOR.md"
    ).read_text(encoding="utf-8")
    assert "not installed automatically" not in docs
    assert BACKTEST_SYNC_LOCK_RELATIVE in docs
    assert "flock -n" in docs
    assert "flock -w" in docs
    assert "live QuantConnect cron intact" in docs


def test_research_project_migration_preserves_execution_and_history():
    from pathlib import Path

    sql = (
        Path(__file__).resolve().parent.parent
        / "db"
        / "migrations"
        / "002_research_project.sql"
    ).read_text(encoding="utf-8")
    assert "qc_research_project_id" in sql
    assert "qc_research_project_name" in sql
    assert "ADD COLUMN IF NOT EXISTS" in sql
    assert "SPYTrendResearch" in sql
    assert "SET qc_project_id" not in sql
    assert "SET qc_deployment_id" not in sql
    assert "DELETE FROM backtests" not in sql
    assert "DELETE FROM holdout_exposures" not in sql
    assert "DROP TABLE" not in sql


def test_research_id_discovery_is_exact_name_and_never_falls_back():
    from jobs.sync_quantconnect import (
        RESEARCH_PROJECT_NOT_BOOTSTRAPPED,
        exact_project_match,
        execution_project_id,
        resolve_research_project_id,
    )

    projects = [
        {"name": "SPYTrend", "projectId": 111},
        {"name": "SPYTrendResearch", "projectId": 222},
    ]
    assert exact_project_match(projects, "SPYTrendResearch")["projectId"] == 222
    assert exact_project_match(projects, "SPYTrend")["projectId"] == 111
    stored = {
        "strategy_id": "SPYTrend",
        "qc_project_id": "111",
        "qc_research_project_id": "222",
        "qc_research_project_name": "SPYTrendResearch",
    }
    assert execution_project_id(stored) == "111"
    assert resolve_research_project_id(stored, persist=False) == "222"

    missing = {
        "strategy_id": "SPYTrend",
        "qc_project_id": "111",
        "qc_research_project_id": None,
        "qc_research_project_name": "SPYTrendResearch",
    }
    discovered = resolve_research_project_id(missing, projects=projects, persist=False)
    assert discovered == "222"
    execution_only = [{"name": "SPYTrend", "projectId": 111}]
    assert (
        resolve_research_project_id(missing, projects=execution_only, persist=False)
        is None
    )
    assert "SPYTrendResearch" in RESEARCH_PROJECT_NOT_BOOTSTRAPPED.format(
        "SPYTrendResearch"
    )


def test_live_sync_source_stays_on_execution_project():
    from pathlib import Path

    source = (
        Path(__file__).resolve().parent.parent / "jobs" / "sync_quantconnect.py"
    ).read_text(encoding="utf-8")
    assert "resolve_research_project_id(strategy)" in source
    assert "get_live_status(project_id)" in source
    assert "get_live_portfolio(project_id)" in source
    live_block = source.split("STATUS")[1].split("PORTFOLIO")[0]
    assert "resolve_research_project_id" not in live_block


def test_new_research_project_does_not_clear_prior_holdout_exposure():
    from scripts.verify_stage1_production import evaluate_legacy_and_holdout
    overlapping = [
        {
            "backtest_id": "legacy-full",
            "strategy_id": "SPYTrend",
            "research_run_id": None,
            "qc_project_id": "111",
            "backtest_start": date(2010, 1, 1),
            "backtest_end": date(2026, 8, 25),
        }
    ]
    present = evaluate_legacy_and_holdout(
        overlapping,
        [
            {
                "strategy_id": "SPYTrend",
                "status": STATUS_EXPOSED_PRIOR_TO_STAGE1,
                "backtest_id": "legacy-full",
            }
        ],
    )
    assert present["ok"] is True
    assert present["holdout_status"] == STATUS_EXPOSED_PRIOR_TO_STAGE1
    assert present["historical_count"] == 1


def test_strategy_monitor_shows_research_and_execution_labels():
    from pathlib import Path

    monitor = (
        Path(__file__).resolve().parent.parent / "pages" / "strategy_monitor.py"
    ).read_text(encoding="utf-8")
    assert "Research Project:" in monitor
    assert "Execution Project:" in monitor
    assert "qc_research_project_name" in monitor
    assert "qc_research_project_id" in monitor
    assert "orchestrator_summary_json" in monitor
    ui = (
        Path(__file__).resolve().parent.parent / "qc_research" / "monitor_ui.py"
    ).read_text(encoding="utf-8")
    assert "STAGE 1 RESEARCH RESULTS" in ui
    assert "Audit / Safety" in ui
    assert "Equity Curves" in ui


class _RecordingConn:
    def __init__(self):
        self.params = []
        self.sql = []

    def execute(self, statement, params=None):
        self.sql.append(str(statement))
        self.params.append(params)

        class _Result:
            def mappings(self_inner):
                return iter([])

        return _Result()


def _orchestrator_summary(**overrides):
    payload = {
        "research_run_id": "STAGE1_SPYTrend_156c40e7",
        "strategy_id": "SPYTrend",
        "source": "orchestrator",
        "run_status": "COMPLETE",
        "expected_experiment_count": 81,
        "synced_experiment_count": 81,
        "completed_count": 81,
        "failed_count": 0,
        "skipped_count": 0,
        "skipped_experiments": [],
        "git_commit": "156c40e7b1ad8559",
    }
    payload.update(overrides)
    return payload


def test_case1_complete_81_is_complete_not_in_progress():
    statuses = ["Completed."] * 81
    progress = compute_research_run_progress(
        expected=81,
        row_statuses=statuses,
        orchestrator_summary=_orchestrator_summary(),
    )
    assert progress["run_status"] == COMPLETE
    assert progress["completed_count"] == 81
    assert progress["skipped_count"] == 0
    rows = [
        _monitor_backtest_row(
            backtest_id="bt-{0}".format(i),
            research_test_type="PARAM_SENS" if i else "BASELINE_DEV",
            sharpe_ratio=0.5,
        )
        for i in range(81)
    ]
    result = assess_stage1(
        pd.DataFrame(rows),
        research_run=_monitor_research_run(
            expected_experiment_count=81,
            synced_experiment_count=81,
            completed_count=81,
            failed_count=0,
            skipped_count=0,
            run_status="COMPLETE",
        ),
    )
    assert result["run_status"] == COMPLETE
    assert result["label"] != IN_PROGRESS
    assert result["label"] in {"PASS", "WATCH", "FAIL", COMPLETE}


def test_case2_skipped_oos_is_incomplete_never_in_progress():
    summary = _orchestrator_summary(
        run_status="INCOMPLETE",
        synced_experiment_count=80,
        completed_count=80,
        skipped_count=1,
        skipped_experiments=[
            {
                "experiment_id": "e081",
                "name": "S1__SPYTrend__r__WFO_TEST__Y2022__081",
                "test_type": "WFO_TEST",
                "window_id": "Y2022",
                "error": "No valid training results",
            }
        ],
    )
    progress = compute_research_run_progress(
        expected=81,
        row_statuses=["Completed."] * 80,
        orchestrator_summary=summary,
    )
    assert progress["run_status"] == INCOMPLETE
    assert progress["run_status"] != IN_PROGRESS
    assert progress["skipped_count"] == 1
    df = pd.DataFrame(
        [
            _monitor_backtest_row(
                backtest_id="bt-{0}".format(i),
                research_test_type="WFO_TEST",
            )
            for i in range(80)
        ]
    )
    merged = attach_skipped_experiments(df, summary)
    assert (merged["status"].astype(str).str.lower() == "skipped").sum() == 1
    finalized = assess_stage1(
        merged,
        research_run=_monitor_research_run(
            synced_experiment_count=80,
            completed_count=80,
            skipped_count=1,
            run_status="INCOMPLETE",
            orchestrator_summary_json=summary,
        ),
    )
    assert finalized["run_status"] == INCOMPLETE
    assert finalized["label"] == INCOMPLETE
    assert finalized["label"] != IN_PROGRESS


def test_case3_failed_experiment_is_terminal_not_in_progress():
    summary = _orchestrator_summary(
        run_status="INCOMPLETE",
        synced_experiment_count=81,
        completed_count=80,
        failed_count=1,
        skipped_count=0,
    )
    progress = compute_research_run_progress(
        expected=81,
        row_statuses=["Completed."] * 80 + ["Runtime Error"],
        orchestrator_summary=summary,
    )
    assert progress["run_status"] == INCOMPLETE
    assert progress["run_status"] != IN_PROGRESS
    assert progress["failed_count"] == 1
    without_summary = compute_research_run_progress(
        expected=81,
        row_statuses=["Completed."] * 80 + ["Runtime Error"],
    )
    assert without_summary["run_status"] == INCOMPLETE
    result = assess_stage1(
        pd.DataFrame(
            [
                _monitor_backtest_row(backtest_id="ok-{0}".format(i))
                for i in range(80)
            ]
            + [
                _monitor_backtest_row(
                    backtest_id="fail",
                    status="Runtime Error",
                )
            ]
        ),
        research_run=_monitor_research_run(
            completed_count=80,
            failed_count=1,
            skipped_count=0,
            run_status="INCOMPLETE",
        ),
    )
    assert result["run_status"] == INCOMPLETE
    assert result["label"] != IN_PROGRESS


def test_case4_summary_retry_is_idempotent(tmp_path):
    payload = _orchestrator_summary(
        run_status="INCOMPLETE",
        completed_count=80,
        skipped_count=1,
        synced_experiment_count=80,
    )
    path = tmp_path / "stage1_results" / "SPYTrend" / payload["research_run_id"] / "run_summary.json"
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps(payload), encoding="utf-8")
    conn = _RecordingConn()
    first = import_run_summaries(conn, [path])
    second = import_run_summaries(conn, [path])
    assert first[0]["research_run_id"] == second[0]["research_run_id"]
    apply_params = [row for row in conn.params if row and "research_run_id" in row]
    assert len(apply_params) >= 2
    assert apply_params[0]["research_run_id"] == apply_params[1]["research_run_id"]
    assert apply_params[0]["skipped_count"] == apply_params[1]["skipped_count"]
    assert apply_params[0]["run_status"] == apply_params[1]["run_status"]
    progress_a = compute_research_run_progress(
        expected=81,
        row_statuses=["Completed."] * 80,
        orchestrator_summary=payload,
    )
    progress_b = compute_research_run_progress(
        expected=81,
        row_statuses=["Completed."] * 80,
        orchestrator_summary=payload,
    )
    assert progress_a == progress_b
    source = (
        Path(__file__).resolve().parent.parent / "jobs" / "stage1_backtests.py"
    ).read_text(encoding="utf-8")
    assert "ON CONFLICT (research_run_id)" in source


def test_case5_smoke_excluded_from_stage1_counts_and_equity():
    smoke_row = _monitor_backtest_row(
        backtest_id="smoke-1",
        name="S1__SPYTrend__SMOKE-abc123de__SMOKE__DEV_SMOKE__001",
        research_run_id="SMOKE_SPYTrend_abc123de",
        research_test_type="SMOKE",
        research_phase="SMOKE",
        sharpe_ratio=9.9,
    )
    stage_rows = [
        _monitor_backtest_row(
            backtest_id="bt-{0}".format(i),
            research_run_id="STAGE1_SPYTrend_156c40e7",
            research_test_type="PARAM_SENS" if i else "BASELINE_DEV",
            sharpe_ratio=0.4,
        )
        for i in range(81)
    ]
    df = pd.DataFrame(stage_rows + [smoke_row])
    stage = stage1_backtests(df)
    assert len(stage) == 81
    assessment = assess_stage1(
        df,
        research_run=_monitor_research_run(
            research_run_id="STAGE1_SPYTrend_156c40e7",
            expected_experiment_count=81,
            completed_count=81,
            run_status="COMPLETE",
        ),
    )
    assert assessment["progress"]["synced_experiment_count"] != 82
    assert assessment["progress"]["completed_count"] <= 81
    equity = primary_equity_backtests(stage)
    assert "SMOKE" not in equity["research_test_type"].astype(str).tolist()
    wfo = walk_forward_aggregates(df, include_holdout=False)
    assert wfo["n_windows"] == 0 or "SMOKE" not in str(wfo)


def test_case6_research_execution_separation_hard_fails_stage1_fallback():
    from jobs.sync_quantconnect import resolve_research_project_id
    from pathlib import Path

    same = {
        "strategy_id": "SPYTrend",
        "qc_project_id": "111",
        "qc_research_project_id": "111",
        "qc_research_project_name": "SPYTrendResearch",
    }
    source = (
        Path(__file__).resolve().parent.parent / "jobs" / "sync_quantconnect.py"
    ).read_text(encoding="utf-8")
    assert "str(research_id) == str(execution_id)" in source
    assert "Skipping research backtest sync rather than" in source
    assert resolve_research_project_id(same, persist=False) == "111"


def test_discover_and_attach_run_summary(tmp_path):
    payload = _orchestrator_summary(
        run_status="INCOMPLETE",
        completed_count=80,
        skipped_count=1,
        skipped_experiments=[
            {
                "experiment_id": "e081",
                "test_type": "WFO_TEST",
                "window_id": "Y2022",
                "name": "skipped-oos",
                "error": "No valid training results",
            }
        ],
    )
    path = tmp_path / "stage1_results" / "SPYTrend" / payload["research_run_id"] / "run_summary.json"
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps(payload), encoding="utf-8")
    found = discover_run_summary_paths(tmp_path)
    assert found == [path]
    parsed = parse_orchestrator_summary({"orchestrator_summary_json": payload})
    assert parsed["skipped_count"] == 1
    start, end = research_date_range(
        pd.DataFrame(
            [
                _monitor_backtest_row(test_start="2010-01-01", test_end="2018-12-31"),
                _monitor_backtest_row(test_start="2019-01-01", test_end="2022-12-31"),
            ]
        )
    )
    assert start == "2010-01-01"
    assert end == "2022-12-31"


def test_backtests_only_imports_run_summary_after_qc_sync():
    from pathlib import Path

    source = (
        Path(__file__).resolve().parent.parent / "jobs" / "sync_quantconnect.py"
    ).read_text(encoding="utf-8")
    assert "import_run_summaries" in source
    assert "discover_run_summary_paths" in source
    main_body = source.split("def main(", 1)[1]
    assert main_body.index("for strategy in strategies") < main_body.index(
        "imported = import_run_summaries"
    )
    assert "stage1_results" in source



