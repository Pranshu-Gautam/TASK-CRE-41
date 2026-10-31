-- TASK-CRE-41 — defence in depth: a loader role that *cannot* write outside
-- iris_staging, even if the application code were wrong.
--
-- Run as a superuser / DBA once per environment. Replace the password with a
-- value from your secret store; never commit a real one.
--
--   psql "$ADMIN_DB_URI" -v loader_password="'<from-secret-store>'" \
--        -f sql/002_least_privilege_role.sql
--
-- Afterwards the pipeline connects as iris_cre41_loader:
--   DB_URI=postgresql+psycopg://iris_cre41_loader:<pw>@host:5432/<db>

\set ON_ERROR_STOP on
\if :{?loader_password}
\else
  \set loader_password '''change-me-local-only'''
\endif

DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'iris_cre41_loader') THEN
        EXECUTE format('CREATE ROLE iris_cre41_loader LOGIN PASSWORD %L', :loader_password);
    END IF;
END
$$;

CREATE SCHEMA IF NOT EXISTS iris_staging;

-- The loader owns its own schema and nothing else.
ALTER SCHEMA iris_staging OWNER TO iris_cre41_loader;
GRANT USAGE, CREATE ON SCHEMA iris_staging TO iris_cre41_loader;

-- Read-only on PostGIS internals that live in public; explicitly NO CREATE.
GRANT USAGE ON SCHEMA public TO iris_cre41_loader;
REVOKE CREATE ON SCHEMA public FROM iris_cre41_loader;
REVOKE CREATE ON SCHEMA public FROM PUBLIC;

-- Make the intent explicit for any production schema that exists here.
DO $$
DECLARE
    target_schema text;
BEGIN
    FOREACH target_schema IN ARRAY ARRAY['iris_core', 'iris_prod', 'core']
    LOOP
        IF EXISTS (SELECT 1 FROM information_schema.schemata
                   WHERE schema_name = target_schema) THEN
            EXECUTE format('REVOKE ALL ON SCHEMA %I FROM iris_cre41_loader',
                           target_schema);
            EXECUTE format(
                'REVOKE ALL ON ALL TABLES IN SCHEMA %I FROM iris_cre41_loader',
                target_schema);
        END IF;
    END LOOP;
END
$$;

-- Default search_path for the role, so even an interactive session is pinned.
ALTER ROLE iris_cre41_loader SET search_path TO iris_staging, public;
