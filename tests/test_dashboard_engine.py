"""Dashboard engine requires DASHBOARD_READONLY_URL and never uses mi_readonly."""

from __future__ import annotations

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


def test_writer_fallback_is_opt_in(monkeypatch):
    monkeypatch.delenv("DASHBOARD_READONLY_URL", raising=False)
    monkeypatch.setenv("DASHBOARD_ALLOW_WRITER_FALLBACK", "1")

    class _Writer:
        pass

    monkeypatch.setattr("db.connection.engine", _Writer())
    engine = module.dashboard_engine()
    assert isinstance(engine, _Writer)
