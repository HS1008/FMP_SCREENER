"""Immutable release script exists beside the live git-pull deploy."""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def test_release_script_is_additive_and_supports_rollback():
    script = (ROOT / "scripts" / "deploy_release.sh").read_text(encoding="utf-8")
    assert "/opt/fmp/releases" in script
    assert "--rollback" in script
    rollback_block = script.split('if [ "$ROLLBACK" = 1 ]; then', 1)[1].split('if [ -z "$SHA" ]; then', 1)[0]
    assert "qc_research.contracts.digests" in rollback_block
    assert "scripts/provision_dashboard_readonly.sh --require" in rollback_block
    assert "scripts/verify_dashboard_identity.sh" in rollback_block
    assert rollback_block.index("verify_dashboard_identity.sh") < rollback_block.index(
        "systemctl restart fmp-dashboard"
    )
    assert "rollback_identity=skipped" in rollback_block
    assert "--skip-restart" in script
    assert "git reset --hard" not in script
    assert 'git -C "$target" fetch --depth 1 origin "$SHA"' in script
    assert 'git -C "$target" checkout --detach "$SHA"' in script
    assert "does not match requested" in script
    fetch_block = script.split("if [ ! -d \"$target/.git\" ]; then", 1)[1]
    assert 'git -C "$target" fetch --depth 1 origin "$SHA"' not in fetch_block.split("fi", 1)[0]
    deploy = (ROOT / ".github" / "workflows" / "deploy.yml").read_text(encoding="utf-8")
    assert "git pull --ff-only origin main" in deploy
    assert "qc_research.contracts.digests" in deploy
    assert "scripts/provision_dashboard_readonly.sh --require" in deploy
    assert deploy.index("scripts/provision_dashboard_readonly.sh --require") < deploy.index(
        "scripts/verify_dashboard_identity.sh"
    )
    assert "scripts/verify_dashboard_identity.sh" in deploy
    assert "FMP_IDENTITY_ENV_ONLY=1" in deploy
    assert "FMP_DASHBOARD_ENV=/etc/fmp/fmp-dashboard.env" in deploy
    assert deploy.index("FMP_IDENTITY_ENV_ONLY=1") < deploy.index("scripts/verify_dashboard_identity.sh")
    assert "/opt/fmp/current/scripts/verify_dashboard_identity.sh" in deploy
    assert "immutable current is missing scripts/verify_dashboard_identity.sh" in deploy
    assert "source /opt/fmp/current/venv/bin/activate" in deploy
    assert "PYTHONPATH=/opt/fmp/current" in deploy
    immutable_verify = deploy.split("Verifying Streamlit identity from immutable release checkout", 1)[1]
    assert immutable_verify.index("source /opt/fmp/current/venv/bin/activate") < immutable_verify.index(
        "scripts/verify_dashboard_identity.sh"
    )
    assert "--skip-identity" in script
    assert "DEPLOY_BREAK_GLASS" in script
    assert "refusing --skip-identity without DEPLOY_BREAK_GLASS=1" in script
    assert "--skip-identity" not in deploy
    assert "DEPLOY_BREAK_GLASS" not in deploy
    assert "qc_research.contracts.digests" in script
    assert "scripts/provision_dashboard_readonly.sh --require" in script
    assert "scripts/verify_dashboard_identity.sh" in script
    assert 'python3 -m venv "$target/venv"' in script
    assert "release venv is missing streamlit" in script
    assert "layout_only_skip_venv=1" in script
    assert script.index('python3 -m venv "$target/venv"') < script.index('if [ "$SKIP_PREFLIGHT" != 1 ]')
    identity_block = script[script.index('if [ "$SKIP_IDENTITY" != 1 ]'):]
    assert "qc_research.contracts.digests" in identity_block
    assert "provision_dashboard_readonly.sh --require" in identity_block
    assert "FMP_IDENTITY_ENV_ONLY=1" in identity_block
    assert "/etc/fmp/fmp-dashboard.env" in identity_block
    assert "/root/FMP_SCREENER/.env" not in identity_block
    preflight_block = script[script.index('if [ "$SKIP_PREFLIGHT" != 1 ]'):script.index('if [ "$SKIP_IDENTITY" != 1 ]')]
    assert "provision_dashboard_readonly.sh" not in preflight_block
    assert "verify_dashboard_identity.sh" not in preflight_block
    assert "--verify-rc" in deploy
    assert "dashboard identity verify failed" in deploy
    assert deploy.index("jobs.report_deploy_identity") < deploy.index("systemctl restart fmp-dashboard")
    assert deploy.index("--verify-rc") < deploy.index("systemctl restart fmp-dashboard")
    assert "/opt/fmp/releases" in deploy
    assert "--skip-restart" in deploy
    assert "--skip-identity" not in deploy
    assert "FMP_IMMUTABLE_RELEASE_STRICT" in deploy
    assert "immutable release populate failed" in deploy
    assert "FMP_IMMUTABLE_RELEASE_STRICT=1" not in deploy.split("immutable release populate failed", 1)[1][:80]
    assert "jobs.report_deploy_identity" in deploy
    assert "jobs.record_deploy_identity_db" in deploy
    assert "jobs.audit_host_dashboard" in deploy
    assert "--require-readonly" in deploy
    assert "/var/lib/fmp/deploy/host_audit.json" in deploy
    assert deploy.index("jobs.report_deploy_identity") < deploy.index("jobs.record_deploy_identity_db")
    assert deploy.index("jobs.record_deploy_identity_db") < deploy.index("jobs.audit_host_dashboard")
    assert deploy.index("jobs.audit_host_dashboard") < deploy.index("systemctl restart fmp-dashboard")
    assert deploy.index("/etc/fmp/fmp-dashboard.env") < deploy.index("jobs.audit_host_dashboard")
    record_prefix = deploy.split("jobs.record_deploy_identity_db", 1)[0]
    assert "/etc/fmp/fmp-writer.env" in record_prefix[-500:]
    assert "/root/FMP_SCREENER/.env" in record_prefix[-500:]
    assert record_prefix.rfind("/etc/fmp/fmp-writer.env") > record_prefix.rfind(
        "Persisting sanitized deploy identity"
    )
    assert "/etc/fmp/fmp-dashboard.env" not in record_prefix.split("Persisting sanitized deploy identity", 1)[-1]
    report_block = deploy.split("Recording deploy identity", 1)[1].split(
        "Persisting sanitized deploy identity", 1
    )[0]
    assert "/etc/fmp/fmp-dashboard.env" in report_block
    assert "jobs.report_deploy_identity" in report_block
    assert "--env-file /etc/fmp/fmp-dashboard.env" in report_block
    assert "unset DATABASE_URL" in report_block
    assert "source /root/FMP_SCREENER/.env" not in report_block
    assert ". /root/FMP_SCREENER/.env" not in report_block
    assert "jobs.cutover_dashboard_systemd" in deploy
    assert "qc_research.verify_csfml_v1 --live" in deploy
    assert "--require-present" not in deploy
    assert "--apply" not in deploy
    assert "/var/lib/fmp/deploy/cutover_readiness.json" in deploy
    assert deploy.index("jobs.cutover_dashboard_systemd") < deploy.index("systemctl restart fmp-dashboard")
    assert "systemctl restart fmp-dashboard" in deploy
    assert (ROOT / "docs" / "IMMUTABLE_DEPLOY.md").is_file()
    docs = (ROOT / "docs" / "IMMUTABLE_DEPLOY.md").read_text(encoding="utf-8")
    assert "/var/lib/fmp/streamlit" in docs
    assert "DASHBOARD_READONLY_URL" in docs
    assert "FMP_IDENTITY_ENV_ONLY=1" in docs
    assert "does not source `/root/FMP_SCREENER/.env`" in docs
    assert "Inherited writer keys" in docs
    assert "activate_market_intelligence_host.sh" in docs
    assert "creating `$target/venv`" in docs or "bootable" in docs.lower()
    example = (ROOT / "deploy" / "fmp-dashboard.service.example").read_text(encoding="utf-8")
    assert "DASHBOARD_READONLY_URL" in example
    assert "Environment=FMP_STREAMLIT_READONLY=1" in example
    env = (ROOT / "deploy" / "fmp-dashboard.env.example").read_text(encoding="utf-8")
    assert "DASHBOARD_READONLY_URL=" in env
    assert "FMP_STREAMLIT_READONLY=1" in env
    assert "mi_readonly" not in env.split("DASHBOARD_READONLY_URL=", 1)[1].splitlines()[0]


