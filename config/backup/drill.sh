#!/bin/sh
# Restore drill: restore the newest dump into a scratch database, compare row counts with
# the live database, drop the scratch copy. Never touches the live database.
set -eu
export PGOPTIONS="-c client_min_messages=warning"  # no NOTICEs for the scratch database
dump="$(ls -1 /backups/gateway-*.dump 2>/dev/null | tail -1)"
[ -n "$dump" ] || { echo "drill: no dumps in /backups" >&2; exit 1; }
scratch="gateway_restore_drill"
psql -v ON_ERROR_STOP=1 -d postgres -qc "DROP DATABASE IF EXISTS $scratch WITH (FORCE)"
psql -v ON_ERROR_STOP=1 -d postgres -qc "CREATE DATABASE $scratch"
trap 'psql -d postgres -qc "DROP DATABASE IF EXISTS $scratch WITH (FORCE)"' EXIT
pg_restore --no-owner --exit-on-error -d "$scratch" "$dump"
count() { psql -At -d "$1" -c "SELECT count(*) FROM $2"; }
echo "drill: restored $dump"
echo "  api_keys: restored $(count "$scratch" api_keys) · live now $(count "$PGDATABASE" api_keys)"
for t in usage_log judge_scores; do
  echo "  $t: restored $(count "$scratch" "$t") · live now $(count "$PGDATABASE" "$t")"
done
# Keys are revoked, never deleted: the live table can only have grown since the dump.
# (usage_log isn't compared: retention may prune old rows after the dump was taken.)
if [ "$(count "$scratch" api_keys)" -le "$(count "$PGDATABASE" api_keys)" ]; then
  echo "drill: OK"
else
  echo "drill: FAILED (the dump has keys the live database doesn't)" >&2
  exit 1
fi
