"""Canonical artifact hashing shared by producer and consumer.

SHA-256 over canonical JSON with artifact_sha256 excluded.
"""

from __future__ import annotations

import hashlib
import json
from typing import Any


class ArtifactHashError(ValueError):
    """Artifact hash failed."""


def canonical_dumps(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), default=str)


def sha256_payload(value: Any) -> str:
    return hashlib.sha256(canonical_dumps(value).encode("utf-8")).hexdigest()


def payload_for_hash(payload: dict[str, Any]) -> dict[str, Any]:
    return {key: value for key, value in payload.items() if key != "artifact_sha256"}


def verify_artifact_sha256(payload: dict[str, Any], expected: str | None) -> str:
    actual = sha256_payload(payload_for_hash(payload) if isinstance(payload, dict) else payload)
    if expected and actual != str(expected).strip():
        raise ArtifactHashError(
            "SHA-256 mismatch: expected {0}, computed {1}".format(expected, actual)
        )
    return actual
