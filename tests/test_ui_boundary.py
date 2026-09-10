"""Streamlit provider-fetch default is refuse."""

from __future__ import annotations

import pytest

from qc_research.ui_boundary import provider_fetch_allowed, refuse_provider_fetch


def test_provider_fetch_denied_by_default(monkeypatch):
    monkeypatch.delenv("STREAMLIT_ALLOW_PROVIDER_FETCH", raising=False)
    assert provider_fetch_allowed() is False
    with pytest.raises(RuntimeError, match="FMP"):
        refuse_provider_fetch("FMP")


def test_provider_fetch_opt_in(monkeypatch):
    monkeypatch.setenv("STREAMLIT_ALLOW_PROVIDER_FETCH", "1")
    assert provider_fetch_allowed() is True
    refuse_provider_fetch("FMP")
