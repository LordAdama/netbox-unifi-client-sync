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

Switches themselves are **not created** by this tool — it assumes your
switches are already inventoried in NetBox (matched by the MAC address of
one of their interfaces) with interfaces named to match your switch's
physical port labels. Client devices, on the other hand, are fully managed
by the sync and tagged `unifi-sync` so they're easy to find and safe to
distinguish from manually-entered NetBox data.

## How matching works

| UniFi side | NetBox side |
| --- | --- |
| Switch MAC address | Looked up via any NetBox interface whose `mac_address` matches |
| Switch port index (`sw_port`) | Looked up on the matched switch device by trying each of `PORT_NAME_TEMPLATES` in order (e.g. `"4"`, `"Port 4"`, `"GE4"`, `"Gi4"`) until one matches an existing interface name |
| Client MAC address | Stored in a `unifi_mac` custom field on the NetBox client device, created automatically on first run |

If a switch or matching port can't be found, the client device is still
synced — only the cable is skipped, and the reason is logged and included
in the run summary. Every switch/port match (including which of the
candidate name templates matched) and every ambiguous match (a MAC that
appears on more than one NetBox interface) is logged at INFO/WARNING for
troubleshooting.

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

set -a; source .env; set +a
unifi-netbox-sync --dry-run   # see what would change, without writing anything
unifi-netbox-sync             # perform the sync
```

Run it on a schedule yourself (cron, systemd timer, etc.).

### Configuration

All configuration is via environment variables — see `.env.example` for the
full list and defaults. Key ones:

| Variable | Purpose |
| --- | --- |
| `UNIFI_HOST` | Base URL of the controller, e.g. `https://192.168.1.1` |
| `UNIFI_API_KEY` | Local UniFi OS API key; leave blank to use username/password instead (see "Authentication") |
| `UNIFI_IS_UDM` | `true` for UDM/UDM-Pro/CloudKey Gen2+ (UniFi OS), `false` for a classic self-hosted controller |
| `UNIFI_SITE` | UniFi site name to sync (default `default`) |
| `NETBOX_URL` / `NETBOX_TOKEN` | NetBox API endpoint and token |
| `NETBOX_SITE_SLUG` | NetBox site clients should be placed in |
| `NETBOX_IP_STATUS` | Status set on IP addresses this tool creates (default `active`) |
| `PORT_NAME_TEMPLATES` | Candidate interface-name patterns tried against your switches, in order |
| `CABLE_CONFLICT_POLICY` | `skip` (default, non-destructive) or `replace` when a target port is already cabled to something else |
| `MARK_STALE_OFFLINE` | Mark client devices no longer seen by UniFi as `offline` instead of leaving them `active` |
| `DRY_RUN` | Plan only, make no changes (same as `--dry-run`) |
| `LOG_FORMAT` | `text` (default) or `json` — one JSON object per log line, for log shippers |
| `METRICS_FILE` | If set, write Prometheus textfile-collector metrics to this path after every run |
| `SYNC_INTERVAL_SECONDS` | Docker only — `0`/unset runs once and exits, a positive number loops forever on that interval |
| `LOCK_FILE` | Docker only, optional — `flock` path to avoid two instances racing on the same NetBox (see "Known limitations") |
| `HEARTBEAT_FILE` | Docker only — path the healthcheck reads (default `/tmp/last-sync-ok`); override if `/tmp` is constrained in your setup |

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

## Known limitations

Documented rather than solved, because they're fine at the scale this was
built for (a home/SMB network) but worth knowing about before relying on it
for something bigger:

- **No distributed lock.** Two instances syncing the same NetBox
  concurrently can race on cable creation/deletion. The Docker entrypoint
  can guard against overlap between instances that share a `LOCK_FILE`
  path (e.g. two containers with the same bind-mounted volume), but that's
  opt-in and does nothing for instances that don't share the file — don't
  run multiple unrelated instances against the same NetBox.
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
  in dry-run mode, with planned actions logged instead. Per-client errors
  are caught narrowly (see "Known limitations") so a real bug isn't masked
  as a routine sync failure.
- `naming.py` — device-name sanitization and the deterministic
  MAC-suffix collision fallback.
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
