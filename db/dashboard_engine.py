"""Streamlit / Strategy Monitor database engine.

Requires DASHBOARD_READONLY_URL. Does not fall back to mi_readonly
(that role lacks Strategy Monitor tables). Writer fallback is opt-in
only via DASHBOARD_ALLOW_WRITER_FALLBACK=1.
"""

from __future__ import annotations

import os

from sqlalchemy import create_engine
from sqlalchemy.engine import Engine


_ENGINE: Engine | None = None


class DashboardIdentityError(RuntimeError):
    """Strategy Monitor has no read-only identity and writer fallback is off."""


def dashboard_database_url() -> str | None:
    return (os.environ.get("DASHBOARD_READONLY_URL") or "").strip() or None


def writer_fallback_allowed() -> bool:
    return (os.environ.get("DASHBOARD_ALLOW_WRITER_FALLBACK") or "").strip().lower() in {
        "1",
        "true",
        "yes",
        "on",
    }


WRITER_ENV_KEYS = (
    "DATABASE_URL",
    "MARKET_INTELLIGENCE_DATABASE_URL",
    "DB_PASSWORD",
)


def strip_writer_database_env() -> list[str]:
    """Remove writer DB credentials from the Streamlit process unless fallback is on.

    Leaves DASHBOARD_READONLY_URL, DATABASE_READONLY_URL, and FMP_API_KEY in place.
    """
    if writer_fallback_allowed():
        return []
    removed: list[str] = []
    for key in WRITER_ENV_KEYS:
        if os.environ.get(key):
            os.environ.pop(key, None)
            removed.append(key)
    return removed


def reset_dashboard_engine_for_tests() -> None:
    global _ENGINE
    _ENGINE = None


def dashboard_engine() -> Engine:
    global _ENGINE
    if _ENGINE is not None:
        return _ENGINE
    url = dashboard_database_url()
    if url:
        _ENGINE = create_engine(
            url.replace("postgresql://", "postgresql+psycopg2://", 1)
            if url.startswith("postgresql://") and "+psycopg2" not in url
            else url,
            pool_pre_ping=True,
            execution_options={"isolation_level": "AUTOCOMMIT"},
        )
        return _ENGINE
    if writer_fallback_allowed():
        from db.connection import engine as writer_engine

        _ENGINE = writer_engine
        return _ENGINE
    raise DashboardIdentityError(
        "DASHBOARD_READONLY_URL is required for Strategy Monitor. "
        "Provision dashboard_readonly, or set DASHBOARD_ALLOW_WRITER_FALLBACK=1 "
        "only as a temporary host escape. Do not use mi_readonly."
    )
