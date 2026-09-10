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

__all__ = [
    "KIND_REQUIRED_FIELDS",
    "PLATFORM_KINDS",
    "PRICE_TECH_V1_FEATURE_ORDER",
    "SCHEMA_VERSION",
    "canonical_dumps",
    "payload_for_hash",
    "reject_holdout_access",
    "reject_synthetic_official",
    "sha256_payload",
    "verify_artifact_sha256",
]
