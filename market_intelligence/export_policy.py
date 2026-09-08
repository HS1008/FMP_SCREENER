"""Source-aware export filtering for AI/external consumers (``export_safe_v2``).

Scope semantics (every declared ``export_scope`` value has explicit behaviour):

* ``ATTRIBUTION_REQUIRED`` / ``PUBLIC``: values exported; attribution retained.
* ``RESTRICTED_REDISTRIBUTION`` (ICE BofA credit via FRED): identity kept, every
  value-bearing field removed (levels, deltas, percentiles, z-scores, history, ranks).
* ``INTERNAL_ONLY`` (legacy FMP constituent analytics, QC-derived sector aggregates until a
  policy decision says otherwise): identity kept, values removed, reason states the policy.
* ``INTERNAL_SUMMARY`` (strategy monitor / data-health status rows): exported; these carry
  statuses and counts only, never market values.
* unknown / missing scope on a *data entry*: treated as restricted (fail closed). A data
  entry is any object that carries a value-bearing key; structural envelope objects
  (``sections``, ``categories``, ``datasets`` ...) have no value keys of their own and are
  traversed, not scoped.

Lists that contain at least one redacted entry are re-ordered by identity key so position
leaks nothing. The final response envelope is hashed as a whole (``export_sha256`` covers
every field except itself); callers must build the complete response before calling
:func:`finalize_envelope` and must not append fields afterwards.
"""

from __future__ import annotations

import copy
from typing import Any

from market_intelligence.catalog import EXPORT_ATTRIBUTION_REQUIRED, EXPORT_INTERNAL_ONLY, EXPORT_RESTRICTED
from market_intelligence.nulls import canonical_sha256, normalize_payload

EXPORT_SCHEMA_VERSION = "export_safe_v2"
EXPORT_PUBLIC = "PUBLIC"
EXPORT_INTERNAL_SUMMARY = "INTERNAL_SUMMARY"

RESTRICTED_REASON = "Restricted redistribution (ICE BofA via FRED). Values available internally only."
INTERNAL_ONLY_REASON = "Internal-only source (no redistribution entitlement decided). Values available on the DB-only dashboard."
UNSCOPED_REASON = "No export scope declared for this entry; exported as unavailable (fail closed)."
UNKNOWN_SCOPE_REASON = "Unknown export scope {0!r}; exported as unavailable (fail closed)."

EXPORTABLE_SCOPES = {EXPORT_ATTRIBUTION_REQUIRED, EXPORT_PUBLIC, EXPORT_INTERNAL_SUMMARY}
REDACTED_SCOPES = {EXPORT_RESTRICTED, EXPORT_INTERNAL_ONLY}

# Value-bearing keys: their presence makes a dict a scoped data entry.
VALUE_KEYS = {
    "value", "oas_bps", "change_1d_bps", "change_1w_bps", "change_1m_bps", "change_3m_bps",
    "percentile", "zscore", "yield_pct", "chg_prev_bps", "chg_1w_bps", "chg_1m_bps", "chg_3m_bps",
    "latest", "transforms", "history", "metrics", "comparison", "mean", "std",
    "rs_chg_1w", "rs_chg_1m", "rs_chg_3m", "rs_chg_6m", "rs_chg_12m", "ret_1m", "ret_1w", "ret_3m", "ret_6m", "ret_12m",
    "rank", "ranks", "leadership", "score", "weights", "returns", "coverage_stats",
}
IDENTITY_KEYS = {
    "series_id", "metric_id", "label", "title", "bucket", "as_of", "observation_date", "units", "category",
    "subcategory", "tenor", "export_scope", "source", "attribution", "history_status", "history_first_date",
    "window_observations", "percentile_window", "frequency", "seasonal_adjustment", "vintage_kind", "pit_safe",
    "metadata_status", "publication_status", "notes", "transform_version", "status", "sector_key", "entity_kind",
    "instrument_id", "canonical_sector", "provider_label", "benchmark", "return_basis", "value_basis",
    "universe_method", "research_eligible", "source_id", "dataset", "industry_key", "aggregation", "catalog_units",
    "artifact_sha256", "schema_version", "methodology_version", "computed_at", "coverage",
}
_ORDER_KEYS = ("series_id", "metric_id", "sector_key", "industry_key", "instrument_id", "tenor", "bucket", "label")

# Keys that are never exported regardless of scope or nesting (defensive).
ALWAYS_EXCLUDED_KEYS = {"api_key", "token", "password", "account_id", "account_ids", "client_holdings", "model_binary", "object_store_key", "secret", "database_url"}


def _is_data_entry(entry: dict[str, Any]) -> bool:
    return any(k in VALUE_KEYS for k in entry)


def scope_of(entry: dict[str, Any]) -> str | None:
    scope = entry.get("export_scope")
    return str(scope).upper() if scope not in (None, "") else None


def decide(entry: dict[str, Any]) -> tuple[bool, str | None]:
    """Return ``(export_values, redaction_reason)`` for a data entry."""
    scope = scope_of(entry)
    if scope is None:
        return False, UNSCOPED_REASON
    if scope in EXPORTABLE_SCOPES:
        return True, None
    if scope == EXPORT_RESTRICTED:
        return False, RESTRICTED_REASON
    if scope == EXPORT_INTERNAL_ONLY:
        return False, INTERNAL_ONLY_REASON
    return False, UNKNOWN_SCOPE_REASON.format(scope)


def is_restricted(entry: dict[str, Any]) -> bool:
    return isinstance(entry, dict) and _is_data_entry(entry) and not decide(entry)[0]


