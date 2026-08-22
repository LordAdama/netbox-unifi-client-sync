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
#
# HEARTBEAT_FILE (optional) -> where the loop records its last successful
# run for healthcheck.sh to read. Defaults under /tmp; override if /tmp is a
# size-constrained tmpfs shared with other processes in your setup.
interval="${SYNC_INTERVAL_SECONDS:-0}"
heartbeat="${HEARTBEAT_FILE:-/tmp/last-sync-ok}"

if [ -n "${LOCK_FILE:-}" ] && ! command -v flock >/dev/null 2>&1; then
    echo "unifi-netbox-sync: LOCK_FILE is set but 'flock' is not available in this image" >&2
    exit 1
fi

stop_requested=0
on_stop_signal() {
    echo "unifi-netbox-sync: stop signal received, finishing the current cycle then exiting"
    stop_requested=1
}
trap on_stop_signal TERM INT

# Interruptible in ~1s increments so a stop signal received mid-wait doesn't
# have to wait out the full remaining interval (docker stop's grace period
# is usually much shorter than SYNC_INTERVAL_SECONDS).
interruptible_sleep() {
    remaining="$1"
    while [ "$remaining" -gt 0 ] && [ "$stop_requested" -eq 0 ]; do
        sleep 1
        remaining=$((remaining - 1))
    done
}

if [ "$interval" -gt 0 ] 2>/dev/null; then
    echo "unifi-netbox-sync: looping every ${interval}s (SYNC_INTERVAL_SECONDS)"
    while [ "$stop_requested" -eq 0 ]; do
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
        [ "$stop_requested" -eq 0 ] && interruptible_sleep "$interval"
    done
    echo "unifi-netbox-sync: stopped"
else
    if [ -n "${LOCK_FILE:-}" ]; then
        exec flock -n "$LOCK_FILE" unifi-netbox-sync "$@"
    else
        exec unifi-netbox-sync "$@"
    fi
fi
