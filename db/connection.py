"""Writer PostgreSQL engine for jobs and CLI.

Streamlit must not use this module. After ``strip_writer_database_env()``
sets ``FMP_STREAMLIT_READONLY``, ``load_dotenv`` and engine creation are refused.
"""

from __future__ import annotations

import os

from dotenv import load_dotenv
from sqlalchemy import create_engine

STREAMLIT_READONLY_ENV = "FMP_STREAMLIT_READONLY"
WRITER_ENV_FILE = "/etc/fmp/fmp-writer.env"
CHECKOUT_WRITER_ENV = "/root/FMP_SCREENER/.env"

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


def load_writer_dotenv(
    *,
    writer_env: str | None = None,
    checkout_env: str | None = None,
) -> list[str]:
    """Load writer credentials without overriding already-set variables.

    Prefers ``/etc/fmp/fmp-writer.env``, then the git-pull checkout ``.env``,
    then the process cwd. Never loads ``/etc/fmp/fmp-dashboard.env``.
    Refused when Streamlit read-only identity is already active.
    """
    if streamlit_readonly_active():
        raise WriterEngineRefused(
            "writer dotenv is refused in the Streamlit read-only process"
        )
    loaded: list[str] = []
    for path in (
        writer_env if writer_env is not None else WRITER_ENV_FILE,
        checkout_env if checkout_env is not None else CHECKOUT_WRITER_ENV,
    ):
        if path and os.path.isfile(path):
            load_dotenv(path)
            loaded.append(path)
    load_dotenv()
    # Writer jobs must not inherit Streamlit identity from checkout .env.
    os.environ.pop(STREAMLIT_READONLY_ENV, None)
    return loaded


def _normalize_writer_url(url: str) -> str:
    text = url.strip()
    if text.startswith("postgres://"):
        return "postgresql+psycopg2://" + text[len("postgres://") :]
    if text.startswith("postgresql://") and "+psycopg2" not in text:
        return "postgresql+psycopg2://" + text[len("postgresql://") :]
    return text


def _build_url() -> str:
    url = (os.getenv("DATABASE_URL") or "").strip()
    if url:
        return _normalize_writer_url(url)
    host = os.getenv("DB_HOST")
    name = os.getenv("DB_NAME")
    user = os.getenv("DB_USER")
    if not (host and name and user):
        raise RuntimeError(
            "writer engine needs DATABASE_URL or DB_HOST/DB_NAME/DB_USER"
        )
    port = os.getenv("DB_PORT", "5432")
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
    load_writer_dotenv()
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
