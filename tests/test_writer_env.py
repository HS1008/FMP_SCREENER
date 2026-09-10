"""Writer jobs load /etc/fmp/fmp-writer.env, not Streamlit dashboard env."""

from __future__ import annotations

import os
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
CRON = (ROOT / "scripts" / "install_backtest_sync_cron.sh").read_text(encoding="utf-8")
SYNC = (ROOT / "jobs" / "sync_quantconnect.py").read_text(encoding="utf-8")
CONNECTION = (ROOT / "db" / "connection.py").read_text(encoding="utf-8")
RELEASE = (ROOT / "scripts" / "deploy_release.sh").read_text(encoding="utf-8")
STAGE1_VERIFY = (ROOT / ".github" / "workflows" / "stage1_verify.yml").read_text(
    encoding="utf-8"
)


def test_cron_sources_writer_env_before_python_and_skips_dashboard_env():
    assert 'CHECKOUT_ENV="${LOCK_ROOT}/.env"' in CRON
    assert 'WRITER_ENV="/etc/fmp/fmp-writer.env"' in CRON
    assert "[ -f ${CHECKOUT_ENV} ] && . ${CHECKOUT_ENV}" in CRON
    assert "[ -f ${WRITER_ENV} ] && . ${WRITER_ENV}" in CRON
    assert CRON.index("[ -f ${CHECKOUT_ENV} ]") < CRON.index("[ -f ${WRITER_ENV} ]")
    assert "unset FMP_STREAMLIT_READONLY" in CRON
    assert "unset FMP_STREAMLIT_READONLY STREAMLIT_ALLOW_PROVIDER_FETCH DASHBOARD_ALLOW_WRITER_FALLBACK" in CRON
    line = [row for row in CRON.splitlines() if row.startswith("LINE=")][0]
    assert "fmp-dashboard.env" not in line
    assert "cd ${CODE_ROOT}" in line
    assert "jobs.sync_quantconnect --backtests-only" in line


def test_stage1_verify_sources_writer_env_before_immutable_migrate_and_sync():
    writer = STAGE1_VERIFY.split("Loading writer identity for immutable CODE_ROOT", 1)[1]
    writer = writer.split("Applying database migrations (idempotent)", 1)[0]
    assert ". /root/FMP_SCREENER/.env" in writer
    assert ". /etc/fmp/fmp-writer.env" in writer
    assert writer.index(". /root/FMP_SCREENER/.env") < writer.index(". /etc/fmp/fmp-writer.env")
    assert "unset FMP_STREAMLIT_READONLY STREAMLIT_ALLOW_PROVIDER_FETCH DASHBOARD_ALLOW_WRITER_FALLBACK" in writer
    assert "fmp-dashboard.env" not in writer
    assert STAGE1_VERIFY.index("Loading writer identity for immutable CODE_ROOT") < STAGE1_VERIFY.index(
        "python -m jobs.apply_migrations"
    )
    assert STAGE1_VERIFY.index("python -m jobs.apply_migrations") < STAGE1_VERIFY.index(
        "python -m jobs.sync_quantconnect --live-only"
    )


def test_deploy_release_sources_writer_env_before_preflight_migrate():
    preflight = RELEASE.split('if [ "$SKIP_PREFLIGHT" != 1 ]; then', 1)[1]
    preflight = preflight.split('if [ "$SKIP_IDENTITY" != 1 ]; then', 1)[0]
    assert "/etc/fmp/fmp-writer.env" in preflight
    assert preflight.index("/etc/fmp/fmp-writer.env") < preflight.index(
        "python -m jobs.apply_migrations"
    )
    assert "unset FMP_STREAMLIT_READONLY STREAMLIT_ALLOW_PROVIDER_FETCH DASHBOARD_ALLOW_WRITER_FALLBACK" in preflight
    assert "fmp-dashboard.env" not in preflight
    assert "/root/FMP_SCREENER/.env" not in preflight


def test_sync_quantconnect_uses_writer_dotenv_helper():
    assert "from dotenv import load_dotenv" not in SYNC
    assert "load_writer_dotenv()" in SYNC
    assert "load_dotenv()" not in SYNC
    assert CONNECTION.count("load_dotenv()") == 1
    assert "def load_writer_dotenv(" in CONNECTION
    assert 'WRITER_ENV_FILE = "/etc/fmp/fmp-writer.env"' in CONNECTION
    assert 'CHECKOUT_WRITER_ENV = "/root/FMP_SCREENER/.env"' in CONNECTION
    assert 'load_dotenv("/etc/fmp/fmp-dashboard.env")' not in CONNECTION


