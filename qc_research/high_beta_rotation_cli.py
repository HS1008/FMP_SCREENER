"""Authorized HighBetaRotation ingest.

Platform ingestion does not call this module. The command opens a writer
connection, calls ingest_hbr_bundle without a caller-supplied prior, and
reads the stored hashes back.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

from market_intelligence.writer_db import writer_engine, writer_url
from qc_research.high_beta_rotation_ingest import ingest_hbr_bundle
from sqlalchemy import text


READBACK_SQL = """
SELECT artifact_type, sha256
FROM research_artifacts
WHERE research_run_id = :research_run_id
ORDER BY artifact_type, sha256
"""


def load_bundle(path: Path) -> dict[str, Any]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError("HighBetaRotation bundle must be a JSON object")
    return payload


def readback(conn: Any, research_run_id: str) -> list[dict[str, str]]:
    rows = conn.execute(text(READBACK_SQL), {"research_run_id": research_run_id})
    loaded = []
    for row in rows.mappings():
        loaded.append(
            {
                "artifact_type": str(row.get("artifact_type") or ""),
                "sha256": str(row.get("sha256") or ""),
            }
        )
    return loaded


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="high_beta_rotation_cli")
    parser.add_argument("bundle")
    args = parser.parse_args(argv)
    if writer_url() is None:
        print(json.dumps({"status": "credentials_unavailable", "ingested": False}))
        return 2
    bundle = load_bundle(Path(args.bundle))
    engine = writer_engine()
    with engine.connect() as conn:
        result = ingest_hbr_bundle(conn, bundle)
        stored = readback(conn, str(result["research_run_id"]))
    print(
        json.dumps(
            {
                "status": "ingested",
                "ingested": True,
                "research_run_id": result["research_run_id"],
                "run_status": result.get("run_status"),
                "written_artifacts": result.get("written_artifacts"),
                "readback": stored,
            },
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
