"""Source-aware export filtering for AI/external consumers.

Entries tagged ``export_scope == RESTRICTED_REDISTRIBUTION`` (ICE BofA credit series via
FRED) keep their identity (series id, label, dates, units, coverage, attribution) but lose
every value-bearing field, including deltas, percentiles, z-scores and history arrays.
Lists containing restricted entries are kept in catalog order (never value-ranked) so the
position leaks nothing. The filtered body gets its own hash (``export_sha256``) and keeps
``source_snapshot_hash`` pointing at the unfiltered internal snapshot.
"""

from __future__ import annotations

import copy
from typing import Any

from market_intelligence.catalog import EXPORT_RESTRICTED
from market_intelligence.nulls import canonical_sha256, normalize_payload

EXPORT_SCHEMA_VERSION = "export_safe_v1"
RESTRICTED_REASON = "Restricted redistribution (ICE BofA via FRED). Values available internally only."

VALUE_KEYS = {
    "value", "oas_bps", "change_1d_bps", "change_1w_bps", "change_1m_bps", "change_3m_bps",
    "percentile", "zscore", "yield_pct", "chg_prev_bps", "chg_1w_bps", "chg_1m_bps", "chg_3m_bps",
    "latest", "transforms", "history", "series", "values", "metrics", "comparison", "mean", "std",
}
IDENTITY_KEYS = {
    "series_id", "metric_id", "label", "title", "bucket", "as_of", "observation_date", "units", "category",
    "subcategory", "tenor", "export_scope", "source", "attribution", "history_status", "history_first_date",
    "window_observations", "percentile_window", "frequency", "seasonal_adjustment", "vintage_kind", "pit_safe",
    "metadata_status", "notes", "transform_version", "status",
}

# Keys that are never exported regardless of scope (defensive against accidental inclusion).
ALWAYS_EXCLUDED_KEYS = {"api_key", "token", "password", "account_id", "client_holdings", "model_binary", "object_store_key"}


def is_restricted(entry: dict[str, Any]) -> bool:
    return str(entry.get("export_scope") or "").upper() == EXPORT_RESTRICTED


def redact_entry(entry: dict[str, Any]) -> dict[str, Any]:
    kept = {k: v for k, v in entry.items() if k in IDENTITY_KEYS and k not in VALUE_KEYS}
    if isinstance(entry.get("latest"), dict):
        kept["latest"] = {k: v for k, v in entry["latest"].items() if k in {"observation_date", "units", "retrieved_at"}}
    kept["restricted"] = True
    kept["restriction_reason"] = RESTRICTED_REASON
    return kept


def filter_for_export(obj: Any) -> Any:
    """Recursively apply the export policy; returns a deep-copied filtered structure."""
    if isinstance(obj, dict):
        if is_restricted(obj):
            return redact_entry(obj)
        out: dict[str, Any] = {}
        for key, value in obj.items():
            if key in ALWAYS_EXCLUDED_KEYS:
                continue
            out[key] = filter_for_export(value)
        return out
    if isinstance(obj, list):
        return [filter_for_export(item) for item in obj]
    return copy.deepcopy(obj)


def restricted_count(obj: Any) -> int:
    if isinstance(obj, dict):
        if obj.get("restricted") is True:
            return 1
        return sum(restricted_count(v) for v in obj.values())
    if isinstance(obj, list):
        return sum(restricted_count(v) for v in obj)
    return 0


def export_safe_payload(body: dict[str, Any], *, source_snapshot_hash: str | None) -> dict[str, Any]:
    """Filter ``body`` and wrap it with export identity (own hash + source snapshot hash)."""
    filtered = filter_for_export(normalize_payload(body))
    altered = filtered != normalize_payload(body)
    envelope = {
        "export_schema_version": EXPORT_SCHEMA_VERSION,
        "source_snapshot_hash": source_snapshot_hash,
        "export_filtered": altered,
        "restricted_entries": restricted_count(filtered),
        "body": filtered,
    }
    envelope["export_sha256"] = export_hash(envelope)
    return envelope


def export_hash(envelope: dict[str, Any]) -> str:
    """SHA-256 of the canonical envelope excluding ``export_sha256`` (same convention as artifact_sha256)."""
    return canonical_sha256({k: v for k, v in envelope.items() if k != "export_sha256"})


def verify_export_hash(envelope: dict[str, Any]) -> bool:
    expected = str(envelope.get("export_sha256") or "")
    return bool(expected) and export_hash(envelope) == expected


__all__ = [
    "EXPORT_SCHEMA_VERSION",
    "RESTRICTED_REASON",
    "export_hash",
    "export_safe_payload",
    "filter_for_export",
    "is_restricted",
    "redact_entry",
    "restricted_count",
    "verify_export_hash",
]
