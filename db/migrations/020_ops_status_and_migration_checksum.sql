-- Operational status view for Data Health / System.
-- schema_migrations.sha256 is also ensured by jobs.apply_migrations.

CREATE OR REPLACE VIEW mi_v_ops_status AS
SELECT
    'data_health'::text AS surface,
    (SELECT COUNT(*) FROM mi_source_registry) AS source_count,
    (SELECT COUNT(*) FROM mi_data_freshness WHERE freshness_status = 'STALE') AS stale_count,
    (SELECT COUNT(*) FROM mi_data_freshness WHERE transport_status IN ('FAILED', 'METADATA_REJECTED')) AS failed_transport_count,
    (SELECT MAX(last_success_at) FROM mi_data_freshness) AS last_successful_refresh,
    (SELECT COUNT(*) FROM research_runs) AS research_run_count,
    (SELECT COUNT(*) FROM schema_migrations) AS migration_count;
