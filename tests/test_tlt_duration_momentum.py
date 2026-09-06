"""TLTDurationMomentum V0 ingest and Strategy Monitor read model."""

from __future__ import annotations

import json

from qc_research.ingest_platform_artifacts import main as ingest_main
from qc_research.ml_monitor_ui import build_platform_monitor_view, infer_research_labels
from qc_research.platform_ingest import (
    DEFAULT_ARTIFACT_ROOT,
    ingest_platform_files,
    monitor_view_from_artifacts,
    normalize_platform_file,
    wrap_smoke_record,
)
from qc_research.tlt_duration_momentum import (
    BASELINE_TRIAL,
    LINEAGE_ID,
    OFFICIAL_WINDOWS,
    RUN_ID,
    SELECTED_TRIAL,
    STRATEGY_ID,
    WINDOW_IDS,
    is_tlt_duration_momentum_record,
    platform_oos_window_frame,
    verify_tlt_monitor_view,
    wrap_tlt_duration_momentum_record,
)


class FakeConn:
    def __init__(self):
        self.calls = []

    def execute(self, statement, params=None):
        self.calls.append((str(statement), params))


def _tlt_path():
    path = DEFAULT_ARTIFACT_ROOT / "tlt_duration_momentum.json"
    assert path.is_file()
    return path


def test_official_tlt_artifact_wraps_ten_windows_and_identity():
    record = json.loads(_tlt_path().read_text(encoding="utf-8"))
    assert is_tlt_duration_momentum_record(record)
    assert record["strategy_id"] == STRATEGY_ID
    assert record["research_lineage_id"] == LINEAGE_ID
    assert record["economic_gate"] == "NOT_DEFINED"
    assert record["holdout_locked"] is True
    wrapped = wrap_tlt_duration_momentum_record(record)
    kinds = [kind for kind, _ in wrapped]
    assert kinds == ["run_summary", "oos_aggregate", "trials", "experiment_manifest"]
    summary = wrapped[0][1]["payload"]
    assert summary["strategy_id"] == STRATEGY_ID
    assert summary["research_lineage_id"] == LINEAGE_ID
    assert summary["research_kind"] == "platform_research"
    assert summary["research_mode"] == "ML_DISCOVERY"
    assert summary["strategy_family_id"] == "FIXED_INCOME_TREND"
    assert summary["asset_class"] == "BOND_ETF"
    assert summary["symbol"] == "TLT"
    assert summary["economic_gate"] == "NOT_DEFINED"
    assert summary["research_status"] == "COMPLETE"
    assert summary["promotion_gate"] == "HUMAN_REVIEW_REQUIRED"
    assert summary["holdout_status"] == "LOCKED"
    assert summary["economic_pass"] is None
    assert summary["holdout_accessed"] is False
    assert summary["selected_candidate"] == SELECTED_TRIAL
    assert summary["baseline_trial_id"] == BASELINE_TRIAL
    oos = wrapped[1][1]["payload"]["windows"]
    assert [row["window_id"] for row in oos] == list(WINDOW_IDS)
    for row in oos:
        pinned = OFFICIAL_WINDOWS[row["window_id"]]
        for key, value in pinned.items():
            assert row[key] == value
        assert not str(row.get("oos_end") or "").startswith("2025")
    view = verify_tlt_monitor_view(monitor_view_from_artifacts(wrapped))
    assert view["provenance_kind"] == "REAL_QC"
    assert view["economic_pass"] is None
    assert view["window_count"] == 10
    assert view["research_status"] == "COMPLETE"
    assert view["promotion_gate"] == "HUMAN_REVIEW_REQUIRED"
    assert view["holdout_status"] == "LOCKED"
    assert view["holdout_accessed"] is False
    assert view["display_name"] == "TLT Duration Momentum"
    assert view["delivery_status"] == "DELIVERED"
    assert view["strategy_definition"]["instrument"] == "TLT"
    assert "ret_1" in [str(item).lower() for item in view["strategy_definition"]["features"]]
    assert view["strategy_definition"]["winner"]["trial_id"] == SELECTED_TRIAL
    assert view["robustness_summary"]["selected_in"] == "10 / 10"
    frame = platform_oos_window_frame(view["oos_windows"])
    assert list(frame["window_id"]) == list(WINDOW_IDS)
    assert "2025" not in "".join(frame["oos_end"].astype(str))


