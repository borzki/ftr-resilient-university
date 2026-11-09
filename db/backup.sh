#!/bin/sh
# Backup infrastructure: periodic logical backups of the primary, keeping the last 6.
set -e
mkdir -p /backups
while true; do
  ts=$(date +%Y%m%d_%H%M%S)
  if PGPASSWORD="$POSTGRES_PASSWORD" pg_dump -h pg-primary -U "$POSTGRES_USER" -Fc "$POSTGRES_DB" \
       > "/backups/university_$ts.dump.tmp"; then
    mv "/backups/university_$ts.dump.tmp" "/backups/university_$ts.dump"
    echo "{\"event\":\"backup_completed\",\"file\":\"university_$ts.dump\"}"
  else
    rm -f "/backups/university_$ts.dump.tmp"
    echo "{\"event\":\"backup_failed\"}"
  fi
  ls -1t /backups/*.dump 2>/dev/null | tail -n +7 | xargs -r rm -f
  sleep "${BACKUP_INTERVAL_S:-300}"
done
