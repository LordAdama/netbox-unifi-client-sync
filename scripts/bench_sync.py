#!/usr/bin/env python3
"""Time a full sync on a synthetic estate, with and without the bulk index.

The estate is 39 sites and 1,084 clients. Names, MACs, and addresses are
synthetic (locally administered 02: MACs, 10.0.0.0/8 documentation-style
addresses). Nothing here is a real network.

Two clocks are reported:

- Measured wall time with a uniform injected delay per modeled NetBox HTTP
  call, so concurrency shows up in the clock.
- A serial estimate that prices the one slow call from production profiles
  (a per-client ``cf_unifi_mac`` filter) at 1.5s, each bulk page at 0.2s, and
  every other call at 0.05s. That is the shape behind a ~28 minute dry-run
  at one site worker. It is not a live NetBox measurement.

Dry-run client devices hide ``interfaces`` behind an object that is not a
dict, matching a pynetbox record. The planner therefore cannot see an
existing client-side cable, which is what a real dry-run does.
"""

from __future__ import annotations

import argparse
import logging
import sys
import time
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "tests"))

from unifi_netbox_sync.caching import CachingNetboxGateway  # noqa: E402
from unifi_netbox_sync.config import Settings  # noqa: E402
from unifi_netbox_sync.sync import SyncEngine  # noqa: E402
from fakes import FakeNetboxGateway, FakeUnifiClient, make_unifi_client  # noqa: E402


def make_settings(**overrides) -> Settings:
    defaults = dict(
        unifi_host="https://unifi.example.com",
        unifi_username="admin",
        unifi_password="secret",
        netbox_url="https://netbox.example.com",
        netbox_token="token",
        netbox_site_slug="main",
        dry_run=True,
    )
    defaults.update(overrides)
    return Settings(**defaults)

SITES = 39
# 31 * 28 + 8 * 27 = 1,084 clients.
CLIENTS_PER_SITE = tuple(28 if i < 31 else 27 for i in range(SITES))
PAGE_SIZE = 1000
INTERFACE_CHUNK = 50
# Production profile: one unindexed custom-field filter per client.
SLOW_SECONDS = 1.5
BULK_PAGE_SECONDS = 0.20
OTHER_SECONDS = 0.05

IN_MEMORY = {
    "match_client_device",
    "device_name_taken_among",
    "select_synced_clients",
    "note_prefetched_devices",
    "note_prefetched_interfaces",
}
BULK = {
    "bulk_devices_with_unifi_mac",
    "bulk_devices_at_site",
    "bulk_interfaces",
}
SLOW = {"find_client_device_by_mac"}


class _IfaceView:
    """Interface map that is not a dict, like a pynetbox device."""

    def __init__(self, mapping: dict) -> None:
        self._mapping = mapping

    def values(self):
        return self._mapping.values()

    def get(self, key, default=None):
        return self._mapping.get(key, default)

    def __contains__(self, key: object) -> bool:
        return key in self._mapping

    def __getitem__(self, key: str):
        return self._mapping[key]

    def __setitem__(self, key: str, value: object) -> None:
        self._mapping[key] = value


