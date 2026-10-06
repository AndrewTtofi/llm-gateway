#!/bin/sh
# Daily pg_dump of the gateway database (custom format: compressed, restorable per table).
# Keeps BACKUP_KEEP_DAYS days; `last-success` holds the time of the last good dump.
set -eu
umask 077
keep="${BACKUP_KEEP_DAYS:-14}"
while true; do
  file="/backups/gateway-$(date -u +%Y%m%dT%H%M%SZ).dump"
  if pg_dump -Fc -f "$file.part" && pg_restore --list "$file.part" > /dev/null; then
    mv "$file.part" "$file"
    date -u +%Y-%m-%dT%H:%M:%SZ > /backups/last-success
    echo "backup: wrote $file ($(du -h "$file" | cut -f1))"
  else
    rm -f "$file.part"
    echo "backup: FAILED" >&2
  fi
  find /backups -name 'gateway-*.dump' -mtime +"$keep" -delete
  sleep "${BACKUP_INTERVAL_SECONDS:-86400}"
done
