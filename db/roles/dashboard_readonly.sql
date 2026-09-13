-- Human-provisioned read-only role for Streamlit / Strategy Monitor.
-- NOT applied by jobs/apply_migrations.py. Repeatable. Does not print passwords.
-- Existing roles are repaired to a known least-privilege state, not only re-granted.

\set ON_ERROR_STOP on

SELECT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'dashboard_readonly') AS role_exists \gset

\if :role_exists
    \echo 'dashboard_readonly exists: repairing grants and memberships to least privilege'
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

ALTER ROLE dashboard_readonly NOSUPERUSER NOCREATEDB NOCREATEROLE NOINHERIT NOREPLICATION;
ALTER ROLE dashboard_readonly CONNECTION LIMIT 20;

GRANT CONNECT ON DATABASE :"DBNAME" TO dashboard_readonly;
REVOKE ALL ON SCHEMA public FROM dashboard_readonly;
GRANT USAGE ON SCHEMA public TO dashboard_readonly;
REVOKE CREATE ON SCHEMA public FROM dashboard_readonly;
REVOKE TEMP ON DATABASE :"DBNAME" FROM dashboard_readonly;
REVOKE CREATE ON DATABASE :"DBNAME" FROM dashboard_readonly;

DO $$
DECLARE
    obj text;
    seq text;
    parent_role text;
    owned text;
BEGIN
    -- Strip inherited and leftover DML before re-granting SELECT.
    REVOKE ALL ON ALL TABLES IN SCHEMA public FROM dashboard_readonly;
    REVOKE ALL ON ALL SEQUENCES IN SCHEMA public FROM dashboard_readonly;
    REVOKE ALL ON ALL FUNCTIONS IN SCHEMA public FROM dashboard_readonly;

    FOR parent_role IN
        SELECT r.rolname
        FROM pg_auth_members m
        JOIN pg_roles u ON u.oid = m.member
        JOIN pg_roles r ON r.oid = m.roleid
        WHERE u.rolname = 'dashboard_readonly'
    LOOP
        EXECUTE format('REVOKE %I FROM dashboard_readonly', parent_role);
    END LOOP;

    FOR owned IN
        SELECT n.nspname || '.' || c.relname
        FROM pg_class c
        JOIN pg_namespace n ON n.oid = c.relnamespace
        JOIN pg_roles r ON r.oid = c.relowner
        WHERE r.rolname = 'dashboard_readonly'
    LOOP
        RAISE EXCEPTION 'dashboard_readonly owns %; move ownership before continuing', owned;
    END LOOP;

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
        'holdout_exposures',
        'strategy_specs',
        'research_pair_diagnostics',
        'research_fixed_income_metrics',
        'research_risk_metrics',
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

    IF has_schema_privilege('dashboard_readonly', 'public', 'CREATE') THEN
        RAISE EXCEPTION
            'dashboard_readonly still has CREATE on schema public (often inherited from PUBLIC). '
            'This script does not change global PUBLIC policy. Human admin must '
            'REVOKE CREATE ON SCHEMA public FROM PUBLIC, then re-run provisioning.';
    END IF;

    IF EXISTS (
        SELECT 1
        FROM pg_auth_members m
        JOIN pg_roles u ON u.oid = m.member
        JOIN pg_roles r ON r.oid = m.roleid
        WHERE u.rolname = 'dashboard_readonly'
    ) THEN
        RAISE EXCEPTION
            'dashboard_readonly still has role memberships after repair; '
            'SET ROLE into a writer identity remains possible';
    END IF;
END $$;

ALTER ROLE dashboard_readonly SET default_transaction_read_only = on;
ALTER ROLE dashboard_readonly SET statement_timeout = '30s';
ALTER ROLE dashboard_readonly SET idle_in_transaction_session_timeout = '60s';
