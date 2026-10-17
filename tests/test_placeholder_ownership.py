"""Placeholder interfaces stay off devices the sync no longer owns."""

from __future__ import annotations

import logging

from unifi_netbox_sync.sync import SyncEngine

from .fakes import FakeDevice, FakeInterface, FakeIP, FakeNetboxGateway, FakeUnifiClient, make_unifi_client
from .test_sync import make_settings

MAC = "aa:bb:cc:dd:ee:10"
SWITCH = "aa:bb:cc:dd:ee:01"


def _forbid_writes(netbox: FakeNetboxGateway) -> None:
    def boom(*_args, **_kwargs):
        raise AssertionError("placeholder interface or IP should not be written")

    netbox.ensure_interface = boom  # type: ignore[method-assign]
    netbox.assign_ip = boom  # type: ignore[method-assign]


def _promoted(
    netbox: FakeNetboxGateway,
    *,
    role: str = "switch",
    tags: tuple[str, ...] = ("unifi-sync",),
    name: str = "core-switch",
) -> FakeDevice:
    """A device a person rebuilt, still carrying the UniFi MAC."""
    device = FakeDevice(name=name, mac=MAC, site_slug="main")
    device.role_slug = role
    device.tags = list(tags)
    device.device_type_slug = "usw-24-poe"
    device.interfaces["Port 1"] = FakeInterface(device, "Port 1")
    device.primary_ip4 = FakeIP("10.4.0.8")
    netbox.switches[MAC] = device
    return device


def _client(**overrides):
    fields = dict(
        mac=MAC,
        name="printer-01",
        ip="10.4.0.9",
        is_wired=True,
        switch_mac=SWITCH,
        switch_port=2,
    )
    fields.update(overrides)
    return make_unifi_client(**fields)


def _run(netbox: FakeNetboxGateway, client=None, **settings):
    netbox.seed_switch(SWITCH, "switch-01", ["Port 1", "Port 2"])
    engine = SyncEngine(
        unifi=FakeUnifiClient(clients=[client or _client()]),
        netbox=netbox,
        settings=make_settings(**settings),
    )
    return engine.run()


def _skip_lines(caplog) -> list[str]:
    return [record.getMessage() for record in caplog.records if "skipping placeholder interface and IP" in record.getMessage()]


def test_rerolled_device_is_matched_and_not_given_eth0(caplog):
    netbox = FakeNetboxGateway()
    device = _promoted(netbox)
    device_id = device.id
    _forbid_writes(netbox)

    with caplog.at_level(logging.INFO):
        summary = _run(netbox, device_update_policy="sync")

    assert summary.errors == []
    assert summary.devices_created == 0
    assert _skip_lines(caplog) == [
        "skipping placeholder interface and IP for 'core-switch' "
        f"({MAC}): role 'switch' is not a managed client role (unifi-client). "
        "Matched by MAC; not creating a duplicate."
    ]
    assert "eth0" not in device.interfaces
    assert set(device.interfaces) == {"Port 1"}
    assert device.primary_ip4.address == "10.4.0.8/32"
    assert device.id == device_id
    assert device.role_slug == "switch"
    assert MAC not in netbox.clients_by_mac
    assert netbox.cables == set()


def test_each_pass_logs_one_skip_line(caplog):
    netbox = FakeNetboxGateway()
    device = _promoted(netbox)
    _forbid_writes(netbox)

    with caplog.at_level(logging.INFO):
        _run(netbox)
        _run(netbox)

    assert len(_skip_lines(caplog)) == 2
    assert "eth0" not in device.interfaces
    assert device.primary_ip4.address == "10.4.0.8/32"


def test_opt_out_tag_skips_interface_and_ip_without_a_duplicate(caplog):
    netbox = FakeNetboxGateway()
    device, *_ = netbox.upsert_client_device(
        MAC, "printer-01", "main", "unifi-client", "generic-network-client", "unifi-sync"
    )
    device.tags.append("sync-unmanaged")
    device.interfaces["lan1"] = FakeInterface(device, "lan1")
    device.primary_ip4 = FakeIP("10.4.0.8")
    device_id = device.id
    _forbid_writes(netbox)

    with caplog.at_level(logging.INFO):
        summary = _run(netbox, _client(name="renamed-printer"), device_update_policy="sync")

    assert summary.errors == []
    assert summary.devices_created == 0
    assert len(netbox.clients_by_mac) == 1
    assert netbox.clients_by_mac[MAC] is device
    assert device.id == device_id
    assert device.name == "printer-01"
    assert "eth0" not in device.interfaces
    assert "lan1" in device.interfaces
    assert device.primary_ip4.address == "10.4.0.8/32"
    assert len(_skip_lines(caplog)) == 1
    assert "tagged 'sync-unmanaged'" in _skip_lines(caplog)[0]
    assert netbox.cables == set()


