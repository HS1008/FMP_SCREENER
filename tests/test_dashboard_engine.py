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


def test_strip_writer_database_env_removes_writer_keys(monkeypatch):
    monkeypatch.delenv("DASHBOARD_ALLOW_WRITER_FALLBACK", raising=False)
    monkeypatch.setenv("DATABASE_URL", "postgresql://writer:secret@127.0.0.1/fmp")
    monkeypatch.setenv("MARKET_INTELLIGENCE_DATABASE_URL", "postgresql://mi_writer:secret@127.0.0.1/fmp")
    monkeypatch.setenv("DB_PASSWORD", "writer-password")
    monkeypatch.setenv("DASHBOARD_READONLY_URL", "postgresql://dashboard_readonly:x@127.0.0.1/fmp")
    monkeypatch.setenv("DATABASE_READONLY_URL", "postgresql://mi_readonly:x@127.0.0.1/fmp")
    monkeypatch.setenv("FMP_API_KEY", "not-a-db-secret")
    removed = module.strip_writer_database_env()
    assert set(removed) == {"DATABASE_URL", "MARKET_INTELLIGENCE_DATABASE_URL", "DB_PASSWORD"}
    assert os.environ.get("DATABASE_URL") in {None, ""}
    assert os.environ["DASHBOARD_READONLY_URL"].startswith("postgresql://dashboard_readonly:")
    assert os.environ["DATABASE_READONLY_URL"].startswith("postgresql://mi_readonly:")
    assert os.environ["FMP_API_KEY"] == "not-a-db-secret"


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
