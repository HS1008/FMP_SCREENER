"""Streamlit / Strategy Monitor database engine.

Prefers DASHBOARD_READONLY_URL. Does not fall back to mi_readonly
(that role lacks Strategy Monitor tables). If the dedicated URL is
unset, uses the existing DB_* engine so current hosts keep rendering
until the role is provisioned.
"""

from __future__ import annotations

import os

from sqlalchemy import create_engine
from sqlalchemy.engine import Engine


_ENGINE: Engine | None = None


def dashboard_database_url() -> str | None:
    return (os.environ.get("DASHBOARD_READONLY_URL") or "").strip() or None


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
    from db.connection import engine as writer_engine

    _ENGINE = writer_engine
    return _ENGINE