def test_tlt_ingest_is_idempotent_and_registers_monitor_strategy():
    path = _tlt_path()
    conn = FakeConn()
    first = ingest_platform_files(conn, [path], root=DEFAULT_ARTIFACT_ROOT)
    second = ingest_platform_files(conn, [path], root=DEFAULT_ARTIFACT_ROOT)
    assert first["ingested"] == 4
    assert second["ingested"] == 4
    assert not first["errors"]
    assert not second["errors"]
    joined = " ".join(sql for sql, _ in conn.calls)
    assert "INSERT INTO research_runs" in joined
    assert "platform_research" in joined
    assert "INSERT INTO research_oos_windows" in joined
    assert "INSERT INTO strategies" in joined
    assert "objectstore" not in joined.lower()
    window_ids = {
        params["outer_window_id"]
        for sql, params in conn.calls
        if params and params.get("outer_window_id")
    }
    assert window_ids == set(WINDOW_IDS)
    qc_ids = {
        params["metrics_json"]
        for sql, params in conn.calls
        if params and params.get("outer_window_id") == "W2024"
    }
    assert qc_ids
    assert any(
        "377b2e086b8121fbe7dc3d28d8a1fe7b" in str(blob) and "7817e46209101c701ca93342a4c67059" in str(blob)
        for blob in qc_ids
    )


def test_tlt_labels_and_cli_dry_run(monkeypatch):
    labels = infer_research_labels(strategy_id=STRATEGY_ID, run_summary={})
    assert labels["research_mode"] == "ML_DISCOVERY"
    assert labels["asset_class"] == "BOND_ETF"
    assert labels["strategy_family"] == "FIXED_INCOME_TREND"
    built = build_platform_monitor_view(
        strategy_id=STRATEGY_ID,
        selected_run=RUN_ID,
        run_summary=normalize_platform_file(_tlt_path())[0][1],
        oos=normalize_platform_file(_tlt_path())[1][1],
        trials=normalize_platform_file(_tlt_path())[2][1],
    )
    assert built["research_mode_label"] == "ML Discovery"
    assert built["asset_class_label"] == "Bond ETF"
    assert built["display_name"] == "TLT Duration Momentum"
    assert built["delivery_status"] == "DELIVERED"
    assert built["strategy_definition"]["target"]
    assert built["metric_kind"] == "mean_across_windows"
    monkeypatch.delenv("DATABASE_URL", raising=False)
    monkeypatch.delenv("DB_HOST", raising=False)
    monkeypatch.delenv("DB_NAME", raising=False)
    monkeypatch.delenv("DB_USER", raising=False)
    assert ingest_main(["--root", str(_tlt_path()), "--dry-run", "--verify-monitor"]) == 0
    from qc_research.verify_tlt_monitor import main as verify_main

    assert verify_main(["--dry-run", "--root", str(_tlt_path())]) == 0
    assert verify_main(["--dry-run", "--apptest-preview", "--root", str(_tlt_path())]) == 0


def test_platform_section_treats_oos_window_lists_as_present():
    source = (DEFAULT_ARTIFACT_ROOT.parent.parent / "qc_research" / "ml_monitor_ui.py").read_text(encoding="utf-8")
    assert 'oos_windows") not in {None, UNAVAILABLE}' not in source
    assert "oos_windows is not None and oos_windows != UNAVAILABLE" in source
    assert "not in {None, UNAVAILABLE, {}}" not in source
    assert "if robustness is not None and robustness != UNAVAILABLE and robustness != {}:" in source


