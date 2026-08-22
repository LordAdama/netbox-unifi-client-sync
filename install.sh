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
set -euo pipefail

REPO_URL="https://github.com/LordAdama/netbox-unifi-client-sync.git"
DIR="netbox-unifi-client-sync"
IMAGE="netbox-unifi-client-sync:latest"
CONTAINER="unifi-netbox-sync"

# Prompts must read from the controlling terminal, not stdin, so this also
# works when piped in via `curl ... | bash` (stdin is the script itself).
TTY=/dev/tty

require() {
    command -v "$1" >/dev/null 2>&1 || {
        echo "Error: '$1' is required but not installed." >&2
        exit 1
    }
}

require docker
require git

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

    : > .env
    printf '%s\n' "UNIFI_HOST=$unifi_host" >> .env
    printf '%s\n' "UNIFI_API_KEY=$unifi_api_key" >> .env
    printf '%s\n' "UNIFI_USERNAME=$unifi_user" >> .env
    printf '%s\n' "UNIFI_PASSWORD=$unifi_pass" >> .env
    printf '%s\n' "UNIFI_IS_UDM=$is_udm_bool" >> .env
    printf '%s\n' "UNIFI_SITE=$unifi_site" >> .env
    printf '%s\n' "UNIFI_VERIFY_SSL=false" >> .env
    printf '%s\n' "NETBOX_URL=$netbox_url" >> .env
    printf '%s\n' "NETBOX_TOKEN=$netbox_token" >> .env
    printf '%s\n' "NETBOX_VERIFY_SSL=true" >> .env
    printf '%s\n' "NETBOX_SITE_SLUG=$netbox_site_slug" >> .env
    printf '%s\n' "NETBOX_IP_STATUS=active" >> .env
    printf '%s\n' "DRY_RUN=false" >> .env
    printf '%s\n' "LOG_LEVEL=INFO" >> .env
    printf '%s\n' "LOG_FORMAT=text" >> .env
    printf '%s\n' "SYNC_TAG=unifi-sync" >> .env
    printf '%s\n' "CLIENT_DEVICE_ROLE_SLUG=unifi-client" >> .env
    printf '%s\n' "CLIENT_MANUFACTURER_SLUG=generic" >> .env
    printf '%s\n' "CLIENT_DEVICE_TYPE_SLUG=generic-network-client" >> .env
    printf '%s\n' "PORT_NAME_TEMPLATES={port},Port {port},GE{port},Gi{port}" >> .env
    printf '%s\n' "CABLE_CONFLICT_POLICY=skip" >> .env
    printf '%s\n' "MARK_STALE_OFFLINE=true" >> .env
    printf '%s\n' "METRICS_FILE=" >> .env
    printf '%s\n' "SYNC_INTERVAL_SECONDS=$interval" >> .env
    printf '%s\n' "LOCK_FILE=" >> .env
    chmod 600 .env
    echo "==> Wrote .env (chmod 600)"
fi

echo "==> Building Docker image"
docker build -t "$IMAGE" .

echo "==> Running a dry-run so you can check what it would do before anything is written to NetBox"
docker run --rm --env-file .env "$IMAGE" --dry-run

read -rp "Dry-run output above look correct? Start the live sync container now? [y/N]: " confirm <"$TTY"
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
