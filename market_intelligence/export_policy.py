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

Export modes
------------

* ``external`` (default, and the only mode a remote REST/MCP client can ever receive):
  the scope rules above apply unchanged. ``INTERNAL_ONLY`` and ``RESTRICTED_REDISTRIBUTION``
  values are redacted; unknown scope fails closed. Authentication proves *who* is asking,
  not that a provider licence permits transmitting the values to a third-party AI service.
* ``owner``: a LOCAL, private session on the data owner's own machine (stdio MCP or a
  loopback HTTP client with no proxy headers). Stored dashboard values in the redacted
  scopes are included. Secrets, model binaries, unknown scopes, and the Stage 2 holdout
  still fail closed. The gateway never grants owner mode to a remote request.
* ``remote_value_sources``: an explicit, source-specific allowlist (``source_id`` values)
  whose ``INTERNAL_ONLY`` / ``RESTRICTED_REDISTRIBUTION`` entries may carry values in
  external mode because a contract or licence has been confirmed by a human. Empty by
  default. It is a per-source decision recorded in configuration, never a blanket
  re-labelling of providers as PUBLIC.
"""

from __future__ import annotations

import copy
from typing import Any, Collection

from market_intelligence.catalog import EXPORT_ATTRIBUTION_REQUIRED, EXPORT_INTERNAL_ONLY, EXPORT_RESTRICTED
from market_intelligence.nulls import canonical_sha256, normalize_payload

EXPORT_SCHEMA_VERSION = "export_safe_v2"
EXPORT_PUBLIC = "PUBLIC"
EXPORT_INTERNAL_SUMMARY = "INTERNAL_SUMMARY"
EXPORT_MODE_EXTERNAL = "external"
EXPORT_MODE_OWNER = "owner"
EXPORT_MODES = (EXPORT_MODE_EXTERNAL, EXPORT_MODE_OWNER)

RESTRICTED_REASON = "Restricted redistribution (ICE BofA via FRED). Values available internally only."
INTERNAL_ONLY_REASON = "Internal-only source (no redistribution entitlement decided). Values available on the DB-only dashboard."
UNSCOPED_REASON = "No export scope declared for this entry; exported as unavailable (fail closed)."
UNKNOWN_SCOPE_REASON = "Unknown export scope {0!r}; exported as unavailable (fail closed)."
REMOTE_SOURCE_ALLOWED_NOTE = "Source-specific remote export right recorded in configuration (AI_GATEWAY_REMOTE_VALUE_SOURCES)."

EXPORTABLE_SCOPES = {EXPORT_ATTRIBUTION_REQUIRED, EXPORT_PUBLIC, EXPORT_INTERNAL_SUMMARY}
REDACTED_SCOPES = {EXPORT_RESTRICTED, EXPORT_INTERNAL_ONLY}

# Value-bearing keys: their presence makes a dict a scoped data entry.
VALUE_KEYS = {
    "value", "oas_bps", "change_1d_bps", "change_1w_bps", "change_1m_bps", "change_3m_bps",
    "percentile", "zscore", "yield_pct", "chg_prev_bps", "chg_1w_bps", "chg_1m_bps", "chg_3m_bps",
    "latest", "transforms", "history", "metrics", "comparison", "mean", "std",
    "rs_chg_1d", "rs_chg_1w", "rs_chg_1m", "rs_chg_3m", "rs_chg_6m", "rs_chg_12m", "ret_1d", "ret_1m", "ret_1w", "ret_3m", "ret_6m", "ret_12m",
    "rs_1d", "rs_1w", "rs_1m", "rs_3m", "rs_6m", "rs_12m", "pct_vs_50dma", "pct_vs_200dma", "rank_1d",
    "rank", "ranks", "leadership", "score", "weights", "returns", "coverage_stats",
    "total_volume", "total_trades", "total_transactions", "volume_change", "trade_count_change",
    "customer_net_volume", "customer_buy_volume", "customer_sell_volume", "total_volume_quantity",
    "total_trade_count",
}
IDENTITY_KEYS = {
    "series_id", "metric_id", "label", "title", "bucket", "as_of", "observation_date", "units", "category",
    "subcategory", "tenor", "export_scope", "source", "attribution", "history_status", "history_first_date",
    "window_observations", "percentile_window", "frequency", "seasonal_adjustment", "vintage_kind", "pit_safe",
    "metadata_status", "publication_status", "notes", "transform_version", "status", "sector_key", "entity_kind",
    "instrument_id", "canonical_sector", "provider_label", "benchmark", "return_basis", "value_basis",
    "universe_method", "research_eligible", "source_id", "dataset", "industry_key", "aggregation", "catalog_units",
    "artifact_sha256", "schema_version", "methodology_version", "computed_at", "coverage",
    "product_category", "capability_status", "grade_code",
    # Provenance / hierarchy identity used by the AI gateway (labels and statuses, never values).
    "sector", "industry", "dataset_kind", "group_kind", "members", "members_used", "members_missing", "weighting",
    "taxonomy_version", "classification_version", "source_kind", "legacy_fmp", "prev_session",
    "aligned_with_benchmark", "adjustment_basis", "return_kind", "provider_series_id", "selection_reason",
    "fallback", "retrieved_at", "revision_seq", "ingestion_run_id", "rank_metric", "value_kind", "hierarchy",
}
_ORDER_KEYS = ("series_id", "metric_id", "sector_key", "industry_key", "instrument_id", "tenor", "bucket", "label")

# Keys that are never exported regardless of scope or nesting (defensive).
ALWAYS_EXCLUDED_KEYS = {"api_key", "token", "password", "account_id", "account_ids", "client_holdings", "model_binary", "object_store_key", "secret", "database_url"}

# Nested payloads that belong to the *same* series/metric as an allowed parent. Scope
# inheritance is limited to these keys so an ATTRIBUTION_REQUIRED parent cannot bless an
# unrelated nested source (credit, sector, another series) that happens to sit beside it.
INHERIT_SCOPE_KEYS = frozenset({"latest", "transforms", "metrics", "comparison", "display", "history", "freshness"})

# Coverage metadata that may be retained on a redacted entry. Counts and labels only;
# never nested value-bearing objects or free-form dicts that could carry secrets.
COVERAGE_ALLOWED_KEYS = frozenset({
    "universe_size", "n", "count", "priced_count", "constituent_count", "status", "note",
    "universe_method", "coverage_status", "held_names", "held_status", "trailing_status",
})


def _is_data_entry(entry: dict[str, Any]) -> bool:
    return any(k in VALUE_KEYS for k in entry)


def scope_of(entry: dict[str, Any]) -> str | None:
    scope = entry.get("export_scope")
    return str(scope).upper() if scope not in (None, "") else None


def normalize_export_mode(mode: str | None) -> str:
    """Unknown or empty modes fail closed to ``external``."""
    text = str(mode or "").strip().lower()
    return text if text in EXPORT_MODES else EXPORT_MODE_EXTERNAL


def _entry_source(entry: dict[str, Any]) -> str | None:
    for key in ("source_id", "provider"):
        value = entry.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip().upper()
    source = entry.get("source")
    if isinstance(source, dict):
        for key in ("source_id", "provider"):
            value = source.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip().upper()
    return None


def decide(
    entry: dict[str, Any],
    *,
    export_mode: str = EXPORT_MODE_EXTERNAL,
    remote_value_sources: Collection[str] = (),
) -> tuple[bool, str | None]:
    """Return ``(export_values, redaction_reason)`` for a data entry.

    ``export_mode`` is ``external`` unless the caller proved a local owner session.
    ``remote_value_sources`` lists ``source_id`` values with a human-confirmed remote
    export right; only entries that name one of those sources may pass a redacted scope
    in external mode. Unknown / missing scope always fails closed.
    """
    scope = scope_of(entry)
    if scope is None:
        return False, UNSCOPED_REASON
    if scope in EXPORTABLE_SCOPES:
        return True, None
    mode = normalize_export_mode(export_mode)
    if scope in REDACTED_SCOPES:
        if mode == EXPORT_MODE_OWNER:
            # Local owner session: stored dashboard values are the owner's own view.
            # Secrets, binaries, unknown scopes, and holdout still fail closed elsewhere.
            return True, None
        source = _entry_source(entry)
        allowed = {str(s).strip().upper() for s in (remote_value_sources or ()) if str(s).strip()}
        if source and source in allowed:
            return True, None
    if scope == EXPORT_RESTRICTED:
        return False, RESTRICTED_REASON
    if scope == EXPORT_INTERNAL_ONLY:
        return False, INTERNAL_ONLY_REASON
    return False, UNKNOWN_SCOPE_REASON.format(scope)


def is_restricted(
    entry: dict[str, Any],
    *,
    export_mode: str = EXPORT_MODE_EXTERNAL,
    remote_value_sources: Collection[str] = (),
) -> bool:
    return isinstance(entry, dict) and _is_data_entry(entry) and not decide(entry, export_mode=export_mode, remote_value_sources=remote_value_sources)[0]


def _sanitize_coverage(coverage: Any) -> dict[str, Any] | None:
    """Allowlisted coverage metadata only; recursively secret-stripped, never value-bearing."""
    if not isinstance(coverage, dict):
        return None
    kept: dict[str, Any] = {}
    for key, value in coverage.items():
        if key in ALWAYS_EXCLUDED_KEYS or key in VALUE_KEYS or key not in COVERAGE_ALLOWED_KEYS:
            continue
        if isinstance(value, dict):
            nested = _sanitize_coverage(value)
            if nested:
                kept[key] = nested
            continue
        if isinstance(value, list):
            continue
        kept[key] = copy.deepcopy(value)
    return _strip_secrets(kept) if kept else None


def redact_entry(
    entry: dict[str, Any],
    reason: str | None = None,
    *,
    export_mode: str = EXPORT_MODE_EXTERNAL,
    remote_value_sources: Collection[str] = (),
) -> dict[str, Any]:
    if reason is None:
        reason = decide(entry, export_mode=export_mode, remote_value_sources=remote_value_sources)[1] or RESTRICTED_REASON
    kept: dict[str, Any] = {}
    for k, v in entry.items():
        if k in ALWAYS_EXCLUDED_KEYS or k not in IDENTITY_KEYS or k in VALUE_KEYS or k == "coverage":
            continue
        kept[k] = _strip_secrets(v)
    latest = entry.get("latest")
    if isinstance(latest, dict):
        kept["latest"] = _strip_secrets({k: latest.get(k) for k in ("observation_date", "units", "retrieved_at", "revision_seq") if k in latest})
    coverage = _sanitize_coverage(entry.get("coverage"))
    if coverage is not None:
        kept["coverage"] = coverage
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


def filter_for_export(
    obj: Any,
    inherited_scope: str | None = None,
    *,
    export_mode: str = EXPORT_MODE_EXTERNAL,
    remote_value_sources: Collection[str] = (),
) -> Any:
    """Recursively apply the export policy; returns a deep-copied filtered structure.

    ``inherited_scope`` is the export scope of an already-allowed parent series/metric.
    It applies only to unscoped nested payloads under :data:`INHERIT_SCOPE_KEYS` (or
    through those objects). An explicit child ``export_scope`` always wins. Structural
    envelopes and unrelated nested sources do not inherit.
    """
    options = {"export_mode": export_mode, "remote_value_sources": remote_value_sources}
    if isinstance(obj, dict):
        working = obj
        if inherited_scope and not scope_of(obj) and _is_data_entry(obj):
            working = dict(obj)
            working["export_scope"] = inherited_scope
        if _is_data_entry(working):
            allowed, reason = decide(working, **options)
            if not allowed:
                return redact_entry(working, reason, **options)
            parent_scope = scope_of(working)
            out: dict[str, Any] = {}
            for key, value in obj.items():
                if key in ALWAYS_EXCLUDED_KEYS:
                    continue
                child_scope = parent_scope if key in INHERIT_SCOPE_KEYS else None
                out[key] = filter_for_export(value, child_scope, **options)
            return out
        out = {}
        for key, value in obj.items():
            if key in ALWAYS_EXCLUDED_KEYS:
                continue
            out[key] = filter_for_export(value, inherited_scope, **options)
        return out
    if isinstance(obj, list):
        items = [filter_for_export(item, inherited_scope, **options) for item in obj]
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


def export_safe_body(
    body: Any,
    *,
    export_mode: str = EXPORT_MODE_EXTERNAL,
    remote_value_sources: Collection[str] = (),
) -> tuple[Any, bool]:
    """Filter ``body``; return ``(filtered, altered)``."""
    normalized = normalize_payload(body)
    filtered = filter_for_export(normalized, export_mode=export_mode, remote_value_sources=remote_value_sources)
    return filtered, filtered != normalized


def build_envelope(
    body: Any,
    *,
    provenance: dict[str, Any],
    export_mode: str = EXPORT_MODE_EXTERNAL,
    remote_value_sources: Collection[str] = (),
    **extra: Any,
) -> dict[str, Any]:
    """Assemble the complete (unhashed) export envelope.

    ``provenance`` must state where the body came from (``kind`` = FROZEN_SNAPSHOT / LIVE_VIEW,
    ``source_snapshot_hash`` for frozen bodies, capture/evaluation timestamps for live views).
    Extra top-level fields (``available``, ``delivery_health`` ...) are passed here so they
    are part of the hashed contract. ``export_mode`` is recorded in the envelope so a
    consumer can see whether redacted scopes were included (owner) or not (external).
    """
    mode = normalize_export_mode(export_mode)
    filtered, altered = export_safe_body(body, export_mode=mode, remote_value_sources=remote_value_sources)
    envelope: dict[str, Any] = {
        "export_schema_version": EXPORT_SCHEMA_VERSION,
        "export_mode": mode,
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
    "EXPORT_MODES",
    "EXPORT_MODE_EXTERNAL",
    "EXPORT_MODE_OWNER",
    "EXPORT_PUBLIC",
    "EXPORT_SCHEMA_VERSION",
    "INHERIT_SCOPE_KEYS",
    "INTERNAL_ONLY_REASON",
    "REDACTED_SCOPES",
    "REMOTE_SOURCE_ALLOWED_NOTE",
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
    "normalize_export_mode",
    "redact_entry",
    "restricted_count",
    "verify_export_hash",
]