def test_generic_smoke_wrap_is_unchanged():
    smoke = json.loads((DEFAULT_ARTIFACT_ROOT / "ml_ridge_transport.json").read_text(encoding="utf-8"))
    assert not is_tlt_duration_momentum_record(smoke)
    wrapped = wrap_smoke_record(smoke)
    assert [kind for kind, _ in wrapped] == ["run_summary", "oos_aggregate"]
    assert len(wrapped[1][1]["payload"]["windows"]) == 2


def test_generic_canonical_artifact_needs_no_tlt_ui():
    from qc_research.platform_ingest import wrap_canonical_platform_record

    record = {
        "strategy_id": "FutureBondTrend",
        "research_lineage_id": "LINEAGE_FUTURE_BOND_TREND_V0",
        "research_kind": "platform_research",
        "research_mode": "ML_DISCOVERY",
        "strategy_family_id": "FIXED_INCOME_TREND",
        "asset_class": "BOND_ETF",
        "symbol": "IEF",
        "thesis": "Generic canonical artifact must render without TLT-specific UI.",
        "research_status": "COMPLETE",
        "promotion_gate": "HUMAN_REVIEW_REQUIRED",
        "holdout_status": "LOCKED",
        "economic_gate": "NOT_DEFINED",
        "holdout_locked": True,
        "model_family": "ridge",
        "selected_trial_id": "ridge::lb60_a1",
        "baseline_trial_id": "deterministic::sma60_long_cash",
        "search_space_hash": "abcd1234abcd1234",
        "provenance": "REAL_QC",
        "official_windows": [
            {
                "window_id": "W2019",
                "oos_start": "2019-01-02",
                "oos_end": "2019-12-31",
                "train_backtest_id": "train-ief",
                "winner_backtest_id": "win-ief",
                "baseline_backtest_id": "base-ief",
                "selected_trial_id": "ridge::lb60_a1",
                "ml": {"sharpe_ratio": 0.2, "cagr": 0.01},
                "baseline": {"sharpe_ratio": 0.1, "cagr": 0.005},
                "ml_minus_baseline": {"sharpe_ratio": 0.1},
            }
        ],
        "aggregate": {
            "windows": [
                {
                    "window_id": "W2019",
                    "oos_start": "2019-01-02",
                    "oos_end": "2019-12-31",
                    "train_backtest_id": "train-ief",
                    "winner_backtest_id": "win-ief",
                    "baseline_backtest_id": "base-ief",
                    "selected_trial_id": "ridge::lb60_a1",
                    "ml": {"sharpe_ratio": 0.2, "cagr": 0.01},
                    "baseline": {"sharpe_ratio": 0.1, "cagr": 0.005},
                    "ml_minus_baseline": {"sharpe_ratio": 0.1},
                }
            ],
            "aggregate": {
                "ml": {"sharpe_ratio": 0.2, "cagr": 0.01},
                "baseline": {"sharpe_ratio": 0.1, "cagr": 0.005},
                "ml_minus_baseline": {"sharpe_ratio": 0.1},
            },
        },
    }
    wrapped = wrap_canonical_platform_record(record)
    view = monitor_view_from_artifacts(wrapped)
    assert view["strategy_id"] == "FutureBondTrend"
    assert view["research_status"] == "COMPLETE"
    assert view["promotion_gate"] == "HUMAN_REVIEW_REQUIRED"
    assert view["holdout_status"] == "LOCKED"
    assert view["economic_gate"] == "NOT_DEFINED"
    assert view["window_count"] == 1
    assert view["selected_candidate"] == "ridge::lb60_a1"
    window_only = {
        "strategy_id": "FutureBondTrendWindowOnly",
        "research_lineage_id": "LINEAGE_FUTURE_BOND_TREND_WINDOW_ONLY_V0",
        "research_kind": "platform_research",
        "research_mode": "ML_DISCOVERY",
        "strategy_family_id": "FIXED_INCOME_TREND",
        "asset_class": "BOND_ETF",
        "research_status": "COMPLETE",
        "promotion_gate": "HUMAN_REVIEW_REQUIRED",
        "holdout_status": "LOCKED",
        "economic_gate": "NOT_DEFINED",
        "holdout_locked": True,
        "provenance": "REAL_QC",
        "official_windows": [
            {
                "window_id": "W2019",
                "oos_start": "2019-01-02",
                "oos_end": "2019-12-31",
                "selected_trial_id": "elasticnet::lb90_a0p1",
                "baseline_trial_id": "deterministic::sma90_long_cash",
            }
        ],
    }
    window_view = monitor_view_from_artifacts(wrap_canonical_platform_record(window_only))
    assert window_view["selected_candidate"] == "elasticnet::lb90_a0p1"
    assert window_view["baseline"] == "deterministic::sma90_long_cash"
    assert str(window_view["model_family"]).lower() == "elasticnet"
    source = (DEFAULT_ARTIFACT_ROOT.parent.parent / "qc_research" / "ml_monitor_ui.py").read_text(encoding="utf-8")
    assert 'if str(view.get("strategy_id") or "") == "TLTDurationMomentum"' not in source
    monitor = (DEFAULT_ARTIFACT_ROOT.parent.parent / "pages" / "strategy_monitor.py").read_text(encoding="utf-8")
    body = monitor.split("def _render_live_monitor_body")[1]
    assert body.index("render_platform_section") < body.index("_render_paper_and_execution")
    assert "if not show_platform:" in body
    rules_block = body.split("if not show_platform:")[1]
    assert "No structured rules stored for this strategy." in rules_block
    assert 'if str(view.get("strategy_id") or "") == "TLTDurationMomentum"' not in monitor
    assert "cron: \"23 * * * *\"" in (
        DEFAULT_ARTIFACT_ROOT.parent.parent / ".github" / "workflows" / "ingest_platform_research.yml"
    ).read_text(encoding="utf-8") or "23 * * * *" in (
        DEFAULT_ARTIFACT_ROOT.parent.parent / ".github" / "workflows" / "ingest_platform_research.yml"
    ).read_text(encoding="utf-8")


