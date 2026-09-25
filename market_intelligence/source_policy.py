"""Versioned source-policy catalog. Definitions are not production enablement."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any

POLICY_CATALOG_VERSION = "source_policy_v1"


@dataclass(frozen=True)
class SourcePolicy:
    policy_id: str
    source_id: str
    dataset: str
    enabled: bool
    priority: str
    cadence: str
    market_or_release_window: str
    freshness_slo: str
    retention_detail: str
    retention_rollup: str
    rights_scope: str
    activation_state: str = "OFF"


POLICIES: tuple[SourcePolicy, ...] = (
    SourcePolicy("fred_macro", "FRED", "fred_series_observations", False, "MARKET_HUB", "release-aware", "FRED release calendar America/New_York", "release+catch-up", "all revisions", "long-term", "ATTRIBUTION_REQUIRED", "CONFIGURED_NOT_AUTO_ENABLED"),
    SourcePolicy("treasury_xml", "TREASURY", "daily_par_curve", False, "MARKET_HUB", "afternoon probes", "15:35/16:05/17:00 ET", "stop after expected date", "all revisions", "long-term", "ATTRIBUTION_REQUIRED", "CONFIGURED_NOT_AUTO_ENABLED"),
    SourcePolicy("equity_eod", "EQUITY_EOD", "equity_etf_daily_bars", False, "CORE_REALTIME", "18:15 ET after session", "NYSE completed session", "incremental 1W", "long-term", "long-term", "INTERNAL_ONLY", "SOAK_IN_PROGRESS"),
    SourcePolicy("ibkr_quotes", "IBKR_MARKET_DATA", "market_quotes", False, "CORE_REALTIME", "session streaming", "TWS local", "coalesce ~5s", "90d 1-minute", "2y 5-minute", "INTERNAL_ONLY", "WINDOWS_COLLECTOR"),
    SourcePolicy("eia_v2", "EIA", "eia_v2_catalog", False, "MARKET_HUB", "release + catch-up", "EIA publication windows", "native cadence", "all revisions", "long-term", "ATTRIBUTION_REQUIRED", "OFF"),
    SourcePolicy("cftc_cot", "CFTC_COT", "cot_futures_only", False, "MARKET_HUB", "Fri 15:30 ET then hourly catch-up", "CFTC release calendar", "position date vs availability separate", "all revisions", "long-term", "ATTRIBUTION_REQUIRED", "OFF"),
    SourcePolicy("openfigi", "OPENFIGI", "mapping", False, "REPAIR_BACKFILL", "event-driven", "new/unresolved identifiers", "batch cache", "cache TTL", "identifier long-term", "ATTRIBUTION_REQUIRED", "OFF"),
    SourcePolicy("sec_edgar", "SEC_EDGAR", "filings_facts", False, "MARKET_HUB", "06:00-22:00 ET ~2min", "SEC fair access", "bounded issuers", "all acquired revisions", "long-term", "ATTRIBUTION_REQUIRED", "OFF"),
    SourcePolicy("finra_query", "FINRA_QUERY", "aggregates", False, "MARKET_HUB", "daily publication", "FINRA Query", "incremental", "aggregates long-term", "long-term", "INTERNAL_ONLY", "RETAINED"),
    SourcePolicy("trace_prints", "FINRA_TRACE", "individual_prints", False, "MARKET_HUB", "entitled only", "TRAQS/TRACE", "disabled while gated", "n/a", "n/a", "INTERNAL_ONLY", "ENTITLEMENT_REQUIRED"),
    SourcePolicy("fmp_legacy", "FMP_LEGACY", "precomputed_sector_bundles", False, "BROAD_BACKGROUND", "nightly if allowed", "legacy bundles", "transitional", "retain while FMP on", "retain", "INTERNAL_ONLY", "TRANSITION"),
)


def policies_as_dicts() -> list[dict[str, Any]]:
    return [asdict(item) for item in POLICIES]


def persist_policies(conn, *, dry_run: bool = True) -> dict[str, Any]:
    """Write policy definitions. Dry-run default; never flips enabled=true."""
    rows = policies_as_dicts()
    if dry_run:
        return {"dry_run": True, "count": len(rows), "enabled_any": any(item.enabled for item in POLICIES)}
    from sqlalchemy import text

    written = 0
    for item in rows:
        conn.execute(
            text(
                """
                INSERT INTO mi_source_policies (
                    policy_id, source_id, dataset, enabled, priority, cadence, market_or_release_window,
                    freshness_slo, retention_detail, retention_rollup, rights_scope, catalog_version
                ) VALUES (
                    :policy_id, :source_id, :dataset, FALSE, :priority, :cadence, :window,
                    :slo, :detail, :rollup, :rights, :catalog
                )
                ON CONFLICT (policy_id) DO UPDATE SET
                    cadence = EXCLUDED.cadence, market_or_release_window = EXCLUDED.market_or_release_window,
                    freshness_slo = EXCLUDED.freshness_slo, catalog_version = EXCLUDED.catalog_version, updated_at = NOW()
                """
            ),
            {
                "policy_id": item["policy_id"],
                "source_id": item["source_id"],
                "dataset": item["dataset"],
                "priority": item["priority"],
                "cadence": item["cadence"],
                "window": item["market_or_release_window"],
                "slo": item["freshness_slo"],
                "detail": item["retention_detail"],
                "rollup": item["retention_rollup"],
                "rights": item["rights_scope"],
                "catalog": POLICY_CATALOG_VERSION,
            },
        )
        written += 1
    return {"dry_run": False, "count": written, "enabled_any": False}
