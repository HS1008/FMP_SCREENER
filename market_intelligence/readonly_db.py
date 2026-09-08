"""Dedicated read-only database access for Market Intelligence pages and the AI context API.

Configuration: ``DATABASE_READONLY_URL`` (a PostgreSQL URL for the ``mi_readonly`` role).
There is deliberately *no* fallback to the writer identity (``DB_*`` / ``DATABASE_URL``):
in production a missing read-only URL fails closed with an actionable message.

Every query runs in a READ ONLY transaction with a statement timeout and a bounded row
count. Tests inject isolated engines through :func:`set_engine_for_tests`.
"""

from __future__ import annotations

import os
from contextlib import contextmanager
from typing import Any, Iterator, Mapping

from sqlalchemy import text

READONLY_URL_ENV = "DATABASE_READONLY_URL"
DEFAULT_STATEMENT_TIMEOUT_MS = 15000
DEFAULT_ROW_LIMIT = 5000

_engine_override = None
_cached_engine = None


class ReadOnlyUnavailable(RuntimeError):
    """The read-only database is not configured or reachable."""

    def __init__(self, message: str, *, reason: str):
        super().__init__(message)
        self.reason = reason


def readonly_url(env: Mapping[str, str] | None = None) -> str | None:
    env = os.environ if env is None else env
    url = (env.get(READONLY_URL_ENV) or "").strip()
    if not url:
        return None
    if url.startswith("postgres://"):
        return "postgresql+psycopg2://" + url[len("postgres://"):]
    if url.startswith("postgresql://") and "+psycopg2" not in url:
        return "postgresql+psycopg2://" + url[len("postgresql://"):]
    return url


def set_engine_for_tests(engine) -> None:
    """Inject an isolated engine (tests only). ``None`` clears the override."""
    global _engine_override
    _engine_override = engine


def readonly_engine():
    """Lazily create the read-only engine; raise :class:`ReadOnlyUnavailable` when unconfigured."""
    global _cached_engine
    if _engine_override is not None:
        return _engine_override
    if _cached_engine is not None:
        return _cached_engine
    url = readonly_url()
    if not url:
        raise ReadOnlyUnavailable(
            "{0} is not configured. Provision the mi_readonly role (db/roles/market_intelligence_readonly.sql) "
            "and set {0} in the protected host environment. The writer identity is never used here.".format(READONLY_URL_ENV),
            reason="CONFIGURATION_REQUIRED",
        )
    from sqlalchemy import create_engine

    _cached_engine = create_engine(
        url,
        pool_pre_ping=True,
        pool_size=2,
        max_overflow=2,
        future=True,
        connect_args={"options": "-c default_transaction_read_only=on -c statement_timeout={0}".format(DEFAULT_STATEMENT_TIMEOUT_MS)},
    )
    return _cached_engine


@contextmanager
def readonly_connection() -> Iterator[Any]:
    """Connection inside an explicit READ ONLY transaction with a statement timeout."""
    try:
        engine = readonly_engine()
    except ReadOnlyUnavailable:
        raise
    try:
        conn = engine.connect()
    except Exception as exc:  # noqa: BLE001 - sanitized: never echo the URL
        raise ReadOnlyUnavailable("read-only database unreachable ({0})".format(exc.__class__.__name__), reason="UNREACHABLE") from None
    try:
        with conn.begin():
            conn.execute(text("SET TRANSACTION READ ONLY"))
            conn.execute(text("SET LOCAL statement_timeout = {0}".format(int(DEFAULT_STATEMENT_TIMEOUT_MS))))
            yield conn
    finally:
        conn.close()


def fetch_all(sql: str, params: Mapping[str, Any] | None = None, *, limit: int = DEFAULT_ROW_LIMIT) -> list[dict[str, Any]]:
    """Run a bounded SELECT and return dict rows. ``sql`` must be a curated view query."""
    bounded = "SELECT * FROM ({0}) AS bounded_q LIMIT {1}".format(sql.strip().rstrip(";"), int(limit))
    with readonly_connection() as conn:
        rows = conn.execute(text(bounded), dict(params or {})).mappings().all()
    return [dict(r) for r in rows]


def fetch_one(sql: str, params: Mapping[str, Any] | None = None) -> dict[str, Any] | None:
    rows = fetch_all(sql, params, limit=1)
    return rows[0] if rows else None


REQUIRED_VIEWS = (
    "mi_v_source_health",
    "mi_v_macro_latest",
    "mi_v_metric_latest",
    "mi_v_credit_latest",
    "mi_v_sector_latest",
    "mi_v_morning_context_latest",
    "mi_v_morning_context_index",
    "mi_v_strategy_research_summary",
)


def probe_readonly(required_views: tuple[str, ...] = REQUIRED_VIEWS) -> dict[str, Any]:
    """Readiness probe: every required curated view must be *readable* by the connected role.

    ``SELECT 1`` succeeding or ``information_schema`` listing zero views is not readiness.
    Returns ``status`` OK only when all required views answered a bounded SELECT; otherwise
    ``VIEWS_UNAVAILABLE`` with the failing view names (never the URL or credentials).
    """
    try:
        with readonly_connection() as conn:
            visible = {
                r[0]
                for r in conn.execute(text("SELECT table_name FROM information_schema.views WHERE table_name LIKE 'mi\\_v\\_%'")).all()
            }
            failing: list[str] = []
            for view in required_views:
                try:
                    conn.execute(text("SAVEPOINT probe_view"))
                    conn.execute(text("SELECT * FROM {0} LIMIT 1".format(view)))
                    conn.execute(text("RELEASE SAVEPOINT probe_view"))
                except Exception as exc:  # noqa: BLE001 - permission or missing relation
                    conn.execute(text("ROLLBACK TO SAVEPOINT probe_view"))
                    failing.append("{0}:{1}".format(view, exc.__class__.__name__))
            role_ro = conn.execute(text("SHOW transaction_read_only")).scalar()
        if failing or not visible:
            return {"status": "VIEWS_UNAVAILABLE", "curated_views_visible": len(visible), "required_views": len(required_views), "failing_views": failing, "transaction_read_only": role_ro}
        return {"status": "OK", "curated_views_visible": len(visible), "required_views": len(required_views), "failing_views": [], "transaction_read_only": role_ro}
    except ReadOnlyUnavailable as exc:
        return {"status": exc.reason, "message": str(exc)}


__all__ = [
    "DEFAULT_ROW_LIMIT",
    "READONLY_URL_ENV",
    "REQUIRED_VIEWS",
    "ReadOnlyUnavailable",
    "fetch_all",
    "fetch_one",
    "probe_readonly",
    "readonly_connection",
    "readonly_engine",
    "readonly_url",
    "set_engine_for_tests",
]
