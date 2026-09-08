-- Human-provisioned read-only role for Market Intelligence pages and the AI context API.
-- NOT applied by jobs/apply_migrations.py (this directory is outside db/migrations).
--
-- Run as a database superuser / owner after migrations 008+ are applied:
--   psql "$ADMIN_DATABASE_URL" -v ro_password='<generate-and-store-in-host-env>' \
--        -f db/roles/market_intelligence_readonly.sql
--
-- The role can only SELECT the curated mi_v_* views. No raw tables, no research
-- write tables, no CREATE, no superuser. The password is supplied by the operator
-- (psql -v), never committed.

CREATE ROLE mi_readonly
    LOGIN
    NOSUPERUSER
    NOCREATEDB
    NOCREATEROLE
    NOINHERIT
    NOREPLICATION
    CONNECTION LIMIT 10
    PASSWORD :'ro_password';

-- Connect + schema usage only.
GRANT CONNECT ON DATABASE :"DBNAME" TO mi_readonly;
GRANT USAGE ON SCHEMA public TO mi_readonly;

-- Explicit deny of anything inherited through PUBLIC for the curated scope only.
REVOKE ALL ON ALL TABLES IN SCHEMA public FROM mi_readonly;
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

-- Defensive session defaults for the role.
ALTER ROLE mi_readonly SET default_transaction_read_only = on;
ALTER ROLE mi_readonly SET statement_timeout = '15s';
ALTER ROLE mi_readonly SET idle_in_transaction_session_timeout = '30s';

-- Future views: views are owned by the migration user, so new mi_v_* views need an
-- explicit GRANT SELECT here (re-run this file; GRANTs are idempotent). Do not use
-- ALTER DEFAULT PRIVILEGES ... GRANT SELECT ON TABLES because that would also expose
-- new raw tables.
