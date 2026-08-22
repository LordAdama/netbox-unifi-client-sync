#!/usr/bin/env sh
set -eu

# SYNC_INTERVAL_SECONDS unset/0  -> run once and exit (good for `docker run --rm` + host cron)
# SYNC_INTERVAL_SECONDS=<N>      -> loop forever, syncing every N seconds (good for a long-running container)
interval="${SYNC_INTERVAL_SECONDS:-0}"

if [ "$interval" -gt 0 ] 2>/dev/null; then
    echo "unifi-netbox-sync: looping every ${interval}s (SYNC_INTERVAL_SECONDS)"
    while true; do
        unifi-netbox-sync "$@" || echo "unifi-netbox-sync: run failed (exit $?)" >&2
        sleep "$interval"
    done
else
    exec unifi-netbox-sync "$@"
fi
