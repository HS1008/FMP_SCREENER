"""Ingest canonical Stage 2 JSON from the GitHub stage2-results publication path.

Does not download QuantConnect Object Store objects. Does not call object_get.
Streamlit is not involved.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any, Iterable

from qc_research.object_store_sync import ingest_artifact


logger = logging.getLogger(__name__)

STAGE2_RESULTS_RELATIVE = "stage2_results"
STAGE2_RESULTS_OUTPUTS_RELATIVE = "outputs/stage2_results"
TRANSPORT = "github_stage2_results"

KIND_BY_FILENAME = {
    "run_manifest.json": "run_manifest",
    "run_summary.json": "run_summary",
    "training_summary.json": "training_summary",
    "model_metadata.json": "model_metadata",
    "oos_diagnostics.json": "oos_diagnostics",
    "baseline_oos_diagnostics.json": "oos_diagnostics",
    "oos_aggregate.json": "oos_aggregate",
    "nonholdout_assessment.json": "nonholdout_assessment",
    "strategy_spec.json": "strategy_spec",
    "experiment_manifest.json": "experiment_manifest",
    "assessment.json": "assessment",
    "risk_diagnostics.json": "risk_diagnostics",
    "parameter_sensitivity.json": "parameter_sensitivity",
    "walk_forward.json": "walk_forward",
    "trials.json": "trials",
    "feature_diagnostics.json": "feature_diagnostics",
    "selection_diagnostics.json": "selection_diagnostics",
    "strategy_intent.json": "strategy_intent",
    "search_space.json": "search_space",
    "pair_diagnostics.json": "pair_diagnostics",
    "fixed_income_risk.json": "fixed_income_risk",
    "fixed_income_diagnostics.json": "fixed_income_diagnostics",
    "curve_diagnostics.json": "curve_diagnostics",
    "futures_roll_diagnostics.json": "futures_roll_diagnostics",
    "roll_diagnostics.json": "roll_diagnostics",
}


def repo_root() -> Path:
    return Path(__file__).resolve().parents[1]


def discover_stage2_result_paths(root: Path | None = None) -> list[Path]:
    base = Path(root) if root is not None else repo_root()
    found: list[Path] = []
    seen: set[Path] = set()
    for relative in (STAGE2_RESULTS_RELATIVE, STAGE2_RESULTS_OUTPUTS_RELATIVE):
        directory = base / relative
        if not directory.is_dir():
            continue
        for path in sorted(directory.rglob("*.json")):
            if not path.is_file() or path.name not in KIND_BY_FILENAME:
                continue
            resolved = path.resolve()
            if resolved in seen:
                continue
            seen.add(resolved)
            found.append(path)
    return found


def logical_artifact_path(path: Path, root: Path | None = None) -> str:
    base = Path(root) if root is not None else repo_root()
    try:
        relative = path.resolve().relative_to(base.resolve())
    except ValueError:
        relative = Path(path.name)
    return relative.as_posix()


def load_stage2_json(path: Path) -> dict[str, Any]:
    with Path(path).open("r", encoding="utf-8") as handle:
        payload = json.load(handle)
    if not isinstance(payload, dict):
        raise ValueError("{0} is not a JSON object".format(path))
    return payload


def ingest_stage2_result_files(
    conn,
    paths: Iterable[Path],
    *,
    root: Path | None = None,
) -> dict[str, Any]:
    summary = {"ingested": 0, "skipped": 0, "errors": []}
    seen: set[Path] = set()
    for raw in paths:
        path = Path(raw)
        resolved = path.resolve() if path.exists() else path
        if resolved in seen:
            summary["skipped"] += 1
            continue
        seen.add(resolved)
        kind = KIND_BY_FILENAME.get(path.name)
        if not kind:
            continue
        try:
            payload = load_stage2_json(path)
            logical = logical_artifact_path(path, root)
            key = "github_stage2_results/{0}".format(logical)
            ingest_artifact(
                conn,
                key=key,
                kind=kind,
                payload=payload,
                expected_hash=payload.get("artifact_sha256"),
                transport=TRANSPORT,
                logical_path=logical,
            )
            summary["ingested"] += 1
        except Exception as exc:
            logger.exception("Stage 2 result %s failed", path)
            summary["errors"].append("{0}: {1}".format(path, exc))
    return summary


def sync_stage2_results(
    engine,
    *,
    strategy_id: str | None = None,
    root: Path | None = None,
) -> dict[str, Any]:
    """Required Stage 2 ingest path: GitHub results JSON, not Object Store download."""
    paths = discover_stage2_result_paths(root)
    if strategy_id:
        needle = "/{0}/".format(strategy_id)
        paths = [path for path in paths if needle in path.as_posix() or path.as_posix().endswith("/{0}".format(strategy_id))]
    summary = {"runs": 0, "ingested": 0, "skipped": 0, "errors": []}
    if not paths:
        return summary
    with engine.begin() as conn:
        result = ingest_stage2_result_files(conn, paths, root=root)
    summary.update(result)
    summary["runs"] = len({path.parent.parent.name for path in paths})
    return summary
