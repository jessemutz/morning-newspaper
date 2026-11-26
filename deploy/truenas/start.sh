#!/usr/bin/env bash
# One container, two jobs: the HTTP server for the Kindle plugin and the daily cron run.
# If either exits, exit too, so the container restarts instead of half-working.
set -u
supercronic /app/crontab &
python /app/scripts/paper_server.py &
wait -n
exit 1
