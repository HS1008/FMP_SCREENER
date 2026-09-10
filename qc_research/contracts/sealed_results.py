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


def _pin_value_matches(incoming: Any, expected: Any) -> bool:
    if incoming == expected:
        return True
    if isinstance(expected, bool):
        if expected:
            return incoming in {True, 1, "true", "t"}
        return incoming in {False, 0, "false", "f", None, ""}
    if isinstance(expected, int) and not isinstance(expected, bool):
        try:
            return int(incoming) == expected
        except (TypeError, ValueError):
            return False
    return str(incoming or "") == str(expected or "")


def official_stage1_identity_blockers(
    *,
    strategy_id: str | None,
    research_run_id: str | None,
    row: Mapping[str, Any] | None = None,
    engine: Any = None,
) -> list[str]:
    """Blockers when the selected run is official Stage 1. Empty otherwise.

    Query failures fail closed. This is not an economic PASS/WATCH/FAIL.
    """
    run_id = str(research_run_id or "").strip()
    pin = (load_sealed_results().get("stage1_pins") or {}).get(run_id)
    if not pin:
        return []
    if strategy_id and str(strategy_id) != str(pin.get("strategy_id") or ""):
        return ["strategy_id_mismatch"]
    record: Mapping[str, Any] | None = row
    if record is None:
        if engine is None:
            return ["identity_query_failed"]
        try:
            from qc_research.read_models.monitor_queries import load_research_run_row

            record = load_research_run_row(engine, run_id)
        except Exception:
            return ["identity_query_failed"]
    if not record:
        return ["official_run_missing"]
    blockers: list[str] = []
    for key, expected in pin.items():
        if not _pin_value_matches(record.get(key), expected):
            blockers.append(key)
    if record.get("holdout_accessed") in {True, 1, "true", "t"}:
        blockers.append("holdout_accessed")
    try:
        if int(record.get("holdout_access_count") or 0) != 0:
            blockers.append("holdout_access_count")
    except (TypeError, ValueError):
        blockers.append("holdout_access_count")
    return blockers


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


def _committed_official_path(run_id: str) -> Path | None:
    rel = (load_sealed_results().get("committed_trees") or {}).get(run_id)
    if not rel:
        return None
    path = REPO_ROOT / rel
    return path if path.exists() else None


def _hashes_from_official_file(path: Path) -> set[str]:
    official = json.loads(path.read_text(encoding="utf-8"))
    hashes = {sha256_payload(payload_for_hash(official))}
    from qc_research.tlt_duration_momentum import (
        is_tlt_duration_momentum_record,
        wrap_tlt_duration_momentum_record,
    )

    if is_tlt_duration_momentum_record(official):
        for _kind, artifact in wrap_tlt_duration_momentum_record(official):
            hashes.add(sha256_payload(payload_for_hash(artifact)))
    return hashes


def refuse_sealed_committed_mismatch(
    payload: Mapping[str, Any] | None,
    *,
    logical_path: str | None = None,
) -> None:
    """Refuse sealed payloads that do not match the committed official tree.

    Sealed run ids without a committed tree or file are refused on first
    insert. Identical official re-ingest is allowed.
    """
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
    path = _committed_official_path(run_id)
    if path is None:
        raise SealedResultsError(
            "refusing sealed {0} ingest; no committed official tree".format(run_id)
        )
    if path.is_file():
        if incoming in _hashes_from_official_file(path):
            return
        raise SealedResultsError(
            "refusing sealed {0} payload that is not in the committed official file".format(
                run_id
            )
        )
    tree = path if path.is_dir() else None
    if tree is None:
        raise SealedResultsError(
            "refusing sealed {0} ingest; no committed official tree".format(run_id)
        )
    window = str(record.get("window_id") or "")
    roots = [tree / window] if window and (tree / window).is_dir() else [tree]
    for folder in roots:
        for candidate in folder.rglob("*.json"):
            if not candidate.is_file():
                continue
            official = json.loads(candidate.read_text(encoding="utf-8"))
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
