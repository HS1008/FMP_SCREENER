-- Persist official CSFML / TLT live query-back on the latest deploy identity row.
-- Additive. Do not edit 023. Streamlit still reads only through mi_v_ops_status.
-- New view columns are appended; CREATE OR REPLACE cannot insert or rename.
-- Host JSON stays off the UI. This is not an economic PASS/WATCH/FAIL.

ALTER TABLE mi_deploy_host_identity
    ADD COLUMN IF NOT EXISTS csfml_v1_live_present BOOLEAN;
ALTER TABLE mi_deploy_host_identity
    ADD COLUMN IF NOT EXISTS csfml_v1_live_identity_ok BOOLEAN;
ALTER TABLE mi_deploy_host_identity
    ADD COLUMN IF NOT EXISTS csfml_v1_live_blockers TEXT;
ALTER TABLE mi_deploy_host_identity
    ADD COLUMN IF NOT EXISTS tlt_v0_live_present BOOLEAN;
ALTER TABLE mi_deploy_host_identity
    ADD COLUMN IF NOT EXISTS tlt_v0_live_identity_ok BOOLEAN;
ALTER TABLE mi_deploy_host_identity
    ADD COLUMN IF NOT EXISTS tlt_v0_live_blockers TEXT;

COMMENT ON COLUMN mi_deploy_host_identity.csfml_v1_live_present IS
    'Official CSFML V1 research_runs row was present during deploy query-back.';
COMMENT ON COLUMN mi_deploy_host_identity.csfml_v1_live_identity_ok IS
    'Official CSFML V1 stored identity matched the pin during deploy query-back.';
COMMENT ON COLUMN mi_deploy_host_identity.csfml_v1_live_blockers IS
    'Sanitized CSFML V1 identity blockers. Never a URL or password.';
COMMENT ON COLUMN mi_deploy_host_identity.tlt_v0_live_present IS
    'Official TLT V0 research_runs row was present during deploy query-back.';
COMMENT ON COLUMN mi_deploy_host_identity.tlt_v0_live_identity_ok IS
    'Official TLT V0 stored identity matched the frozen contract during deploy query-back.';
COMMENT ON COLUMN mi_deploy_host_identity.tlt_v0_live_blockers IS
    'Sanitized TLT V0 identity blockers. Never a URL or password.';

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
    (SELECT streamlit_readonly FROM mi_deploy_host_identity ORDER BY recorded_at DESC LIMIT 1) AS dashboard_streamlit_readonly,
    (SELECT csfml_v1_live_present FROM mi_deploy_host_identity ORDER BY recorded_at DESC LIMIT 1) AS csfml_v1_live_present,
    (SELECT csfml_v1_live_identity_ok FROM mi_deploy_host_identity ORDER BY recorded_at DESC LIMIT 1) AS csfml_v1_live_identity_ok,
    (SELECT csfml_v1_live_blockers FROM mi_deploy_host_identity ORDER BY recorded_at DESC LIMIT 1) AS csfml_v1_live_blockers,
    (SELECT tlt_v0_live_present FROM mi_deploy_host_identity ORDER BY recorded_at DESC LIMIT 1) AS tlt_v0_live_present,
    (SELECT tlt_v0_live_identity_ok FROM mi_deploy_host_identity ORDER BY recorded_at DESC LIMIT 1) AS tlt_v0_live_identity_ok,
    (SELECT tlt_v0_live_blockers FROM mi_deploy_host_identity ORDER BY recorded_at DESC LIMIT 1) AS tlt_v0_live_blockers;
