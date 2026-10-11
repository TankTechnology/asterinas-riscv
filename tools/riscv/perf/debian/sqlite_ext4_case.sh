#!/bin/sh
# SPDX-License-Identifier: MPL-2.0
set -eu

db=/var/lib/asterinas-perf/sqlite-ext4.db
mkdir -p /var/lib/asterinas-perf
rm -f "$db" "$db-journal" "$db-wal" "$db-shm"
start=$(date +%s%N)
sqlite3 "$db" <<'SQL'
PRAGMA journal_mode=DELETE;
PRAGMA synchronous=FULL;
CREATE TABLE events(id INTEGER PRIMARY KEY, payload TEXT NOT NULL);
BEGIN;
WITH RECURSIVE rows(id) AS (SELECT 1 UNION ALL SELECT id + 1 FROM rows WHERE id < 10000)
INSERT INTO events SELECT id, printf('debian-event-%08d', id) FROM rows;
COMMIT;
CREATE INDEX events_payload_idx ON events(payload);
PRAGMA wal_checkpoint;
SELECT count(*) FROM events;
SQL
end=$(date +%s%N)
elapsed_us=$(( (end - start) / 1000 ))
[ "$elapsed_us" -gt 0 ] || elapsed_us=1
printf 'PERF_SAMPLE=%s\n' "$elapsed_us"
