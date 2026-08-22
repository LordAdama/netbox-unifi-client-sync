#!/usr/bin/env sh
set -eu

# SYNC_INTERVAL_SECONDS unset/0  -> run once and exit (good for `docker run --rm` + host cron)
# SYNC_INTERVAL_SECONDS=<N>      -> loop forever, syncing every N seconds (good for a long-running container)
#
# LOCK_FILE (optional) -> path to an flock(1) lock file, only meaningful if
# it's on storage shared with other instances (e.g. a bind-mounted volume
# shared by another container or host syncing the same NetBox). This tool
# has no distributed lock of its own; a shared LOCK_FILE is the opt-in way
# to stop two instances from racing on the same NetBox objects. Instances
# that don't share the path aren't protected from each other by this.
interval="${SYNC_INTERVAL_SECONDS:-0}"
heartbeat=/tmp/last-sync-ok

if [ "$interval" -gt 0 ] 2>/dev/null; then
    echo "unifi-netbox-sync: looping every ${interval}s (SYNC_INTERVAL_SECONDS)"
    while true; do
        if [ -n "${LOCK_FILE:-}" ]; then
            if flock -n "$LOCK_FILE" unifi-netbox-sync "$@"; then
                date > "$heartbeat"
            else
                echo "unifi-netbox-sync: run failed or lock busy (exit $?)" >&2
            fi
        else
            if unifi-netbox-sync "$@"; then
                date > "$heartbeat"
            else
                echo "unifi-netbox-sync: run failed (exit $?)" >&2
            fi
        fi
        sleep "$interval"
    done
else
    if [ -n "${LOCK_FILE:-}" ]; then
        exec flock -n "$LOCK_FILE" unifi-netbox-sync "$@"
    else
        exec unifi-netbox-sync "$@"
    fi
fi
