"""Investor-facing Platform Research labels and Strategy Definition."""

from __future__ import annotations

from qc_research.ml_monitor_ui import investor_wfo_frame, render_platform_view
from qc_research.platform_ingest import DEFAULT_ARTIFACT_ROOT, monitor_view_from_artifacts, normalize_platform_file
from qc_research.platform_presentation import (
    format_inner_cv,
    format_model_choice,
    format_validation,
    friendly_label,
    picker_label,
    robustness_summary,
    strategy_definition_from_payloads,
)


def test_friendly_labels_hide_raw_enums():
    assert friendly_label("ML_DISCOVERY") == "ML Discovery"
    assert friendly_label("FIXED_INCOME_TREND") == "Fixed Income Trend"
    assert friendly_label("REAL_QC") == "Real QuantConnect"
    assert friendly_label("NOT_DEFINED") == "Not Defined"
    assert friendly_label("HUMAN_REVIEW_REQUIRED") == "Review Required for Promotion"
    assert friendly_label("COMPLETE") == "Research Complete"
    assert friendly_label("LOCKED") == "Holdout Locked"
    assert friendly_label("DELIVERED") == "Delivered"


def test_definition_and_winner_come_from_canonical_payload():
    definition = strategy_definition_from_payloads(
        summary={
            "strategy_id": "FutureBondTrend",
            "display_name": "Future Bond Trend",
            "research_mode": "ML_DISCOVERY",
            "symbol": "IEF",
            "thesis": "Generic thesis",
            "selected_candidate": "elasticnet::lb90_a0p1_l10p5",
            "baseline_trial_id": "deterministic::sma90_long_cash",
            "cost_model_id": "BOND_ETF_V1",
        },
        spec={
            "identity": {"strategy_id": "FutureBondTrend", "research_mode": "ML_DISCOVERY"},
            "features": {"ordered_feature_schema": ["ret_1", "sma_gap"]},
            "target": {"label_horizon": 21},
            "signal": {"lookback": 90},
            "model": {"approved_model_families": ["ridge", "elasticnet"]},
            "validation": {
                "outer_wfo": {"train_years": 5, "oos_years": 1, "first_oos_year": 2015, "last_oos_year": 2024},
                "inner_cv": {"n_folds": 3, "embargo_trading_days": 5},
                "purge": True,
            },
        },
    )
    assert definition["display_name"] == "Future Bond Trend"
    assert definition["instrument"] == "IEF"
    assert definition["features"] == ["ret_1", "sma_gap"]
    assert "21-session" in str(definition["target"])
    assert format_model_choice(definition["winner"]).startswith("ElasticNet")
    assert "90-day SMA" in format_model_choice(definition["baseline"])
    assert "2015–2024" in format_validation(definition["validation"])
    assert "3 chronological folds" in format_inner_cv(definition["validation"])


def test_robustness_summary_does_not_invent_stable_plateau():
    windows = [
        {
            "window_id": "W2015",
            "selected_trial_id": "elasticnet::lb120_a0p1_l10p5",
            "ml": {"sharpe_ratio": -0.1, "trade_count": 8},
            "baseline": {"sharpe_ratio": -0.4},
        },
        {
            "window_id": "W2016",
            "selected_trial_id": "elasticnet::lb120_a0p1_l10p5",
            "ml": {"sharpe_ratio": 0.2, "trade_count": 7},
            "baseline": {"sharpe_ratio": 0.05},
        },
    ]
    summary = robustness_summary(windows)
    assert summary["selected_in"] == "2 / 2"
    assert summary["positive_ml_sharpe"] == "1 / 2"
    assert summary["ml_beats_baseline"] == "2 / 2"
    assert summary["parameter_stability"] == "High"
    assert "STABLE_PLATEAU" not in summary.values()


def test_picker_label_is_investor_readable():
    assert picker_label(
        {
            "strategy_id": "TLTDurationMomentum",
            "name": "TLT Duration Momentum",
            "environment": "research",
            "research_mode": "ML_DISCOVERY",
            "research_kind": "platform_research",
        }
    ) == "TLT Duration Momentum · Research · ML Discovery"


def test_investor_wfo_frame_omits_qc_ids():
    frame = investor_wfo_frame(
        [
            {
                "window_id": "W2019",
                "oos_end": "2019-12-31",
                "selected_trial_id": "ridge::lb60_a1",
                "train_backtest_id": "train-secret",
                "winner_backtest_id": "win-secret",
                "ml": {"sharpe_ratio": 0.2, "cagr": 0.01, "trade_count": 4},
                "baseline": {"sharpe_ratio": 0.1, "cagr": 0.005},
                "ml_minus_baseline": {"sharpe_ratio": 0.1},
            }
        ]
    )
    assert "train_backtest_id" not in frame.columns
    assert "winner_backtest_id" not in frame.columns
    assert list(frame["Window"]) == ["W2019"]


def test_official_tlt_preview_has_definition_not_empty_rules():
    from streamlit.testing.v1 import AppTest

    preview = AppTest.from_file(str(DEFAULT_ARTIFACT_ROOT.parent / "preview_platform_monitor.py"), default_timeout=45)
    preview.run()
    assert not preview.exception
    labels = [str(getattr(metric, "label", "") or "") for metric in preview.metric]
    joined = " ".join(labels)
    for needle in ("Research status", "Economic gate", "Promotion gate", "Holdout status", "Baseline"):
        assert needle in joined
    texts = []
    for block in list(preview.markdown) + list(preview.subheader) + list(preview.info):
        texts.append(str(getattr(block, "value", "") or getattr(block, "label", "") or ""))
    page = " ".join(texts)
    assert "Thesis" in page
    assert "Strategy Definition" in page or "Research Design" in page
    assert "No structured rules stored for this strategy." not in page
    assert "Current Paper State" not in page
    source = (DEFAULT_ARTIFACT_ROOT.parent / "ml_monitor_ui.py").read_text(encoding="utf-8")
    assert 'if str(view.get("strategy_id") or "") == "TLTDurationMomentum"' not in source
    render_platform_view  # imported for coverage of the public renderer


def test_tlt_monitor_view_definition_is_data_driven():
    view = monitor_view_from_artifacts(normalize_platform_file(DEFAULT_ARTIFACT_ROOT / "tlt_duration_momentum.json"))
    definition = view["strategy_definition"]
    assert definition["display_name"] == "TLT Duration Momentum"
    assert definition["instrument"] == "TLT"
    assert "ret_1" in [str(item).lower() for item in definition["features"]]
    assert definition["lookback"] == 120
    assert "21-session" in str(definition["target"])
    assert view["delivery_status"] == "DELIVERED"
    assert view["robustness_summary"]["parameter_stability"] == "High"
