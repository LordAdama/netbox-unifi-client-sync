"""Prefetch and the unchanged fast path must not change sync decisions."""

from __future__ import annotations

import logging
import time

import responses

from unifi_netbox_sync.caching import CachingNetboxGateway
from unifi_netbox_sync.models import UnifiKnownClient, UnifiPort, UnifiSwitchDevice
from unifi_netbox_sync.netbox_client import TimeoutSession
from unifi_netbox_sync.sync import SyncEngine

from .fakes import CountingNetboxGateway, FakeNetboxGateway, FakeUnifiClient, make_unifi_client
from .test_sync import make_settings

SW = "02:aa:00:00:00:01"
ODD = "02:aa:00:00:00:02"
NOW = time.time()
DAY = 24 * 3600


def _mac(n: int) -> str:
    return f"02:00:00:00:00:{n:02x}"


def _seed():
    """A small estate covering creates, skips, cables, and offline decisions."""
    netbox = FakeNetboxGateway()
    netbox.seed_site("site-alpha", "Site Alpha")
    netbox.seed_site("site-beta", "Site Beta")
    switch = netbox.seed_switch(SW, "sw-beta", [str(port) for port in range(1, 9)])
    switch.site_slug = "site-beta"
    odd = netbox.seed_switch(ODD, "sw-odd", ["mgmt", "irb"])
    odd.site_slug = "site-beta"

    def existing(mac, name, site, ip=None, wired=False, switch_mac=None, port=None, tag="unifi-sync"):
        device, *_ = netbox.upsert_client_device(
            mac, name, site, "unifi-client", "generic-network-client", tag
        )
        iface_name = "eth0" if wired else "wlan0"
        iface = netbox.ensure_interface(device, iface_name, wired)
        if ip:
            netbox.assign_ip(device, iface, ip, "active", tag)
        if wired and switch_mac and port is not None:
            sw = netbox.switches[switch_mac]
            netbox.ensure_cable(iface, sw.interfaces[str(port)], "skip", tag)
        return device

    existing(_mac(0x02), "steady-wireless", "site-alpha", ip="10.1.0.2", wired=False)
    existing(_mac(0x03), "old-laptop", "site-alpha", ip="10.1.0.3", wired=False)
    existing(_mac(0x04), "hand-built", "site-alpha", ip="10.1.0.4", wired=False, tag="manual")
    existing(_mac(0x06), "taken-name", "site-alpha", ip="10.1.0.6", wired=False)
    existing(_mac(0x07), "gone-laptop", "site-alpha", ip="10.1.0.7", wired=False)
    existing(_mac(0x08), "asleep-laptop", "site-alpha", ip="10.1.0.8", wired=False)
    existing(_mac(0x09), "timeless-laptop", "site-alpha", ip="10.1.0.9", wired=False)
    existing(_mac(0x0B), "steady-wired", "site-beta", ip="10.2.0.11", wired=True, switch_mac=SW, port=4)
    existing(_mac(0x12), "moved-ip", "site-beta", ip="10.2.0.90", wired=False)
    existing(_mac(0x13), "roams", "site-alpha", ip="10.1.0.19", wired=False)

    holder, *_ = netbox.upsert_client_device(
        _mac(0x20), "port-holder", "site-beta", "unifi-client", "generic-network-client", "unifi-sync"
    )
    holder_iface = netbox.ensure_interface(holder, "eth0", True)
    netbox.ensure_cable(holder_iface, switch.interfaces["3"], "skip", "unifi-sync")

    alpha_clients = [
        make_unifi_client(mac=_mac(0x01), name="new-wireless", ip="10.1.0.1", is_wired=False),
        make_unifi_client(mac=_mac(0x02), name="steady-wireless", ip="10.1.0.2", is_wired=False),
        make_unifi_client(mac=_mac(0x03), name="renamed-laptop", ip="10.1.0.3", is_wired=False),
        make_unifi_client(mac=_mac(0x04), name="hand-built", ip="10.1.0.4", is_wired=False),
        make_unifi_client(mac=_mac(0x05), name="taken-name", ip="10.1.0.5", is_wired=False),
    ]
    beta_clients = [
        make_unifi_client(
            mac=_mac(0x0A), name="new-wired", ip="10.2.0.10", is_wired=True, switch_mac=SW, switch_port=1
        ),
        make_unifi_client(
            mac=_mac(0x0B), name="steady-wired", ip="10.2.0.11", is_wired=True, switch_mac=SW, switch_port=4
        ),
        make_unifi_client(
            mac=_mac(0x0C), name="shared-a", ip="10.2.0.12", is_wired=True, switch_mac=SW, switch_port=2
        ),
        make_unifi_client(
            mac=_mac(0x0D), name="shared-b", ip="10.2.0.13", is_wired=True, switch_mac=SW, switch_port=2
        ),
        make_unifi_client(
            mac=_mac(0x0E), name="on-uplink", ip="10.2.0.14", is_wired=True, switch_mac=SW, switch_port=8
        ),
        make_unifi_client(
            mac=_mac(0x0F),
            name="no-switch",
            ip="10.2.0.15",
            is_wired=True,
            switch_mac="02:ff:00:00:00:01",
            switch_port=1,
        ),
        make_unifi_client(
            mac=_mac(0x10), name="odd-port", ip="10.2.0.16", is_wired=True, switch_mac=ODD, switch_port=1
        ),
        make_unifi_client(
            mac=_mac(0x11), name="busy-port", ip="10.2.0.17", is_wired=True, switch_mac=SW, switch_port=3
        ),
        make_unifi_client(mac=_mac(0x12), name="moved-ip", ip="10.2.0.91", is_wired=False),
        make_unifi_client(mac=_mac(0x13), name="roams", ip="10.1.0.19", is_wired=False),
    ]
    devices = [
        UnifiSwitchDevice(
            mac=SW,
            name="sw-beta",
            model="USW-24",
            device_type="usw",
            serial="SERIAL-BETA",
            ports=[UnifiPort(index=8, name="Uplink", is_uplink=True)],
        )
    ]
    known = {
        "alpha": [
            UnifiKnownClient(mac=_mac(0x07), last_seen=NOW - 30 * DAY),
            UnifiKnownClient(mac=_mac(0x08), last_seen=NOW - DAY),
            UnifiKnownClient(mac=_mac(0x09), last_seen=None),
            UnifiKnownClient(mac=_mac(0x06), last_seen=NOW),
            UnifiKnownClient(mac=_mac(0x13), last_seen=NOW),
        ],
        "beta": [UnifiKnownClient(mac=_mac(0x20), last_seen=NOW)],
    }
    unifi = FakeUnifiClient(
        by_site={"alpha": alpha_clients, "beta": beta_clients},
        devices_by_site={"beta": devices},
        known_by_site=known,
    )
    return netbox, unifi


