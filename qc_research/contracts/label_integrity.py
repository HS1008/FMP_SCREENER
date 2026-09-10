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

# JSON cannot expand these. Digest updates still require the pin SHAs to stay
# inside this floor so a copied Actions tree cannot reopen a third V1 SHA.
MINIMUM_AUTHORITATIVE_CSFML_V1_SHAS = frozenset(
    {
        "ef270841621933f5039680cb070559f43bd1e3c8",
        "54a5543f5796073cdf9192b04daa5dc08e8d1747",
    }
)


def load_csfml_v1_label_integrity() -> dict[str, Any]:
    return json.loads(SNAPSHOT.read_text(encoding="utf-8"))


def pin_declared_csfml_v1_shas(pin: Mapping[str, Any] | None = None) -> set[str]:
    data = dict(pin or load_csfml_v1_label_integrity())
    found = {
        str(data.get("authoritative_csfml_v1_sha") or ""),
        str(data.get("authoritative_csfml_v1_qc_sha") or ""),
    }
    return {item for item in found if item}


def refuse_pin_shas_outside_minimum(pin: Mapping[str, Any] | None = None) -> None:
    extra = sorted(pin_declared_csfml_v1_shas(pin) - set(MINIMUM_AUTHORITATIVE_CSFML_V1_SHAS))
    if extra:
        raise ArtifactContractError(
            "csfml_v1_label_integrity.json SHAs not in MINIMUM_AUTHORITATIVE_CSFML_V1_SHAS: {0}".format(
                ", ".join(extra)
            )
        )


def official_csfml_v1_shas(pin: Mapping[str, Any] | None = None) -> frozenset[str]:
    refuse_pin_shas_outside_minimum(pin)
    return frozenset(pin_declared_csfml_v1_shas(pin) | set(MINIMUM_AUTHORITATIVE_CSFML_V1_SHAS))


def _first_present(record: Mapping[str, Any], nested: Mapping[str, Any], key: str) -> Any:
    if key in record:
        return record[key]
    if key in nested:
        return nested[key]
    return None


def refuse_impersonated_official_csfml_v1(payload: Mapping[str, Any] | None) -> None:
    """Refuse official V1 run-id with a non-pin SHA or mutated pin fields.

    Does not authorize a rerun. Window artifacts without identity fields still
    pass when they do not contradict the pin.
    """
    record = dict(payload or {})
    nested = record.get("payload") if isinstance(record.get("payload"), dict) else {}
    if not isinstance(nested, dict):
        nested = {}
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
    strategy_id = _first_present(record, nested, "strategy_id")
    if strategy_id and str(strategy_id) != str(pin.get("strategy_id") or ""):
        raise ArtifactContractError("Official CSFML V1 strategy_id cannot be changed")
    if record.get("holdout_accessed") is True or nested.get("holdout_accessed") is True:
        raise ArtifactContractError("Official CSFML V1 pin has holdout_accessed=false")
    holdout_count = _first_present(record, nested, "holdout_access_count")
    if holdout_count not in (None, 0, "0"):
        raise ArtifactContractError("Official CSFML V1 pin has holdout_accessed=false")
    if "economic_gate" in record or "economic_gate" in nested:
        gate = _first_present(record, nested, "economic_gate")
        if str(gate) != str(pin.get("economic_gate") or "NOT_DEFINED"):
            raise ArtifactContractError("Official CSFML V1 economic_gate cannot be changed")
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
    impact = _first_present(record, nested, "historical_v1_impact")
    if impact is None:
        impact = _first_present(record, nested, "historical_impact")
    if impact is not None and str(impact) != str(pin.get("historical_v1_impact") or "CANNOT_RULE_OUT"):
        raise ArtifactContractError("Official CSFML V1 historical_v1_impact cannot be changed")


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


def csfml_status_distinction(
    strategy_id: str | None = None,
    research_run_id: str | None = None,
) -> dict[str, str] | None:
    """Separate historical integrity, engineering completion, and economic approval.

    Passing current-code tests does not clear official V1. economic_gate stays
    NOT_DEFINED.
    """
    caption = csfml_v1_integrity_caption(strategy_id, research_run_id)
    if caption is None:
        return None
    pin = load_csfml_v1_label_integrity()
    return {
        "historical_integrity": str(pin["historical_v1_impact"]),
        "historical_caption": caption,
        "engineering_completion": (
            "Engineering completion of delisting-event semantics does not "
            "quantify or clear official V1 results."
        ),
        "economic_approval": str(pin.get("economic_gate") or "NOT_DEFINED"),
    }


def csfml_v1_historical_impact_for_run(
    strategy_id: str | None,
    research_run_id: str | None,
) -> str | None:
    """V1 forensic bound applies only to the official full-suite run id."""
    run_id = str(research_run_id or "").strip()
    if not run_id or csfml_v1_integrity_caption(strategy_id, run_id) is None:
        return None
    return str(load_csfml_v1_label_integrity()["historical_v1_impact"])


HISTORICAL_LABEL_FIELDS = (
    "delisted_target_count",
    "invalid_target_count",
    "label_quality",
    "delisting_event_date",
    "resolution_reason",
    "exit_reason",
    "proceeds_known",
)

OFFICIAL_CSFML_V1_TREE = (
    Path(__file__).resolve().parents[2]
    / "stage2_results"
    / "CrossSectionalFactorML"
    / "STAGE2_CrossSectionalFactorML_54a5543f"
)


def _json_keys(value: Any) -> set[str]:
    found: set[str] = set()
    if isinstance(value, dict):
        for key, item in value.items():
            found.add(str(key))
            found |= _json_keys(item)
    elif isinstance(value, list):
        for item in value:
            found |= _json_keys(item)
    return found


def scan_official_csfml_v1_published_tree() -> dict[str, Any]:
    """Bound historical V1 from committed artifacts. Does not quantify contamination.

    Published JSON lacks per-row exit reasons. cohorts_rejected=0 is not proof
    of zero missing-t+21 plus later-delist impact.
    """
    pin = load_csfml_v1_label_integrity()
    tree = OFFICIAL_CSFML_V1_TREE
    if not tree.is_dir():
        raise ArtifactContractError("Official CSFML V1 published tree is missing")
    present: set[str] = set()
    json_files = 0
    for path in tree.rglob("*.json"):
        if not path.is_file():
            continue
        json_files += 1
        payload = json.loads(path.read_text(encoding="utf-8"))
        present |= _json_keys(payload) & set(HISTORICAL_LABEL_FIELDS)
    if str(pin.get("historical_v1_impact") or "") != "CANNOT_RULE_OUT":
        raise ArtifactContractError("Official CSFML V1 pin historical_v1_impact must stay CANNOT_RULE_OUT")
    if pin.get("rerun_authorized") is not False:
        raise ArtifactContractError("Official CSFML V1 pin has rerun_authorized=false")
    if present:
        raise ArtifactContractError(
            "Official CSFML V1 published tree contains {0}; do not treat "
            "cohorts_rejected=0 as zero historical impact".format(sorted(present))
        )
    return {
        "json_files": json_files,
        "historical_fields_present": [],
        "historical_v1_impact": pin["historical_v1_impact"],
        "rerun_authorized": pin["rerun_authorized"],
    }