def redact_entry(entry: dict[str, Any], reason: str | None = None) -> dict[str, Any]:
    if reason is None:
        reason = decide(entry)[1] or RESTRICTED_REASON
    kept: dict[str, Any] = {}
    for k, v in entry.items():
        if k in ALWAYS_EXCLUDED_KEYS or k not in IDENTITY_KEYS or k in VALUE_KEYS:
            continue
        kept[k] = _strip_secrets(v)
    latest = entry.get("latest")
    if isinstance(latest, dict):
        kept["latest"] = {k: latest.get(k) for k in ("observation_date", "units", "retrieved_at", "revision_seq") if k in latest}
    if isinstance(entry.get("coverage"), dict):
        kept["coverage"] = {k: v for k, v in entry["coverage"].items() if not isinstance(v, (int, float)) or k in {"universe_size", "n", "count"}}
    kept["restricted"] = True
    kept["restriction_reason"] = reason
    return kept


def _strip_secrets(obj: Any) -> Any:
    if isinstance(obj, dict):
        return {k: _strip_secrets(v) for k, v in obj.items() if k not in ALWAYS_EXCLUDED_KEYS}
    if isinstance(obj, list):
        return [_strip_secrets(v) for v in obj]
    return copy.deepcopy(obj)


def _order_key(item: Any) -> str:
    if isinstance(item, dict):
        for k in _ORDER_KEYS:
            if item.get(k) is not None:
                return "{0}:{1}".format(k, item[k])
    return "~"


def filter_for_export(obj: Any) -> Any:
    """Recursively apply the export policy; returns a deep-copied filtered structure."""
    if isinstance(obj, dict):
        if _is_data_entry(obj):
            allowed, reason = decide(obj)
            if not allowed:
                return redact_entry(obj, reason)
        out: dict[str, Any] = {}
        for key, value in obj.items():
            if key in ALWAYS_EXCLUDED_KEYS:
                continue
            out[key] = filter_for_export(value)
        return out
    if isinstance(obj, list):
        items = [filter_for_export(item) for item in obj]
        if any(isinstance(i, dict) and i.get("restricted") is True for i in items):
            items = sorted(items, key=_order_key)
        return items
    return copy.deepcopy(obj)


def restricted_count(obj: Any) -> int:
    if isinstance(obj, dict):
        if obj.get("restricted") is True:
            return 1
        return sum(restricted_count(v) for v in obj.values())
    if isinstance(obj, list):
        return sum(restricted_count(v) for v in obj)
    return 0


def export_safe_body(body: Any) -> tuple[Any, bool]:
    """Filter ``body``; return ``(filtered, altered)``."""
    normalized = normalize_payload(body)
    filtered = filter_for_export(normalized)
    return filtered, filtered != normalized


def build_envelope(body: Any, *, provenance: dict[str, Any], **extra: Any) -> dict[str, Any]:
    """Assemble the complete (unhashed) export envelope.

    ``provenance`` must state where the body came from (``kind`` = FROZEN_SNAPSHOT / LIVE_VIEW,
    ``source_snapshot_hash`` for frozen bodies, capture/evaluation timestamps for live views).
    Extra top-level fields (``available``, ``delivery_health`` ...) are passed here so they
    are part of the hashed contract.
    """
    filtered, altered = export_safe_body(body)
    envelope: dict[str, Any] = {
        "export_schema_version": EXPORT_SCHEMA_VERSION,
        "provenance": provenance,
        "source_snapshot_hash": provenance.get("source_snapshot_hash"),
        "export_filtered": altered,
        "restricted_entries": restricted_count(filtered),
        "body": filtered,
    }
    for k, v in extra.items():
        envelope[k] = v
    return envelope


def finalize_envelope(envelope: dict[str, Any]) -> dict[str, Any]:
    """Normalize the complete envelope and stamp ``export_sha256`` over everything else."""
    env = normalize_payload({k: v for k, v in envelope.items() if k != "export_sha256"})
    env["export_sha256"] = export_hash(env)
    return env


def export_safe_payload(body: dict[str, Any], *, source_snapshot_hash: str | None, **extra: Any) -> dict[str, Any]:
    """Convenience: frozen-snapshot envelope, finalized. Do not append fields to the result."""
    provenance = {"kind": "FROZEN_SNAPSHOT" if source_snapshot_hash else "UNPUBLISHED", "source_snapshot_hash": source_snapshot_hash}
    return finalize_envelope(build_envelope(body, provenance=provenance, **extra))


def export_hash(envelope: dict[str, Any]) -> str:
    """SHA-256 of the canonical envelope excluding ``export_sha256`` (every other field counts)."""
    return canonical_sha256({k: v for k, v in envelope.items() if k != "export_sha256"})


def verify_export_hash(envelope: dict[str, Any]) -> bool:
    """Verify the digest over the complete envelope as delivered (no fields removed first)."""
    if not isinstance(envelope, dict):
        return False
    expected = str(envelope.get("export_sha256") or "")
    return bool(expected) and export_hash(normalize_payload(envelope)) == expected


__all__ = [
    "EXPORTABLE_SCOPES",
    "EXPORT_INTERNAL_SUMMARY",
    "EXPORT_PUBLIC",
    "EXPORT_SCHEMA_VERSION",
    "INTERNAL_ONLY_REASON",
    "REDACTED_SCOPES",
    "RESTRICTED_REASON",
    "UNSCOPED_REASON",
    "build_envelope",
    "decide",
    "export_hash",
    "export_safe_body",
    "export_safe_payload",
    "filter_for_export",
    "finalize_envelope",
    "is_restricted",
    "redact_entry",
    "restricted_count",
    "verify_export_hash",
]