def _fingerprint(summary, netbox):
    rows = []
    for result in summary.client_results or []:
        skip = None
        if result.cable_skip is not None:
            skip = (
                result.cable_skip.unifi_site,
                result.cable_skip.netbox_site,
                result.cable_skip.switch_mac,
                result.cable_skip.unifi_port,
                result.cable_skip.reason,
            )
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
                skip,
            )
        )
    devices = []
    for mac, device in sorted(netbox.clients_by_mac.items()):
        cables = []
        for name, iface in sorted(device.interfaces.items()):
            if not iface.link_peers:
                continue
            peers = tuple(sorted(peer.name for peer in iface.link_peers))
            cables.append((name, peers))
        devices.append(
            (
                mac,
                device.name,
                device.site_slug,
                device.status.value,
                tuple(device.tags),
                getattr(device.primary_ip4, "address", None),
                tuple(cables),
            )
        )
    counts = (
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
        tuple(sorted(summary.errors or [])),
    )
    return counts, tuple(sorted(rows)), tuple(devices)


def _run(dry_run, prefetch, fast_path, workers):
    netbox, unifi = _seed()
    engine = SyncEngine(
        unifi=unifi,
        netbox=CachingNetboxGateway(netbox),
        settings=make_settings(
            site_map={"alpha": "site-alpha", "beta": "site-beta"},
            dry_run=dry_run,
            netbox_prefetch=prefetch,
            netbox_fast_path=fast_path,
            max_workers=workers,
            stale_offline_grace_seconds=7 * DAY,
        ),
    )
    summary = engine.run()
    return _fingerprint(summary, netbox), summary


