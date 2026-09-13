"""Shared response envelope: provenance, export policy, size bound.

The export mode is never taken from configuration alone. ``respond`` asks the request
context (``ai_gateway.context``) whether this session is a proven local owner session;
everything else is filtered with the restrictive ``external`` policy plus any explicit,
source-specific remote rights recorded by the operator.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from market_intelligence.export_policy import (
    EXPORT_MODE_EXTERNAL,
    EXPORT_MODE_OWNER,
    build_envelope,
    finalize_envelope,
    normalize_export_mode,
)
from market_intelligence.nulls import normalize_payload, strict_dumps
from market_intelligence.read_models import snapshot_age

from ai_gateway import SCHEMA_VERSION
from ai_gateway.config import MAX_RESPONSE_CHARS
from ai_gateway.context import current_context, effective_export_mode, effective_remote_value_sources
from ai_gateway.errors import PAYLOAD_TOO_LARGE, GatewayError

KIND_FROZEN = "FROZEN_SNAPSHOT"
KIND_LIVE = "LIVE_VIEW"
KIND_DERIVED = "DERIVED"

REMOTE_NOTICE = (
    "External export policy: INTERNAL_ONLY and RESTRICTED_REDISTRIBUTION values are redacted "
    "(identity, dates, status and provenance are kept). Authentication does not grant "
    "redistribution rights; a source appears with values only when its scope is PUBLIC / "
    "ATTRIBUTION_REQUIRED or an operator recorded an explicit remote right for that source."
)
OWNER_NOTICE = (
    "Local owner session (stdio or loopback without proxy headers). Stored dashboard values may "
    "include internal or restricted-redistribution series for the data owner only. Do not republish."
)


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def live_provenance(*, views: list[str], captured_at: Any, note: str | None = None) -> dict[str, Any]:
    return {
        "kind": KIND_LIVE,
        "source_snapshot_hash": None,
        "captured_at": captured_at,
        "views": views,
        "note": note or "Live read of curated views inside one READ ONLY transaction.",
    }


def frozen_provenance(snapshot: dict[str, Any] | None) -> dict[str, Any]:
    if not snapshot:
        return {"kind": "UNPUBLISHED", "source_snapshot_hash": None, "snapshot_id": None, "cutoff_at": None}
    return {
        "kind": KIND_FROZEN,
        "source_snapshot_hash": snapshot.get("snapshot_sha256"),
        "snapshot_id": snapshot.get("snapshot_id"),
        "cutoff_at": snapshot.get("cutoff_at"),
        "generated_at": snapshot.get("generated_at"),
        "schema_version": snapshot.get("schema_version"),
        "quality_status": snapshot.get("quality_status"),
    }


def snapshot_meta(snapshot: dict[str, Any] | None) -> dict[str, Any] | None:
    if not snapshot:
        return None
    return {
        k: snapshot.get(k)
        for k in (
            "snapshot_id",
            "schema_version",
            "generated_at",
            "cutoff_at",
            "as_of_date",
            "completeness",
            "snapshot_sha256",
            "content_sha256",
            "quality_status",
        )
    }


def resolve_mode(mode: str | None = None) -> str:
    """Explicit ``mode`` may only tighten the policy; it can never widen a remote session."""
    effective = effective_export_mode(current_context())
    if mode is None:
        return effective
    requested = normalize_export_mode(mode)
    if requested == EXPORT_MODE_OWNER and effective != EXPORT_MODE_OWNER:
        return EXPORT_MODE_EXTERNAL
    return requested


def respond(
    body: Any,
    *,
    provenance: dict[str, Any],
    tool: str,
    available: bool,
    unavailable_reason: str | None = None,
    degraded: bool = False,
    snapshot: dict[str, Any] | None = None,
    extra: dict[str, Any] | None = None,
    mode: str | None = None,
) -> dict[str, Any]:
    chosen = resolve_mode(mode)
    sources = effective_remote_value_sources(current_context()) if chosen == EXPORT_MODE_EXTERNAL else ()
    envelope = build_envelope(
        body,
        provenance=provenance,
        export_mode=chosen,
        remote_value_sources=sources,
        schema_version=SCHEMA_VERSION,
        tool=tool,
        available=available,
        unavailable_reason=unavailable_reason,
        degraded=bool(degraded),
        latest_snapshot=snapshot_meta(snapshot),
        delivery_health=snapshot_age(snapshot),
        interpretation="NONE",
        **(extra or {}),
    )
    if chosen == EXPORT_MODE_OWNER:
        envelope["owner_session"] = True
        envelope["redistribution_notice"] = OWNER_NOTICE
    else:
        envelope["owner_session"] = False
        envelope["remote_value_sources"] = list(sources)
        envelope["redistribution_notice"] = REMOTE_NOTICE
    finalized = finalize_envelope(normalize_payload(envelope))
    encoded = strict_dumps(finalized)
    if len(encoded) > MAX_RESPONSE_CHARS:
        raise GatewayError(PAYLOAD_TOO_LARGE, "response exceeded the maximum size", http_status=413)
    finalized["_row_count"] = _count_rows(finalized.get("body"))
    finalized["_response_chars"] = len(encoded)
    return finalized


def public_payload(envelope: dict[str, Any]) -> dict[str, Any]:
    return {k: v for k, v in envelope.items() if not str(k).startswith("_")}


def _count_rows(body: Any) -> int:
    if isinstance(body, list):
        return len(body)
    if not isinstance(body, dict):
        return 0 if body is None else 1
    for key in ("rows", "series", "buckets", "strategies", "windows", "experiments", "sources", "items", "changes"):
        value = body.get(key)
        if isinstance(value, list):
            return len(value)
    return 1
