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
- Optionally marks previously-synced client devices as `offline` (never
  deletes them) when UniFi stops reporting them.

Your UniFi hardware — switches, APs, gateways — can be created too, with
`SYNC_UNIFI_DEVICES=true` (off by default; see "Syncing UniFi devices"). With
it on, an empty NetBox is populated end to end in a single run: the switches
with their real ports, the clients, and the cables between them. With it off,
switches must already be inventoried in NetBox and only clients are created.

Everything this tool creates is tagged `unifi-sync`, so it's easy to find and
safe to distinguish from manually-entered NetBox data.

## How matching works

| UniFi side | NetBox side |
| --- | --- |
| Switch MAC address | Four strategies, first match wins — see "Finding your switches" |
| Switch port index (`sw_port`) | Looked up on the matched switch device by trying each of `PORT_NAME_TEMPLATES` in order (e.g. `"4"`, `"Port 4"`, `"GE4"`, `"Gi4"`) until one matches an existing interface name |
| Client MAC address | Stored in a `unifi_mac` custom field on the NetBox client device, created automatically on first run |

If a switch or matching port can't be found, the client device is still
synced — only the cable is skipped, and the reason is logged and included
in the run summary. Every switch/port match (including which of the
candidate name templates matched) and every ambiguous match (a MAC that
appears on more than one NetBox interface) is logged at INFO/WARNING for
troubleshooting.

## Syncing UniFi devices (switches, APs, gateways)

By default this tool only creates *clients*, and expects your UniFi hardware
to already be in NetBox. Set `SYNC_UNIFI_DEVICES=true` and it will create the
adopted infrastructure too:

```bash
SYNC_UNIFI_DEVICES=true
SITE_POLICY=create      # if the NetBox sites don't exist yet either
```

Each adopted device becomes a NetBox device with:

- **Its real ports**, built from the controller's own `port_table` — so a
  24-port switch gets 24 interfaces, typed from the reported media and speed
  (`1000base-t`, `10gbase-x-sfpp`, …). This is more accurate than any static
  library, because it reflects the hardware actually in front of you.
- **A per-type role**: `switch`, `wireless-ap` or `router`, so you can filter
  them apart in NetBox. Anything unrecognised gets `UNIFI_DEVICE_ROLE_SLUG`.
- **Its serial and a `unifi_mac` custom field**, which is the exact-match key
  the cable code uses — devices created this way always wire up.

Devices are created **before** clients in each run, so a switch created now
terminates this same run's cables. Starting from an empty NetBox, one run
gives you the switch, its ports, the clients, and the cables between them.

Ports are named `Port 1`, `Port 2`, … — which the default
`PORT_NAME_TEMPLATES` already resolve, so cabling needs no extra setup.

Only *adopted* devices are synced; pending-adoption ones are ignored.

### Nicer model names via devicetype-library