def test_prefetch_and_fast_path_match_the_per_client_path():
    """Creates, updates, skips, cables, and offline decisions stay the same."""
    for dry_run in (False, True):
        baseline, _ = _run(dry_run, prefetch=False, fast_path=False, workers=1)
        indexed, indexed_summary = _run(dry_run, prefetch=True, fast_path=False, workers=1)
        fast, fast_summary = _run(dry_run, prefetch=True, fast_path=True, workers=1)
        parallel, _ = _run(dry_run, prefetch=True, fast_path=True, workers=4)
        assert indexed == baseline
        assert fast == baseline
        assert parallel == baseline
        if not dry_run:
            assert fast_summary.clients_fast_path > 0
            assert indexed_summary.prefetch_seconds >= 0


def test_sync_policy_rename_still_matches_with_prefetch():
    unifi = FakeUnifiClient(
        by_site={"alpha": [make_unifi_client(mac=_mac(0x03), name="renamed-laptop", ip=None, is_wired=False)]}
    )

    def once(prefetch, fast_path):
        fresh, _ = _seed_rename()
        engine = SyncEngine(
            unifi=unifi,
            netbox=CachingNetboxGateway(fresh),
            settings=make_settings(
                site_map={"alpha": "site-alpha"},
                device_update_policy="sync",
                netbox_prefetch=prefetch,
                netbox_fast_path=fast_path,
                max_workers=1,
                mark_stale_offline=False,
            ),
        )
        summary = engine.run()
        device = fresh.clients_by_mac[_mac(0x03)]
        return summary.devices_updated, summary.devices_created, device.name

    def _seed_rename():
        box = FakeNetboxGateway()
        box.seed_site("site-alpha", "Site Alpha")
        box.upsert_client_device(
            _mac(0x03), "old-laptop", "site-alpha", "unifi-client", "generic-network-client", "unifi-sync"
        )
        return box, None

    assert once(False, False) == once(True, True) == (1, 0, "renamed-laptop")


