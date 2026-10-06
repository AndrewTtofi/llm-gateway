#!/bin/sh
# Restore the gateway database from a dump: sh /restore.sh /backups/<file>.dump
# Stop the gateway first (no writes while restoring). Replaces the database's contents.
set -eu
dump="${1:?usage: restore.sh /backups/<file>.dump}"
pg_restore --list "$dump" > /dev/null || { echo "not a readable dump: $dump" >&2; exit 1; }
echo "restoring $dump into $PGDATABASE …"
psql -v ON_ERROR_STOP=1 -d postgres -c "DROP DATABASE IF EXISTS \"$PGDATABASE\" WITH (FORCE)"
psql -v ON_ERROR_STOP=1 -d postgres -c "CREATE DATABASE \"$PGDATABASE\""
pg_restore --no-owner --exit-on-error -d "$PGDATABASE" "$dump"
psql -At -d "$PGDATABASE" -c "SELECT 'api_keys ' || count(*) FROM api_keys UNION ALL SELECT 'usage_log ' || count(*) FROM usage_log"
echo "restored. Start the stack again: migrations run first, then the gateway."
