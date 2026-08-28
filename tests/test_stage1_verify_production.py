from datetime import date, datetime
from pathlib import Path

from qc_research.holdout import STATUS_EXPOSED_PRIOR_TO_STAGE1
from scripts.verify_stage1_production import (
    EXPECTED_RESEARCH_PROJECT_ID,
    EXPECTED_RESEARCH_PROJECT_NAME,
    check_schema,
    evaluate_legacy_and_holdout,
    evaluate_live_parser,
    evaluate_research_and_smoke,
    evaluate_stage1_run,
    evaluate_working_tree,
    format_report,
    format_working_tree_report,
    redact,
    REQUIRED_BACKTEST_COLUMNS,
    REQUIRED_MIGRATION,
    REQUIRED_RESEARCH_PROJECT_MIGRATION,
    REQUIRED_TABLES,
)


def test_schema_check_pass_and_fail():
    assert check_schema(
        set(REQUIRED_TABLES),
        set(REQUIRED_BACKTEST_COLUMNS),
        {REQUIRED_MIGRATION, REQUIRED_RESEARCH_PROJECT_MIGRATION},
    ) == []
    failures = check_schema({"backtests"}, set(), set())
    assert any("missing tables" in item for item in failures)
    assert any("backtests missing columns" in item for item in failures)
    assert any(REQUIRED_MIGRATION in item for item in failures)
    missing_research = check_schema(
        set(REQUIRED_TABLES),
        set(REQUIRED_BACKTEST_COLUMNS),
        {REQUIRED_MIGRATION},
    )
    assert any(REQUIRED_RESEARCH_PROJECT_MIGRATION in item for item in missing_research)


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


def _workflow_text(name: str) -> str:
    return (
        Path(__file__).resolve().parent.parent / ".github" / "workflows" / name
    ).read_text(encoding="utf-8")


def test_workflow_uses_existing_secrets_and_does_not_install_cron():
    workflow = _workflow_text("stage1_verify.yml")
    deploy = _workflow_text("deploy.yml")
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


def test_deploy_installs_backtest_sync_cron_after_migrations():
    deploy = _workflow_text("deploy.yml")
    verify = _workflow_text("stage1_verify.yml")
    assert "install_backtest_sync_cron.sh" in deploy
    assert "python -m jobs.apply_migrations" in deploy
    assert deploy.index("apply_migrations") < deploy.index("install_backtest_sync_cron")
    assert deploy.index("install_backtest_sync_cron") < deploy.index("systemctl restart")
    uncommented_verify = "\n".join(
        line for line in verify.splitlines() if not line.lstrip().startswith("#")
    )
    assert "install_backtest_sync_cron" not in uncommented_verify


def test_verify_workflow_runs_after_successful_main_deploy_only():
    workflow = _workflow_text("stage1_verify.yml")
    deploy = _workflow_text("deploy.yml")
    deploy_name = None
    for line in deploy.splitlines():
        if line.startswith("name:"):
            deploy_name = line.split(":", 1)[1].strip()
            break
    assert deploy_name == "Deploy FMP Dashboard"
    assert "workflow_run:" in workflow
    assert "Deploy FMP Dashboard" in workflow
    assert "workflow_dispatch:" in workflow
    assert "types:" in workflow
    assert "completed" in workflow
    assert "github.event_name == 'workflow_dispatch'" in workflow
    assert "github.event.workflow_run.conclusion == 'success'" in workflow
    assert "Trigger: automatic after Deploy FMP Dashboard" in workflow
    assert "Trigger: manual workflow_dispatch" in workflow
    assert "Deploy workflow run ID:" in workflow
    # Recursion guard: verification listens only for the deploy workflow.
    on_block = workflow.split("\non:", 1)[1].split("\njobs:", 1)[0]
    assert "Deploy FMP Dashboard" in on_block
    assert "- Stage 1 Production Verification" not in on_block
    assert "branches:" in on_block
    assert "- main" in on_block
    uncommented = "\n".join(
        line for line in workflow.splitlines() if not line.lstrip().startswith("#")
    )
    assert "systemctl restart" not in uncommented
    assert "--working-tree-only" in workflow
    assert "--untracked-files=no" not in workflow
    assert 'if [ -n "$(git status --porcelain)" ]' not in workflow


