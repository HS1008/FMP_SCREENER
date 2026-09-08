"""Backend writer engine for ingestion jobs (lazy; never created at import time).

Resolution order: ``MARKET_INTELLIGENCE_DATABASE_URL`` (dedicated writer), then
``DATABASE_URL``, then ``DB_*`` (the existing FMP writer identity used by
``jobs.apply_migrations``). Streamlit pages and the API never use this module.
"""

from __future__ import annotations

import os

from qc_research.platform_ingest import postgres_url_from_env


class WriterConfigurationError(RuntimeError):
    """No writer database configuration is present."""


def writer_url(env: dict[str, str] | None = None) -> str | None:
    env = os.environ if env is None else env
    dedicated = (env.get("MARKET_INTELLIGENCE_DATABASE_URL") or "").strip()
    if dedicated:
        if dedicated.startswith("postgres://"):
            return "postgresql+psycopg2://" + dedicated[len("postgres://"):]
        if dedicated.startswith("postgresql://") and "+psycopg2" not in dedicated:
            return "postgresql+psycopg2://" + dedicated[len("postgresql://"):]
        return dedicated
    if env is os.environ:
        return postgres_url_from_env()
    return None


def writer_engine(url: str | None = None):
    from sqlalchemy import create_engine

    resolved = url or writer_url()
    if not resolved:
        raise WriterConfigurationError(
            "No writer database configured (MARKET_INTELLIGENCE_DATABASE_URL, DATABASE_URL, or DB_*)."
        )
    return create_engine(resolved, pool_pre_ping=True, future=True)


__all__ = ["WriterConfigurationError", "writer_engine", "writer_url"]
