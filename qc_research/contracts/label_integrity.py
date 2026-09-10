"""Pinned CSFML V1 label-integrity bound for the dashboard.

Display only. Does not change economic_gate, holdout, or authorize a rerun.
Ingest uses the same pin to refuse impersonated official V1 identity.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Mapping

from qc_research.contracts.kinds import ArtifactContractError


SNAPSHOT = Path(__file__).resolve().parent / "csfml_v1_label_integrity.json"


def load_csfml_v1_label_integrity() -> dict[str, Any]:
    return json.loads(SNAPSHOT.read_text(encoding="utf-8"))


def official_csfml_v1_shas(pin: Mapping[str, Any] | None = None) -> frozenset[str]:
    data = dict(pin or load_csfml_v1_label_integrity())
    return frozenset(
        {
            str(data["authoritative_csfml_v1_sha"]),
            str(data["authoritative_csfml_v1_qc_sha"]),
        }
    )


def refuse_impersonated_official_csfml_v1(payload: Mapping[str, Any] | None) -> None:
    """Refuse official V1 run-id with a non-pin SHA. Does not authorize a rerun."""
    record = dict(payload or {})
    nested = record.get("payload") if isinstance(record.get("payload"), dict) else {}
    pin = load_csfml_v1_label_integrity()
    run_id = str(
        record.get("research_run_id")
        or record.get("run_id")
        or nested.get("research_run_id")
        or nested.get("run_id")
        or ""
    )
    official_run = str(pin.get("full_suite_run_id") or "")
    if not official_run or run_id != official_run:
        return
    commit = str(
        record.get("git_commit")
        or record.get("source_git_sha")
        or nested.get("git_commit")
        or nested.get("source_git_sha")
        or ""
    ).strip()
    allowed = official_csfml_v1_shas(pin)
    identity = any(
        key in record or key in nested
        for key in ("git_commit", "source_git_sha", "dirty", "resolved_config", "run_status")
    )
    if not commit:
        if identity:
            raise ArtifactContractError(
                "Official CSFML V1 ingest requires git_commit to be the pinned QC or integration SHA"
            )
        return
    if commit not in allowed:
        raise ArtifactContractError(
            "Official CSFML V1 run-id cannot be ingested with a non-pin git_commit"
        )
    if record.get("rerun_authorized") is True or nested.get("rerun_authorized") is True:
        raise ArtifactContractError("Official CSFML V1 pin has rerun_authorized=false")


def csfml_v1_integrity_caption(
    strategy_id: str | None = None,
    research_run_id: str | None = None,
) -> str | None:
    pin = load_csfml_v1_label_integrity()
    if strategy_id and str(strategy_id) != pin["strategy_id"]:
        return None
    official_run = str(pin.get("full_suite_run_id") or "")
    if research_run_id and official_run and str(research_run_id) != official_run:
        return None
    return str(pin["monitor_caption"])
