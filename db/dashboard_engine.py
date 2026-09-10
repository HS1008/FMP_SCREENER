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


STREAMLIT_READONLY_ENV = "FMP_STREAMLIT_READONLY"

STREAMLIT_ESCAPE_KEYS = (
    "DASHBOARD_ALLOW_WRITER_FALLBACK",
    "STREAMLIT_ALLOW_PROVIDER_FETCH",
)

# Checkout ``.env`` is writer-capable. Once systemd (or a prior strip) has
# already imposed Streamlit read-only, only FMP_API_KEY may be filled from it.
READONLY_DOTENV_ALLOWLIST = frozenset({"FMP_API_KEY"})

WRITER_ENV_KEYS = (
    "DATABASE_URL",
    "MARKET_INTELLIGENCE_DATABASE_URL",
    "DB_PASSWORD",
    "DB_HOST",
    "DB_USER",
    "DB_NAME",
    "DB_PORT",
)


def streamlit_readonly_active() -> bool:
    return (os.environ.get(STREAMLIT_READONLY_ENV) or "").strip().lower() in {
        "1",
        "true",
        "yes",
        "on",
    }


def strip_writer_database_env() -> list[str]:
    """Remove writer DB credentials from the Streamlit process unless fallback is on.

    Leaves DASHBOARD_READONLY_URL, DATABASE_READONLY_URL, and FMP_API_KEY in place.
    Sets FMP_STREAMLIT_READONLY so ``db.connection`` cannot reload ``.env``.
    """
    if writer_fallback_allowed():
        os.environ.pop(STREAMLIT_READONLY_ENV, None)
        return []
    removed: list[str] = []
    for key in WRITER_ENV_KEYS:
        if os.environ.get(key):
            os.environ.pop(key, None)
            removed.append(key)
    os.environ[STREAMLIT_READONLY_ENV] = "1"
    return removed


def load_streamlit_env(path: str | os.PathLike[str] | None = None) -> list[str]:
    """Load `.env` then strip writer DB keys. Safe to call from any Streamlit page.

    Checkout ``.env`` is writer-capable for ingest/CLI. It must not re-enable
    Streamlit writer fallback or provider fetch after systemd already refused
    those flags. When ``FMP_STREAMLIT_READONLY`` is already set, the full
    checkout file is not loaded; only missing ``FMP_API_KEY`` may be copied.
    """
    from dotenv import dotenv_values, load_dotenv

    if streamlit_readonly_active():
        values = dotenv_values(path) if path else dotenv_values()
        if isinstance(values, dict):
            for key in READONLY_DOTENV_ALLOWLIST:
                if os.environ.get(key):
                    continue
                value = values.get(key)
                if value:
                    os.environ[key] = str(value)
    elif path:
        load_dotenv(path)
    else:
        load_dotenv()
    removed: list[str] = []
    for key in STREAMLIT_ESCAPE_KEYS:
        if os.environ.get(key):
            os.environ.pop(key, None)
            removed.append(key)
    removed.extend(strip_writer_database_env())
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
