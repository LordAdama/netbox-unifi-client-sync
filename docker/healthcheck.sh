#!/usr/bin/env sh
set -eu

interval="${SYNC_INTERVAL_SECONDS:-0}"

# One-shot mode: the container is meant to run once and exit, so there's no
# long-running loop to monitor here — report healthy.
if [ "$interval" -le 0 ] 2>/dev/null; then
    exit 0
fi

heartbeat=/tmp/last-sync-ok
[ -f "$heartbeat" ] || exit 1

now=$(date +%s)
last=$(date -r "$heartbeat" +%s)
age=$((now - last))
max_age=$((interval * 2 + 60))

[ "$age" -le "$max_age" ]