def test_steady_state_avoids_per_client_lookups():
    """A quiet re-run is bulk loads plus one switch lookup, not one GET per client."""
    sites = 4
    per_site = 30
    netbox = FakeNetboxGateway()
    by_site = {}
    site_map = {}
    for i in range(sites):
        unifi_site = f"site-{i}"
        slug = f"nb-{i}"
        site_map[unifi_site] = slug
        netbox.seed_site(slug, f"Site {i}")
        switch_mac = f"02:bb:00:00:00:{i:02x}"
        switch = netbox.seed_switch(switch_mac, f"sw-{i}", [str(port) for port in range(1, 25)])
        switch.site_slug = slug
        clients = []
        for c in range(per_site):
            wired = c % 2 == 0
            mac = f"02:10:{i:02x}:00:00:{c:02x}"
            client = make_unifi_client(
                mac=mac,
                name=f"c-{i}-{c}",
                ip=f"10.{i}.0.{c}",
                is_wired=wired,
                switch_mac=switch_mac if wired else None,
                switch_port=(c // 2) + 1 if wired else None,
            )
            clients.append(client)
            device, *_ = netbox.upsert_client_device(
                mac, client.display_name, slug, "unifi-client", "generic-network-client", "unifi-sync"
            )
            iface = netbox.ensure_interface(device, "eth0" if wired else "wlan0", wired)
            netbox.assign_ip(device, iface, client.ip, "active", "unifi-sync")
            if wired:
                netbox.ensure_cable(iface, switch.interfaces[str(client.switch_port)], "skip", "unifi-sync")
        by_site[unifi_site] = clients

    counting = CountingNetboxGateway(netbox)
    summary = SyncEngine(
        unifi=FakeUnifiClient(by_site=by_site),
        netbox=CachingNetboxGateway(counting),
        settings=make_settings(
            site_map=site_map,
            dry_run=True,
            netbox_prefetch=True,
            netbox_fast_path=True,
            max_workers=1,
        ),
    ).run()

    assert summary.errors == []
    assert summary.devices_created == 0
    assert summary.cables_created == 0
    assert counting.calls["find_client_device_by_mac"] == 0
    assert counting.calls["device_name_taken_by_other"] == 0
    assert counting.calls["ensure_interface"] == 0
    assert counting.calls["upsert_client_device"] == 0
    assert counting.calls["assign_ip"] == 0
    assert counting.calls["find_switch_device_by_mac"] == sites
    assert summary.clients_fast_path == sites * per_site


def test_disabling_prefetch_restores_per_client_lookups():
    netbox = FakeNetboxGateway()
    netbox.seed_site("main", "Main")
    client = make_unifi_client(mac=_mac(1), name="laptop", ip="10.0.0.5", is_wired=False)
    device, *_ = netbox.upsert_client_device(
        client.mac, "laptop", "main", "unifi-client", "generic-network-client", "unifi-sync"
    )
    netbox.ensure_interface(device, "wlan0", False)
    netbox.assign_ip(device, device.interfaces["wlan0"], client.ip, "active", "unifi-sync")
    counting = CountingNetboxGateway(netbox)
    SyncEngine(
        unifi=FakeUnifiClient(clients=[client]),
        netbox=CachingNetboxGateway(counting),
        settings=make_settings(netbox_prefetch=False, netbox_fast_path=False, dry_run=True),
    ).run()
    assert counting.calls["find_client_device_by_mac"] == 1


def test_timeout_session_retries_429_then_returns_success():
    with responses.RequestsMock() as mocked:
        mocked.add(responses.GET, "https://netbox.example/api/dcim/devices/", status=429, headers={"Retry-After": "0"})
        mocked.add(responses.GET, "https://netbox.example/api/dcim/devices/", json={"results": []}, status=200)
        session = TimeoutSession(timeout=5, max_inflight=2, rate_limit_retries=3)
        response = session.get("https://netbox.example/api/dcim/devices/")
    assert response.status_code == 200
    assert session.requests_made == 2


def test_timeout_session_stops_after_the_retry_budget():
    with responses.RequestsMock() as mocked:
        mocked.add(responses.GET, "https://netbox.example/api/dcim/devices/", status=429, headers={"Retry-After": "0"})
        mocked.add(responses.GET, "https://netbox.example/api/dcim/devices/", status=429, headers={"Retry-After": "0"})
        session = TimeoutSession(timeout=5, max_inflight=1, rate_limit_retries=1)
        response = session.get("https://netbox.example/api/dcim/devices/")
    assert response.status_code == 429
    assert session.requests_made == 2


def test_timing_summary_is_logged(caplog):
    netbox, unifi = _seed()
    engine = SyncEngine(
        unifi=unifi,
        netbox=CachingNetboxGateway(netbox),
        settings=make_settings(
            site_map={"alpha": "site-alpha", "beta": "site-beta"},
            dry_run=True,
            max_workers=1,
        ),
    )
    with caplog.at_level(logging.INFO):
        summary = engine.run()
    assert summary.prefetch_seconds >= 0
    text = "\n".join(record.getMessage() for record in caplog.records)
    assert "Sync timing:" in text
    assert "prefetch" in text
    assert "Slowest sites:" in text
