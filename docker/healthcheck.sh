#!/usr/bin/env sh
set -eu

interval="${SYNC_INTERVAL_SECONDS:-0}"

# One-shot mode: the container is meant to run once and exit, so there's no
# long-running loop to monitor here — report healthy.
if [ "$interval" -le 0 ] 2>/dev/null; then
    exit 0
fi

heartbeat="${HEARTBEAT_FILE:-/tmp/last-sync-ok}"
[ -f "$heartbeat" ] || exit 1

now=$(date +%s)
# mtime, not the file's contents. stat is used rather than `date -r`, which
# means something else on non-GNU date and would make a successful run look
# unhealthy.
last=$(stat -c %Y "$heartbeat")
age=$((now - last))
max_age=$((interval * 2 + 60))

[ "$age" -le "$max_age" ]
