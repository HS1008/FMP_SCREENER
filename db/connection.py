"""Writer PostgreSQL engine for jobs and CLI.

Streamlit must not use this module. After ``strip_writer_database_env()``
sets ``FMP_STREAMLIT_READONLY``, ``load_dotenv`` and engine creation are refused.
"""

from __future__ import annotations

import os

from dotenv import load_dotenv
from sqlalchemy import create_engine

STREAMLIT_READONLY_ENV = "FMP_STREAMLIT_READONLY"

_engine = None
_database_url = None


class WriterEngineRefused(RuntimeError):
    """Writer engine is refused in the Streamlit read-only process."""


def streamlit_readonly_active() -> bool:
    return (os.environ.get(STREAMLIT_READONLY_ENV) or "").strip().lower() in {
        "1",
        "true",
        "yes",
        "on",
    }


def reset_writer_engine_for_tests() -> None:
    global _engine, _database_url
    _engine = None
    _database_url = None


def _build_url() -> str:
    host = os.getenv("DB_HOST")
    port = os.getenv("DB_PORT", "5432")
    name = os.getenv("DB_NAME")
    user = os.getenv("DB_USER")
    password = os.getenv("DB_PASSWORD")
    return (
        f"postgresql+psycopg2://{user}:{password}"
        f"@{host}:{port}/{name}"
    )


def get_engine():
    global _engine, _database_url
    if streamlit_readonly_active():
        raise WriterEngineRefused(
            "db.connection writer engine is refused in the Streamlit read-only process"
        )
    if _engine is not None:
        return _engine
    load_dotenv()
    if streamlit_readonly_active():
        raise WriterEngineRefused(
            "db.connection writer engine is refused in the Streamlit read-only process"
        )
    _database_url = _build_url()
    _engine = create_engine(_database_url, pool_pre_ping=True)
    return _engine


class _EngineProxy:
    def __getattr__(self, name):
        return getattr(get_engine(), name)

    def connect(self, *args, **kwargs):
        return get_engine().connect(*args, **kwargs)

    def begin(self, *args, **kwargs):
        return get_engine().begin(*args, **kwargs)


engine = _EngineProxy()


def __getattr__(name: str):
    if name == "DATABASE_URL":
        if streamlit_readonly_active():
            raise WriterEngineRefused(
                "DATABASE_URL is refused in the Streamlit read-only process"
            )
        get_engine()
        return _database_url
    raise AttributeError(name)
