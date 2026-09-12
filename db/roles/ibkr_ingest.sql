-- Dedicated least-privilege role for the private IBKR ingest API.
-- NOT applied by jobs/apply_migrations.py. Repeatable grant refresh; password set only on create.
--
--   psql "$ADMIN_DATABASE_URL" -v ingest_password="$(cat /etc/fmp/mi_ibkr_ingest.pw)" -f db/roles/ibkr_ingest.sql
--
-- This role cannot: run arbitrary SQL as admin, write research tables, read mi_readonly-only
-- secrets, place orders, or mutate FRED/FINRA/macro observations.

\set ON_ERROR_STOP on

SELECT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'mi_ibkr_ingest') AS role_exists \gset

\if :role_exists
    \echo 'mi_ibkr_ingest exists: refreshing grants only (password unchanged)'
\else
    \if :{?ingest_password}
    \else
        \prompt 'mi_ibkr_ingest password: ' ingest_password
    \endif
    CREATE ROLE mi_ibkr_ingest
        LOGIN
        NOSUPERUSER
        NOCREATEDB
        NOCREATEROLE
        NOINHERIT
        NOREPLICATION
        CONNECTION LIMIT 8
        PASSWORD :'ingest_password';
\endif

\if :{?DBNAME}
\else
SELECT current_database() AS "DBNAME" \gset
\endif
GRANT CONNECT ON DATABASE :"DBNAME" TO mi_ibkr_ingest;
GRANT USAGE ON SCHEMA public TO mi_ibkr_ingest;
REVOKE CREATE ON SCHEMA public FROM mi_ibkr_ingest;

REVOKE ALL ON ALL TABLES IN SCHEMA public FROM mi_ibkr_ingest;
REVOKE ALL ON ALL SEQUENCES IN SCHEMA public FROM mi_ibkr_ingest;

GRANT SELECT, INSERT, UPDATE ON mi_source_registry TO mi_ibkr_ingest;
GRANT SELECT, INSERT, UPDATE ON mi_ingestion_runs TO mi_ibkr_ingest;
GRANT SELECT, INSERT, UPDATE ON mi_data_freshness TO mi_ibkr_ingest;
GRANT SELECT, INSERT, UPDATE ON mi_market_instruments TO mi_ibkr_ingest;
GRANT SELECT, INSERT, UPDATE ON mi_instrument_identifiers TO mi_ibkr_ingest;
GRANT SELECT, INSERT ON mi_market_quotes TO mi_ibkr_ingest;
GRANT SELECT, INSERT, UPDATE ON mi_collector_status TO mi_ibkr_ingest;
GRANT SELECT, INSERT, UPDATE ON mi_market_bars TO mi_ibkr_ingest;
GRANT SELECT, INSERT, UPDATE ON mi_sector_snapshots TO mi_ibkr_ingest;
GRANT SELECT, INSERT, UPDATE ON mi_industry_snapshots TO mi_ibkr_ingest;
GRANT SELECT, INSERT, UPDATE ON mi_equity_eod_batches TO mi_ibkr_ingest;
GRANT SELECT, INSERT, UPDATE ON mi_equity_eod_batch_chunks TO mi_ibkr_ingest;
GRANT USAGE, SELECT ON SEQUENCE mi_market_quotes_id_seq TO mi_ibkr_ingest;
GRANT USAGE, SELECT ON SEQUENCE mi_instrument_identifiers_id_seq TO mi_ibkr_ingest;
GRANT USAGE, SELECT ON SEQUENCE mi_market_bars_id_seq TO mi_ibkr_ingest;
GRANT USAGE, SELECT ON SEQUENCE mi_sector_snapshots_id_seq TO mi_ibkr_ingest;
GRANT USAGE, SELECT ON SEQUENCE mi_industry_snapshots_id_seq TO mi_ibkr_ingest;
GRANT SELECT ON mi_v_ibkr_collector_status TO mi_ibkr_ingest;
GRANT SELECT ON mi_v_ibkr_quotes_latest TO mi_ibkr_ingest;
