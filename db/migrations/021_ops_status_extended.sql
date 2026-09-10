-- Richer Data Health / System ops row. Additive view replacement only.

CREATE OR REPLACE VIEW mi_v_ops_status AS
SELECT
    'data_health'::text AS surface,
    (SELECT COUNT(*) FROM mi_source_registry) AS source_count,
    (SELECT COUNT(*) FROM mi_data_freshness WHERE freshness_status = 'STALE') AS stale_count,
    (SELECT COUNT(*) FROM mi_data_freshness WHERE transport_status IN ('FAILED', 'METADATA_REJECTED')) AS failed_transport_count,
    (SELECT MAX(last_success_at) FROM mi_data_freshness) AS last_successful_refresh,
    (SELECT COUNT(*) FROM research_runs) AS research_run_count,
    (SELECT COUNT(*) FROM schema_migrations) AS migration_count,
    (SELECT COUNT(*) FROM schema_migrations WHERE sha256 IS NULL) AS migrations_missing_checksum,
    (SELECT MAX(applied_at) FROM schema_migrations) AS last_migration_at,
    (SELECT MAX(last_seen_at) FROM research_runs) AS latest_research_update,
    (SELECT COUNT(*) FROM research_runs WHERE COALESCE(holdout_accessed, FALSE) = TRUE) AS holdout_accessed_runs,
    (SELECT MAX(heartbeat_age_seconds) FROM mi_v_ibkr_collector_status) AS ibkr_oldest_heartbeat_age_seconds,
    (SELECT COUNT(*) FROM mi_v_ibkr_quotes_latest) AS ibkr_quote_count;
