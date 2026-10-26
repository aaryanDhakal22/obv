-- grafana-ro.sql — the read-only role Grafana uses on main's Postgres
-- (TENANT-SCOPING.md §2). Idempotent: safe to re-run (it also rotates the
-- password).
--
-- Run once per main database, as the database owner or a superuser, with the
-- password in a psql variable (never commit it; keep it in Doppler `obv`):
--
--   psql -v ON_ERROR_STOP=1 -v pw='…' -f grafana-ro.sql <db>
--
--   e.g. on a docker host:
--   docker exec -i <postgres-container> psql -U <owner> -v ON_ERROR_STOP=1 \
--       -v pw="$GRAFANA_PG_PASSWORD" -d <db> < grafana-ro.sql
--
-- What grafana_ro can read: the views in schema `reporting` (created by main
-- migration 00016_reporting.sql) and nothing in `public` — no customer names,
-- phones, addresses or notes. The views run with their owner's rights, so no
-- grant on the underlying tables is needed. Order of operations doesn't
-- matter: run before the migration and the migration grants when it creates
-- the schema; run after and this script grants (and sets default privileges
-- so views a later migration adds are readable too).
--
-- Note: `pw` is interpolated by psql (:'pw'), so this must run through psql,
-- not a driver.

\set ON_ERROR_STOP on

-- 1. The role (created once; password set / rotated every run).
SELECT 'CREATE ROLE grafana_ro LOGIN'
 WHERE NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'grafana_ro')
\gexec
ALTER ROLE grafana_ro WITH LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION PASSWORD :'pw';
ALTER ROLE grafana_ro SET statement_timeout = '15s';
ALTER ROLE grafana_ro SET default_transaction_read_only = on;

SELECT format('GRANT CONNECT ON DATABASE %I TO grafana_ro', current_database())
\gexec

-- 2. Nothing in public: drop any grant it may have picked up.
REVOKE ALL ON ALL TABLES IN SCHEMA public FROM grafana_ro;
REVOKE ALL ON ALL SEQUENCES IN SCHEMA public FROM grafana_ro;
REVOKE CREATE ON SCHEMA public FROM grafana_ro;

-- 3. The reporting views (only if the migration already made the schema).
DO $$
DECLARE
    owner_role text;
BEGIN
    SELECT nspowner::regrole::text INTO owner_role FROM pg_namespace WHERE nspname = 'reporting';
    IF owner_role IS NULL THEN
        RAISE NOTICE 'schema reporting does not exist yet: main migration 00016 grants grafana_ro when it runs';
        RETURN;
    END IF;
    GRANT USAGE ON SCHEMA reporting TO grafana_ro;
    GRANT SELECT ON ALL TABLES IN SCHEMA reporting TO grafana_ro;
    -- Views a later migration adds (created by the schema owner = main's DB user).
    EXECUTE format('ALTER DEFAULT PRIVILEGES FOR ROLE %I IN SCHEMA reporting GRANT SELECT ON TABLES TO grafana_ro', owner_role);
END $$;
