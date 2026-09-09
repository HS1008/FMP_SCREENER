"""Bounded SQLite outbound queue for IBKR quote records."""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any

from ibkr_collector.values import utcnow


class OutboundQueue:
    def __init__(self, path: Path, *, max_records: int = 20000) -> None:
        self.path = path
        self.max_records = max_records
        path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(str(path), check_same_thread=False)
        self._conn.execute(
            """
            CREATE TABLE IF NOT EXISTS outbound (
                record_id TEXT PRIMARY KEY,
                payload TEXT NOT NULL,
                created_at TEXT NOT NULL,
                attempts INTEGER NOT NULL DEFAULT 0,
                last_error TEXT,
                status TEXT NOT NULL DEFAULT 'pending'
            )
            """
        )
        cols = {row[1] for row in self._conn.execute("PRAGMA table_info(outbound)").fetchall()}
        if "status" not in cols:
            self._conn.execute("ALTER TABLE outbound ADD COLUMN status TEXT NOT NULL DEFAULT 'pending'")
        self.overflow_count = 0
        self._conn.commit()

    def put(self, record: dict[str, Any]) -> str:
        payload = json.dumps(record, default=str)
        self._conn.execute(
            """
            INSERT INTO outbound (record_id, payload, created_at, status) VALUES (?, ?, ?, 'pending')
            ON CONFLICT(record_id) DO NOTHING
            """,
            (record["record_id"], payload, utcnow().isoformat()),
        )
        dropped = self._trim()
        self._conn.commit()
        if dropped:
            return "overflow_dropped"
        return "queued"

    def _trim(self) -> int:
        count = self._conn.execute("SELECT COUNT(*) FROM outbound WHERE status = 'pending'").fetchone()[0]
        if count <= self.max_records:
            return 0
        drop = count - self.max_records
        self._conn.execute(
            "DELETE FROM outbound WHERE record_id IN (SELECT record_id FROM outbound WHERE status = 'pending' ORDER BY created_at ASC LIMIT ?)",
            (drop,),
        )
        self.overflow_count += drop
        return drop

    def peek(self, limit: int = 50) -> list[dict[str, Any]]:
        rows = self._conn.execute(
            "SELECT payload FROM outbound WHERE status = 'pending' ORDER BY created_at ASC LIMIT ?",
            (int(limit),),
        ).fetchall()
        return [json.loads(row[0]) for row in rows]

    def ack(self, record_ids: list[str]) -> None:
        if not record_ids:
            return
        self._conn.executemany("DELETE FROM outbound WHERE record_id = ?", [(rid,) for rid in record_ids])
        self._conn.commit()

    def fail(self, record_ids: list[str], error: str) -> None:
        self._conn.executemany(
            "UPDATE outbound SET attempts = attempts + 1, last_error = ? WHERE record_id = ?",
            [(error[:200], rid) for rid in record_ids],
        )
        self._conn.commit()

    def quarantine(self, record_ids: list[str], error: str) -> None:
        self._conn.executemany(
            "UPDATE outbound SET status = 'quarantined', attempts = attempts + 1, last_error = ? WHERE record_id = ?",
            [(error[:200], rid) for rid in record_ids],
        )
        self._conn.commit()

    def size(self) -> int:
        return int(self._conn.execute("SELECT COUNT(*) FROM outbound WHERE status = 'pending'").fetchone()[0])
