"""Name fallback must not adopt or cable a different device that shares a name.

Documentation addresses only: 00:00:5e:00:53:xx. No site hostnames.
"""

from __future__ import annotations

import logging

from unifi_netbox_sync.identity import name_fallback_conflict
from unifi_netbox_sync.models import UnifiPort, UnifiSwitchDevice
from unifi_netbox_sync.sync import SyncEngine

from .fakes import FakeNetboxGateway, FakeUnifiClient, make_unifi_client
from .test_sync import make_settings

SWITCH_MAC = "00:00:5e:00:53:30"
OTHER_MAC = "00:00:5e:00:53:31"
CLIENT_MAC = "00:00:5e:00:53:40"
SHARED_NAME = "access-switch"
SAME_SERIAL = "LABSERIAL01"
OTHER_SERIAL = "LABSERIAL02"


def _unifi_switch(**kwargs):
    serial = kwargs.pop("serial", OTHER_SERIAL)
    return UnifiSwitchDevice(
        mac=SWITCH_MAC,
        name=SHARED_NAME,
        model="USW-24",
        device_type="usw",
        serial=serial,
        ports=[UnifiPort(index=i, media="GE", max_speed=1000) for i in range(1, 5)],
        **kwargs,
    )


def _foreign(netbox: FakeNetboxGateway, *, manufacturer: str, serial: str | None):
    device = netbox.seed_unmatchable_switch(
        SHARED_NAME,
        ["GigabitEthernet1/0/1", "GigabitEthernet1/0/2"],
        serial=serial,
    )
    device.site_slug = "main"
    device.manufacturer = manufacturer
    device.role_slug = "switch"
    return device


def _client():
    return make_unifi_client(
        mac=CLIENT_MAC,
        name="desk-client",
        is_wired=True,
        switch_mac=SWITCH_MAC,
        switch_port=1,
    )


def test_name_fallback_conflict_rules():
    from .fakes import FakeDevice

    foreign = FakeDevice(name=SHARED_NAME)
    foreign.manufacturer = "Other Vendor"
    assert "not Ubiquiti" in name_fallback_conflict(foreign, mac=SWITCH_MAC, serial="")

    same = FakeDevice(name=SHARED_NAME, mac=SWITCH_MAC)
    same.manufacturer = "Ubiquiti"
    same.serial = SAME_SERIAL
    assert name_fallback_conflict(same, mac=SWITCH_MAC, serial=SAME_SERIAL) == ""

    drifted = FakeDevice(name=SHARED_NAME)
    drifted.manufacturer = "Ubiquiti"
    drifted.serial = SAME_SERIAL
    assert name_fallback_conflict(drifted, mac=SWITCH_MAC, serial=OTHER_SERIAL) == "serial differs"

    other_mac = FakeDevice(name=SHARED_NAME, mac=OTHER_MAC)
    other_mac.manufacturer = "Ubiquiti"
    assert name_fallback_conflict(other_mac, mac=SWITCH_MAC, serial="") == "unifi_mac differs"

    unknown = FakeDevice(name=SHARED_NAME)
    assert name_fallback_conflict(unknown, mac=SWITCH_MAC, serial="") == ""


def test_cross_vendor_same_name_is_not_matched_or_cabled(caplog):
    netbox = FakeNetboxGateway()
    foreign = _foreign(netbox, manufacturer="Other Vendor", serial=SAME_SERIAL)
    engine = SyncEngine(
        unifi=FakeUnifiClient(clients=[_client()], devices=[_unifi_switch()]),
        netbox=netbox,
        settings=make_settings(sync_unifi_devices=True),
    )

    with caplog.at_level(logging.WARNING):
        summary = engine.run()

    assert summary.errors == []
    assert summary.infra_created == 1
    assert summary.infra_name_conflicts == 1
    assert foreign.serial == SAME_SERIAL
    assert "unifi-sync" not in foreign.tags
    assert all(iface.cable is None for iface in foreign.interfaces.values())
    created = netbox.infra_by_mac[SWITCH_MAC]
    assert created is not foreign
    assert created.name != SHARED_NAME
    assert created.serial == OTHER_SERIAL
    assert "cross-site match avoided" not in caplog.text
    assert "not Ubiquiti" in caplog.text
    # A client cable may land on the new UniFi device. It must not land on the
    # other vendor's interfaces, which is what the assertions above lock in.
    if summary.cables_created:
        assert created.interfaces["Port 1"].cable is not None


