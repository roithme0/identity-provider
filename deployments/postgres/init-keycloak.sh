#!/usr/bin/env bash
set -euo pipefail

psql --username "$POSTGRES_USER" --dbname "$POSTGRES_DB" --set ON_ERROR_STOP=1 \
  --set db_name="$POSTGRES_DB" --set db_user="$KC_DB_USER" \
  --set db_password="$KC_DB_PASSWORD" <<'SQL'
CREATE ROLE :"db_user" LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE PASSWORD :'db_password';
ALTER DATABASE :"db_name" OWNER TO :"db_user";
SQL
