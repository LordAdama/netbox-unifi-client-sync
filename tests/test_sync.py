from __future__ import annotations

import pytest

from unifi_netbox_sync.config import Settings
from unifi_netbox_sync.sync import SyncEngine

from .fakes import FakeNetboxGateway, FakeUnifiClient, make_unifi_client


def make_settings(**overrides) -> Settings:
    defaults = dict(
        unifi_host="https://unifi.example.com",
        unifi_username="admin",
        unifi_password="secret",
        netbox_url="https://netbox.example.com",
        netbox_token="token",
        netbox_site_slug="main",
    )
    defaults.update(overrides)
    return Settings(**defaults)


def test_wired_client_creates_device_and_cable():
    netbox = FakeNetboxGateway()
    netbox.seed_switch("aa:bb:cc:dd:ee:01", "switch-1", ["1", "2", "3"])
    client = make_unifi_client(
        mac="11:22:33:44:55:66",
        name="laptop",
        is_wired=True,
        switch_mac="aa:bb:cc:dd:ee:01",
        switch_port=2,
    )
    unifi = FakeUnifiClient(clients=[client])
    engine = SyncEngine(unifi=unifi, netbox=netbox, settings=make_settings())

    summary = engine.run()

    assert summary.devices_created == 1
    assert summary.cables_created == 1
    assert summary.errors == []
    device = netbox.clients_by_mac["11:22:33:44:55:66"]
    assert device.interfaces["eth0"].cable is not None


def test_name_collision_with_different_mac_gets_disambiguated():
    netbox = FakeNetboxGateway()
    netbox.upsert_client_device(
        "aa:aa:aa:aa:aa:aa", "laptop", "main", "unifi-client", "generic-network-client", "unifi-sync"
    )
    client = make_unifi_client(mac="11:22:33:44:55:66", name="laptop", is_wired=False)
    unifi = FakeUnifiClient(clients=[client])
    engine = SyncEngine(unifi=unifi, netbox=netbox, settings=make_settings())

    summary = engine.run()

    assert summary.devices_created == 1
    new_device = netbox.clients_by_mac["11:22:33:44:55:66"]
    assert new_device.name == "laptop-5566"
    assert netbox.clients_by_mac["aa:aa:aa:aa:aa:aa"].name == "laptop"  # untouched


def test_same_client_keeps_its_own_name_across_runs():
    netbox = FakeNetboxGateway()
    client = make_unifi_client(mac="11:22:33:44:55:66", name="laptop", is_wired=False)
    unifi = FakeUnifiClient(clients=[client])
    engine = SyncEngine(unifi=unifi, netbox=netbox, settings=make_settings())

    engine.run()
    summary = engine.run()  # second run: same client, same name — not a collision with itself

    # Nothing differs, so nothing is written: reported as unchanged, not updated.
    assert summary.devices_updated == 0
    assert summary.clients_unchanged == 1
    assert netbox.clients_by_mac["11:22:33:44:55:66"].name == "laptop"


def test_wireless_client_has_no_cable():
    netbox = FakeNetboxGateway()
    client = make_unifi_client(mac="11:22:33:44:55:77", name="phone", is_wired=False)
    unifi = FakeUnifiClient(clients=[client])
    engine = SyncEngine(unifi=unifi, netbox=netbox, settings=make_settings())

    summary = engine.run()

    assert summary.devices_created == 1
    assert summary.cables_created == 0
    device = netbox.clients_by_mac["11:22:33:44:55:77"]
    assert "wlan0" in device.interfaces
    assert device.interfaces["wlan0"].cable is None


def test_cable_skipped_when_switch_missing():
    netbox = FakeNetboxGateway()  # no switches seeded
    client = make_unifi_client(
        mac="11:22:33:44:55:88",
        is_wired=True,
        switch_mac="aa:bb:cc:dd:ee:99",
        switch_port=5,
    )
    unifi = FakeUnifiClient(clients=[client])
    engine = SyncEngine(unifi=unifi, netbox=netbox, settings=make_settings())

    summary = engine.run()

    assert summary.cables_created == 0
    assert summary.cables_skipped == 1
    assert "switch" in summary.client_results[0].cable_skipped_reason