def test_working_tree_clean_passes():
    result = evaluate_working_tree("")
    assert result["ok"] is True
    assert result["tracked_status"] == "CLEAN"
    assert result["working_tree_check"] == "PASS"
    report = format_working_tree_report(result)
    assert "Working tree tracked files: CLEAN" in report
    assert "Unexpected untracked files: NONE" in report
    assert report.strip().endswith("PASS")


def test_working_tree_allows_known_server_only_files():
    result = evaluate_working_tree("?? test_db.py\n?? update_dashboard.sh\n")
    assert result["ok"] is True
    assert result["tracked_status"] == "CLEAN"
    assert result["allowed_untracked"] == ["test_db.py", "update_dashboard.sh"]
    report = format_working_tree_report(result)
    assert "  test_db.py" in report
    assert "  update_dashboard.sh" in report
    assert "Unexpected untracked files: NONE" in report
    assert result["working_tree_check"] == "PASS"


def test_working_tree_fails_on_unexpected_untracked():
    result = evaluate_working_tree("?? random_file.py\n")
    assert result["ok"] is False
    assert "random_file.py" in result["unexpected_untracked"]
    assert result["working_tree_check"] == "FAIL"


def test_working_tree_fails_on_modified_tracked_file():
    result = evaluate_working_tree(" M jobs/sync_quantconnect.py\n")
    assert result["ok"] is False
    assert result["tracked_status"] == "DIRTY"
    assert any("jobs/sync_quantconnect.py" in item for item in result["tracked"])


def test_working_tree_fails_on_added_tracked_file():
    result = evaluate_working_tree("A  unexpected_tracked_file.py\n")
    assert result["ok"] is False
    assert any("unexpected_tracked_file.py" in item for item in result["tracked"])


def test_working_tree_fails_when_known_files_plus_unexpected():
    result = evaluate_working_tree(
        "?? test_db.py\n?? update_dashboard.sh\n?? malicious.py\n"
    )
    assert result["ok"] is False
    assert "malicious.py" in result["unexpected_untracked"]
    assert "test_db.py" in result["allowed_untracked"]


def test_working_tree_does_not_allow_nested_allowlist_paths():
    result = evaluate_working_tree("?? extra/test_db.py\n")
    assert result["ok"] is False
    assert "extra/test_db.py" in result["unexpected_untracked"]


def test_production_verification_and_cron_share_the_same_backtest_sync_lock():
    from jobs.sync_quantconnect import (
        BACKTEST_SYNC_LOCK_RELATIVE,
        BACKTEST_SYNC_LOCK_WAIT_SECONDS,
    )

    verify = _workflow_text("stage1_verify.yml")
    cron = (
        Path(__file__).resolve().parent.parent
        / "scripts"
        / "install_backtest_sync_cron.sh"
    ).read_text(encoding="utf-8")
    assert BACKTEST_SYNC_LOCK_RELATIVE == "outputs/backtest_sync.flock"
    assert BACKTEST_SYNC_LOCK_RELATIVE in verify
    assert BACKTEST_SYNC_LOCK_RELATIVE in cron
    assert "flock -w {0}".format(BACKTEST_SYNC_LOCK_WAIT_SECONDS) in verify
    assert "flock -n" in cron
    uncommented = "\n".join(
        line for line in verify.splitlines() if not line.lstrip().startswith("#")
    )
    live_line = [
        line for line in uncommented.splitlines() if "--live-only" in line
    ][0]
    backtest_lines = [
        line for line in uncommented.splitlines() if "--backtests-only" in line
    ]
    assert "flock" not in live_line
    assert any("flock -w" in line for line in backtest_lines) or "flock -w" in uncommented
    assert "python -m jobs.sync_quantconnect --live-only" in uncommented
    assert "python -m jobs.sync_quantconnect --backtests-only" in uncommented


def _strategy(**kwargs):
    row = {
        "strategy_id": "SPYTrend",
        "qc_project_id": "111",
        "qc_research_project_id": EXPECTED_RESEARCH_PROJECT_ID,
        "qc_research_project_name": EXPECTED_RESEARCH_PROJECT_NAME,
    }
    row.update(kwargs)
    return row


def _smoke_row(**kwargs):
    row = {
        "backtest_id": "3964761d8996893b047591df5d876d88",
        "name": "S1__SPYTrend__SMOKE_SPYTrend_156c40e7_4d30f55e7f65__SMOKE__DEV_SMOKE__001",
        "strategy_id": "SPYTrend",
        "research_run_id": "SMOKE_SPYTrend_156c40e7_4d30f55e7f65",
        "research_test_type": "SMOKE",
        "research_is_holdout": False,
        "backtest_start": date(2017, 1, 1),
        "backtest_end": date(2018, 12, 31),
    }
    row.update(kwargs)
    return row


