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
# A library cloned at build time, or mounted at this path, is used when
# DEVICETYPE_LIBRARY_PATH is unset or empty. A non-empty path is left alone.
if [ -z "${DEVICETYPE_LIBRARY_PATH:-}" ] && [ -d /opt/devicetype-library/device-types ]; then
    export DEVICETYPE_LIBRARY_PATH=/opt/devicetype-library
fi

interval="${SYNC_INTERVAL_SECONDS:-0}"
# Exported so the Python process can write the marker itself when a run
# finishes with no errors. The shell writes it too, on exit 0, which is the
# same condition: per-site and per-client errors exit non-zero and must not
# look like a healthy sync. A hung HTTP call never reaches either write.
export HEARTBEAT_FILE="${HEARTBEAT_FILE:-/tmp/last-sync-ok}"
heartbeat="$HEARTBEAT_FILE"

record_success() {
    if date -u > "$heartbeat"; then
        echo "unifi-netbox-sync: run succeeded; wrote $heartbeat"
    else
        echo "unifi-netbox-sync: run succeeded but could not write $heartbeat" >&2
        return 1
    fi
}

record_failure() {
    # $1 is the sync process's exit status. Capture it before any other
    # command: under `set -e` a bare `$?` later in the loop is easy to lose.
    echo "unifi-netbox-sync: run finished with errors (exit $1); not updating $heartbeat" >&2
}

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
                record_success
            else
                record_failure "$?"
            fi
        else
            if unifi-netbox-sync "$@"; then
                record_success
            else
                record_failure "$?"
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