def test_custom_opt_out_tag_is_the_one_that_counts(caplog):
    netbox = FakeNetboxGateway()
    device = _promoted(netbox, role="unifi-client", tags=("unifi-sync", "leave-alone"), name="printer-01")
    netbox.clients_by_mac[MAC] = device
    del netbox.switches[MAC]
    _forbid_writes(netbox)

    with caplog.at_level(logging.INFO):
        summary = _run(netbox, sync_unmanaged_tag="leave-alone")

    assert summary.errors == []
    assert "eth0" not in device.interfaces
    assert "tagged 'leave-alone'" in _skip_lines(caplog)[0]


def test_default_tag_does_not_match_a_different_opt_out_name():
    netbox = FakeNetboxGateway()
    device, *_ = netbox.upsert_client_device(
        MAC, "printer-01", "main", "unifi-client", "generic-network-client", "unifi-sync"
    )
    device.tags.append("leave-alone")

    summary = _run(netbox, _client(is_wired=False, switch_mac=None, switch_port=None))

    assert summary.errors == []
    assert "wlan0" in device.interfaces
    assert device.primary_ip4.address.startswith("10.4.0.9")


def test_extra_managed_role_still_gets_the_placeholder():
    netbox = FakeNetboxGateway()
    device, *_ = netbox.upsert_client_device(
        MAC, "printer-01", "main", "endpoint", "generic-network-client", "unifi-sync"
    )

    summary = _run(
        netbox,
        _client(is_wired=True, switch_mac=None, switch_port=None),
        managed_client_role_slugs=("unifi-client", "endpoint"),
    )

    assert summary.errors == []
    assert "eth0" in device.interfaces
    assert device.primary_ip4.address.startswith("10.4.0.9")
    assert len(netbox.clients_by_mac) == 1


def test_role_outside_the_list_is_left_alone(caplog):
    netbox = FakeNetboxGateway()
    device = _promoted(netbox, role="endpoint", name="printer-01")
    _forbid_writes(netbox)

    with caplog.at_level(logging.INFO):
        _run(netbox, managed_client_role_slugs=("unifi-client",))

    assert "eth0" not in device.interfaces
    assert "role 'endpoint' is not a managed client role (unifi-client)" in _skip_lines(caplog)[0]


def test_real_device_type_with_the_client_role_is_still_managed():
    netbox = FakeNetboxGateway()
    device, *_ = netbox.upsert_client_device(
        MAC, "printer-01", "main", "unifi-client", "generic-network-client", "unifi-sync"
    )
    device.device_type_slug = "usw-24-poe"
    device.interfaces["Port 1"] = FakeInterface(device, "Port 1")

    summary = _run(netbox, _client(is_wired=True, switch_mac=None, switch_port=None))

    assert summary.errors == []
    assert "eth0" in device.interfaces
    assert "Port 1" in device.interfaces
    assert device.primary_ip4.address.startswith("10.4.0.9")


def test_owned_client_still_gets_placeholder_interface_and_ip():
    netbox = FakeNetboxGateway()

    summary = _run(netbox, _client(is_wired=False, switch_mac=None, switch_port=None))

    device = netbox.clients_by_mac[MAC]
    assert summary.devices_created == 1
    assert summary.errors == []
    assert "wlan0" in device.interfaces
    assert device.primary_ip4.address.startswith("10.4.0.9")
    assert device.role_slug == "unifi-client"


def test_dry_run_logs_one_skip_and_writes_nothing(caplog):
    netbox = FakeNetboxGateway()
    device = _promoted(netbox)
    _forbid_writes(netbox)

    with caplog.at_level(logging.INFO):
        summary = _run(netbox, dry_run=True)

    assert summary.errors == []
    assert summary.devices_created == 0
    assert _skip_lines(caplog) == [
        "[dry-run] skipping placeholder interface and IP for 'core-switch' "
        f"({MAC}): role 'switch' is not a managed client role (unifi-client). "
        "Matched by MAC; not creating a duplicate."
    ]
    assert "would assign IP" not in caplog.text
    assert "eth0" not in device.interfaces
    assert device.primary_ip4.address == "10.4.0.8/32"


def test_managed_roles_follow_the_client_role_when_unset():
    settings = make_settings(client_device_role_slug="endpoint")

    assert settings.managed_client_role_slugs == ()
    assert settings.managed_client_roles() == ("endpoint",)
    assert settings.sync_unmanaged_tag == "sync-unmanaged"
