"""Writer jobs load /etc/fmp/fmp-writer.env, not Streamlit dashboard env."""

from __future__ import annotations

import os
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
CRON = (ROOT / "scripts" / "install_backtest_sync_cron.sh").read_text(encoding="utf-8")
SYNC = (ROOT / "jobs" / "sync_quantconnect.py").read_text(encoding="utf-8")
CONNECTION = (ROOT / "db" / "connection.py").read_text(encoding="utf-8")


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


def test_load_writer_dotenv_refused_when_streamlit_readonly(monkeypatch, tmp_path):
    from db.connection import WriterEngineRefused, load_writer_dotenv

    monkeypatch.setenv("FMP_STREAMLIT_READONLY", "1")
    writer = tmp_path / "fmp-writer.env"
    writer.write_text("DB_HOST=should-not-load\n", encoding="utf-8")
    monkeypatch.delenv("DB_HOST", raising=False)
    with pytest.raises(WriterEngineRefused, match="writer dotenv"):
        load_writer_dotenv(writer_env=str(writer), checkout_env=str(tmp_path / "missing.env"))
    assert os.environ.get("DB_HOST") in {None, ""}
