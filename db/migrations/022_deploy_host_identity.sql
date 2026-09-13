-- Sanitized host Streamlit identity for Data Health. No URLs or passwords.
-- Streamlit reads this only through mi_v_ops_status. Host JSON stays off the UI.

CREATE TABLE IF NOT EXISTS mi_deploy_host_identity (
    recorded_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    git_sha TEXT NOT NULL,
    checkout TEXT,
    deploy_mode TEXT,
    immutable_release_rc INTEGER,
    immutable_current_present BOOLEAN,
    systemd_still_git_pull BOOLEAN,
    systemd_cutover_proven BOOLEAN,
    readonly_verify_rc INTEGER,
    readonly_proven BOOLEAN,
    dashboard_readonly_url_set BOOLEAN,
    dashboard_readonly_password_file_present BOOLEAN,
    writer_fallback BOOLEAN,
    writer_env_keys_present TEXT,
    provider_fetch BOOLEAN,
    csfml_v1_label_integrity TEXT,
    csfml_v1_rerun_authorized BOOLEAN
);

CREATE INDEX IF NOT EXISTS mi_deploy_host_identity_recorded_at_idx
    ON mi_deploy_host_identity (recorded_at DESC);

COMMENT ON TABLE mi_deploy_host_identity IS
    'Append-only sanitized deploy identity. Latest row is exposed on mi_v_ops_status.';

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
    (SELECT recorded_at FROM mi_deploy_host_identity ORDER BY recorded_at DESC LIMIT 1) AS deploy_identity_recorded_at;
