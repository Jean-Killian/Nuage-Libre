#!/usr/bin/env bash
# =============================================================================
# Database bootstrap: roles, databases and privileges.
#
# Executed automatically by the postgres image the first time the data
# volume is created (files in /docker-entrypoint-initdb.d/).
#
# Principle of least privilege - three roles, each with only what it needs:
#
#   postgres     superuser, container administration only. Never used by
#                the application.
#   nuage_owner  owns the databases and their schema. Used ONLY by Alembic to
#                run migrations (CREATE / ALTER / DROP TABLE).
#   nuage_app    used by the running FastAPI application. Can only read and
#                write rows (SELECT, INSERT, UPDATE, DELETE). It cannot drop or
#                alter tables: even if the application were compromised
#                (e.g. SQL injection), the attacker could not destroy the schema.
#
# Two databases are created: nuage_libre (real data) and nuage_libre_test
# (wiped by the automated tests).
# =============================================================================
set -euo pipefail

: "${NUAGE_OWNER_PASSWORD:?NUAGE_OWNER_PASSWORD must be set in .env}"
: "${NUAGE_APP_PASSWORD:?NUAGE_APP_PASSWORD must be set in .env}"

# ON_ERROR_STOP=1: abort at the first failing statement.
# psql variables (:'name') are quoted safely by psql itself.
psql -v ON_ERROR_STOP=1 --username "$POSTGRES_USER" --dbname postgres \
     -v owner_pw="$NUAGE_OWNER_PASSWORD" -v app_pw="$NUAGE_APP_PASSWORD" <<'SQL'
CREATE ROLE nuage_owner LOGIN PASSWORD :'owner_pw';
CREATE ROLE nuage_app   LOGIN PASSWORD :'app_pw';

CREATE DATABASE nuage_libre      OWNER nuage_owner;
CREATE DATABASE nuage_libre_test OWNER nuage_owner;

-- By default every role may connect to every database: restrict it.
REVOKE ALL ON DATABASE nuage_libre, nuage_libre_test FROM PUBLIC;
GRANT CONNECT ON DATABASE nuage_libre, nuage_libre_test TO nuage_app;
SQL

for db in nuage_libre nuage_libre_test; do
psql -v ON_ERROR_STOP=1 --username "$POSTGRES_USER" --dbname "$db" <<'SQL'
-- pgvector, for the future RAG feature (needs superuser to install).
CREATE EXTENSION IF NOT EXISTS vector;

-- The application may use objects in the schema, but not create new ones.
REVOKE CREATE ON SCHEMA public FROM PUBLIC;
GRANT USAGE ON SCHEMA public TO nuage_app;

-- Tables do not exist yet (Alembic will create them as nuage_owner).
-- DEFAULT PRIVILEGES = "for every table nuage_owner creates in the future,
-- automatically grant these rights to nuage_app".
ALTER DEFAULT PRIVILEGES FOR ROLE nuage_owner IN SCHEMA public
    GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO nuage_app;
ALTER DEFAULT PRIVILEGES FOR ROLE nuage_owner IN SCHEMA public
    GRANT USAGE, SELECT ON SEQUENCES TO nuage_app;
SQL
done

echo "Nuage Libre: roles and databases created."
