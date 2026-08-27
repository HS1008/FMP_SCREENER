import pandas as pd
import pytest

from qc_research.aggregation import (
    assess_stage1,
    holdout_access_count,
    legacy_backtests,
    research_runs,
    select_comparison_backtest,
    stage1_backtests,
    walk_forward_aggregates,
)
from qc_research.parsing import (
    extract_stage1_metadata,
    normalize_statistics,
    parse_equity_chart,
    parse_percent_to_decimal,
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
    assert result["label"] in {"PASS", "WATCH", "FAIL"}
    assert result["validation"]["oos_is_sharpe_ratio"] == pytest.approx(0.9 / 1.2)