def test_cable_skipped_when_port_name_does_not_match_templates():
    netbox = FakeNetboxGateway()
    # A naming scheme none of the default templates produce.
    netbox.seed_switch("aa:bb:cc:dd:ee:01", "switch-1", ["xe-0/0/1"])
    client = make_unifi_client(
        mac="11:22:33:44:55:66",
        is_wired=True,
        switch_mac="aa:bb:cc:dd:ee:01",
        switch_port=1,
    )
    unifi = FakeUnifiClient(clients=[client])
    engine = SyncEngine(unifi=unifi, netbox=netbox, settings=make_settings())

    summary = engine.run()

    assert summary.cables_created == 0
    assert summary.cables_skipped == 1


def test_dry_run_does_not_mutate_netbox():
    netbox = FakeNetboxGateway()
    netbox.seed_switch("aa:bb:cc:dd:ee:01", "switch-1", ["1"])
    client = make_unifi_client(
        mac="11:22:33:44:55:66",
        is_wired=True,
        switch_mac="aa:bb:cc:dd:ee:01",
        switch_port=1,
    )
    unifi = FakeUnifiClient(clients=[client])
    engine = SyncEngine(unifi=unifi, netbox=netbox, settings=make_settings(dry_run=True))

    summary = engine.run()

    assert summary.devices_created == 1  # planned, not actually created
    assert summary.cables_created == 1  # planned
    assert netbox.clients_by_mac == {}  # nothing actually written
    assert netbox.ensure_prerequisites_called is False


class RaisingNetboxGateway(FakeNetboxGateway):
    """Fake whose upsert_client_device always raises a given exception."""

    def __init__(self, exc: Exception) -> None:
        super().__init__()
        self._exc = exc

    def upsert_client_device(self, *args, **kwargs):
        raise self._exc


def test_expected_error_is_captured_per_client():
    netbox = RaisingNetboxGateway(LookupError("NetBox site 'main' does not exist"))
    client = make_unifi_client(mac="11:22:33:44:55:66", is_wired=False)
    unifi = FakeUnifiClient(clients=[client])
    engine = SyncEngine(unifi=unifi, netbox=netbox, settings=make_settings())

    summary = engine.run()

    assert len(summary.errors) == 1
    assert "does not exist" in summary.errors[0]
    assert summary.client_results[0].error is not None


def test_unexpected_error_propagates_instead_of_being_swallowed():
    netbox = RaisingNetboxGateway(TypeError("programming error, not a network/API failure"))
    client = make_unifi_client(mac="11:22:33:44:55:66", is_wired=False)
    unifi = FakeUnifiClient(clients=[client])
    engine = SyncEngine(unifi=unifi, netbox=netbox, settings=make_settings())

    with pytest.raises(TypeError):
        engine.run()


def test_metrics_file_written_when_configured(tmp_path):
    netbox = FakeNetboxGateway()
    client = make_unifi_client(mac="11:22:33:44:55:66", is_wired=False)
    unifi = FakeUnifiClient(clients=[client])
    metrics_path = tmp_path / "metrics.prom"
    engine = SyncEngine(
        unifi=unifi, netbox=netbox, settings=make_settings(metrics_file=str(metrics_path))
    )

    engine.run()

    content = metrics_path.read_text()
    assert "unifi_netbox_sync_clients_seen 1" in content
    assert "unifi_netbox_sync_last_run_success 1" in content


def test_stale_client_marked_offline():
    netbox = FakeNetboxGateway()
    stale_device, *_ = netbox.upsert_client_device(
        "99:88:77:66:55:44", "old-laptop", "main", "unifi-client", "generic-network-client", "unifi-sync"
    )
    unifi = FakeUnifiClient(clients=[])  # UniFi no longer reports this client
    engine = SyncEngine(unifi=unifi, netbox=netbox, settings=make_settings())

    summary = engine.run()

    assert summary.stale_marked_offline == 1
    assert stale_device.status.value == "offline"