def test_legacy_human_review_required_is_not_research_terminal():
    from qc_research.lifecycle import normalize_research_lifecycle

    legacy = normalize_research_lifecycle(
        {
            "state": "HUMAN_REVIEW_REQUIRED",
            "economic_gate": "NOT_DEFINED",
            "holdout_locked": True,
            "window_count": 10,
            "official_windows": [{"window_id": "W2015"}],
        }
    )
    assert legacy["research_status"] == "COMPLETE"
    assert legacy["economic_gate"] == "NOT_DEFINED"
    assert legacy["promotion_gate"] == "HUMAN_REVIEW_REQUIRED"
    assert legacy["holdout_status"] == "LOCKED"


def test_generic_ingest_workflow_is_event_driven():
    workflow = (
        DEFAULT_ARTIFACT_ROOT.parent.parent / ".github" / "workflows" / "ingest_platform_research.yml"
    ).read_text(encoding="utf-8")
    tlt = (
        DEFAULT_ARTIFACT_ROOT.parent.parent / ".github" / "workflows" / "ingest_tlt_duration_momentum.yml"
    ).read_text(encoding="utf-8")
    assert "platform-research-ingest" in workflow
    assert "repository_dispatch" in workflow
    assert "ingest_platform_live.sh" in workflow
    assert "DO_SSH_KEY" in workflow
    assert "push:" not in tlt
    assert "superseded" in tlt.lower()
    verify = (
        DEFAULT_ARTIFACT_ROOT.parent.parent / ".github" / "workflows" / "platform_research_verify.yml"
    ).read_text(encoding="utf-8")
    assert "verify_tlt_monitor --live" in verify
    assert "Does not create QuantConnect jobs" in verify
    assert "DO_SSH_KEY" in verify
    from qc_research.fetch_remote_artifact import github_raw_url

    assert github_raw_url("hs1008/quant-strategies", "abc", "research/platform_smokes/x.json").endswith(
        "research/platform_smokes/x.json"
    )
