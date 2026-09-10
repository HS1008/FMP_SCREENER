"""Dashboard engine requires DASHBOARD_READONLY_URL and never uses mi_readonly."""

from __future__ import annotations

import os

import pytest

from db import dashboard_engine as module


@pytest.fixture(autouse=True)
def _reset_engine():
    module.reset_dashboard_engine_for_tests()
    yield
    module.reset_dashboard_engine_for_tests()


def test_dashboard_url_does_not_fall_back_to_mi_readonly(monkeypatch):
    monkeypatch.setenv("DATABASE_READONLY_URL", "postgresql://mi_readonly:x@127.0.0.1/fmp")
    monkeypatch.delenv("DASHBOARD_READONLY_URL", raising=False)
    monkeypatch.delenv("DASHBOARD_ALLOW_WRITER_FALLBACK", raising=False)
    assert module.dashboard_database_url() is None
    with pytest.raises(module.DashboardIdentityError, match="DASHBOARD_READONLY_URL"):
        module.dashboard_engine()


def test_dashboard_url_uses_dedicated_identity(monkeypatch):
    monkeypatch.setenv("DASHBOARD_READONLY_URL", "postgresql://dashboard_readonly:x@127.0.0.1/fmp")
    assert "dashboard_readonly" in module.dashboard_database_url()
    assert "mi_readonly" not in module.dashboard_database_url()


def test_load_streamlit_env_reloads_then_strips_writer(tmp_path, monkeypatch):
    env_file = tmp_path / ".env"
    env_file.write_text(
        "FMP_API_KEY=keep-me\nDATABASE_URL=postgresql://writer:secret@127.0.0.1/fmp\n",
        encoding="utf-8",
    )
    monkeypatch.delenv("DASHBOARD_ALLOW_WRITER_FALLBACK", raising=False)
    monkeypatch.delenv("DATABASE_URL", raising=False)
    monkeypatch.delenv("FMP_API_KEY", raising=False)
    removed = module.load_streamlit_env(env_file)
    assert "DATABASE_URL" in removed
    assert os.environ.get("DATABASE_URL") in {None, ""}
    assert os.environ.get("FMP_API_KEY") == "keep-me"


def test_strip_writer_database_env_removes_writer_keys(monkeypatch):
    monkeypatch.delenv("DASHBOARD_ALLOW_WRITER_FALLBACK", raising=False)
    monkeypatch.setenv("DATABASE_URL", "postgresql://writer:secret@127.0.0.1/fmp")
    monkeypatch.setenv("MARKET_INTELLIGENCE_DATABASE_URL", "postgresql://mi_writer:secret@127.0.0.1/fmp")
    monkeypatch.setenv("DB_PASSWORD", "writer-password")
    monkeypatch.setenv("DB_HOST", "127.0.0.1")
    monkeypatch.setenv("DB_USER", "writer")
    monkeypatch.setenv("DB_NAME", "fmp")
    monkeypatch.setenv("DB_PORT", "5432")
    monkeypatch.setenv("DASHBOARD_READONLY_URL", "postgresql://dashboard_readonly:x@127.0.0.1/fmp")
    monkeypatch.setenv("DATABASE_READONLY_URL", "postgresql://mi_readonly:x@127.0.0.1/fmp")
    monkeypatch.setenv("FMP_API_KEY", "not-a-db-secret")
    removed = module.strip_writer_database_env()
    assert set(removed) == {
        "DATABASE_URL",
        "MARKET_INTELLIGENCE_DATABASE_URL",
        "DB_PASSWORD",
        "DB_HOST",
        "DB_USER",
        "DB_NAME",
        "DB_PORT",
    }
    assert os.environ.get("DATABASE_URL") in {None, ""}
    assert os.environ.get("DB_HOST") in {None, ""}
    assert os.environ.get("DB_USER") in {None, ""}
    assert os.environ.get("DB_NAME") in {None, ""}
    assert os.environ["DASHBOARD_READONLY_URL"].startswith("postgresql://dashboard_readonly:")
    assert os.environ["DATABASE_READONLY_URL"].startswith("postgresql://mi_readonly:")
    assert os.environ["FMP_API_KEY"] == "not-a-db-secret"
    assert os.environ[module.STREAMLIT_READONLY_ENV] == "1"


def test_writer_engine_refused_after_streamlit_strip(monkeypatch):
    from db import connection as writer

    monkeypatch.delenv("DASHBOARD_ALLOW_WRITER_FALLBACK", raising=False)
    monkeypatch.setenv("DB_HOST", "127.0.0.1")
    monkeypatch.setenv("DB_USER", "writer")
    monkeypatch.setenv("DB_NAME", "fmp")
    monkeypatch.setenv("DB_PASSWORD", "writer-password")
    writer.reset_writer_engine_for_tests()
    module.strip_writer_database_env()
    with pytest.raises(writer.WriterEngineRefused, match="Streamlit read-only"):
        writer.get_engine()
    writer.reset_writer_engine_for_tests()


def test_strip_writer_database_env_keeps_writer_when_fallback_on(monkeypatch):
    monkeypatch.setenv("DASHBOARD_ALLOW_WRITER_FALLBACK", "1")
    monkeypatch.setenv("DATABASE_URL", "postgresql://writer:secret@127.0.0.1/fmp")
    assert module.strip_writer_database_env() == []
    assert os.environ["DATABASE_URL"].startswith("postgresql://writer:")


def test_writer_fallback_is_opt_in(monkeypatch):
    monkeypatch.delenv("DASHBOARD_READONLY_URL", raising=False)
    monkeypatch.setenv("DASHBOARD_ALLOW_WRITER_FALLBACK", "1")

    class _Writer:
        pass

    monkeypatch.setattr("db.connection.engine", _Writer())
    engine = module.dashboard_engine()
    assert isinstance(engine, _Writer)
