#!/usr/bin/env bash
# One-command install/update for netbox-unifi-client-sync.
#
#   curl -fsSL https://raw.githubusercontent.com/LordAdama/netbox-unifi-client-sync/main/install.sh | bash
#
# Clones the repo (or updates it if already present), prompts for
# UniFi/NetBox settings on first run, builds the Docker image, runs a
# dry-run for you to sanity check, and then offers to start the
# long-running synced container.
#
# Safe to re-run: it reuses an existing .env untouched and just pulls the
# latest code, rebuilds, and restarts the container.
#
# Non-interactive / automation use (CI, IaC, config management): pass
# --non-interactive with the required values as environment variables —
# no prompts, and the container is started immediately after a successful
# dry-run instead of asking for confirmation:
#
#   UNIFI_HOST=https://192.168.1.1 UNIFI_API_KEY=... \
#   NETBOX_URL=https://netbox.example.com NETBOX_TOKEN=... NETBOX_SITE_SLUG=main \
#   bash install.sh --non-interactive
#
# Required: UNIFI_HOST, NETBOX_URL, NETBOX_TOKEN, either UNIFI_API_KEY or
# both UNIFI_USERNAME and UNIFI_PASSWORD, and either NETBOX_SITE_SLUG
# (single site) or SITE_MAP (multiple sites — see .env.example). Optional:
# UNIFI_IS_UDM (default true), UNIFI_SITE (default "default"),
# SYNC_INTERVAL_SECONDS (default 3600). Ignored entirely if .env already
# exists — non-interactive mode only applies to first-time setup. This
# installer's interactive prompts only set up a single site; edit SITE_MAP
# in .env afterwards if you need more than one.
set -euo pipefail

REPO_URL="https://github.com/LordAdama/netbox-unifi-client-sync.git"
DIR="netbox-unifi-client-sync"
IMAGE="netbox-unifi-client-sync:latest"
CONTAINER="unifi-netbox-sync"

NONINTERACTIVE=false
for arg in "$@"; do
    case "$arg" in
        --non-interactive) NONINTERACTIVE=true ;;
        *)
            echo "Unknown argument: $arg" >&2
            exit 1
            ;;
    esac
done

# Interactive prompts must read from the controlling terminal, not stdin,
# so this also works when piped in via `curl ... | bash` (stdin is the
# script itself). Not used at all in --non-interactive mode.
TTY=/dev/tty

require() {
    command -v "$1" >/dev/null 2>&1 || {
        echo "Error: '$1' is required but not installed." >&2
        exit 1
    }
}

require docker
require git

write_env() {
    : > .env
    printf '%s\n' "UNIFI_HOST=$1" >> .env
    printf '%s\n' "UNIFI_API_KEY=$2" >> .env
    printf '%s\n' "UNIFI_USERNAME=$3" >> .env
    printf '%s\n' "UNIFI_PASSWORD=$4" >> .env
    printf '%s\n' "UNIFI_IS_UDM=$5" >> .env
    printf '%s\n' "UNIFI_SITE=$6" >> .env
    printf '%s\n' "UNIFI_VERIFY_SSL=false" >> .env
    printf '%s\n' "NETBOX_URL=$7" >> .env
    printf '%s\n' "NETBOX_TOKEN=$8" >> .env
    printf '%s\n' "NETBOX_VERIFY_SSL=true" >> .env
    printf '%s\n' "NETBOX_SITE_SLUG=$9" >> .env
    printf '%s\n' "SITE_MAP=${11:-}" >> .env
    printf '%s\n' "SITE_MATCH=normalized" >> .env
    printf '%s\n' "SITE_POLICY=require" >> .env
    printf '%s\n' "NETBOX_IP_STATUS=active" >> .env
    printf '%s\n' "DRY_RUN=false" >> .env
    printf '%s\n' "LOG_LEVEL=INFO" >> .env
    printf '%s\n' "LOG_FORMAT=text" >> .env
    printf '%s\n' "SYNC_TAG=unifi-sync" >> .env
    printf '%s\n' "CLIENT_DEVICE_ROLE_SLUG=unifi-client" >> .env
    printf '%s\n' "CLIENT_MANUFACTURER_SLUG=generic" >> .env
    printf '%s\n' "CLIENT_DEVICE_TYPE_SLUG=generic-network-client" >> .env
    printf '%s\n' "PORT_NAME_TEMPLATES=" >> .env
    printf '%s\n' "USE_OUI_MANUFACTURER=true" >> .env
    printf '%s\n' "OUI_FILE=" >> .env
    printf '%s\n' "SYNC_UNIFI_DEVICES=false" >> .env
    printf '%s\n' "DEVICETYPE_LIBRARY_PATH=" >> .env
    printf '%s\n' "UNIFI_DEVICE_ROLE_SLUG=network-device" >> .env
    printf '%s\n' "CABLE_CONFLICT_POLICY=skip" >> .env
    printf '%s\n' "MARK_STALE_OFFLINE=true" >> .env
    printf '%s\n' "DEVICE_UPDATE_POLICY=sync" >> .env
    printf '%s\n' "METRICS_FILE=" >> .env
    printf '%s\n' "MAX_WORKERS=1" >> .env
    printf '%s\n' "SYNC_INTERVAL_SECONDS=${10}" >> .env
    printf '%s\n' "LOCK_FILE=" >> .env
    chmod 600 .env
}

