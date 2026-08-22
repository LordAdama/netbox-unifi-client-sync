# netbox-unifi-client-sync

Syncs client devices and their switch-port cable connections from a
[Ubiquiti UniFi Network Controller](https://ui.com) into
[NetBox](https://netboxlabs.com/), so NetBox stays an accurate source of
truth for what's actually plugged into the network.

## What it does

For every active client the UniFi controller reports:

- Creates or updates a NetBox device (role `unifi-client` by default) with
  an interface (`eth0` for wired, `wlan0` for wireless).
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
in the run summary.

## Requirements

- Python 3.10+
- A UniFi controller account with read access (local admin or a
  read-only role is enough)
- A NetBox API token with permission to manage `dcim` and `ipam` objects
- A NetBox site (specified by slug) that the client devices should belong to

## Installation

```bash
pip install -e .
cp .env.example .env
# edit .env with your controller/NetBox details
```

## Usage

```bash
set -a; source .env; set +a
unifi-netbox-sync --dry-run   # see what would change, without writing anything
unifi-netbox-sync             # perform the sync
```

Run it on a schedule (cron, systemd timer, etc.) to keep NetBox continuously
up to date.

### Configuration

All configuration is via environment variables — see `.env.example` for the
full list and defaults. Key ones:

| Variable | Purpose |
| --- | --- |
| `UNIFI_HOST` | Base URL of the controller, e.g. `https://192.168.1.1` |
| `UNIFI_IS_UDM` | `true` for UDM/UDM-Pro/CloudKey Gen2+ (UniFi OS), `false` for a classic self-hosted controller |
| `UNIFI_SITE` | UniFi site name to sync (default `default`) |
| `NETBOX_URL` / `NETBOX_TOKEN` | NetBox API endpoint and token |
| `NETBOX_SITE_SLUG` | NetBox site clients should be placed in |
| `PORT_NAME_TEMPLATES` | Candidate interface-name patterns tried against your switches, in order |
| `CABLE_CONFLICT_POLICY` | `skip` (default, non-destructive) or `replace` when a target port is already cabled to something else |
| `MARK_STALE_OFFLINE` | Mark client devices no longer seen by UniFi as `offline` instead of leaving them `active` |
| `DRY_RUN` | Plan only, make no changes (same as `--dry-run`) |

### NetBox compatibility

Written against NetBox's generic cable-termination API
(`a_terminations`/`b_terminations`, NetBox 3.3+) and the `role` field name
on `dcim.devices` (NetBox 4.x). If you're on NetBox 3.x, rename `role` to
`device_role` in `netbox_client.py`'s device-creation payload.

## Development

```bash
pip install -e ".[dev]"
pytest
```

Tests mock both the UniFi HTTP API (via `responses`) and NetBox (via an
in-memory fake gateway in `tests/fakes.py`), so no live controller or
NetBox instance is required to run the suite.

## Design notes

- `unifi_client.py` — thin REST client for the UniFi controller (login,
  active clients, infrastructure devices).
- `netbox_client.py` — `NetboxGateway` defines the interface the sync
  engine depends on; `PynetboxGateway` is the real implementation backed by
  [pynetbox](https://github.com/netbox-community/pynetbox). Tests substitute
  a fake implementing the same interface, so sync logic is tested without
  touching a real NetBox instance.
- `sync.py` — orchestrates the sync; all mutating NetBox calls are skipped
  in dry-run mode, with planned actions logged instead.
- `cli.py` — entry point (`unifi-netbox-sync`).
