"""Dashboard engine prefers DASHBOARD_READONLY_URL and never uses mi_readonly."""

from __future__ import annotations

from db import dashboard_engine as module


def test_dashboard_url_does_not_fall_back_to_mi_readonly(monkeypatch):
    monkeypatch.setenv("DATABASE_READONLY_URL", "postgresql://mi_readonly:x@127.0.0.1/fmp")
    monkeypatch.delenv("DASHBOARD_READONLY_URL", raising=False)
    assert module.dashboard_database_url() is None


def test_dashboard_url_uses_dedicated_identity(monkeypatch):
    monkeypatch.setenv("DASHBOARD_READONLY_URL", "postgresql://dashboard_readonly:x@127.0.0.1/fmp")
    assert "dashboard_readonly" in module.dashboard_database_url()
    assert "mi_readonly" not in module.dashboard_database_url()
