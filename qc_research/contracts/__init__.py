"""Shared research artifact contracts (schema, hashes, lifecycle)."""

from qc_research.contracts.hashing import canonical_dumps, payload_for_hash, sha256_payload, verify_artifact_sha256
from qc_research.contracts.kinds import (
    KIND_REQUIRED_FIELDS,
    PLATFORM_KINDS,
    PRICE_TECH_V1_FEATURE_ORDER,
    SCHEMA_VERSION,
    reject_holdout_access,
    reject_synthetic_official,
)
from qc_research.contracts.label_integrity import (
    csfml_v1_historical_impact_for_run,
    csfml_v1_integrity_caption,
    load_csfml_v1_label_integrity,
    refuse_impersonated_official_csfml_v1,
)
from qc_research.contracts.sealed_results import (
    sealed_results_run_ids,
    refuse_sealed_stage1_summary,
)

__all__ = [
    "KIND_REQUIRED_FIELDS",
    "PLATFORM_KINDS",
    "PRICE_TECH_V1_FEATURE_ORDER",
    "SCHEMA_VERSION",
    "canonical_dumps",
    "csfml_v1_historical_impact_for_run",
    "csfml_v1_integrity_caption",
    "load_csfml_v1_label_integrity",
    "payload_for_hash",
    "refuse_impersonated_official_csfml_v1",
    "refuse_sealed_stage1_summary",
    "reject_holdout_access",
    "sealed_results_run_ids",
    "reject_synthetic_official",
    "sha256_payload",
    "verify_artifact_sha256",
]
