-- Human-provisioned read-only role for Streamlit / Strategy Monitor.
-- NOT applied by jobs/apply_migrations.py. Repeatable. Does not print passwords.

\set ON_ERROR_STOP on

SELECT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'dashboard_readonly') AS role_exists \gset

\if :role_exists
    \echo 'dashboard_readonly exists: refreshing grants only (password unchanged)'
\else
    \if :{?ro_password}
    \else
        \prompt 'dashboard_readonly password: ' ro_password
    \endif
    CREATE ROLE dashboard_readonly
        LOGIN
        NOSUPERUSER
        NOCREATEDB
        NOCREATEROLE
        NOINHERIT
        NOREPLICATION
        CONNECTION LIMIT 20
        PASSWORD :'ro_password';
\endif

GRANT CONNECT ON DATABASE :"DBNAME" TO dashboard_readonly;
GRANT USAGE ON SCHEMA public TO dashboard_readonly;
REVOKE CREATE ON SCHEMA public FROM dashboard_readonly;

DO $$
DECLARE
    obj text;
BEGIN
    FOREACH obj IN ARRAY ARRAY[
        'strategies',
        'backtests',
        'backtest_equity_points',
        'research_runs',
        'research_artifacts',
        'research_experiments',
        'research_trials',
        'research_oos_windows',
        'ml_trials',
        'ml_models',
        'ml_feature_diagnostics',
        'ml_signal_points',
        'live_snapshots',
        'positions',
        'orders',
        'trades'
    ]
    LOOP
        IF EXISTS (
            SELECT 1 FROM information_schema.tables
            WHERE table_schema = 'public' AND table_name = obj
        ) THEN
            EXECUTE format('GRANT SELECT ON TABLE %I TO dashboard_readonly', obj);
        END IF;
    END LOOP;
    FOREACH obj IN ARRAY ARRAY[
        'mi_v_source_health',
        'mi_v_ops_status',
        'mi_v_strategy_research_summary'
    ]
    LOOP
        IF EXISTS (
            SELECT 1 FROM information_schema.views
            WHERE table_schema = 'public' AND table_name = obj
        ) THEN
            EXECUTE format('GRANT SELECT ON TABLE %I TO dashboard_readonly', obj);
        END IF;
    END LOOP;
END $$;

ALTER ROLE dashboard_readonly SET default_transaction_read_only = on;
ALTER ROLE dashboard_readonly SET statement_timeout = '30s';
ALTER ROLE dashboard_readonly SET idle_in_transaction_session_timeout = '60s';