def test_same_vendor_same_name_and_serial_matches():
    netbox = FakeNetboxGateway()
    existing = _foreign(netbox, manufacturer="Ubiquiti", serial=SAME_SERIAL)
    existing.tags.append("unifi-sync")
    summary = SyncEngine(
        unifi=FakeUnifiClient(devices=[_unifi_switch(serial=SAME_SERIAL)]),
        netbox=netbox,
        settings=make_settings(sync_unifi_devices=True, device_update_policy="sync"),
    ).run()

    assert summary.errors == []
    assert summary.infra_created == 0
    assert summary.infra_name_conflicts == 0
    assert existing.custom_fields["unifi_mac"] == SWITCH_MAC
    assert existing.serial == SAME_SERIAL
    assert netbox.infra_by_mac[SWITCH_MAC] is existing


def test_same_vendor_differing_serial_does_not_match():
    netbox = FakeNetboxGateway()
    existing = _foreign(netbox, manufacturer="Ubiquiti", serial=SAME_SERIAL)
    existing.tags.append("unifi-sync")
    summary = SyncEngine(
        unifi=FakeUnifiClient(devices=[_unifi_switch(serial=OTHER_SERIAL)]),
        netbox=netbox,
        settings=make_settings(sync_unifi_devices=True),
    ).run()

    assert summary.errors == []
    assert summary.infra_created == 1
    assert summary.infra_name_conflicts == 1
    assert existing.serial == SAME_SERIAL
    assert (existing.custom_fields.get("unifi_mac") or "") != SWITCH_MAC
    created = netbox.infra_by_mac[SWITCH_MAC]
    assert created is not existing
    assert created.serial == OTHER_SERIAL
    assert all(iface.cable is None for iface in existing.interfaces.values())


def test_same_site_untagged_serial_match_is_not_called_cross_site(caplog):
    netbox = FakeNetboxGateway()
    existing = _foreign(netbox, manufacturer="Ubiquiti", serial=SAME_SERIAL)
    engine = SyncEngine(
        unifi=FakeUnifiClient(devices=[_unifi_switch(serial=SAME_SERIAL)]),
        netbox=netbox,
        settings=make_settings(sync_unifi_devices=True),
    )

    with caplog.at_level(logging.INFO):
        summary = engine.run()

    assert summary.infra_created == 0
    assert summary.infra_duplicates_avoided == 1
    assert existing.serial == SAME_SERIAL
    assert "same-site match avoided" in caplog.text
    assert "cross-site match avoided" not in caplog.text


def test_number_only_ports_are_not_used_on_another_vendor():
    """A device found by MAC still must not take a structural port guess."""
    netbox = FakeNetboxGateway()
    foreign = netbox.seed_switch(SWITCH_MAC, "upstream", ["GigabitEthernet1/0/1"])
    foreign.manufacturer = "Other Vendor"
    client = make_unifi_client(
        mac=CLIENT_MAC,
        name="desk-client",
        is_wired=True,
        switch_mac=SWITCH_MAC,
        switch_port=1,
    )
    summary = SyncEngine(
        unifi=FakeUnifiClient(clients=[client]),
        netbox=netbox,
        settings=make_settings(),
    ).run()

    assert summary.cables_created == 0
    assert summary.cables_skipped == 1
    assert foreign.interfaces["GigabitEthernet1/0/1"].cable is None


def test_number_only_ports_still_match_a_ubiquiti_device():
    netbox = FakeNetboxGateway()
    device = netbox.seed_switch(SWITCH_MAC, "upstream", ["GigabitEthernet1/0/1"])
    device.manufacturer = "Ubiquiti"
    client = make_unifi_client(
        mac=CLIENT_MAC,
        name="desk-client",
        is_wired=True,
        switch_mac=SWITCH_MAC,
        switch_port=1,
    )
    summary = SyncEngine(
        unifi=FakeUnifiClient(clients=[client]),
        netbox=netbox,
        settings=make_settings(),
    ).run()

    assert summary.cables_created == 1
    assert device.interfaces["GigabitEthernet1/0/1"].cable is not None
