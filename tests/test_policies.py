from __future__ import annotations

from unifi_netbox_sync.sync import SyncEngine

from .fakes import FakeNetboxGateway, FakeUnifiClient, make_unifi_client
from .test_sync import make_settings


def test_require_policy_fails_when_site_missing():
    netbox = FakeNetboxGateway()
    netbox.missing_sites.add("main")
    client = make_unifi_client(mac="11:22:33:44:55:66", is_wired=False)
    unifi = FakeUnifiClient(clients=[client])
    engine = SyncEngine(unifi=unifi, netbox=netbox, settings=make_settings(site_policy="require"))

    summary = engine.run()  # does not raise: recorded as a site-level error instead

    assert len(summary.errors) == 1
    assert "main" in summary.errors[0]
    assert summary.devices_created == 0
    assert netbox.clients_by_mac == {}


def test_create_policy_creates_missing_site_and_proceeds():
    netbox = FakeNetboxGateway()
    netbox.missing_sites.add("main")
    client = make_unifi_client(mac="11:22:33:44:55:66", is_wired=False)
    unifi = FakeUnifiClient(clients=[client])
    engine = SyncEngine(unifi=unifi, netbox=netbox, settings=make_settings(site_policy="create"))

    summary = engine.run()

    assert summary.sites_created == 1
    assert summary.devices_created == 1
    assert "main" in netbox.created_sites


def test_create_policy_does_not_touch_an_existing_site():
    netbox = FakeNetboxGateway()  # "main" exists by default (not in missing_sites)
    client = make_unifi_client(mac="11:22:33:44:55:66", is_wired=False)
    unifi = FakeUnifiClient(clients=[client])
    engine = SyncEngine(unifi=unifi, netbox=netbox, settings=make_settings(site_policy="create"))

    summary = engine.run()

    assert summary.sites_created == 0
    assert "main" not in netbox.created_sites


def test_dry_run_warns_but_does_not_raise_when_site_missing_and_policy_require():
    netbox = FakeNetboxGateway()
    netbox.missing_sites.add("main")
    client = make_unifi_client(mac="11:22:33:44:55:66", is_wired=False)
    unifi = FakeUnifiClient(clients=[client])
    engine = SyncEngine(
        unifi=unifi, netbox=netbox, settings=make_settings(site_policy="require", dry_run=True)
    )

    summary = engine.run()  # must not raise in dry-run

    assert summary.sites_created == 0


def test_dry_run_previews_site_creation_under_create_policy():
    netbox = FakeNetboxGateway()
    netbox.missing_sites.add("main")
    client = make_unifi_client(mac="11:22:33:44:55:66", is_wired=False)
    unifi = FakeUnifiClient(clients=[client])
    engine = SyncEngine(
        unifi=unifi, netbox=netbox, settings=make_settings(site_policy="create", dry_run=True)
    )

    summary = engine.run()

    assert summary.sites_created == 1  # planned, not actually created
    assert "main" not in netbox.created_sites  # dry-run: nothing actually written


def test_create_only_policy_leaves_existing_device_name_unchanged():
    netbox = FakeNetboxGateway()
    client_v1 = make_unifi_client(mac="11:22:33:44:55:66", name="old-name", is_wired=False)
    engine = SyncEngine(
        unifi=FakeUnifiClient(clients=[client_v1]),
        netbox=netbox,
        settings=make_settings(device_update_policy="create-only"),
    )
    engine.run()
    assert netbox.clients_by_mac["11:22:33:44:55:66"].name == "old-name"

    client_v2 = make_unifi_client(mac="11:22:33:44:55:66", name="new-name", is_wired=False)
    engine2 = SyncEngine(
        unifi=FakeUnifiClient(clients=[client_v2]),
        netbox=netbox,
        settings=make_settings(device_update_policy="create-only"),
    )
    summary = engine2.run()

    assert netbox.clients_by_mac["11:22:33:44:55:66"].name == "old-name"  # untouched
    assert summary.devices_update_skipped == 1
    assert summary.devices_updated == 0
    assert summary.client_results[0].device_update_skipped_reason is not None


def test_sync_policy_still_updates_name_by_default():
    netbox = FakeNetboxGateway()
    engine = SyncEngine(
        unifi=FakeUnifiClient(clients=[make_unifi_client(mac="11:22:33:44:55:66", name="old-name", is_wired=False)]),
        netbox=netbox,
        settings=make_settings(),  # default device_update_policy="sync"
    )
    engine.run()

    engine2 = SyncEngine(
        unifi=FakeUnifiClient(clients=[make_unifi_client(mac="11:22:33:44:55:66", name="new-name", is_wired=False)]),
        netbox=netbox,
        settings=make_settings(),
    )
    summary = engine2.run()

    assert netbox.clients_by_mac["11:22:33:44:55:66"].name == "new-name"
    assert summary.devices_updated == 1
    assert summary.devices_update_skipped == 0


def test_dry_run_previews_create_only_skip():
    netbox = FakeNetboxGateway()
    netbox.upsert_client_device(
        "11:22:33:44:55:66", "old-name", "main", "unifi-client", "generic-network-client", "unifi-sync"
    )
    client = make_unifi_client(mac="11:22:33:44:55:66", name="new-name", is_wired=False)
    engine = SyncEngine(
        unifi=FakeUnifiClient(clients=[client]),
        netbox=netbox,
        settings=make_settings(device_update_policy="create-only", dry_run=True),
    )

    summary = engine.run()

    assert summary.devices_updated == 0
    assert summary.devices_update_skipped == 1
    assert netbox.clients_by_mac["11:22:33:44:55:66"].name == "old-name"  # dry-run: unchanged
