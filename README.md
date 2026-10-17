# netbox-unifi-client-sync

[![Tests](https://github.com/LordAdama/netbox-unifi-client-sync/actions/workflows/tests.yml/badge.svg)](https://github.com/LordAdama/netbox-unifi-client-sync/actions/workflows/tests.yml)

Syncs client devices and their switch-port cable connections from a
[Ubiquiti UniFi Network Controller](https://ui.com) into
[NetBox](https://netboxlabs.com/), so NetBox stays an accurate source of
truth for what's actually plugged into the network.

## What it does

For every active client the UniFi controller reports:

- Creates or updates a NetBox device (role `unifi-client` by default) with
  an interface (`eth0` for wired, `wlan0` for wireless). The device name is
  sanitized (control characters stripped, truncated to NetBox's 64-char
  limit) and, if it collides with a *different* device already using that
  name in the same NetBox site, disambiguated by appending the last 4 hex
  digits of the client's MAC (e.g. `laptop` → `laptop-5566`) — deterministic
  across runs, so the same client always resolves to the same name.
- Assigns the client's current IP address and sets it as the device's
  primary IP.
- For **wired** clients, looks up the switch (by MAC address, matched
  against an existing NetBox device's interface) and the specific switch
  port the client is plugged into, then creates a `Cable` in NetBox
  connecting the client's interface to that switch port — mirroring the
  physical connection UniFi already knows about.
- For **wireless** clients, records the connection without creating a
  cable (there isn't one).
- Optionally marks client devices **this tool created** (tagged `unifi-sync`
  and using the client role) as `offline` when their UniFi `last_seen` is
  older than `STALE_OFFLINE_GRACE` (default 7 days). Dropping out of the
  active client list is not enough — that is how an idle phone looks.
  It never deletes them, and it does not offline infrastructure devices or
  anything that does not carry that tag. The status write does not rewrite
  the rest of the device.

Your UniFi hardware — switches, APs, gateways — can be created too, with
`SYNC_UNIFI_DEVICES=true` (off by default; see "Syncing UniFi devices"). With
it on, an empty NetBox is populated end to end in a single run: the switches
with their real ports, the clients, and the cables between them. With it off,
switches must already be inventoried in NetBox and only clients are created.

Everything this tool creates is tagged `unifi-sync`. Updates, cable
replacement, and stale-offline marking are limited to objects with that tag,
so a device or cable entered by hand in NetBox is left alone even when it
shares a MAC or a switch port with something UniFi reports.

The placeholder interface (`eth0` for wired clients, `wlan0` for wireless)
and the IP moved onto it are written only for a device this sync still owns.
Owned means it carries `SYNC_TAG`, its role is listed in
`MANAGED_CLIENT_ROLE_SLUGS` (by default, just `CLIENT_DEVICE_ROLE_SLUG`), and
it does not carry `SYNC_UNMANAGED_TAG` (default `sync-unmanaged`). A device
someone has re-roled, or tagged `sync-unmanaged` after building it by hand,
is matched by MAC so a second device is not created. That pass logs one line
and does not add the placeholder interface, move the IP, or cable that
interface.

## How matching works

| UniFi side | NetBox side |
| --- | --- |
| Switch MAC address | Four strategies, first match wins — see "Finding your switches" |
| Switch port index (`sw_port`) | Looked up on the matched switch by `PORT_MODEL_TEMPLATES` (if this UniFi model has any), then `PORT_NAME_TEMPLATES` (`"Port 4"`, `"4"`, `"GE4"`, `"Gi4"`, …). Comparison ignores case and leading zeros. If nothing hits and the model has no templates of its own, a unique structural match is used: `Gi1/0/4`, `1/1/4`, `ge-0/0/4`, `Ethernet1/4`, and an SFP ordinal such as `SFP 1` when the controller says that index is an SFP |
| Client MAC address | Stored in a `unifi_mac` custom field on the NetBox client device, created automatically on first run |

If a switch or matching port can't be found, the client device is still
synced — only the cable is skipped. Every skip is one log line (site,
switch, UniFi port, names tried, the NetBox interface names, and a reason)
and the run ends with a count by reason. Set `CABLE_SKIP_REPORT` to also
write that table as a CSV; dry-run writes the file too, because it is not
a NetBox change. A port that more than one wired client shares, and a port
the controller marks as an uplink, is never cabled: that is an uplink or
an unmanaged switch, not a drop to one device.

Every switch/port match (including whether a template or a structural name
matched) and every ambiguous match (a MAC that appears on more than one
NetBox interface, or two interfaces that parse to the same port) is logged
at INFO/WARNING for troubleshooting.

Infra-to-infra uplinks (a UniFi device's own uplink port cabled to the
upstream port) use that same match on both ends, then two fallbacks. An
interface whose trailing number equals the port index matches when nothing
earlier did, for any prefix (`SFP+ 49`, `SFP 49`, `SFP28 49`, `Port 49`,
`GE49`, `Gi49`, `eth12`). Wireless and virtual interfaces are ignored
(`wlan*`, `vlan*`, LAG and bridge names, subinterfaces, and NetBox types
`virtual`, `lag`, `bridge`, and `ieee802.11*`). More than one remaining
physical interface is a skip, and the run logs the names. A device with
exactly one physical wired interface — an access point whose interfaces are
`eth0` plus `wlan*` — takes uplink port 1, or an uplink whose local port
the controller left blank, on that interface. Preference is an exact
candidate name, then the other template and structural matches, then the
trailing number, then the single interface. `PORT_MODEL_TEMPLATES` is tried
first and, when the model is listed, suppresses the structural guess and
these fallbacks for a known port.

## Syncing UniFi devices (switches, APs, gateways, PDUs)

By default this tool only creates *clients*, and expects your UniFi hardware
to already be in NetBox. Set `SYNC_UNIFI_DEVICES=true` and it will create the
adopted infrastructure too:

```bash
SYNC_UNIFI_DEVICES=true
SITE_POLICY=create      # if the NetBox sites don't exist yet either
```

Each adopted device becomes a NetBox device with:

- **Interfaces, and power components for PDUs.** When
  `DEVICETYPE_LIBRARY_PATH` matches the model, the device type's interface,
  power-port, and power-outlet templates come from that YAML. Otherwise they
  are built from the controller: `port_table` becomes interfaces (typed from
  the reported media and speed, `1000base-t`, `10gbase-x-sfpp`, …) and
  `outlet_table` becomes power outlets. A portless AP still gets `Port 1`
  so an uplink cable has somewhere to land.
- **A per-kind role.** Defaults stay `switch`, `wireless-ap`, `router`, and
  `pdu`. Anything unrecognised gets `UNIFI_DEVICE_ROLE_SLUG` (`network-device`).
  Point the per-kind settings at roles you already have and those roles are
  reused; the log line is `Reusing existing NetBox device role`. A missing
  slug is still created, so a first run needs no extra setup.
- **Its serial, a `unifi_mac` custom field, and the UniFi site it came from**
  (`unifi_site_name` and `unifi_site_internal_id` on the device). `unifi_mac`
  is the exact-match key the cable code uses.

Devices are created **before** clients in each run, so a switch created now
terminates this same run's cables. Starting from an empty NetBox, one run
gives you the switch, its ports, the clients, and the cables between them.

Generated ports are named `Port 1`, `Port 2`, … — which the default
`PORT_NAME_TEMPLATES` already resolve, so cabling needs no extra setup.
Library templates keep the names from the YAML (`eth0` on an access point,
`SFP+ 49` on a switch). Uplink cabling resolves those without a per-model
entry; see "How matching works".

Only *adopted* devices are synced; pending-adoption ones are ignored.

A dry-run logs the plan and writes nothing. The site line lists creates by
kind (`switch`, `ap`, `gateway`, `power`), merges, moves, cross-site matches
that were not duplicated, and whether each type came from the library or was
generated.

### Roles

| Setting | Default | Recommended when the role already exists |
| --- | --- | --- |
| `UNIFI_SWITCH_ROLE_SLUG` | `switch` | `access-switch` |
| `UNIFI_AP_ROLE_SLUG` | `wireless-ap` | `wireless-access-point` |
| `UNIFI_GATEWAY_ROLE_SLUG` | `router` | `gateway` |
| `UNIFI_POWER_ROLE_SLUG` | `pdu` | `pdu` |
| `UNIFI_DEVICE_ROLE_SLUG` | `network-device` | leave as-is |

Defaults match what earlier releases created. Changing them does not rename
roles that already exist; it only chooses which slug new and merged devices
use.

### Legacy client devices

Earlier releases stored adopted switches, APs, and gateways as client
devices: role `unifi-client`, name equal to the MAC, a single `eth0`, and
`unifi_mac` set. `MERGE_CLIENT_INFRA` (default `true`, and the recommended
value) converts that device in place when an adopted UniFi device has the
same MAC. The NetBox id, journal, and addresses stay. The name, role, device
type, and components are updated from UniFi. This happens even when
`DEVICE_UPDATE_POLICY=create-only`.

`eth0` is removed only when it has no IP and no cable. If it does, those
are moved onto the first interface of the new type. `eth0` is kept when the
new type already has an interface of that name (the UAP-AC-Pro library entry
does) or when the target interface is already cabled.

`MERGE_CLIENT_INFRA=false` leaves the client device untouched and does not
create a second device for that MAC. Only devices that carry the sync tag
and the client role are merged. An untagged device with the same MAC is left
alone and is not duplicated.

### Inventory and staging sites

`UNIFI_INVENTORY_SITES` is a comma-separated list of UniFi site ids. Each
token may be the short API name (`staging`) or the controller `_id`. Adopted
devices on those sites are created with `INVENTORY_DEVICE_STATUS` (default
`inventory`; also `offline`, `active`, `planned`, `staged`, `failed`,
`decommissioning`).

Matching is by MAC, then by serial, across every NetBox site, before a
device is created. An inventory observation never creates a duplicate of a
device that already exists somewhere else, and it never moves a device off a
real site. A tagged legacy client (`unifi-client`, `unifi_mac` set) is the
exception to that skip: with `MERGE_CLIENT_INFRA` on, the device is merged
in place. It keeps its NetBox id and site, and its role and device type are
converted to the adopted hardware. It is not moved onto the inventory site.
With `MERGE_CLIENT_INFRA` off it is left unchanged and is not duplicated.
When that same device later shows up adopted at a real site, it
moves there — including under `create-only`, when its recorded UniFi site
(`unifi_site_name` / `unifi_site_internal_id`) or its current NetBox site is
one of the inventory sites. A move between two real sites happens only when
`DEVICE_UPDATE_POLICY=sync`. Under `create-only` the device stays where it
is and the run logs that the duplicate was avoided.

Name matching stays inside one NetBox site. The same name at another site
is a different device.

### devicetype-library

Left alone, a device type is named after the UniFi model code (`USW24POE`).
Point `DEVICETYPE_LIBRARY_PATH` at a clone of
[netbox-community/devicetype-library](https://github.com/netbox-community/devicetype-library)
and a match uses that project's model name, part number, rack height, and
component templates:

```bash
git clone --depth 1 https://github.com/netbox-community/devicetype-library
DEVICETYPE_LIBRARY_PATH=/path/to/devicetype-library
```

Controller model codes (`US24`, `U7PG2`, `US24PRO2`, `USPPDUP`) are mapped
onto the library SKU (`US-24`, `UAP-AC-Pro`, `USW-Pro-24`, `USP-PDU-Pro`).
`U7PG2` is the AC Pro, not a Wi-Fi 7 AP. Matching also folds punctuation, so
`USW24POE` finds `USW-24-POE`. A device type already in NetBox with the same
slug, part number, or model is reused rather than duplicated.

An unmatched model still syncs. Its interfaces come from `port_table`. The
library is only read when the path is set. The image does not clone it
unless you build with `--build-arg FETCH_DEVICETYPE_LIBRARY=true`, which
puts the tree at `/opt/devicetype-library` and the entrypoint uses that path
when `DEVICETYPE_LIBRARY_PATH` is empty. Compose can mount a clone instead:

```yaml
volumes:
  - /path/to/devicetype-library:/opt/devicetype-library:ro
```

### Power devices

USP PDUs, USP-RPS, smart plugs, and USW-DA / UPS towers sync with the power
role. UniFi often reports them as `type: usw`. They are recognised by a
non-empty `outlet_table`, `type` of `usp` / `ups` / `pdu`, a known model
code (`USPPDUP`, `USPRPS`, `UPS2U`, …), a model or shortname starting with
`USWDA`, `PDU` in the code, or a shortname starting with `UPS`. A Pro XG
switch (`USPXG…`) is not treated as power just because the code starts with
`USP`. A gateway is not treated as power because `outlet_table` has a row:
type `uxg`, `ugw`, or `udm`, and UXG/UGW/UDM model codes (UXG-Pro, USG,
UDM-Pro, UDR), stay gateways. UXG-Pro reports a single outlet.

Outlets become NetBox power outlets fed by an `Input` power port. Network
ports stay interfaces. USP-Strip (`UP6`) has six AC outlets; a shorter
`outlet_table` still generates six, and a complete table is kept as reported.
When the library has the model (USP-PDU-Pro, USP-RPS, UPS-2U), its templates
are used instead of a generated outlet list.

## Finding your switches

Cables can only be created once the tool knows which NetBox device is the
switch UniFi is reporting. It tries four joins, in descending order of
precision, and logs which one matched:

0. If `SYNC_UNIFI_DEVICES=true`, the switch was very likely created by this
   tool with an exact `unifi_mac`, so strategy 2 below matches immediately
   and none of this is a concern.
1. A NetBox **interface** whose `mac_address` equals the switch MAC.
2. A NetBox **device** with a `unifi_mac` custom field equal to the switch MAC.
3. A NetBox **device serial** matching the switch's serial, or its MAC in
   either `aa:bb:cc:…` or `aabbcc…` form.
4. A NetBox **device name** equal to the switch's name in UniFi.

Strategy 1 alone was the original behavior, and it is the reason cables were
often not created despite the switches being present: switch interfaces
usually have no `mac_address` populated, and where they do, a port's MAC is
not the chassis MAC UniFi reports. The other three fix that; **naming the
NetBox device the same as UniFi does is usually all that's needed.**

If a switch still can't be found, the run logs a warning naming it and
listing what was tried. If the switch is found but a port isn't, the warning
lists the interface names that device actually has next to the ones that were
looked for — once per switch, with every individual skip in the summary and
in `CABLE_SKIP_REPORT`. Slot/module and Juniper names match on their own.
Set `PORT_MODEL_TEMPLATES` when two interfaces would both match (two stack
members ending in the same port number) and the run reports `port_ambiguous`.

## Manufacturers

By default each client gets a NetBox device type carrying its real
manufacturer (`Apple, Inc. Client`, `Intel Corporate Client`, …) instead of a
single generic type, so vendor breakdowns in NetBox are meaningful.

The vendor comes from the MAC's OUI as **already resolved by the UniFi
controller**, which reports it per client — no OUI database to ship, update,
or fetch at runtime. For the occasional client the controller can't attribute,
point `OUI_FILE` at an IEEE `oui.txt` or Wireshark `manuf` file and it is
consulted as a fallback (parsed lazily, so an unused file costs nothing).

Randomized/locally-administered MACs — what modern phones use per-SSID — are
deliberately *not* attributed to any vendor: their OUI bytes belong to no one,
so guessing would be wrong rather than merely unknown. Those, and any client
whose vendor can't be determined, fall back to
`CLIENT_MANUFACTURER_SLUG`/`CLIENT_DEVICE_TYPE_SLUG`.

Existing devices created before this feature are corrected on the next sync
(under `DEVICE_UPDATE_POLICY=sync`), so an established install picks up real
manufacturers without being rebuilt. The default `create-only` does not
rewrite device types on objects that already exist. Set
`USE_OUI_MANUFACTURER=false` to keep everything on the single generic type.

## Authentication

Two ways to authenticate to the UniFi controller — set whichever you use in
`.env`/the installer prompts:

- **API key** (`UNIFI_API_KEY`, recommended) — a local UniFi OS API key
  (Settings → Control Plane → Integrations → API Keys). It's sent as an
  `X-API-KEY` header on every request, so there's no login session to
  expire and no password stored beyond this one scoped credential. Support
  depends on your controller's firmware version — verify with `--dry-run`
  before relying on it.
- **Username/password** (`UNIFI_USERNAME` / `UNIFI_PASSWORD`) — the
  original method, works everywhere. Logs in once per run and re-logs-in
  automatically if the session expires mid-run.

Provide one or the other; the tool refuses to start with neither.

## Requirements

- Python 3.10+ (or Docker — see "Running it" below)
- A UniFi controller account with read access (local admin, a read-only
  role, or a scoped API key are all enough)
- A NetBox API token with permission to manage `dcim` and `ipam` objects
- A NetBox site (specified by slug) that the client devices should belong to

## Running it

The sync itself is a one-shot CLI (`unifi-netbox-sync`) — it does one pass
and exits. To keep NetBox continuously up to date you need to invoke it
repeatedly, either with Docker (recommended, especially if NetBox itself
already runs in Docker) or a plain Python install plus cron/systemd.

### Quickest path: `install.sh`

One command, with prompts for your UniFi/NetBox settings:

```bash
curl -fsSL https://raw.githubusercontent.com/LordAdama/netbox-unifi-client-sync/main/install.sh | bash
```

This clones the repo into `./netbox-unifi-client-sync`, asks for your
controller/NetBox details (only on first run — nothing is sent anywhere
but your own local `.env`, written `chmod 600`), builds the Docker image,
runs a `--dry-run` for you to sanity-check, and then asks whether to start
the long-running container (`restart: unless-stopped`, syncing every
`SYNC_INTERVAL_SECONDS`). Re-running the same command later pulls the
latest code, rebuilds, and restarts the container — reusing your existing
`.env` untouched.

If you'd rather read the script before running it (sensible, given it's
piped into `bash`): clone the repo yourself and run `bash install.sh` from
inside it — same behavior, no curl-pipe required.

For automation (CI, IaC, config management), pass `--non-interactive` with
the config as environment variables instead of prompts — see the comment
at the top of `install.sh` for the full variable list:

```bash
UNIFI_HOST=https://192.168.1.1 UNIFI_API_KEY=... \
NETBOX_URL=https://netbox.example.com NETBOX_TOKEN=... NETBOX_SITE_SLUG=main \
bash install.sh --non-interactive
```

This skips every prompt and starts the container immediately after a
successful dry-run (no confirmation step, since there's nobody to ask).

### Option A: Docker, step by step

Prefer to see/control each step yourself rather than run the installer:

```bash
git clone https://github.com/LordAdama/netbox-unifi-client-sync.git
cd netbox-unifi-client-sync
cp .env.example .env
# edit .env with your controller/NetBox details

docker build \
  --build-arg GIT_COMMIT="$(git rev-parse HEAD)" \
  --build-arg APP_VERSION="$(awk -F'"' '/^version = / { print $2; exit }' pyproject.toml)" \
  -t netbox-unifi-client-sync .
```

Add `--build-arg FETCH_DEVICETYPE_LIBRARY=true` only if the image should
carry a clone of devicetype-library. Otherwise mount one at
`/opt/devicetype-library` or set `DEVICETYPE_LIBRARY_PATH`. The default
build does not clone it.

The image records that commit in the OCI label `org.opencontainers.image.revision`
(and in `/app/REVISION`). Trace a running container with:

```bash
docker inspect --format '{{index .Config.Labels "org.opencontainers.image.revision"}}' unifi-netbox-sync
```

**One-off run.** `DRY_RUN` defaults to `true`, so a plain run logs every
intended create, update, and delete and writes nothing. Pass `--apply` (or
set `DRY_RUN=false`) only after that plan looks right:

```bash
docker run --rm --env-file .env netbox-unifi-client-sync            # dry-run
docker run --rm --env-file .env netbox-unifi-client-sync --apply    # write
```

**Run continuously**, syncing on an interval — set `SYNC_INTERVAL_SECONDS`
in `.env` (defaults to `3600` = hourly in `docker-compose.yml`) and either:

```bash
docker compose up -d --build
```

or without compose:

```bash
docker run -d --name unifi-netbox-sync --restart unless-stopped \
  --env-file .env \
  netbox-unifi-client-sync
```

Leave `SYNC_INTERVAL_SECONDS` unset (or `0`) and the container does a
single sync and exits instead — useful if you'd rather drive the schedule
yourself with host cron (`docker run --rm --env-file .env ...` as a cron
job) than run a long-lived container.

Check on it with `docker logs -f unifi-netbox-sync`. The image runs as a
non-root user and, in long-running mode, ships a `HEALTHCHECK` that reports
unhealthy if a sync hasn't completed successfully within roughly two
intervals — `docker ps` / `docker inspect` will show that status, and an
orchestrator (Compose, Swarm, Kubernetes via a translated probe) can act on
it. The heartbeat file the healthcheck reads lives at `/tmp/last-sync-ok`
by default; override its path with `HEARTBEAT_FILE` if `/tmp` in your setup
is a small tmpfs or shared with other processes.

`docker stop` (SIGTERM) on a long-running container is handled gracefully:
if a sync is in progress, it's allowed to finish before the container
exits; if it's between cycles, the container exits within about a second
instead of waiting out the rest of `SYNC_INTERVAL_SECONDS`.

If you set `LOCK_FILE`, the entrypoint checks for `flock` up front and
fails immediately with a clear error if it's missing, rather than silently
skipping the lock — this only matters if you build on a different base
image than the provided Dockerfile's `python:3.12-slim` (which includes it
via `util-linux`).

### Option B: Plain Python

```bash
pip install -e .
cp .env.example .env
# edit .env with your controller/NetBox details

unifi-netbox-sync             # dry-run (the default): log the plan, write nothing
unifi-netbox-sync --apply     # perform the sync
```

`unifi-netbox-sync` loads `.env` from the current directory itself (a
plain `KEY=VALUE` parser, not a shell `source`) — some default values
(`PORT_NAME_TEMPLATES`, `SITE_MAP`) contain spaces and commas that bash's
word-splitting mangles if you `source .env` directly, so don't do that.
Real environment variables, if set, still take precedence over `.env`.

Run it on a schedule yourself (cron, systemd timer, etc.).

### Configuration

All configuration is via environment variables — see `.env.example` for the
full list and defaults. Key ones:

| Variable | Purpose |
| --- | --- |
| `UNIFI_HOST` | Base URL of the controller, e.g. `https://192.168.1.1` |
| `UNIFI_API_KEY` | Local UniFi OS API key; leave blank to use username/password instead (see "Authentication") |
| `UNIFI_IS_UDM` | `true` for UDM/UDM-Pro/CloudKey Gen2+ (UniFi OS), `false` for a classic self-hosted controller |
| `UNIFI_SITE` | UniFi site name to sync (default `default`); ignored if `SITE_MAP` is set |
| `NETBOX_URL` / `NETBOX_TOKEN` | NetBox API endpoint and token |
| `NETBOX_SITE_SLUG` | NetBox site clients should be placed in; ignored if `SITE_MAP` is set |
| `SITE_MAP` | Multi-site: `*` for every site on the controller, and/or `unifi_site:netbox_site_slug` pairs — see "Multi-site sync" |
| `SITE_MATCH` | `normalized` (default), `name`, or `slug` — fallback after the UniFi site id fields; see "Linking to sites you already have" |
| `SITE_POLICY` | `require` (default) or `create` — creating sites is opt-in; see "Site & update policies" |
| `DEVICE_UPDATE_POLICY` | `create-only` (default) or `sync` — see "Site & update policies" |
| `MANAGED_CLIENT_ROLE_SLUGS` | Comma-separated roles whose placeholder interface and IP this sync may write. Unset follows `CLIENT_DEVICE_ROLE_SLUG`. Other roles are matched by MAC and left alone |
| `SYNC_UNMANAGED_TAG` | Opt-out tag (default `sync-unmanaged`). The device is still matched by MAC; its interfaces and IP are not changed |
| `NETBOX_TIMEOUT` / `UNIFI_TIMEOUT` | Per-request HTTP timeouts in seconds (default `30`). A NetBox call with no timeout is what hung a run indefinitely. A NetBox read that times out is retried twice; writes are not |
| `NETBOX_PREFETCH` | Bulk-load devices and interfaces once per site and answer client MAC lookups from that index (default `true`). `false` restores one device lookup per client |
| `NETBOX_FAST_PATH` | Skip the write path when the prefetched client already matches (default `true`). Creates, updates, skips, and offline marks stay the same |
| `NETBOX_PAGE_SIZE` | Page size for NetBox list calls (default `1000`) |
| `NETBOX_MAX_INFLIGHT` | Concurrent NetBox HTTP calls across site workers (default `4`). `0` means no extra cap. A 429 wait does not hold a slot |
| `NETBOX_RATE_LIMIT_RETRIES` | How many times to retry HTTP 429 (default `5`), honoring `Retry-After` |
| `NETBOX_IP_STATUS` | Status set on IP addresses this tool creates (default `active`) |
| `PORT_NAME_TEMPLATES` | Candidate interface-name patterns tried against your switches, in order; blank uses the built-in list. `{port}` is the UniFi index, `{port2}` zero-pads it, `{sfp}` is the SFP ordinal |
| `PORT_MODEL_TEMPLATES` | Per UniFi model, tried first. Example: `USW-Pro-48=Gi1/0/{port};SFP {sfp}`. Separate models with a vertical bar. A model listed here does not get the structural guess, and on an uplink it also suppresses the trailing-number and single-interface fallbacks for a known port |
| `CABLE_SKIP_REPORT` | Path of a CSV listing every skipped cable (site, switch, UniFi port, names tried, NetBox interfaces, reason). Blank logs the same rows and a count by reason without writing a file |
| `SYNC_UNIFI_DEVICES` | Create adopted UniFi switches/APs/gateways/PDUs in NetBox (default `false`) — see "Syncing UniFi devices" |
| `DEVICETYPE_LIBRARY_PATH` | Local devicetype-library clone. A match supplies the device type and its interface/power templates; otherwise they are generated. Recommended mount: `/opt/devicetype-library` |
| `UNIFI_DEVICE_ROLE_SLUG` | Role for created UniFi devices of unrecognised kind (default `network-device`) |
| `UNIFI_SWITCH_ROLE_SLUG` | Role for switches (default `switch`; recommended `access-switch` when that role exists) |
| `UNIFI_AP_ROLE_SLUG` | Role for access points (default `wireless-ap`; recommended `wireless-access-point`) |
| `UNIFI_GATEWAY_ROLE_SLUG` | Role for gateways (default `router`; recommended `gateway`) |
| `UNIFI_POWER_ROLE_SLUG` | Role for PDUs, RPS, and UPS hardware (default `pdu`) |
| `MERGE_CLIENT_INFRA` | Convert a tagged client device into the adopted UniFi device with the same MAC (default `true`). `false` leaves it and does not create a duplicate |
| `UNIFI_INVENTORY_SITES` | Comma-separated UniFi API names and/or `_id`s whose devices are staging stock (default empty) |
| `INVENTORY_DEVICE_STATUS` | NetBox status for devices synced from an inventory site (default `inventory`) |
| `USE_OUI_MANUFACTURER` | Give clients a device type carrying their real vendor (default `true`) — see "Manufacturers" |
| `OUI_FILE` | Optional IEEE `oui.txt` / Wireshark `manuf` file, used only when the controller reports no vendor |
| `CABLE_CONFLICT_POLICY` | `skip` (default) or `replace`. `replace` only deletes cables tagged `SYNC_TAG`; manual cables stay |
| `MARK_STALE_OFFLINE` | Mark **managed** client devices (sync tag + client role) offline when UniFi `last_seen` is older than `STALE_OFFLINE_GRACE`. Absence from the active list is not enough. Infrastructure devices and untagged objects are never changed. The write is status only |
| `STALE_OFFLINE_GRACE` | How long a client may be idle before that offline mark. `7d` (default), `24h`, `30m`, or a number of seconds. `0` disables the grace. Randomized phone MACs use the same window |
| `DRY_RUN` | `true` (default). Log every intended create/update/delete and write nothing. `false` or `--apply` writes |
| `LOG_FORMAT` | `text` (default) or `json` — one JSON object per log line, for log shippers |
| `METRICS_FILE` | If set, write Prometheus textfile-collector metrics to this path after every run |
| `MAX_WORKERS` | NetBox sites synced concurrently (default `4`). `1` is strictly sequential — see "Scaling" |
| `SYNC_INTERVAL_SECONDS` | Docker only — `0`/unset runs once and exits, a positive number loops forever on that interval |
| `LOCK_FILE` | Docker only, optional — `flock` path to avoid two instances racing on the same NetBox (see "Known limitations") |
| `HEARTBEAT_FILE` | Docker only — path the healthcheck reads (default `/tmp/last-sync-ok`); override if `/tmp` is constrained in your setup |

### Multi-site sync

By default this syncs one UniFi site into one NetBox site
(`UNIFI_SITE`/`NETBOX_SITE_SLUG`). To sync more than one, set `SITE_MAP`
instead, which then completely replaces `UNIFI_SITE`/`NETBOX_SITE_SLUG`.

**Every site on the controller**, without listing them:

```bash
SITE_MAP=*
```

The controller is asked which sites exist and each is matched to a NetBox
site. UniFi has two ids: the short API name used in `/s/{name}/` (`default`,
`7xk2p9qr`) and the immutable `_id` (a hex ObjectId). NetBox stores both.
`unifi_site_id` keeps the short API name, which is what earlier releases
wrote there. `unifi_site_name` is that same short name under an explicit
field, and `unifi_site_internal_id` is the `_id`. When those fields are
empty the tool falls back to the slug, the site name, then both folded to
alphanumerics. The slug guess comes from the site's description as shown in
the UniFi UI ("Head Office" → `head-office`), not from the API id. An
explicit `SITE_MAP` entry still chooses the slug when nothing is already
linked; a stored id wins over that slug.

**Missing sites are not created** unless `SITE_POLICY=create`. Leave the
default `require` in place until a dry-run shows every UniFi site landing on
the NetBox site you expect. `create` is how a previous configuration produced
duplicate sites for UniFi sites whose NetBox names had been edited.

**Explicit pairs**, when you want to control exactly which sites sync and
where they land:

```bash
SITE_MAP=default:main,branch-office:branch
```

**Both** — discover everything, but pin individual sites to a chosen slug:

```bash
SITE_MAP=*,default:head-office
```

An explicit entry always wins over the derived name for the site it names.
If two UniFi sites have the same description they'd derive the same slug.
That is a conflict: both are skipped and warned about. Pin one of them with
an explicit `SITE_MAP` entry so each NetBox site has a single UniFi site.

Each pair is synced independently with its own client list and its own
`SITE_POLICY`/site-creation check. Device-name-collision checks and
stale-device marking are scoped by *NetBox site*, not by UniFi site — a
client in one NetBox site never affects another site's devices, and the
same device name is fine in two different sites (NetBox's uniqueness
constraint is per-site, and this tool's collision check respects that).
If two UniFi sites resolve to the *same* NetBox site, that is a conflict:
both are skipped, `unifi_site_id` is not written, and nothing in that site
is marked offline. A site-level failure (e.g. a `require`d NetBox site missing, or the
UniFi controller erroring for one site) is recorded as an error for that
pair without stopping the other pairs in the same run. The run summary
reports totals across all sites combined; per-site progress is logged as
it happens (`Site 'X' -> NetBox 'Y': N clients, ...`).

Policy settings (`SITE_POLICY`, `DEVICE_UPDATE_POLICY`,
`CABLE_CONFLICT_POLICY`, etc.) are global — they apply the same way to
every site pair, not configured per-site.

### Linking to sites you already have

A derived slug is a *guess* at what the site is called in NetBox, and a guess
that misses creates a duplicate instead of adding to the site you already
have. Punctuation is the usual culprit — "Acme's Depot - North Yard"
slugifies to `acme-s-depot-north-yard`, which does not equal an existing
`acmes-depot-north-yard`.

So before syncing, each UniFi site is resolved against the sites already in
NetBox, in order:

1. custom field **`unifi_site_internal_id`** equal to the controller `_id`.
   This still matches after the UniFi description is renamed and after the
   NetBox name or slug changes.
2. custom field **`unifi_site_id`** or **`unifi_site_name`** equal to the
   short API name (`default`, `7xk2p9qr`). `unifi_site_id` is what earlier
   releases stored, and it still holds that short name, not the `_id`.
3. exact slug
4. exact **name** (the UniFi site description vs the NetBox site name)
5. both folded to alphanumerics, making apostrophes, dashes, spacing and case
   irrelevant

Every step is an exact comparison after folding — never fuzzy — so two
genuinely different sites are not bound together. `SITE_MATCH=name` stops
after the name step, `SITE_MATCH=slug` stops after the slug step. The id
fields are always tried first, and a hit counts only when the site's own
field equals the value we asked for. NetBox ignores unknown filters, so
before a custom field exists a `cf_*` query returns *every* site; the first
of those is not a match. A dry-run does not create the fields, which is why
that check matters on the first preview.

`SITE_MAP` keys stay the short API name (`default:main`). The value is the
slug to create when no site is already linked. A site that already carries
`unifi_site_internal_id`, `unifi_site_id`, or `unifi_site_name` for that
UniFi site is used even when `SITE_MAP` names a different slug.

The first time a site is matched by name or slug, and it is the only UniFi
site that resolved to it, empty identity fields are filled. `unifi_site_id`
and `unifi_site_name` receive the short API name. If `unifi_site_id` is
already set, it is copied into an empty `unifi_site_name` and is not
changed. `unifi_site_internal_id` is set from the controller `_id` only;
it is never guessed from the legacy field. A non-empty field that already
holds a different value is not overwritten, and the same value is never
copied onto a second site. **Name, slug, and status are never changed**, so
a rename made in NetBox is not reverted and a later run follows the renamed
site instead of creating the old name again.

The same two fields are also created on devices. A device records the UniFi
site (`unifi_site_name`, `unifi_site_internal_id`) it was last synced from,
which is how an inventory site can tell a staging unit from a device that
already lives somewhere else.

If more than one UniFi site resolves to the same NetBox site, the run skips
all of them, records an error, and does not stamp `unifi_site_id`. That is
what stops a bad match from parking every controller site's clients in one
NetBox site.

**A matched site is only ever linked to, never renamed.** Whatever is already
in it stays, and the UniFi devices and clients are added alongside. Each link
is logged:

```
Linking UniFi site 'north' to existing NetBox site 'acmes-depot-north-yard'
("Acme's Depot - North Yard") instead of creating 'acme-s-depot-north-yard'.
Its name and slug are left unchanged.
```

Resolution happens before sites are grouped for syncing. A NetBox site that
more than one UniFi site resolved to is dropped from the run instead of
being synced as a shared group.

### Site & update policies

Two independent, conservative-by-default policy knobs:

- **`SITE_POLICY`** governs what happens if no existing site matches
  (checked separately for each pair under `SITE_MAP`). `require` (default)
  records an error for that site and creates nothing — a typo, or a site
  you renamed in NetBox and have not linked yet, must not spawn a duplicate.
  `create` is opt-in: it creates a minimal site (name, slug, status, and the
  three UniFi identity fields) when nothing matches. Neither setting modifies
  an existing site's name or slug.
- **`DEVICE_UPDATE_POLICY`** governs a **tagged** client device's `name` and
  `status` after it was created. `create-only` (default) sets them once and
  leaves them alone, so a rename in NetBox sticks. `sync` is opt-in and
  keeps them matched to UniFi, which will revert those renames. Objects
  that do not carry `SYNC_TAG` are never updated, regardless of this
  setting — not their name, interface, IP, or cables. `create-only` still
  updates the IP of a device that *does* carry the tag, is still in a
  managed client role, and is not tagged `SYNC_UNMANAGED_TAG` (addresses
  move). A dry-run logs `would assign IP` only for that case or for a
  device that does not exist yet. An existing untagged device, a device
  whose role is not in `MANAGED_CLIENT_ROLE_SLUGS`, or a device tagged
  `SYNC_UNMANAGED_TAG` produces no IP plan and no IP write.

`SITE_POLICY` and `DEVICE_UPDATE_POLICY` apply in dry-run too (as previews,
not real writes), and every decision — site created vs. reused, an update
skipped by policy — is logged and counted in the run summary
(`sites_created`, `devices_update_skipped`) for auditability.

### Devices you have already built

A client this sync created can later be turned into real gear: the role
changed to `switch`, the device type replaced, or the ports filled in by
hand. The MAC custom field is still set, so the next pass finds that device.
It must not grow a client `eth0` and it must not have its address pulled
onto that interface.

That write happens only when all three are true:

- the device carries `SYNC_TAG`
- its role slug is in `MANAGED_CLIENT_ROLE_SLUGS`
- it does not carry `SYNC_UNMANAGED_TAG`

`MANAGED_CLIENT_ROLE_SLUGS` defaults to `CLIENT_DEVICE_ROLE_SLUG` (normally
`unifi-client`). Set it to a comma-separated list when more than one role
should keep the placeholder, and include the client role in that list so
devices this sync creates stay managed on later passes. Changing only the
device type does not opt a device out; add `sync-unmanaged`, or move it to a
role that is not in the list.

`SYNC_UNMANAGED_TAG` (default `sync-unmanaged`) is the per-device opt-out for
something that is still in a client role. Tag it in NetBox and the sync
keeps matching the MAC, does not create a second device, and does not touch
the placeholder interface or the IP. Each such device logs one line per run,
including in a dry-run.

### NetBox compatibility

Written against NetBox's generic cable-termination API
(`a_terminations`/`b_terminations`, NetBox 3.3+) and the `role` field name
on `dcim.devices` (NetBox 4.x). If you're on NetBox 3.x, rename `role` to
`device_role` in `netbox_client.py`'s device-creation payload.

## Observability

Every run logs a one-line human-readable summary and, right after it, a
`sync_summary={...}` JSON line with the same counts plus run duration —
parse that line if you're shipping logs somewhere (Loki, CloudWatch, etc.)
regardless of `LOG_FORMAT`. Set `LOG_FORMAT=json` to also make every other
log line a JSON object instead of plain text.

Each run also logs a `Sync timing:` line (total, prefetch, clients per
second, workers, cache hits, index hits, and the NetBox HTTP count when the
real client is in use) and a `Slowest sites:` line with the five slowest
site pairs.

For Prometheus, set `METRICS_FILE` to a path on a volume the container can
write to; each run overwrites it (atomically) with textfile-collector
output — point node_exporter's `--collector.textfile.directory` (or
equivalent) at that directory rather than running this tool as its own
metrics server, which doesn't fit a periodic batch job.

## Scaling

A full dry-run spends almost all of its time in NetBox. The expensive call
is one `cf_unifi_mac` filter per client. On about 1,084 clients that is
1.5–2.5 seconds each, roughly 28 minutes with sites processed one at a time.

`NETBOX_PREFETCH` (default on) replaces those per-client filters with a
bulk load:

- one paginated list of every device that has `unifi_mac` set
- one device list per site, and that site's interfaces in batches of 50 devices
- switch interfaces still come from that list and are matched in memory, so
  a port name is not looked up once per client

`NETBOX_FAST_PATH` (default on) then skips the write path when the indexed
client already matches UniFi. Creates, updates, merge decisions, skip
reasons, and offline marks stay on the original path. A dry-run still plans
cables from what a device record exposes directly: a prefetched interface is
not treated as proof that a cable is already in place.

Set `NETBOX_PREFETCH=false` and `NETBOX_FAST_PATH=false` to force the
per-client lookups.

Measured with `scripts/bench_sync.py` on a synthetic estate of 39 sites and
1,084 clients (one switch per site, one third of the clients wired). Names,
MACs, and addresses in that script are synthetic. Every timed case produced
the same creates, updates, skips, and offline decisions as the prefetch-off
path.

| Case | Mode | Modeled HTTP | `cf_unifi_mac` filters | Wall at 5 ms/call | Serial estimate |
| --- | --- | ---: | ---: | ---: | ---: |
| Populated dry-run | before, 1 worker | 1,240 | 1,084 | 6.54 s | 27.2 min |
| Populated dry-run | after, 1 worker | 158 | 0 | 0.87 s | 19.9 s |
| Populated dry-run | after, 4 workers | 158 | 0 | 0.69 s | 19.9 s |
| Empty dry-run | before, 1 worker | 2,324 | 1,084 | 12.02 s | 28.1 min |
| Empty dry-run | after, 4 workers | 157 | 0 | 0.68 s | 19.7 s |
| Populated apply | before, 1 worker | 3,488 | 1,084 | 18.14 s | 29.1 min |
| Populated apply | after, 4 workers | 241 | 0 | 0.94 s | 24.1 s |

The 5 ms column is measured wall time against an in-memory NetBox with that
delay injected on each modeled HTTP call. The serial estimate is the same
call mix priced the way a slow custom-field filter behaves in production:
1.5 seconds for each `cf_unifi_mac` point lookup, 0.2 seconds for a bulk
page, and 0.05 seconds for every other call. It is not a clock reading from
a live NetBox. Prefetch runs once, before the site workers, so extra workers
shorten the remaining per-site calls and do not multiply the bulk load.

**Concurrency.** `MAX_WORKERS` (default `4`) syncs that many NetBox sites at
once. Pairs are grouped by *NetBox* site and a group is never split across
workers, so a single NetBox site is never written by two workers. Two UniFi
sites that resolve to that same site are skipped as a conflict before
grouping, rather than consolidated into it. `NETBOX_MAX_INFLIGHT` (default
`4`, `0` for no extra cap) limits how many NetBox HTTP calls are in flight
across those workers. HTTP 429 is retried up to `NETBOX_RATE_LIMIT_RETRIES`
(default 5), honoring `Retry-After`. The wait does not hold an in-flight
slot. Set `MAX_WORKERS=1` for the original strictly sequential run. Raise
either knob gradually and read the `Slowest sites:` line.

**Partitioning across processes.** For very large estates, split `SITE_MAP`
across several containers so each owns a disjoint set of UniFi sites. Two
instances must never share a NetBox site — there is no distributed lock, so
they would race (see "Known limitations"). Partition on NetBox site
boundaries and the instances never touch the same objects.

**Where this still won't go.** A quiet client no longer costs a device
lookup. What remains is the bulk pages, one switch lookup per switch, and a
write for each real change. Tens of thousands of clients are then bounded by
those list calls and by UniFi returning the whole client list in one
response, not by one filter per MAC.

## Troubleshooting

**`--dry-run` reports skipped cables.** The run tells you why, at WARNING
level, for each distinct switch:

- *"No NetBox device found for UniFi switch …"* — the switch isn't in NetBox
  under any of the four joins, and **no device is created** to stand in for
  it. See "Finding your switches". `SYNC_UNIFI_DEVICES=true` is a separate,
  explicit opt-in for creating adopted UniFi hardware; a skipped cable never
  turns it on.
- *"NetBox device 'X' has no interfaces"* — the device is there but has
  nothing to cable to. The run skips the cable and does not create
  interfaces.
- *"UniFi port N on 'X' matched no NetBox interface. Looked for […]; that
  device's interfaces are named […]"* — a naming mismatch. The same rows are
  counted by reason at the end of the run. Set `CABLE_SKIP_REPORT` for the
  full table. `Gi1/0/4`, `1/1/4`, `ge-0/0/4`, zero-padded names, and SFP
  ordinals are matched already. When the reason is `port_ambiguous` (two
  interfaces parse to the same port), pin the model:

  ```bash
  PORT_MODEL_TEMPLATES=USW-Pro-48=Gi1/0/{port};SFP {sfp}
  ```

  A model with templates does not also take a structural guess, so a wrong
  stack member cannot win. The same pin suppresses an uplink's
  trailing-number fallback (`SFP+ 49` for port 49) and the single-interface
  fallback (`eth0` when that is the only wired port) for a known port. An
  ambiguous trailing number is skipped and logged. A blank local port still
  uses that single wired interface, because a template needs a port index.
- *"port N on X is shared by multiple wired clients"* — more than one client
  is on that switch port (an uplink, or an unmanaged switch behind an AP).
  None of them is cabled. A port the controller marks `is_uplink` is skipped
  even when only one client is on it. An existing cable is left in place.

Dry-run emits exactly the same diagnostics a real run would, so you can
resolve all of this before writing anything. The skip CSV is written during
dry-run when `CABLE_SKIP_REPORT` is set.

**A device or site name looks wrong in the plan.** Device names are
sanitized and de-duplicated per site (see "What it does"); site slugs under
`SITE_MAP=*` come from the UniFi site *description*. Pin any slug you don't
like with an explicit `SITE_MAP` entry.

## Known limitations

Documented rather than solved, because they're fine at the scale this was
built for (a home/SMB network) but worth knowing about before relying on it
for something bigger:

- **No distributed lock.** Two instances syncing the same NetBox
  concurrently can race on cable creation/deletion. The Docker entrypoint
  can guard against overlap between instances that share a `LOCK_FILE`
  path (e.g. two containers with the same bind-mounted volume), but that's
  opt-in and does nothing for instances that don't share the file — don't
  run multiple unrelated instances against the same NetBox. `MAX_WORKERS`
  is *within* one process and is safe: workers never share a NetBox site.
- **A roaming MAC across two NetBox sites is unhandled.** The client-device
  lookup is by MAC and global, so if the same client appears in two UniFi
  sites that map to *different* NetBox sites, both groups will claim the
  same device and fight over its site assignment. Don't map sites that way.
- **No pagination handling.** UniFi's classic `/stat/sta` endpoint returns
  the full active-client list in one response, so there's nothing to
  paginate against today — but this hasn't been exercised at very large
  (thousands of clients) client counts, where response size/timeouts could
  become a factor.
- **IP addresses are plain /32s.** No VRF, tenant, or prefix awareness —
  addresses land in the global table. If your environment needs any of
  those, extend `PynetboxGateway.assign_ip`.
- **HTTP calls time out.** UniFi and NetBox requests use `UNIFI_TIMEOUT` and
  `NETBOX_TIMEOUT` (default 30s). pynetbox itself does not set a timeout; without
  one, a stalled NetBox connection hangs the process and the healthcheck file
  is never written. A NetBox read (`GET`, `HEAD`, `OPTIONS`) that times out or
  fails to connect is retried twice, with one second and then two seconds of
  backoff. Writes are not retried: a timed-out create may already have landed.
  HTTP 429 is retried on every verb, up to `NETBOX_RATE_LIMIT_RETRIES`, because
  NetBox refused the call before applying it.
- **One site cannot abort the others.** A failure while syncing a site —
  including an unexpected exception — is logged, recorded on that site, and
  the run continues. Stale-offline marking for a NetBox site is skipped when
  any of the UniFi sites mapped to it failed, or when client history
  (`last_seen`) could not be read, because a partial client list is not
  evidence that a device is gone. Per-client handling is still narrow:
  expected network/API failures (`requests`, `pynetbox.RequestError`,
  `LookupError`) are recorded and the next client runs. Anything else fails
  the rest of that site only.
- **Primary IP moves.** NetBox refuses to reassign an address that is still
  some device's primary IP. If that device is tagged `SYNC_TAG`, its primary
  is cleared and the move is retried. If it is not, the address is left where
  it is and that client continues; the refusal is a warning, not a crashed run.

## Safe rollout

The defaults are chosen so the first run against a live NetBox cannot create
sites, rename devices, or replace cables:

1. Deploy with `DRY_RUN=true` (the default) and `SITE_POLICY=require` (the
   default). Let one scheduled run finish and read the log.
2. Check three things in that log: which UniFi sites linked to which existing
   NetBox sites (`unifi_site_internal_id`, `unifi_site_id` / `unifi_site_name`,
   or a name match), which sites were **not** matched (those would be created
   only if you later set `SITE_POLICY=create`), and which cables were skipped
   because a switch or a port was missing. Skipped cables do not create switches.
3. For a NetBox site you renamed, either let the dry-run's name match record
   `unifi_site_id` (the short API name) on the next real run, or set that
   custom field yourself before applying. `unifi_site_internal_id` is filled
   from the controller `_id` on that same run. Do not rely on the old slug.
4. Set `CABLE_SKIP_REPORT` and read the counts. `port_not_found` rows still
   need a `PORT_MODEL_TEMPLATES` entry. `shared_port` and `uplink_port` rows
   are intentional. `port_ambiguous` rows need a model template that names
   one of the two interfaces. Offline marks follow `last_seen` and
   `STALE_OFFLINE_GRACE` (default 7 days), not "missing from the active list".
5. Apply with `DRY_RUN=false` or `--apply` only after that plan is what you
   want. `DEVICE_UPDATE_POLICY=sync` and `CABLE_CONFLICT_POLICY=replace`
   overwrite manual edits of tagged objects; leave them at the defaults
   unless that is deliberate. This run does not delete a cable it previously
   created on a shared port; it only stops adding new ones.

The long-running container writes `/tmp/last-sync-ok` only when a run exits 0,
which means it finished and recorded no site or client errors. A timed-out
call or a per-site failure leaves the file untouched, so the `HEALTHCHECK`
stays unhealthy instead of reporting a hung or partial run as success.

## Development

```bash
pip install -e ".[dev]"
pytest
```

Tests mock both the UniFi HTTP API (via `responses`) and NetBox (via an
in-memory fake gateway in `tests/fakes.py`), so no live controller or
NetBox instance is required to run the suite. A GitHub Actions workflow
(`.github/workflows/tests.yml`) runs the suite on Python 3.10–3.12 for
every push/PR.

## License

[MIT](LICENSE).

## Design notes

- `unifi_client.py` — thin REST client for the UniFi controller (login,
  active clients, client history with `last_seen`, infrastructure devices).
- `portmatch.py` — UniFi port index to NetBox interface name: templates,
  per-model templates, structural names (slot/module, Juniper, SFP), and the
  uplink trailing-number and single-wired-interface fallbacks.
- `staleness.py` — offline decision from `last_seen` and the grace window.
- `netbox_client.py` — `NetboxGateway` defines the interface the sync
  engine depends on; `PynetboxGateway` is the real implementation backed by
  [pynetbox](https://github.com/netbox-community/pynetbox). Tests substitute
  a fake implementing the same interface, so sync logic is tested without
  touching a real NetBox instance.
- `sync.py` — orchestrates the sync; all mutating NetBox calls are skipped
  in dry-run mode, and every intended create, update, and delete is logged.
  Loops over `Settings.site_pairs()` (one pair for the default single-site
  case), calling `_run_site()` for each; a site-level failure (a `require`d
  missing NetBox site, an HTTP timeout, or an unexpected exception) is
  caught and recorded as an error for that site without stopping the others.
  Per-client network/API errors are caught inside the site (see "Known
  limitations"). Stale-device marking is deliberately *not* done inside
  `_run_site()`: `run()` first groups every pair's `seen_macs` and
  `last_seen` history by NetBox site slug (so two pairs sharing a slug get
  the union, not each other's false positives), then marks stale **client**
  devices once per distinct slug — skipping any slug whose pair(s) included
  a failure or whose client history could not be read, since acting on an
  incomplete client list risks false offlines. A device still inside
  `STALE_OFFLINE_GRACE` of its UniFi `last_seen` stays active, including a
  randomized phone MAC. Only devices tagged `SYNC_TAG` with the client role
  are eligible, and the status write patches status alone.
- `caching.py` — `CachingNetboxGateway`, a run-scoped read cache wrapping
  any gateway. Caches only what's stable for a run (sites, switches, switch
  interfaces); deliberately does *not* cache client-device or name-collision
  lookups, which change as the run creates devices. Thread-safe, since
  parallel site workers share one instance.
- `naming.py` — device-name sanitization, the deterministic MAC-suffix
  collision fallback, and NetBox slug generation.
- `devicetype_library.py` — optional index over a devicetype-library clone,
  plus the UniFi-media/speed to NetBox-interface-type mapping. A library hit
  supplies interface and power templates; otherwise they are generated.
- `model_map.py` — controller model codes to library SKUs, and which devices
  are PDUs, RPS units, or UPS hardware rather than switches.
- `oui.py` — locally-administered (randomized) MAC detection and the
  optional offline IEEE/Wireshark OUI file parser.
- `metrics.py` — the JSON run-summary log line and optional Prometheus
  textfile-collector output.
- `logging_utils.py` — the `text`/`json` log formatter selected by
  `LOG_FORMAT`.
- `cli.py` — entry point (`unifi-netbox-sync`).
- `Dockerfile` / `docker/entrypoint.sh` / `docker/healthcheck.sh` —
  packages the CLI into a non-root image; the entrypoint either runs one
  sync and exits, or loops on `SYNC_INTERVAL_SECONDS` for a long-running
  container (writing a heartbeat file the `HEALTHCHECK` reads), optionally
  serializing runs via `LOCK_FILE`.
- `install.sh` — clone-or-update, prompt-for-config, build, dry-run,
  launch. The one-command path in "Running it" above.
