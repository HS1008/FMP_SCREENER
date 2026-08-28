from datetime import date
from pathlib import Path

from qc_research.holdout import STATUS_EXPOSED_PRIOR_TO_STAGE1
from scripts.verify_stage1_production import (
    check_schema,
    evaluate_legacy_and_holdout,
    evaluate_live_parser,
    format_report,
    redact,
    REQUIRED_BACKTEST_COLUMNS,
    REQUIRED_MIGRATION,
    REQUIRED_TABLES,
)


def test_schema_check_pass_and_fail():
    assert check_schema(set(REQUIRED_TABLES), set(REQUIRED_BACKTEST_COLUMNS), {REQUIRED_MIGRATION}) == []
    failures = check_schema({"backtests"}, set(), set())
    assert any("missing tables" in item for item in failures)
    assert any("backtests missing columns" in item for item in failures)
    assert any(REQUIRED_MIGRATION in item for item in failures)


def test_live_parser_qpv_holdings_pass():
    from jobs.sync_quantconnect import parse_portfolio

    raw = {
        "portfolio": {
            "cash": {"USD": {"valueInAccountCurrency": 388.12}},
            "holdings": {
                "SPY": {"q": 10.0, "p": 500.0, "v": 5000.0},
            },
        }
    }
    portfolio = parse_portfolio(raw)
    result = evaluate_live_parser(portfolio, raw)
    assert result["ok"] is True
    assert result["equity"] > 0
    assert result["holdings_value"] > 0
    assert result["position_count"] == 1
    assert result["equity_parser"] == "PASS"
    assert result["positions_parser"] == "PASS"


def test_live_parser_fails_when_holdings_collapse_to_zero():
    raw = {
        "portfolio": {
            "cash": {"USD": {"valueInAccountCurrency": 388.12}},
            "holdings": {
                "SPY": {"q": 10.0, "p": 500.0, "v": 5000.0},
            },
        }
    }
    broken = {
        "cash": 388.12,
        "holdings_value": 0.0,
        "equity": 388.12,
        "positions": [],
    }
    result = evaluate_live_parser(broken, raw)
    assert result["ok"] is False
    assert result["positions_parser"] == "FAIL"
    assert any("holdings_value is 0" in item for item in result["failures"])


def test_live_parser_fails_when_equity_not_positive():
    raw = {"portfolio": {"cash": {}, "holdings": {}}}
    empty = {"cash": 0.0, "holdings_value": 0.0, "equity": 0.0, "positions": []}
    result = evaluate_live_parser(empty, raw)
    assert result["ok"] is False
    assert result["equity_parser"] == "FAIL"


def test_holdout_requires_exposed_prior_when_overlap_exists():
    overlapping = [
        {
            "backtest_id": "legacy-full",
            "strategy_id": "SPYTrend",
            "research_run_id": None,
            "backtest_start": date(2010, 1, 1),
            "backtest_end": date(2026, 8, 25),
        }
    ]
    missing = evaluate_legacy_and_holdout(overlapping, [])
    assert missing["ok"] is False
    assert missing["historical_count"] == 1
    assert missing["holdout_status"] == "FAIL"

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
    assert present["dates_status"] == "PASS"
    assert present["holdout_status"] == STATUS_EXPOSED_PRIOR_TO_STAGE1
    assert present["historical_count"] == 1


def test_undated_legacy_rows_fail_hydration():
    rows = [
        {
            "backtest_id": "legacy-undated",
            "strategy_id": "SPYTrend",
            "research_run_id": None,
            "backtest_start": None,
            "backtest_end": None,
        }
    ]
    result = evaluate_legacy_and_holdout(rows, [])
    assert result["ok"] is False
    assert result["dates_status"] == "FAIL"


def test_report_does_not_include_secret_names_as_values():
    report = format_report(
        git_sha="abc123",
        working_tree="CLEAN",
        streamlit="ACTIVE",
        migration="PASS",
        schema="PASS",
        live_status="Running",
        live={
            "equity": 5400.0,
            "cash": 400.0,
            "holdings_value": 5000.0,
            "position_count": 1,
            "equity_parser": "PASS",
            "positions_parser": "PASS",
        },
        backtests={
            "dates_status": "PASS",
            "historical_count": 1,
            "holdout_status": STATUS_EXPOSED_PRIOR_TO_STAGE1,
        },
        overall="PASS",
    )
    assert "STAGE 1 PRODUCTION VERIFICATION" in report
    assert "Overall:" in report
    assert "PASS" in report
    assert "DB_PASSWORD" not in report
    assert "DATABASE_URL" not in report
    assert "QC_API_TOKEN" not in report


def test_redact_strips_password_urls_and_env_values(monkeypatch):
    monkeypatch.setenv("DB_PASSWORD", "super-secret-password")
    monkeypatch.setenv("QC_API_TOKEN", "qc-token-value")
    text = redact(
        "postgresql+psycopg2://user:super-secret-password@localhost/db token=qc-token-value"
    )
    assert "super-secret-password" not in text
    assert "qc-token-value" not in text
    assert "***" in text or "REDACTED" in text


def test_workflow_uses_existing_secrets_and_does_not_install_cron():
    workflow = (
        Path(__file__).resolve().parent.parent
        / ".github"
        / "workflows"
        / "stage1_verify.yml"
    ).read_text(encoding="utf-8")
    deploy = (
        Path(__file__).resolve().parent.parent / ".github" / "workflows" / "deploy.yml"
    ).read_text(encoding="utf-8")
    assert "workflow_dispatch" in workflow
    assert "secrets.DO_SSH_KEY" in workflow
    assert "secrets.DO_HOST" in workflow
    assert "secrets.DO_USER" in workflow
    assert "secrets.DO_SSH_KEY" in deploy
    uncommented = "\n".join(
        line for line in workflow.splitlines() if not line.lstrip().startswith("#")
    )
    assert "install_backtest_sync_cron" not in uncommented
    assert "systemctl restart" not in uncommented
    assert "final-holdout-from-run" not in workflow
    assert "backtest_orchestrator" not in workflow
    assert "--live-only" in workflow
    assert "--backtests-only" in workflow
    assert "python -m jobs.apply_migrations" in workflow
    assert "verify_stage1_production.py" in workflow
    assert "lean cloud backtest" not in workflow
    assert "BEGIN OPENSSH" not in workflow
    assert "-----BEGIN" not in workflow
