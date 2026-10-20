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
STALE_MAC = "00:00:5e:00:53:32"
CLIENT_MAC = "00:00:5e:00:53:40"
SHARED_NAME = "access-switch"
SAME_SERIAL = "LABSERIAL01"
OTHER_SERIAL = "LABSERIAL02"
# Last four hex digits of SWITCH_MAC / OTHER_MAC, used when suffixing is on.
SWITCH_SUFFIX = "access-switch-5330"
OTHER_SUFFIX = "access-switch-5331"


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

    # A MAC typed into serial (colon, dash, or bare hex) is compared to the
    # UniFi MAC, including when the UniFi serial text is something else.
    mac_serial = FakeDevice(name=SHARED_NAME)
    mac_serial.manufacturer = "Ubiquiti"
    mac_serial.serial = "00:00:5e:00:53:30"
    assert name_fallback_conflict(mac_serial, mac=SWITCH_MAC, serial=SAME_SERIAL) == ""
    mac_serial.serial = "00-00-5e-00-53-30"
    assert name_fallback_conflict(mac_serial, mac=SWITCH_MAC, serial="") == ""
    mac_serial.serial = "00005E005330"
    assert name_fallback_conflict(mac_serial, mac=SWITCH_MAC, serial=OTHER_SERIAL) == ""

    stale = FakeDevice(name=SHARED_NAME)
    stale.serial = "00:00:5e:00:53:32"
    assert "serial differs" in name_fallback_conflict(stale, mac=SWITCH_MAC, serial="")
    # The old MAC is still the recorded serial on both sides. The live UniFi
    # MAC is what decides they are not the same chassis.
    assert "serial differs" in name_fallback_conflict(
        stale, mac=SWITCH_MAC, serial="00:00:5e:00:53:32"
    )
    stale.serial = "00005e005332"
    assert "serial differs" in name_fallback_conflict(stale, mac=SWITCH_MAC, serial=SAME_SERIAL)


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
    assert summary.infra_created == 0
    assert summary.infra_name_conflicts == 1
    assert SWITCH_MAC not in netbox.infra_by_mac
    assert foreign.serial == SAME_SERIAL
    assert "unifi-sync" not in foreign.tags
    assert all(iface.cable is None for iface in foreign.interfaces.values())
    assert "cross-site match avoided" not in caplog.text
    assert "not Ubiquiti" in caplog.text
    assert "Not creating a device" in caplog.text
    assert "would create" not in caplog.text


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
    assert summary.infra_created == 0
    assert summary.infra_name_conflicts == 1
    assert SWITCH_MAC not in netbox.infra_by_mac
    assert existing.serial == SAME_SERIAL
    assert (existing.custom_fields.get("unifi_mac") or "") != SWITCH_MAC
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


def _second_switch():
    return UnifiSwitchDevice(
        mac=OTHER_MAC,
        name=SHARED_NAME,
        model="USW-24",
        device_type="usw",
        serial=OTHER_SERIAL,
        ports=[UnifiPort(index=i, media="GE", max_speed=1000) for i in range(1, 5)],
    )


def test_mac_shaped_serial_that_matches_is_the_same_device(caplog):
    """A hand-made record with the MAC in serial is this UniFi device."""
    netbox = FakeNetboxGateway()
    existing = _foreign(netbox, manufacturer="Ubiquiti", serial="00:00:5e:00:53:30")
    engine = SyncEngine(
        unifi=FakeUnifiClient(devices=[_unifi_switch(serial=SAME_SERIAL)]),
        netbox=netbox,
        settings=make_settings(sync_unifi_devices=True),
    )

    with caplog.at_level(logging.INFO):
        summary = engine.run()

    assert summary.errors == []
    assert summary.infra_created == 0
    assert summary.infra_name_conflicts == 0
    assert summary.infra_duplicates_avoided == 1
    assert netbox.infra_by_mac == {}
    assert existing.serial == "00:00:5e:00:53:30"
    assert "unifi-sync" not in existing.tags
    assert "same-site match avoided" in caplog.text
    assert "by name" in caplog.text


def test_stale_mac_in_serial_is_refused_and_not_created(caplog):
    """Replaced hardware: the old MAC is still typed into serial."""
    netbox = FakeNetboxGateway()
    existing = _foreign(netbox, manufacturer="", serial=STALE_MAC)
    engine = SyncEngine(
        unifi=FakeUnifiClient(devices=[_unifi_switch(serial="")]),
        netbox=netbox,
        settings=make_settings(sync_unifi_devices=True, dry_run=True),
    )

    with caplog.at_level(logging.INFO):
        summary = engine.run()

    assert summary.errors == []
    assert summary.infra_name_conflicts == 1
    assert summary.infra_created == 0
    assert netbox.infra_by_mac == {}
    assert existing.serial == STALE_MAC
    assert "unifi-sync" not in existing.tags
    assert "serial differs" in caplog.text
    assert "Not creating a device" in caplog.text
    assert f"would create switch {SWITCH_SUFFIX!r}" not in caplog.text
    assert "would create" not in caplog.text


def test_two_unifi_devices_sharing_a_name_skip_the_second_by_default():
    netbox = FakeNetboxGateway()
    summary = SyncEngine(
        unifi=FakeUnifiClient(devices=[_unifi_switch(serial=SAME_SERIAL), _second_switch()]),
        netbox=netbox,
        settings=make_settings(sync_unifi_devices=True),
    ).run()

    assert summary.errors == []
    assert summary.infra_created == 1
    assert summary.infra_name_conflicts == 1
    assert netbox.infra_by_mac[SWITCH_MAC].name == SHARED_NAME
    assert OTHER_MAC not in netbox.infra_by_mac


def test_suffixed_creation_is_opt_in_when_the_name_is_taken():
    netbox = FakeNetboxGateway()
    summary = SyncEngine(
        unifi=FakeUnifiClient(devices=[_unifi_switch(serial=SAME_SERIAL), _second_switch()]),
        netbox=netbox,
        settings=make_settings(sync_unifi_devices=True, create_suffixed_on_name_conflict=True),
    ).run()

    assert summary.errors == []
    assert summary.infra_created == 2
    assert summary.infra_name_conflicts == 1
    assert netbox.infra_by_mac[SWITCH_MAC].name == SHARED_NAME
    created = netbox.infra_by_mac[OTHER_MAC]
    assert created.name == OTHER_SUFFIX
    assert created.serial == OTHER_SERIAL
    assert created is not netbox.infra_by_mac[SWITCH_MAC]