def test_release_script_refuses_skips_without_break_glass(tmp_path):
    env = os.environ.copy()
    env.pop("DEPLOY_BREAK_GLASS", None)
    env["FMP_CURRENT_LINK"] = str(tmp_path / "current")
    env["FMP_PREVIOUS_LINK"] = str(tmp_path / "previous")
    result = subprocess.run(
        [
            "bash",
            str(ROOT / "scripts" / "deploy_release.sh"),
            "--sha",
            "abc123",
            "--skip-identity",
            "--skip-preflight",
            "--skip-restart",
        ],
        capture_output=True,
        text=True,
        env=env,
        check=False,
    )
    assert result.returncode == 4
    assert "refusing --skip-identity without DEPLOY_BREAK_GLASS=1" in result.stdout


def test_release_script_symlink_layout_without_host_restart(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init"], cwd=repo, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.name", "test"], cwd=repo, check=True)
    (repo / "README").write_text("release\n", encoding="utf-8")
    subprocess.run(["git", "add", "README"], cwd=repo, check=True, capture_output=True)
    subprocess.run(["git", "commit", "-m", "seed"], cwd=repo, check=True, capture_output=True)
    sha = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=repo, text=True).strip()
    release_root = tmp_path / "releases"
    current = tmp_path / "current"
    previous = tmp_path / "previous"
    env = os.environ.copy()
    env["FMP_CURRENT_LINK"] = str(current)
    env["FMP_PREVIOUS_LINK"] = str(previous)
    env["DEPLOY_BREAK_GLASS"] = "1"
    result = subprocess.run(
        [
            "bash",
            str(ROOT / "scripts" / "deploy_release.sh"),
            "--sha",
            sha,
            "--repo",
            str(repo),
            "--release-root",
            str(release_root),
            "--skip-restart",
            "--skip-preflight",
            "--skip-identity",
        ],
        capture_output=True,
        text=True,
        env=env,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert (release_root / sha / "README").is_file()
    assert current.resolve() == (release_root / sha).resolve()
    assert (
        subprocess.check_output(["git", "-C", str(release_root / sha), "rev-parse", "HEAD"], text=True).strip()
        == sha
    )


def test_release_script_rebinds_an_existing_tree_to_the_requested_sha(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init"], cwd=repo, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.name", "test"], cwd=repo, check=True)
    (repo / "README").write_text("first\n", encoding="utf-8")
    subprocess.run(["git", "add", "README"], cwd=repo, check=True, capture_output=True)
    subprocess.run(["git", "commit", "-m", "first"], cwd=repo, check=True, capture_output=True)
    first = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=repo, text=True).strip()
    (repo / "README").write_text("second\n", encoding="utf-8")
    subprocess.run(["git", "add", "README"], cwd=repo, check=True, capture_output=True)
    subprocess.run(["git", "commit", "-m", "second"], cwd=repo, check=True, capture_output=True)
    second = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=repo, text=True).strip()
    release_root = tmp_path / "releases"
    env = os.environ.copy()
    env["FMP_CURRENT_LINK"] = str(tmp_path / "current")
    env["FMP_PREVIOUS_LINK"] = str(tmp_path / "previous")
    env["DEPLOY_BREAK_GLASS"] = "1"
    first_run = subprocess.run(
        [
            "bash",
            str(ROOT / "scripts" / "deploy_release.sh"),
            "--sha",
            first,
            "--repo",
            str(repo),
            "--release-root",
            str(release_root),
            "--skip-restart",
            "--skip-preflight",
            "--skip-identity",
        ],
        capture_output=True,
        text=True,
        env=env,
        check=False,
    )
    assert first_run.returncode == 0, first_run.stderr
    poisoned = release_root / second
    poisoned.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(["cp", "-a", str(release_root / first), str(poisoned)], check=True)
    assert (poisoned / "README").read_text(encoding="utf-8") == "first\n"
    rebound = subprocess.run(
        [
            "bash",
            str(ROOT / "scripts" / "deploy_release.sh"),
            "--sha",
            second,
            "--repo",
            str(repo),
            "--release-root",
            str(release_root),
            "--skip-restart",
            "--skip-preflight",
            "--skip-identity",
        ],
        capture_output=True,
        text=True,
        env=env,
        check=False,
    )
    assert rebound.returncode == 0, rebound.stderr
    assert (poisoned / "README").read_text(encoding="utf-8") == "second\n"
    assert (
        subprocess.check_output(["git", "-C", str(poisoned), "rev-parse", "HEAD"], text=True).strip()
        == second
    )


