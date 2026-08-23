from __future__ import annotations

import logging

from unifi_netbox_sync.models import UnifiSwitchDevice
from unifi_netbox_sync.sync import SyncEngine

from .fakes import FakeNetboxGateway, FakeUnifiClient, make_unifi_client
from .test_sync import make_settings

SWITCH_MAC = "aa:bb:cc:dd:ee:01"


def _wired_client(port: int = 3):
    return make_unifi_client(
        mac="11:22:33:44:55:66",
        name="laptop",
        is_wired=True,
        switch_mac=SWITCH_MAC,
        switch_port=port,
    )


def _unifi(clients, devices):
    return FakeUnifiClient(clients=clients, devices=devices)


def test_cable_created_when_switch_has_no_interface_mac_but_matching_name():
    """The reported bug: the switch is in NetBox, but its interfaces carry no
    mac_address, so the MAC-only join found nothing and every cable was skipped.
    Matching on the UniFi device name recovers it."""
    netbox = FakeNetboxGateway()
    netbox.seed_unmatchable_switch("core-switch", ["Port 1", "Port 2", "Port 3"])
    unifi = _unifi(
        [_wired_client()],
        [UnifiSwitchDevice(mac=SWITCH_MAC, name="core-switch", model="USW-24", device_type="usw")],
    )

    summary = SyncEngine(unifi=unifi, netbox=netbox, settings=make_settings()).run()

    assert summary.cables_created == 1
    assert summary.cables_skipped == 0


def test_cable_created_when_switch_matches_on_serial():
    netbox = FakeNetboxGateway()
    netbox.seed_unmatchable_switch("some-other-name", ["Port 3"], serial="ABC123")
    unifi = _unifi(
        [_wired_client()],
        [
            UnifiSwitchDevice(
                mac=SWITCH_MAC, name="unifi-name", model="USW-24", device_type="usw", serial="ABC123"
            )
        ],
    )

    summary = SyncEngine(unifi=unifi, netbox=netbox, settings=make_settings()).run()

    assert summary.cables_created == 1


def test_interface_mac_match_still_wins_when_available():
    """The original, most precise strategy must keep working."""
    netbox = FakeNetboxGateway()
    netbox.seed_switch(SWITCH_MAC, "switch-by-mac", ["Port 3"])
    netbox.seed_unmatchable_switch("decoy", ["Port 3"])
    unifi = _unifi(
        [_wired_client()],
        [UnifiSwitchDevice(mac=SWITCH_MAC, name="decoy", model="USW", device_type="usw")],
    )

    SyncEngine(unifi=unifi, netbox=netbox, settings=make_settings()).run()

    assert netbox.switches[SWITCH_MAC].interfaces["Port 3"].cable is not None


def test_unmatched_switch_warns_with_actionable_guidance(caplog):
    netbox = FakeNetboxGateway()  # switch genuinely absent
    unifi = _unifi([_wired_client()], [])

    with caplog.at_level(logging.WARNING):
        summary = SyncEngine(unifi=unifi, netbox=netbox, settings=make_settings()).run()

    assert summary.cables_skipped == 1
    warning = "\n".join(r.getMessage() for r in caplog.records)
    assert "No NetBox device found for UniFi switch" in warning
    assert "unifi_mac" in warning and "serial" in warning


def test_port_name_mismatch_reports_the_real_interface_names(caplog):
    """A skipped cable caused by naming should say what the ports are called."""
    netbox = FakeNetboxGateway()
    netbox.seed_switch(SWITCH_MAC, "core-switch", ["xe-0/0/1", "xe-0/0/2", "xe-0/0/3"])
    unifi = _unifi(
        [_wired_client()],
        [UnifiSwitchDevice(mac=SWITCH_MAC, name="core-switch", model="USW", device_type="usw")],
    )

    with caplog.at_level(logging.WARNING):
        summary = SyncEngine(unifi=unifi, netbox=netbox, settings=make_settings()).run()

    assert summary.cables_skipped == 1
    warning = "\n".join(r.getMessage() for r in caplog.records)
    assert "xe-0/0/1" in warning, "operator needs to see the actual interface names"
    assert "PORT_NAME_TEMPLATES" in warning


def test_default_templates_cover_common_switch_port_naming():
    """Each of these is a real-world NetBox naming scheme for switch ports."""
    for port_name in ("Port 3", "3", "GE3", "Gi3", "eth3", "GigabitEthernet3"):
        netbox = FakeNetboxGateway()
        netbox.seed_switch(SWITCH_MAC, "sw", [port_name])
        unifi = _unifi(
            [_wired_client()],
            [UnifiSwitchDevice(mac=SWITCH_MAC, name="sw", model="USW", device_type="usw")],
        )

        summary = SyncEngine(unifi=unifi, netbox=netbox, settings=make_settings()).run()

        assert summary.cables_created == 1, f"port named {port_name!r} should have matched"


def test_missing_unifi_device_list_does_not_break_the_run(caplog):
    """Losing /stat/device costs the name/serial fallbacks, nothing more."""

    class NoDevices(FakeUnifiClient):
        def get_devices(self, site):
            raise LookupError("controller refused /stat/device")

    netbox = FakeNetboxGateway()
    netbox.seed_switch(SWITCH_MAC, "sw", ["Port 3"])
    unifi = NoDevices(clients=[_wired_client()])

    with caplog.at_level(logging.WARNING):
        summary = SyncEngine(unifi=unifi, netbox=netbox, settings=make_settings()).run()

    assert summary.errors == []
    assert summary.cables_created == 1  # interface-MAC match still works