def test_load_writer_dotenv_prefers_writer_file_and_fills_checkout(tmp_path, monkeypatch):
    from db.connection import load_writer_dotenv

    writer = tmp_path / "fmp-writer.env"
    checkout = tmp_path / ".env"
    writer.write_text("DB_HOST=writer-host\nDB_USER=writer\n", encoding="utf-8")
    checkout.write_text(
        "DB_HOST=checkout-host\nQC_USER_ID=qc-user\nFMP_STREAMLIT_READONLY=1\n",
        encoding="utf-8",
    )
    monkeypatch.delenv("FMP_STREAMLIT_READONLY", raising=False)
    monkeypatch.delenv("DB_HOST", raising=False)
    monkeypatch.delenv("DB_USER", raising=False)
    monkeypatch.delenv("QC_USER_ID", raising=False)
    loaded = load_writer_dotenv(writer_env=str(writer), checkout_env=str(checkout))
    try:
        assert loaded == [str(writer), str(checkout)]
        assert os.environ["DB_HOST"] == "writer-host"
        assert os.environ["DB_USER"] == "writer"
        assert os.environ["QC_USER_ID"] == "qc-user"
        assert os.environ.get("FMP_STREAMLIT_READONLY") in {None, ""}
    finally:
        os.environ.pop("DB_HOST", None)
        os.environ.pop("DB_USER", None)
        os.environ.pop("QC_USER_ID", None)
        os.environ.pop("FMP_STREAMLIT_READONLY", None)


def test_get_engine_prefers_database_url_from_writer_env(tmp_path, monkeypatch):
    from db import connection as writer

    writer.reset_writer_engine_for_tests()
    writer_env = tmp_path / "fmp-writer.env"
    checkout = tmp_path / ".env"
    writer_env.write_text(
        "DATABASE_URL=postgresql://writer:secret@writer-host:5432/fmp\n",
        encoding="utf-8",
    )
    checkout.write_text(
        "DB_HOST=checkout-host\nDB_USER=checkout\nDB_NAME=checkout-db\nDB_PASSWORD=pw\n",
        encoding="utf-8",
    )
    monkeypatch.delenv("FMP_STREAMLIT_READONLY", raising=False)
    for key in ("DATABASE_URL", "DB_HOST", "DB_USER", "DB_NAME", "DB_PASSWORD", "DB_PORT"):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setattr(writer, "WRITER_ENV_FILE", str(writer_env))
    monkeypatch.setattr(writer, "CHECKOUT_WRITER_ENV", str(checkout))
    created: list[str] = []

    class FakeEngine:
        pass

    def fake_create(url, **kwargs):
        created.append(url)
        return FakeEngine()

    monkeypatch.setattr(writer, "create_engine", fake_create)
    try:
        engine = writer.get_engine()
        assert isinstance(engine, FakeEngine)
        assert created == ["postgresql+psycopg2://writer:secret@writer-host:5432/fmp"]
    finally:
        writer.reset_writer_engine_for_tests()
        for key in ("DATABASE_URL", "DB_HOST", "DB_USER", "DB_NAME", "DB_PASSWORD"):
            os.environ.pop(key, None)


def test_get_engine_falls_back_to_db_vars_when_url_missing(tmp_path, monkeypatch):
    from db import connection as writer

    writer.reset_writer_engine_for_tests()
    writer_env = tmp_path / "fmp-writer.env"
    checkout = tmp_path / ".env"
    writer_env.write_text("", encoding="utf-8")
    checkout.write_text(
        "DB_HOST=checkout-host\nDB_USER=checkout\nDB_NAME=checkout-db\nDB_PASSWORD=pw\n",
        encoding="utf-8",
    )
    monkeypatch.delenv("FMP_STREAMLIT_READONLY", raising=False)
    for key in ("DATABASE_URL", "DB_HOST", "DB_USER", "DB_NAME", "DB_PASSWORD", "DB_PORT"):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setattr(writer, "WRITER_ENV_FILE", str(writer_env))
    monkeypatch.setattr(writer, "CHECKOUT_WRITER_ENV", str(checkout))
    created: list[str] = []

    def fake_create(url, **kwargs):
        created.append(url)
        return object()

    monkeypatch.setattr(writer, "create_engine", fake_create)
    try:
        writer.get_engine()
        assert created == ["postgresql+psycopg2://checkout:pw@checkout-host:5432/checkout-db"]
    finally:
        writer.reset_writer_engine_for_tests()
        for key in ("DATABASE_URL", "DB_HOST", "DB_USER", "DB_NAME", "DB_PASSWORD"):
            os.environ.pop(key, None)


def test_get_engine_refuses_when_neither_url_nor_db_vars(monkeypatch):
    from db import connection as writer

    writer.reset_writer_engine_for_tests()
    monkeypatch.delenv("FMP_STREAMLIT_READONLY", raising=False)
    for key in ("DATABASE_URL", "DB_HOST", "DB_USER", "DB_NAME", "DB_PASSWORD"):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setattr(writer, "load_writer_dotenv", lambda **kwargs: [])
    monkeypatch.setattr(
        writer,
        "create_engine",
        lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("must not connect")),
    )
    with pytest.raises(RuntimeError, match="DATABASE_URL or DB_HOST"):
        writer.get_engine()
    writer.reset_writer_engine_for_tests()


def test_load_writer_dotenv_refused_when_streamlit_readonly(monkeypatch, tmp_path):
    from db.connection import WriterEngineRefused, load_writer_dotenv

    monkeypatch.setenv("FMP_STREAMLIT_READONLY", "1")
    env_file = tmp_path / "fmp-writer.env"
    env_file.write_text("DB_HOST=should-not-load\n", encoding="utf-8")
    monkeypatch.delenv("DB_HOST", raising=False)
    with pytest.raises(WriterEngineRefused, match="writer dotenv"):
        load_writer_dotenv(writer_env=str(env_file), checkout_env=str(tmp_path / "missing.env"))
    assert os.environ.get("DB_HOST") in {None, ""}
