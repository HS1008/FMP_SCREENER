"""Shared exclusivity for Market Intelligence writers.

A PostgreSQL session-level advisory lock keyed on ``LOCK_KEY`` is held by every job that
writes ``mi_*`` tables (refresh, legacy bridge, morning context, idea registry). If a
process crashes the server releases the lock with the session, so there is nothing to
clean up. Contention is reported distinctly from provider failure (exit code 75).
"""

from __future__ import annotations

import contextlib
import hashlib

from sqlalchemy import text

LOCK_NAME = "market_intelligence_writer"
LOCK_KEY = int.from_bytes(hashlib.sha256(LOCK_NAME.encode("utf-8")).digest()[:8], "big", signed=True)
EXIT_LOCK_CONTENTION = 75


class LockContention(RuntimeError):
    """Another Market Intelligence writer currently holds the lock."""


@contextlib.contextmanager
def writer_lock(engine, *, wait: bool = False):
    """Hold the advisory lock on a dedicated connection for the duration of the block."""
    conn = engine.connect()
    try:
        if wait:
            conn.execute(text("SELECT pg_advisory_lock(:key)"), {"key": LOCK_KEY})
            acquired = True
        else:
            acquired = bool(conn.execute(text("SELECT pg_try_advisory_lock(:key)"), {"key": LOCK_KEY}).scalar())
        if not acquired:
            raise LockContention("{0} lock is held by another process".format(LOCK_NAME))
        try:
            yield conn
        finally:
            with contextlib.suppress(Exception):
                conn.execute(text("SELECT pg_advisory_unlock(:key)"), {"key": LOCK_KEY})
    finally:
        conn.close()


__all__ = ["EXIT_LOCK_CONTENTION", "LOCK_KEY", "LOCK_NAME", "LockContention", "writer_lock"]