if [ -d "$DIR/.git" ]; then
    echo "==> Updating existing checkout in ./$DIR"
    git -C "$DIR" pull --ff-only
else
    echo "==> Cloning $REPO_URL"
    git clone --depth 1 "$REPO_URL" "$DIR"
fi
cd "$DIR"

if [ -f .env ]; then
    echo "==> Reusing existing .env"
elif [ "$NONINTERACTIVE" = true ]; then
    echo "==> Non-interactive first-time setup — reading configuration from environment variables"
    : "${UNIFI_HOST:?UNIFI_HOST must be set}"
    : "${NETBOX_URL:?NETBOX_URL must be set}"
    : "${NETBOX_TOKEN:?NETBOX_TOKEN must be set}"
    if [ -z "${SITE_MAP:-}" ] && [ -z "${NETBOX_SITE_SLUG:-}" ]; then
        echo "Error: set NETBOX_SITE_SLUG (single site), or SITE_MAP (multiple sites)" >&2
        exit 1
    fi
    if [ -z "${UNIFI_API_KEY:-}" ] && { [ -z "${UNIFI_USERNAME:-}" ] || [ -z "${UNIFI_PASSWORD:-}" ]; }; then
        echo "Error: set UNIFI_API_KEY, or both UNIFI_USERNAME and UNIFI_PASSWORD" >&2
        exit 1
    fi

    write_env \
        "$UNIFI_HOST" "${UNIFI_API_KEY:-}" "${UNIFI_USERNAME:-}" "${UNIFI_PASSWORD:-}" \
        "${UNIFI_IS_UDM:-true}" "${UNIFI_SITE:-default}" \
        "$NETBOX_URL" "$NETBOX_TOKEN" "${NETBOX_SITE_SLUG:-}" \
        "${SYNC_INTERVAL_SECONDS:-3600}" "${SITE_MAP:-}"
    echo "==> Wrote .env (chmod 600)"
else
    echo "==> First-time setup — answer a few questions (nothing is sent anywhere but your own .env file)"

    read -rp "UniFi controller URL (e.g. https://192.168.1.1): " unifi_host <"$TTY"
    read -rsp "UniFi API key (Settings > Control Plane > Integrations), or leave blank to use a username/password: " unifi_api_key <"$TTY"; echo
    if [ -z "$unifi_api_key" ]; then
        read -rp "UniFi username: " unifi_user <"$TTY"
        read -rsp "UniFi password: " unifi_pass <"$TTY"; echo
    else
        unifi_user=""
        unifi_pass=""
    fi
    read -rp "Is this a UDM/UDM-Pro/CloudKey Gen2+ (UniFi OS)? [Y/n]: " is_udm <"$TTY"
    read -rp "UniFi site name [default]: " unifi_site <"$TTY"
    read -rp "NetBox URL (e.g. https://netbox.example.com): " netbox_url <"$TTY"
    read -rsp "NetBox API token: " netbox_token <"$TTY"; echo
    read -rp "NetBox site slug clients should belong to: " netbox_site_slug <"$TTY"
    read -rp "Sync interval in seconds for the long-running container [3600]: " interval <"$TTY"

    unifi_site=${unifi_site:-default}
    interval=${interval:-3600}
    case "$is_udm" in
        [Nn]*) is_udm_bool=false ;;
        *) is_udm_bool=true ;;
    esac

    write_env \
        "$unifi_host" "$unifi_api_key" "$unifi_user" "$unifi_pass" \
        "$is_udm_bool" "$unifi_site" \
        "$netbox_url" "$netbox_token" "$netbox_site_slug" \
        "$interval" ""
    echo "==> Wrote .env (chmod 600)"
    echo "==> (This sets up a single site. For more than one, edit SITE_MAP in .env — see .env.example.)"
fi

echo "==> Building Docker image"
docker build -t "$IMAGE" .

echo "==> Running a dry-run so you can check what it would do before anything is written to NetBox"
docker run --rm --env-file .env "$IMAGE" --dry-run

if [ "$NONINTERACTIVE" = true ]; then
    confirm=y
else
    read -rp "Dry-run output above look correct? Start the live sync container now? [y/N]: " confirm <"$TTY"
fi
case "$confirm" in
    [Yy]*)
        docker rm -f "$CONTAINER" >/dev/null 2>&1 || true
        docker run -d --name "$CONTAINER" --restart unless-stopped --env-file .env "$IMAGE"
        echo "==> Started. Follow logs with: docker logs -f $CONTAINER"
        ;;
    *)
        echo "==> Not starting the container. Re-run this script (or 'docker run --env-file .env $IMAGE') when ready."
        ;;
esac