def test_research_and_smoke_ingest_pass():
    points = [
        {"timestamp": datetime(2017, 3, 1)},
        {"timestamp": datetime(2018, 6, 15)},
    ]
    research, smoke = evaluate_research_and_smoke(
        _strategy(), [_smoke_row()], points
    )
    assert research["status"] == "PASS"
    assert research["project_id"] == EXPECTED_RESEARCH_PROJECT_ID
    assert smoke["status"] == "PASS"
    assert smoke["count"] == 1
    assert smoke["equity_count"] == 2
    assert smoke["equity_years"] == "2017-2018"
    assert smoke["research_is_holdout"] is False


def test_research_id_null_and_missing_smoke_fail():
    research, smoke = evaluate_research_and_smoke(
        _strategy(qc_research_project_id=None),
        [{"research_test_type": "BASELINE_DEV"}],
        [],
    )
    assert research["status"] == "FAIL"
    assert smoke["status"] == "FAIL"
    assert any("NULL" in item for item in research["failures"])
    assert any("no SMOKE" in item for item in smoke["failures"])


def test_research_id_must_not_match_execution_project():
    research, smoke = evaluate_research_and_smoke(
        _strategy(qc_research_project_id="111", qc_project_id="111"),
        [_smoke_row()],
        [{"timestamp": datetime(2017, 1, 3)}, {"timestamp": datetime(2018, 1, 3)}],
    )
    assert research["status"] == "FAIL"
    assert any("fallback" in item for item in research["failures"])


def test_smoke_equity_in_2026_fails():
    research, smoke = evaluate_research_and_smoke(
        _strategy(),
        [_smoke_row()],
        [{"timestamp": datetime(2026, 8, 28)}, {"timestamp": datetime(2026, 8, 29)}],
    )
    assert research["status"] == "PASS"
    assert smoke["status"] == "FAIL"
    assert any("2017-2018" in item for item in smoke["failures"])


def test_stage1_run_skipped_when_absent():
    result = evaluate_stage1_run([], [])
    assert result["status"] == "SKIP"
    assert result["present"] is False
    assert result["failures"] == []


def test_stage1_run_complete_81_passes():
    result = evaluate_stage1_run(
        [
            {
                "research_run_id": "STAGE1_SPYTrend_156c40e7",
                "run_status": "COMPLETE",
                "expected_experiment_count": 81,
                "completed_count": 81,
                "failed_count": 0,
                "skipped_count": 0,
            }
        ],
        [{"research_run_id": "STAGE1_SPYTrend_156c40e7", "research_test_type": "BASELINE_DEV"}],
    )
    assert result["status"] == "PASS"
    assert result["present"] is True
    assert result["failures"] == []


def test_stage1_run_incomplete_skipped_oos_passes_terminal_check():
    result = evaluate_stage1_run(
        [
            {
                "research_run_id": "STAGE1_SPYTrend_156c40e7",
                "run_status": "INCOMPLETE",
                "expected_experiment_count": 81,
                "completed_count": 80,
                "failed_count": 0,
                "skipped_count": 1,
            }
        ],
        [],
    )
    assert result["status"] == "PASS"
    assert result["run_status"] == "INCOMPLETE"


def test_stage1_run_in_progress_after_summary_fails():
    result = evaluate_stage1_run(
        [
            {
                "research_run_id": "STAGE1_SPYTrend_156c40e7",
                "run_status": "IN_PROGRESS",
                "expected_experiment_count": 81,
                "completed_count": 80,
                "skipped_count": 0,
            }
        ],
        [],
    )
    assert result["status"] == "FAIL"
    assert any("IN_PROGRESS" in item for item in result["failures"])


def test_stage1_run_rejects_smoke_contamination():
    result = evaluate_stage1_run(
        [
            {
                "research_run_id": "STAGE1_SPYTrend_156c40e7",
                "run_status": "COMPLETE",
                "expected_experiment_count": 81,
            }
        ],
        [
            {
                "research_run_id": "STAGE1_SPYTrend_156c40e7",
                "research_test_type": "SMOKE",
            }
        ],
    )
    assert result["status"] == "FAIL"
    assert any("SMOKE" in item for item in result["failures"])