Left alone, a device type is named after the UniFi model code (`USW24POE`).
Point `DEVICETYPE_LIBRARY_PATH` at a clone of
[netbox-community/devicetype-library](https://github.com/netbox-community/devicetype-library)
and it will instead use that project's canonical model name, part number and
rack height:

```bash
git clone --depth 1 https://github.com/netbox-community/devicetype-library
DEVICETYPE_LIBRARY_PATH=/path/to/devicetype-library
```

Matching is on the model code against each YAML's `part_number`/`model`/`slug`,
normalized so `USW24POE` finds `USW-24-POE`. If you have **already imported**
those device types into NetBox, they're reused rather than duplicated.

This is metadata polish, not a requirement: **ports always come from the
controller**, never from the library, and an unmatched model still syncs with
its full real port list. The library is only read when the path is set.

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
looked for — set `PORT_NAME_TEMPLATES` to match.

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
manufacturers without being rebuilt. Set `USE_OUI_MANUFACTURER=false` to keep
everything on the single generic type.

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

docker build -t netbox-unifi-client-sync .
```

**One-off run** (e.g. to dry-run before trusting it with real changes):

```bash
docker run --rm --env-file .env netbox-unifi-client-sync --dry-run
docker run --rm --env-file .env netbox-unifi-client-sync
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

unifi-netbox-sync --dry-run   # see what would change, without writing anything
unifi-netbox-sync             # perform the sync
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
| `SITE_MATCH` | `normalized` (default), `name`, or `slug` — how an existing NetBox site is found; see "Linking to sites you already have" |
| `SITE_POLICY` | `require` (default) or `create` — see "Site & update policies" |
| `DEVICE_UPDATE_POLICY` | `sync` (default) or `create-only` — see "Site & update policies" |
| `NETBOX_IP_STATUS` | Status set on IP addresses this tool creates (default `active`) |
| `PORT_NAME_TEMPLATES` | Candidate interface-name patterns tried against your switches, in order; blank uses the built-in list |
| `SYNC_UNIFI_DEVICES` | Create adopted UniFi switches/APs/gateways in NetBox (default `false`) — see "Syncing UniFi devices" |
| `DEVICETYPE_LIBRARY_PATH` | Local devicetype-library clone, for canonical model names/part numbers |
| `UNIFI_DEVICE_ROLE_SLUG` | Role for created UniFi devices of unrecognised type (default `network-device`) |
| `USE_OUI_MANUFACTURER` | Give clients a device type carrying their real vendor (default `true`) — see "Manufacturers" |
| `OUI_FILE` | Optional IEEE `oui.txt` / Wireshark `manuf` file, used only when the controller reports no vendor |
| `CABLE_CONFLICT_POLICY` | `skip` (default, non-destructive) or `replace` when a target port is already cabled to something else |
| `MARK_STALE_OFFLINE` | Mark client devices no longer seen by UniFi as `offline` instead of leaving them `active` |
| `DRY_RUN` | Plan only, make no changes (same as `--dry-run`) |
| `LOG_FORMAT` | `text` (default) or `json` — one JSON object per log line, for log shippers |
| `METRICS_FILE` | If set, write Prometheus textfile-collector metrics to this path after every run |
| `MAX_WORKERS` | NetBox sites synced concurrently (default `1`) — see "Scaling" |
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
SITE_POLICY=create
```

The controller is asked which sites exist and each is synced into its own
NetBox site, named from the site's description as shown in the UniFi UI
("Head Office" → `head-office`) — not its API id, which is an opaque string
like `7xk2p9qr`. Sites added in UniFi later are picked up automatically on
the next run.

`SITE_POLICY=create` is effectively required here: discovering sites you
haven't already hand-created in NetBox is the normal case, and the default
`require` would report each missing one as an error. The run logs a reminder
if you leave it on `require`.

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
If two UniFi sites have the same description they'd derive the same slug and
share one NetBox site; that's handled safely (they become one group, and
stale-marking unions their clients) but is warned about, since it's rarely
intended — pin one of them to separate them.

Each pair is synced independently with its own client list and its own
`SITE_POLICY`/site-creation check. Device-name-collision checks and
stale-device marking are scoped by *NetBox site*, not by UniFi site — a
client in one NetBox site never affects another site's devices, and the
same device name is fine in two different sites (NetBox's uniqueness
constraint is per-site, and this tool's collision check respects that).
If two UniFi sites happen to map to the *same* NetBox site slug (e.g.
consolidating them), that's handled correctly too: stale-device marking
uses the union of both pairs' clients for that shared NetBox site, and if
one of those pairs fails, stale-marking for the shared site is skipped
entirely for that run rather than acting on incomplete information — a
site's clients not being known yet is not the same as a device being
gone. A site-level failure (e.g. a `require`d NetBox site missing, or the
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

1. exact slug
2. exact **name** (the UniFi site description vs the NetBox site name)
3. both folded to alphanumerics, making apostrophes, dashes, spacing and case
   irrelevant

Every step is an exact comparison after folding — never fuzzy — so two
genuinely different sites are not bound together. `SITE_MATCH=name` stops at
step 2, `SITE_MATCH=slug` restores the original exact-slug-only behavior.

**A matched site is only ever linked to, never modified.** Its name, slug,
region, tenant and everything else are left alone, whatever is already in it
stays, and the UniFi devices and clients are added alongside. Each link is
logged:

```
Linking UniFi site 'north' to existing NetBox site 'acmes-depot-north-yard'
("Acme's Depot - North Yard") instead of creating 'acme-s-depot-north-yard';
its inventory is added to, never overwritten
```

Resolution happens before sites are grouped for syncing, so if two UniFi
sites turn out to be the same NetBox site they end up in one group — keeping
the stale-marking union correct rather than having each pair mark the other's
clients offline.

### Site & update policies

Two independent, conservative-by-default policy knobs:

- **`SITE_POLICY`** governs what happens if a site slug doesn't exist
  (checked separately for each pair under `SITE_MAP`). `require` (default)
  fails the run with a clear error — the safest choice, since a typo'd slug
  shouldn't silently spawn a new site. `create` creates a minimal site
  (name/slug/status only) if it's missing. Neither setting ever modifies an
  *existing* site's attributes — this tool only ever creates a site, never
  updates one.
- **`DEVICE_UPDATE_POLICY`** governs what happens to a client device's
  `name`/`status` on every run *after* it was first created. `sync`
  (default) keeps them matched to what UniFi currently reports — the same
  behavior as before this setting existed. `create-only` sets them once at
  creation and leaves them alone from then on, so if you (or another admin)
  rename a device or change its status by hand in NetBox, this tool won't
  overwrite that on the next run. This only covers the device's name and
  status; interfaces, IP addresses, and cables are still kept in sync
  either way — that's the tool's actual purpose and isn't gated by this
  setting.

Both apply in dry-run too (as previews, not real writes), and every
decision — site created vs. reused, an update skipped by policy — is
logged and counted in the run summary (`sites_created`,
`devices_update_skipped`) for auditability.

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

For Prometheus, set `METRICS_FILE` to a path on a volume the container can
write to; each run overwrites it (atomically) with textfile-collector
output — point node_exporter's `--collector.textfile.directory` (or
equivalent) at that directory rather than running this tool as its own
metrics server, which doesn't fit a periodic batch job.

## Scaling

Two things dominate runtime: how many NetBox round-trips each client costs,
and whether sites are synced one at a time.

**Per-client round-trips.** A steady-state run — where the network hasn't
changed and the right answer is "do nothing" — costs about **3 gateway calls
per client**, down from 11–14 before this work. Three things get it there,
all on by default:

- *Run-scoped caching* of lookups that are constant for a run: the NetBox
  site, the device type/role, and (the big one) switch devices and their
  interfaces. Resolving a switch port used to cost up to one lookup per
  candidate name per client; a switch's interfaces are now fetched once and
  matched in memory, so 48 clients behind one switch cost 1 interface list
  instead of up to 192 lookups.
- *No-op short-circuiting*: if the device already holds its own name the
  uniqueness query is skipped; if the primary IP already matches, no IP write;
  if the cable already lands on the right port, no cable write.
- *Honest change reporting*: `devices_updated` now counts devices actually
  written, not merely seen. A quiet run reports `clients_unchanged`, and the
  `sync_summary` line makes churn visible at a glance.

Measured on a synthetic 20-site × 2-switch × 48-port estate (1,920 clients),
steady-state re-run: **9,660 → 5,881 gateway calls** (39% fewer) from caching
alone, on top of the no-op savings, with 1,920/1,920 clients correctly
detected as unchanged.

**Concurrency.** `MAX_WORKERS` (default `1`) syncs that many NetBox sites at
once. Pairs are grouped by *NetBox* site and a group is never split across
workers, so two UniFi sites consolidated into one NetBox site still run
sequentially together — the stale-marking union and single-writer-per-site
guarantee are structural, not something you have to configure correctly.
Because the work is round-trip-bound, speedup is near-linear until NetBox
becomes the bottleneck; against a simulated 2 ms/call NetBox, 12 sites went
1.94 s → 0.17 s at `MAX_WORKERS=12`. Raise it gradually and watch the
per-site metrics — the ceiling is your NetBox instance, not this tool.

**Partitioning across processes.** For very large estates, split `SITE_MAP`
across several containers so each owns a disjoint set of UniFi sites. Two
instances must never share a NetBox site — there is no distributed lock, so
they would race (see "Known limitations"). Partition on NetBox site
boundaries and the instances never touch the same objects.

**Where this still won't go.** Hundreds of sites with tens of thousands of
clients will remain round-trip-bound: the floor is a device lookup and an
interface lookup per client, which no amount of caching removes. Getting
below that needs bulk/`ORM` access — i.e. the NetBox-plugin path — rather
than more tuning of an API client.

## Troubleshooting

**`--dry-run` reports skipped cables.** The run tells you why, at WARNING
level, for each distinct switch:

- *"No NetBox device found for UniFi switch …"* — the switch isn't in NetBox
  under any of the four joins. See "Finding your switches", or turn on
  `SYNC_UNIFI_DEVICES=true` and let the tool create it.
- *"UniFi port N on 'X' matched no NetBox interface. Looked for […]; that
  device's interfaces are named […]"* — a naming mismatch. Copy a pattern
  from the reported names into `PORT_NAME_TEMPLATES`, e.g. for Junos-style
  `ge-0/0/1` ports:

  ```bash
  PORT_NAME_TEMPLATES=ge-0/0/{port}
  ```

Dry-run emits exactly the same diagnostics a real run would, so you can
resolve all of this before writing anything.

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
- **Per-client errors are narrowly caught.** The sync loop only catches
  expected network/API failures (`requests` exceptions, `pynetbox.RequestError`,
  `LookupError`) per client, logs them, and moves on to the next client. A
  genuine bug (e.g. an `AttributeError` from bad code) is *not* caught —
  it propagates and stops the run, on purpose, rather than being silently
  swallowed alongside real operational failures.

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
  active clients, infrastructure devices).
- `netbox_client.py` — `NetboxGateway` defines the interface the sync
  engine depends on; `PynetboxGateway` is the real implementation backed by
  [pynetbox](https://github.com/netbox-community/pynetbox). Tests substitute
  a fake implementing the same interface, so sync logic is tested without
  touching a real NetBox instance.
- `sync.py` — orchestrates the sync; all mutating NetBox calls are skipped
  in dry-run mode, with planned actions logged instead. Loops over
  `Settings.site_pairs()` (one pair for the default single-site case),
  calling `_run_site()` for each; a site-level failure (e.g. a `require`d
  missing NetBox site, an API error during `ensure_site()`/
  `ensure_prerequisites()`) is caught and recorded as an error for that
  site without stopping the others. Per-client errors within a site are
  caught the same way (see "Known limitations") — narrowly, so a real bug
  isn't masked as a routine sync failure. Stale-device marking is
  deliberately *not* done inside `_run_site()`: `run()` first groups every
  pair's `seen_macs` by NetBox site slug (so two pairs sharing a slug get
  the union, not each other's false positives), then marks stale devices
  once per distinct slug — skipping any slug whose pair(s) included a
  failure, since acting on an incomplete client list risks false offlines.
- `caching.py` — `CachingNetboxGateway`, a run-scoped read cache wrapping
  any gateway. Caches only what's stable for a run (sites, switches, switch
  interfaces); deliberately does *not* cache client-device or name-collision
  lookups, which change as the run creates devices. Thread-safe, since
  parallel site workers share one instance.
- `naming.py` — device-name sanitization, the deterministic MAC-suffix
  collision fallback, and NetBox slug generation.
- `devicetype_library.py` — optional index over a devicetype-library clone,
  plus the UniFi-media/speed to NetBox-interface-type mapping.
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
