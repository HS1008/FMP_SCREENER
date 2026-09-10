-- Record whether the Streamlit process was pinned read-only.
-- Additive. Do not edit 022. Streamlit still reads only through mi_v_ops_status.
-- New view columns are appended; CREATE OR REPLACE cannot insert or rename.

ALTER TABLE mi_deploy_host_identity
    ADD COLUMN IF NOT EXISTS streamlit_readonly BOOLEAN;

COMMENT ON COLUMN mi_deploy_host_identity.streamlit_readonly IS
    'FMP_STREAMLIT_READONLY was enabled in the dashboard systemd identity.';

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
    (SELECT COUNT(*) FROM mi_v_ibkr_quotes_latest) AS ibkr_quote_count,
    (SELECT git_sha FROM mi_deploy_host_identity ORDER BY recorded_at DESC LIMIT 1) AS deploy_git_sha,
    (SELECT readonly_proven FROM mi_deploy_host_identity ORDER BY recorded_at DESC LIMIT 1) AS dashboard_readonly_proven,
    (SELECT systemd_still_git_pull FROM mi_deploy_host_identity ORDER BY recorded_at DESC LIMIT 1) AS systemd_still_git_pull,
    (SELECT systemd_cutover_proven FROM mi_deploy_host_identity ORDER BY recorded_at DESC LIMIT 1) AS systemd_cutover_proven,
    (SELECT immutable_current_present FROM mi_deploy_host_identity ORDER BY recorded_at DESC LIMIT 1) AS immutable_current_present,
    (SELECT immutable_release_rc FROM mi_deploy_host_identity ORDER BY recorded_at DESC LIMIT 1) AS immutable_release_rc,
    (SELECT csfml_v1_label_integrity FROM mi_deploy_host_identity ORDER BY recorded_at DESC LIMIT 1) AS csfml_v1_label_integrity,
    (SELECT csfml_v1_rerun_authorized FROM mi_deploy_host_identity ORDER BY recorded_at DESC LIMIT 1) AS csfml_v1_rerun_authorized,
    (SELECT recorded_at FROM mi_deploy_host_identity ORDER BY recorded_at DESC LIMIT 1) AS deploy_identity_recorded_at,
    (SELECT streamlit_readonly FROM mi_deploy_host_identity ORDER BY recorded_at DESC LIMIT 1) AS dashboard_streamlit_readonly;