class HttpModel:
    """Counts modeled NetBox HTTP calls and optionally sleeps for each one."""

    def __init__(self, inner, delay: float) -> None:
        self._inner = inner
        self.delay = delay
        self.calls: Counter[str] = Counter()
        self.http: Counter[str] = Counter()

    def __getattr__(self, name: str):
        attr = getattr(self._inner, name)
        if not callable(attr) or name.startswith("_"):
            return attr

        def wrapped(*args, **kwargs):
            self.calls[name] += 1
            result = attr(*args, **kwargs)
            cost = self._cost(name, args, result)
            if cost:
                self.http[name] += cost
                if self.delay:
                    time.sleep(self.delay * cost)
            return result

        return wrapped

    @staticmethod
    def _cost(name: str, args: tuple, result: object) -> int:
        if name in IN_MEMORY:
            return 0
        if name == "bulk_devices_with_unifi_mac":
            count = len(result or [])
            return max(1, (count + PAGE_SIZE - 1) // PAGE_SIZE)
        if name == "bulk_devices_at_site":
            return 1
        if name == "bulk_interfaces":
            ids = args[0] if args else []
            if not ids:
                return 0
            return (len(ids) + INTERFACE_CHUNK - 1) // INTERFACE_CHUNK
        return 1

    @property
    def http_total(self) -> int:
        return sum(self.http.values())


def _mac(site: int, client: int) -> str:
    return f"02:{site:02x}:{client:02x}:00:00:01"


def _switch_mac(site: int) -> str:
    return f"02:aa:00:00:{site:02x}:01"


def build_estate(populated: bool, hide_client_interfaces: bool):
    netbox = FakeNetboxGateway()
    by_site: dict[str, list] = {}
    site_map: dict[str, str] = {}
    for site in range(SITES):
        unifi_site = f"site-{site:02d}"
        slug = f"nb-site-{site:02d}"
        site_map[unifi_site] = slug
        netbox.seed_site(slug, f"Site {site:02d}")
        switch_mac = _switch_mac(site)
        switch = netbox.seed_switch(switch_mac, f"sw-{site:02d}", [str(port) for port in range(1, 49)])
        switch.site_slug = slug
        clients = []
        for client in range(CLIENTS_PER_SITE[site]):
            wired = client % 3 == 0
            mac = _mac(site, client)
            record = make_unifi_client(
                mac=mac,
                name=f"client-{site:02d}-{client:02d}",
                ip=f"10.{site}.0.{client + 1}",
                is_wired=wired,
                switch_mac=switch_mac if wired else None,
                switch_port=(client // 3) + 1 if wired else None,
            )
            clients.append(record)
            if not populated:
                continue
            device, *_ = netbox.upsert_client_device(
                mac,
                record.display_name,
                slug,
                "unifi-client",
                "generic-network-client",
                "unifi-sync",
            )
            iface_name = "eth0" if wired else "wlan0"
            iface = netbox.ensure_interface(device, iface_name, wired)
            netbox.assign_ip(device, iface, record.ip, "active", "unifi-sync")
            if wired:
                netbox.ensure_cable(iface, switch.interfaces[str(record.switch_port)], "skip", "unifi-sync")
            if hide_client_interfaces:
                device.interfaces = _IfaceView(device.interfaces)
        by_site[unifi_site] = clients
    return netbox, FakeUnifiClient(by_site=by_site), site_map


def _fingerprint(summary) -> tuple:
    rows = []
    for result in summary.client_results or []:
        rows.append(
            (
                result.mac,
                result.name,
                result.device_created,
                result.device_updated,
                result.ip_assigned,
                result.cable_created,
                result.cable_skipped_reason,
                result.device_update_skipped_reason,
                result.ip_unchanged,
                result.cable_unchanged,
                result.unchanged,
                result.error,
            )
        )
    skips = tuple(
        sorted(
            (skip.reason, skip.unifi_site, skip.netbox_site, skip.switch, skip.switch_mac, skip.unifi_port)
            for skip in (summary.cable_skips or [])
        )
    )
    totals = (
        summary.clients_seen,
        summary.devices_created,
        summary.devices_updated,
        summary.devices_update_skipped,
        summary.cables_created,
        summary.cables_skipped,
        summary.stale_marked_offline,
        summary.clients_unchanged,
        summary.ips_unchanged,
        summary.cables_unchanged,
        tuple(summary.errors or []),
    )
    return totals, tuple(sorted(rows)), skips


def _serial_estimate(http: Counter[str]) -> float:
    slow = sum(http[name] for name in SLOW)
    bulk = sum(http[name] for name in BULK)
    other = sum(http.values()) - slow - bulk
    return slow * SLOW_SECONDS + bulk * BULK_PAGE_SECONDS + other * OTHER_SECONDS


def run_once(populated: bool, hide: bool, prefetch: bool, fast_path: bool, workers: int, delay: float, dry_run: bool):
    netbox, unifi, site_map = build_estate(populated, hide)
    counted = HttpModel(netbox, delay)
    started = time.perf_counter()
    summary = SyncEngine(
        unifi=unifi,
        netbox=CachingNetboxGateway(counted),
        settings=make_settings(
            site_map=site_map,
            dry_run=dry_run,
            netbox_prefetch=prefetch,
            netbox_fast_path=fast_path,
            max_workers=workers,
            netbox_page_size=PAGE_SIZE,
        ),
    ).run()
    elapsed = time.perf_counter() - started
    return summary, counted, elapsed


def _fmt(seconds: float) -> str:
    if seconds >= 60:
        return f"{seconds / 60:.1f} min"
    return f"{seconds:.2f} s"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--delay", type=float, default=0.01, help="seconds injected per modeled HTTP call")
    args = parser.parse_args()
    logging.disable(logging.CRITICAL)

    cases = [
        ("populated dry-run", True, True, True),
        ("empty dry-run", False, False, True),
        ("populated apply", True, False, False),
    ]
    modes = [
        ("before, 1 worker", False, False, 1),
        ("after, 1 worker", True, True, 1),
        ("after, 4 workers", True, True, 4),
    ]
    print(f"Synthetic estate: {SITES} sites, {sum(CLIENTS_PER_SITE)} clients")
    print(f"Injected delay: {args.delay * 1000:.0f} ms per modeled HTTP call")
    print(
        f"Serial estimate prices cf_unifi_mac at {SLOW_SECONDS:.1f}s, "
        f"a bulk page at {BULK_PAGE_SECONDS:.2f}s, other calls at {OTHER_SECONDS:.2f}s"
    )
    print()
    header = (
        f"{'case':<22} {'mode':<20} {'http':>6} {'cf':>6} {'bulk':>6} "
        f"{'fast':>6} {'wall':>10} {'serial est.':>12}"
    )
    print(header)
    print("-" * len(header))
    failed = False
    for label, populated, hide, dry_run in cases:
        baseline = None
        for mode, prefetch, fast_path, workers in modes:
            summary, counted, elapsed = run_once(
                populated, hide, prefetch, fast_path, workers, args.delay, dry_run
            )
            fingerprint = _fingerprint(summary)
            if baseline is None:
                baseline = fingerprint
            elif fingerprint != baseline:
                failed = True
                print(f"MISMATCH {label} / {mode}")
            cf = counted.http["find_client_device_by_mac"]
            bulk = sum(counted.http[name] for name in BULK)
            print(
                f"{label:<22} {mode:<20} {counted.http_total:6d} {cf:6d} {bulk:6d} "
                f"{summary.clients_fast_path:6d} {_fmt(elapsed):>10} {_fmt(_serial_estimate(counted.http)):>12}"
            )
            if mode.startswith("before"):
                interesting = ("find_client_device_by_mac", "device_name_taken_by_other", "find_switch_device_by_mac", "list_device_interfaces", "list_synced_client_devices", "upsert_client_device", "ensure_interface")
            else:
                interesting = tuple(BULK) + ("find_switch_device_by_mac", "list_device_interfaces", "find_client_device_by_mac", "upsert_client_device", "ensure_interface")
            detail = ", ".join(f"{name}={counted.http[name]}" for name in interesting if counted.http[name])
            print(f"{'':<22} {detail}")
        print()
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
