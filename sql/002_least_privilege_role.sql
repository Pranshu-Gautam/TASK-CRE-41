-- TASK-CRE-41 — defence in depth at the database level.
--
-- Creates a loader role that *cannot* write outside iris_staging even if the
-- application code were wrong, the statement guard were bypassed, or someone
-- connected with psql. The application-level guard is a convenience; THIS is
-- the control that actually constrains the connection.
--
-- Run once per environment as a superuser / the database owner:
--
--   psql "$ADMIN_DB_URI" -v ON_ERROR_STOP=1 \
--        -v loader_password="'<from-your-secret-store>'" \
--        -f sql/002_least_privilege_role.sql
--
-- Then point the pipeline at the restricted role:
--   DB_URI=postgresql+psycopg://iris_cre41_loader:<pw>@host:5432/<db>
--
-- Never commit a real password. The default below is a local-only throwaway
-- so the script stays runnable in a development database.

\set ON_ERROR_STOP on

\if :{?loader_password}
\else
  \set loader_password 'change-me-local-only'
\endif

-- psql does not interpolate variables inside dollar-quoted blocks, so the
-- role is created at the top level via \gexec instead of inside a DO block.
SELECT format(
    'CREATE ROLE iris_cre41_loader LOGIN PASSWORD %L', :'loader_password'
)
WHERE NOT EXISTS (
    SELECT 1 FROM pg_roles WHERE rolname = 'iris_cre41_loader'
)
\gexec

-- Keep an existing role's password in step with the one supplied here.
SELECT format(
    'ALTER ROLE iris_cre41_loader PASSWORD %L', :'loader_password'
)
\gexec

CREATE SCHEMA IF NOT EXISTS iris_staging;

-- ---------------------------------------------------------------------------
-- The ONLY schema the loader may write to.
-- ---------------------------------------------------------------------------
GRANT USAGE, CREATE ON SCHEMA iris_staging TO iris_cre41_loader;
-- Tables that already exist (created by a previous run under another role).
GRANT SELECT, INSERT, UPDATE, DELETE, TRUNCATE
    ON ALL TABLES IN SCHEMA iris_staging TO iris_cre41_loader;
GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA iris_staging TO iris_cre41_loader;
-- Tables created later by anyone.
ALTER DEFAULT PRIVILEGES IN SCHEMA iris_staging
    GRANT SELECT, INSERT, UPDATE, DELETE, TRUNCATE ON TABLES TO iris_cre41_loader;
ALTER DEFAULT PRIVILEGES IN SCHEMA iris_staging
    GRANT USAGE, SELECT ON SEQUENCES TO iris_cre41_loader;

-- ---------------------------------------------------------------------------
-- public: read-only, and no object creation. PostGIS functions and
-- spatial_ref_sys live here, so USAGE and SELECT are required; CREATE is not.
-- ---------------------------------------------------------------------------
GRANT USAGE ON SCHEMA public TO iris_cre41_loader;
REVOKE CREATE ON SCHEMA public FROM iris_cre41_loader;
REVOKE CREATE ON SCHEMA public FROM PUBLIC;

-- ---------------------------------------------------------------------------
-- Production-style schemas: explicitly nothing. A fresh role has no table
-- privileges by default; these statements make the intent auditable and
-- undo anything a previous grant may have left behind.
-- ---------------------------------------------------------------------------
DO $$
DECLARE
    target_schema text;
BEGIN
    FOREACH target_schema IN ARRAY ARRAY['iris_core', 'iris_prod', 'core', 'public']
    LOOP
        IF EXISTS (SELECT 1 FROM information_schema.schemata
                   WHERE schema_name = target_schema) THEN
            EXECUTE format(
                'REVOKE INSERT, UPDATE, DELETE, TRUNCATE, REFERENCES, TRIGGER '
                'ON ALL TABLES IN SCHEMA %I FROM iris_cre41_loader', target_schema);
            EXECUTE format(
                'ALTER DEFAULT PRIVILEGES IN SCHEMA %I '
                'REVOKE INSERT, UPDATE, DELETE, TRUNCATE ON TABLES '
                'FROM iris_cre41_loader', target_schema);
        END IF;
    END LOOP;
    -- iris_core and friends also lose schema-level access entirely.
    FOREACH target_schema IN ARRAY ARRAY['iris_core', 'iris_prod', 'core']
    LOOP
        IF EXISTS (SELECT 1 FROM information_schema.schemata
                   WHERE schema_name = target_schema) THEN
            EXECUTE format('REVOKE ALL ON SCHEMA %I FROM iris_cre41_loader',
                           target_schema);
        END IF;
    END LOOP;
END
$$;

-- Pin the role's search_path so even an interactive session is constrained.
ALTER ROLE iris_cre41_loader SET search_path TO iris_staging, public;

-- ---------------------------------------------------------------------------
-- Verification: what the role can actually do. Expect t / f / f / f.
-- ---------------------------------------------------------------------------
SELECT
    has_schema_privilege('iris_cre41_loader', 'iris_staging', 'CREATE') AS staging_create,
    has_schema_privilege('iris_cre41_loader', 'public', 'CREATE')       AS public_create,
    COALESCE(has_schema_privilege('iris_cre41_loader', 'iris_core', 'USAGE'),
             false)                                                     AS core_usage,
    COALESCE(has_table_privilege('iris_cre41_loader', 'iris_core.portfolio', 'INSERT'),
             false)                                                     AS core_insert;
