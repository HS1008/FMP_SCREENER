"""Official results trees that PostgreSQL must not overwrite.

Mirrors producer sealed_results_run_ids. Does not change economics or
authorize a QuantConnect rerun.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Mapping

from qc_research.contracts.hashing import payload_for_hash, sha256_payload
from qc_research.contracts.kinds import ArtifactContractError


SNAPSHOT = Path(__file__).resolve().parent / "sealed_results.json"
REPO_ROOT = Path(__file__).resolve().parents[2]


class SealedResultsError(ArtifactContractError):
    """Sealed official results would be mutated."""


def load_sealed_results() -> dict[str, Any]:
    return json.loads(SNAPSHOT.read_text(encoding="utf-8"))


def sealed_results_run_ids() -> frozenset[str]:
    data = load_sealed_results()
    return frozenset(str(item) for item in (data.get("run_ids") or []) if item)


def _run_id(payload: Mapping[str, Any] | None) -> str:
    record = dict(payload or {})
    nested = record.get("payload") if isinstance(record.get("payload"), dict) else {}
    return str(
        record.get("research_run_id")
        or record.get("run_id")
        or nested.get("research_run_id")
        or nested.get("run_id")
        or ""
    ).strip()


def refuse_sealed_stage1_summary(payload: Mapping[str, Any] | None) -> None:
    """Refuse official Stage 1 summaries that do not match the pin."""
    record = dict(payload or {})
    run_id = _run_id(record)
    pin = (load_sealed_results().get("stage1_pins") or {}).get(run_id)
    if not pin:
        return
    for key, expected in pin.items():
        incoming = record.get(key)
        if incoming != expected:
            raise SealedResultsError(
                "refusing sealed {0} {1}={2!r}; pin requires {3!r}".format(
                    run_id, key, incoming, expected
                )
            )


def committed_official_file(run_id: str, logical_path: str | None) -> Path | None:
    rel = (load_sealed_results().get("committed_trees") or {}).get(run_id)
    if not rel:
        return None
    tree = REPO_ROOT / rel
    if not tree.is_dir():
        return None
    logical = str(logical_path or "").replace("\\", "/").lstrip("/")
    if not logical:
        return None
    marker = "{0}/".format(run_id)
    if marker in logical:
        relative = logical.split(marker, 1)[1]
    else:
        relative = Path(logical).name
    candidate = tree / relative
    return candidate if candidate.is_file() else None


def _committed_tree(run_id: str) -> Path | None:
    rel = (load_sealed_results().get("committed_trees") or {}).get(run_id)
    if not rel:
        return None
    tree = REPO_ROOT / rel
    return tree if tree.is_dir() else None


def refuse_sealed_committed_mismatch(
    payload: Mapping[str, Any] | None,
    *,
    logical_path: str | None = None,
) -> None:
    """Refuse sealed payloads that do not match the committed official tree."""
    record = dict(payload or {})
    run_id = _run_id(record)
    if run_id not in sealed_results_run_ids():
        return
    incoming = sha256_payload(payload_for_hash(record))
    committed = committed_official_file(run_id, logical_path)
    if committed is not None:
        official = json.loads(committed.read_text(encoding="utf-8"))
        if incoming != sha256_payload(payload_for_hash(official)):
            raise SealedResultsError(
                "refusing sealed {0} mutation of {1}".format(run_id, committed.name)
            )
        return
    tree = _committed_tree(run_id)
    if tree is None:
        return
    window = str(record.get("window_id") or "")
    roots = [tree / window] if window and (tree / window).is_dir() else [tree]
    for folder in roots:
        for path in folder.rglob("*.json"):
            if not path.is_file():
                continue
            official = json.loads(path.read_text(encoding="utf-8"))
            if incoming == sha256_payload(payload_for_hash(official)):
                return
    raise SealedResultsError(
        "refusing sealed {0} payload that is not in the committed official tree".format(run_id)
    )


def existing_artifact_sha(conn, key: str) -> str | None:
    from sqlalchemy import text

    result = conn.execute(
        text("SELECT sha256 FROM research_artifacts WHERE artifact_key = :key"),
        {"key": key},
    )
    if result is None:
        return None
    mappings = getattr(result, "mappings", None)
    if mappings is None:
        return None
    row = mappings().first()
    if not row:
        return None
    if isinstance(row, dict):
        return str(row.get("sha256") or "") or None
    mapping = getattr(row, "_mapping", None)
    if mapping is not None:
        return str(mapping.get("sha256") or "") or None
    return None


def refuse_sealed_artifact_overwrite(conn, *, key: str, run_id: str, incoming_sha: str) -> None:
    if run_id not in sealed_results_run_ids():
        return
    existing = existing_artifact_sha(conn, key)
    if existing and existing != incoming_sha:
        raise SealedResultsError(
            "refusing sealed {0} overwrite of {1}".format(run_id, key)
        )
