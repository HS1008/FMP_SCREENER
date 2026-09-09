-- Human-provisioned read-only role for Market Intelligence pages and the AI context API.
-- NOT applied by jobs/apply_migrations.py (this directory is outside db/migrations).
--
-- Repeatable. Re-running only refreshes grants/defaults; it never resets an existing
-- password. Run as a database superuser / owner after migrations 008+ are applied.
--
-- First provisioning (password prompted interactively; never in argv / shell history):
--   psql "$ADMIN_DATABASE_URL" -f db/roles/market_intelligence_readonly.sql
--   -> prompted "mi_readonly password:" only when the role does not exist yet.
-- Non-interactive first provisioning (read from a protected file, still not in argv):
--   psql "$ADMIN_DATABASE_URL" -v ro_password="$(cat /etc/fmp/mi_readonly.pw)" -f db/roles/market_intelligence_readonly.sql
-- Grant refresh after new mi_v_* views ship (no password involved):
--   psql "$ADMIN_DATABASE_URL" -f db/roles/market_intelligence_readonly.sql
-- Password rotation is an explicit, separate operator action:
--   psql "$ADMIN_DATABASE_URL" -c '\password mi_readonly'
--
-- The role can only SELECT the curated mi_v_* views. No raw tables, no research tables,
-- no CREATE, no superuser. Views run with their owner's privileges (security_invoker off),
-- which is what exposes curated columns without granting the underlying tables.

\set ON_ERROR_STOP on

SELECT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'mi_readonly') AS role_exists \gset

\if :role_exists
    \echo 'mi_readonly exists: refreshing grants only (password unchanged)'
\else
    \if :{?ro_password}
    \else
        \prompt 'mi_readonly password: ' ro_password
    \endif
    CREATE ROLE mi_readonly
        LOGIN
        NOSUPERUSER
        NOCREATEDB
        NOCREATEROLE
        NOINHERIT
        NOREPLICATION
        CONNECTION LIMIT 10
        PASSWORD :'ro_password';
\endif

-- Connect + schema usage only.
GRANT CONNECT ON DATABASE :"DBNAME" TO mi_readonly;
GRANT USAGE ON SCHEMA public TO mi_readonly;

-- Explicit deny of anything granted directly to the role in the curated scope. NOTE: this
-- does not cancel privileges granted to PUBLIC; see the administrator remediation notes in
-- docs/MARKET_INTELLIGENCE.md (PostgreSQL < 15 grants CREATE ON SCHEMA public TO PUBLIC).
REVOKE ALL ON ALL TABLES IN SCHEMA public FROM mi_readonly;
REVOKE ALL ON ALL SEQUENCES IN SCHEMA public FROM mi_readonly;
REVOKE ALL ON ALL FUNCTIONS IN SCHEMA public FROM mi_readonly;
REVOKE CREATE ON SCHEMA public FROM mi_readonly;

-- Curated views (SELECT only).
GRANT SELECT ON mi_v_source_health TO mi_readonly;
GRANT SELECT ON mi_v_ingestion_runs_recent TO mi_readonly;
GRANT SELECT ON mi_v_macro_series TO mi_readonly;
GRANT SELECT ON mi_v_macro_observations_current TO mi_readonly;
GRANT SELECT ON mi_v_macro_latest TO mi_readonly;
GRANT SELECT ON mi_v_metric_latest TO mi_readonly;
GRANT SELECT ON mi_v_metric_history TO mi_readonly;
GRANT SELECT ON mi_v_credit_latest TO mi_readonly;
GRANT SELECT ON mi_v_sector_latest TO mi_readonly;
GRANT SELECT ON mi_v_industry_latest TO mi_readonly;
GRANT SELECT ON mi_v_morning_context_latest TO mi_readonly;
GRANT SELECT ON mi_v_morning_context_index TO mi_readonly;
GRANT SELECT ON mi_v_strategy_research_summary TO mi_readonly;
GRANT SELECT ON mi_v_research_ideas TO mi_readonly;
GRANT SELECT ON mi_v_macro_quarantine_summary TO mi_readonly;
GRANT SELECT ON mi_v_pit_sector_artifacts TO mi_readonly;
GRANT SELECT ON mi_v_pit_sector_internals_current TO mi_readonly;
GRANT SELECT ON mi_v_pit_sector_internals_latest TO mi_readonly;
GRANT SELECT ON mi_v_ibkr_collector_status TO mi_readonly;
GRANT SELECT ON mi_v_ibkr_quotes_latest TO mi_readonly;

-- Defensive session defaults for the role (defaults, not privileges: a session can still
-- SET them back, which is why the GRANT surface above is what enforces read-only).
ALTER ROLE mi_readonly SET default_transaction_read_only = on;
ALTER ROLE mi_readonly SET statement_timeout = '15s';
ALTER ROLE mi_readonly SET idle_in_transaction_session_timeout = '30s';

-- Future views: views are owned by the migration user, so new mi_v_* views need an
-- explicit GRANT SELECT here (re-run this file; GRANTs are idempotent). Do not use
-- ALTER DEFAULT PRIVILEGES ... GRANT SELECT ON TABLES because that would also expose
-- new raw tables.
