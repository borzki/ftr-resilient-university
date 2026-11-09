#!/bin/bash
# Runs once on first start of the primary database (official postgres entrypoint).
# Creates the schema, synthetic seed data, least-privilege service roles and the
# replication role. Passwords come from environment variables (see .env).
set -euo pipefail

psql -v ON_ERROR_STOP=1 --username "$POSTGRES_USER" --dbname "$POSTGRES_DB" \
  -v seed_password="$SEED_PASSWORD" \
  -f /docker-entrypoint-initdb.d/sql/schema.sql \
  -f /docker-entrypoint-initdb.d/sql/seed.sql

psql -v ON_ERROR_STOP=1 --username "$POSTGRES_USER" --dbname "$POSTGRES_DB" \
  -v student_pw="$STUDENT_DB_PASSWORD" \
  -v payment_pw="$PAYMENT_DB_PASSWORD" \
  -v records_pw="$RECORDS_DB_PASSWORD" \
  -v timetable_pw="$TIMETABLE_DB_PASSWORD" \
  -v repl_pw="$REPLICATION_PASSWORD" \
  -v dbname="$POSTGRES_DB" \
  -f /docker-entrypoint-initdb.d/sql/roles.sql

# allow streaming replication from the private container network
echo "host replication replicator all scram-sha-256" >> "$PGDATA/pg_hba.conf"
