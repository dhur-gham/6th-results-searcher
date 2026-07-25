#!/bin/sh
# Nightly Postgres backup loop. Dumps to /backups, keeps the last 14, sleeps 24h.
# The DB is small (well under 1 GB), so each dump is quick and tiny.
set -eu

RETENTION=14

while true; do
	TS=$(date +%Y%m%d_%H%M%S)
	OUT="/backups/results_${TS}.sql.gz"
	echo "[backup] dumping to ${OUT}"
	if pg_dump | gzip > "${OUT}"; then
		echo "[backup] done"
	else
		echo "[backup] FAILED" >&2
		rm -f "${OUT}"
	fi
	# prune old dumps, keep newest $RETENTION
	ls -1t /backups/results_*.sql.gz 2>/dev/null | tail -n +$((RETENTION + 1)) | xargs -r rm -f
	sleep 86400
done
