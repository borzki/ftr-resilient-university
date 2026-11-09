#!/bin/sh
# Hot-standby replica: clone the primary with pg_basebackup on first start,
# then run PostgreSQL in streaming-replication (read-only) mode.
set -e
if [ ! -s "$PGDATA/PG_VERSION" ]; then
  echo "replica: cloning primary..."
  until PGPASSWORD="$REPLICATION_PASSWORD" pg_basebackup -h pg-primary -U replicator \
        -D "$PGDATA" -Fp -Xs -R -c fast; do
    echo "replica: primary not ready, retrying in 2s"; rm -rf "$PGDATA"/*; sleep 2
  done
  chmod 700 "$PGDATA"
fi
exec postgres -c hot_standby=on -c max_connections=200