def test_report_deploy_identity_writes_no_secrets(tmp_path, monkeypatch):
    from jobs.report_deploy_identity import build_record, write_record

    monkeypatch.setenv("DASHBOARD_READONLY_URL", "postgresql://dashboard_readonly:secret@127.0.0.1/fmp")
    monkeypatch.delenv("FMP_DASHBOARD_ENV", raising=False)
    monkeypatch.delenv("DASHBOARD_ALLOW_WRITER_FALLBACK", raising=False)
    monkeypatch.delenv("STREAMLIT_ALLOW_PROVIDER_FETCH", raising=False)
    for key in (
        "DATABASE_URL",
        "MARKET_INTELLIGENCE_DATABASE_URL",
        "DB_PASSWORD",
        "DB_HOST",
        "DB_USER",
        "DB_NAME",
        "DB_PORT",
    ):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("FMP_DASHBOARD_READONLY_PW", str(tmp_path / "missing.pw"))
    monkeypatch.setenv("FMP_CURRENT_LINK", str(tmp_path / "missing-current"))
    monkeypatch.setenv("FMP_SYSTEMD_EXEC_START", "/root/FMP_SCREENER/venv/bin/streamlit run dashboard.py")
    path = tmp_path / "current.json"
    record = build_record(
        sha="abc123",
        checkout="/root/FMP_SCREENER",
        mode="git_pull",
        immutable_rc=0,
        verify_rc=3,
    )
    write_record(record, path)
    text = path.read_text(encoding="utf-8")
    assert "secret" not in text
    assert "postgresql://" not in text
    assert record["dashboard_readonly_url_set"] is True
    assert record["dashboard_readonly_password_file_present"] is False
    assert record["writer_fallback"] is False
    assert record["writer_env_keys_present"] == []
    assert record["provider_fetch"] is False
    assert record["systemd_still_git_pull"] is True
    assert record["systemd_cutover_proven"] is False
    assert record["readonly_verify_rc"] == 3
    assert record["readonly_proven"] is False
    assert record["csfml_v1_label_integrity"] == "CANNOT_RULE_OUT"
    assert record["csfml_v1_rerun_authorized"] is False


def test_report_deploy_identity_loads_url_from_env_file(tmp_path, monkeypatch):
    from jobs.report_deploy_identity import build_record

    monkeypatch.delenv("FMP_DASHBOARD_ENV", raising=False)
    monkeypatch.delenv("DASHBOARD_READONLY_URL", raising=False)
    monkeypatch.delenv("DASHBOARD_ALLOW_WRITER_FALLBACK", raising=False)
    monkeypatch.delenv("STREAMLIT_ALLOW_PROVIDER_FETCH", raising=False)
    monkeypatch.setenv("DATABASE_URL", "postgresql://fmp:secret@127.0.0.1/fmp")
    env_file = tmp_path / "fmp-dashboard.env"
    env_file.write_text(
        "DASHBOARD_READONLY_URL=postgresql://dashboard_readonly:secret@127.0.0.1/fmp\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("FMP_DASHBOARD_READONLY_PW", str(tmp_path / "missing.pw"))
    monkeypatch.setenv("FMP_CURRENT_LINK", str(tmp_path / "missing-current"))
    monkeypatch.setenv("FMP_SYSTEMD_EXEC_START", "/root/FMP_SCREENER/venv/bin/streamlit run dashboard.py")
    record = build_record(
        sha="abc123",
        checkout="/root/FMP_SCREENER",
        mode="git_pull",
        immutable_rc=0,
        verify_rc=0,
        env_file=env_file,
    )
    assert record["dashboard_readonly_url_set"] is True
    assert record["writer_env_keys_present"] == []
    assert record["readonly_proven"] is False


def test_data_health_keeps_ops_off_main_pages():
    ui = (ROOT / "market_intelligence" / "pages_ui.py").read_text(encoding="utf-8")
    assert "Platform ops summary" in ui
    assert "dashboard_readonly_proven" in ui
    assert "deploy_git_sha" in ui
    dashboard = (ROOT / "dashboard.py").read_text(encoding="utf-8")
    assert "Platform ops summary" not in dashboard
    assert "dashboard_readonly_proven" not in dashboard
    pages = "".join(path.read_text(encoding="utf-8") for path in (ROOT / "pages").glob("*.py"))
    assert "/var/lib/fmp/deploy" not in pages
    assert "/var/lib/fmp/deploy" not in ui
