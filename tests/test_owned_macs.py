"""One NetBox device may own more than the single unifi_mac field.

Documentation addresses only: 00:00:5e:00:53:xx and 192.0.2.0/24.
"""

from __future__ import annotations

import logging

from unifi_netbox_sync.caching import CachingNetboxGateway
from unifi_netbox_sync.macs import (
    declared_extra_macs,
    index_owned_macs,
    macs_from_text,
    normalize_mac,
)
from unifi_netbox_sync.models import UnifiPort, UnifiSwitchDevice
from unifi_netbox_sync.netbox_client import PynetboxGateway
from unifi_netbox_sync.sync import SyncEngine, _switch_match_rank

from .fakes import (
    FakeDevice,
    FakeInterface,
    FakeIP,
    FakeNetboxGateway,
    FakeUnifiClient,
    make_unifi_client,
)
from .test_device_sync import _run as run_infra
from .test_sync import make_settings

PRIMARY = "00:00:5e:00:53:10"
SECONDARY = "00:00:5e:00:53:11"
FIELD_MAC = "00:00:5e:00:53:12"
OBJECT_MAC = "00:00:5e:00:53:13"
OTHER_MAC = "00:00:5e:00:53:14"
CLIENT = "00:00:5e:00:53:20"
SWITCH = "00:00:5e:00:53:01"
CLIENT_IP = "192.0.2.20"
GATEWAY_IP = "192.0.2.1"


def _gateway(
    netbox: FakeNetboxGateway,
    *,
    name: str = "edge-gateway",
    primary: str = PRIMARY,
    interface_mac: str | None = None,
    interface_macs: list[str] | None = None,
    extra_field: str | None = None,
    site_slug: str = "main",
) -> FakeDevice:
    device = FakeDevice(name=name, mac=primary, site_slug=site_slug)
    device.role_slug = "router"
    device.tags = ["unifi-sync"]
    device.device_type_slug = "gateway"
    device.primary_ip4 = FakeIP(GATEWAY_IP)
    device.interfaces["lan"] = FakeInterface(device, "lan")
    device.interfaces["wan"] = FakeInterface(
        device, "wan", mac_address=interface_mac, mac_addresses=interface_macs
    )
    if extra_field is not None:
        device.custom_fields["unifi_macs"] = extra_field
    netbox.switches[primary] = device
    netbox.infra_by_mac[primary] = device
    return device


def _client(mac: str, **overrides):
    fields = dict(
        mac=mac,
        name="",
        ip=CLIENT_IP,
        is_wired=True,
        switch_mac=SWITCH,
        switch_port=2,
    )
    fields.update(overrides)
    return make_unifi_client(**fields)


def _run(netbox: FakeNetboxGateway, client=None, **settings):
    netbox.seed_switch(SWITCH, "access-switch", ["Port 1", "Port 2"])
    engine = SyncEngine(
        unifi=FakeUnifiClient(clients=[client or _client(SECONDARY)]),
        netbox=netbox,
        settings=make_settings(**settings),
    )
    return engine.run()


def _owned_lines(caplog) -> list[str]:
    return [record.getMessage() for record in caplog.records if "is owned by NetBox device" in record.getMessage()]


def test_normalize_and_split_mac_lists():
    assert normalize_mac("00:00:5E:00:53:11") == SECONDARY
    assert normalize_mac("00-00-5e-00-53-11") == SECONDARY
    assert normalize_mac("00005e005311") == SECONDARY
    assert normalize_mac("not-a-mac") == ""
    assert macs_from_text(f"{SECONDARY}, {FIELD_MAC};{OBJECT_MAC} {OTHER_MAC}") == [
        SECONDARY,
        FIELD_MAC,
        OBJECT_MAC,
        OTHER_MAC,
    ]
    # A single address is not treated as a list of extras.
    assert declared_extra_macs({"unifi_mac": PRIMARY}) == []
    assert declared_extra_macs({"unifi_mac": f"{PRIMARY}, {SECONDARY}"}) == [PRIMARY, SECONDARY]
    assert declared_extra_macs({"unifi_mac": PRIMARY, "unifi_macs": FIELD_MAC}) == [FIELD_MAC]


def test_single_mac_client_is_unchanged():
    """A client whose MAC is not owned by another device is still created."""
    netbox = FakeNetboxGateway()
    gateway = _gateway(netbox)
    summary = _run(netbox, _client(CLIENT, name="laptop-1", is_wired=False, switch_mac=None, switch_port=None))

    assert summary.errors == []
    assert summary.devices_created == 1
    created = netbox.clients_by_mac[CLIENT]
    assert created.name == "laptop-1"
    assert "wlan0" in created.interfaces
    assert created.primary_ip4.address.startswith(CLIENT_IP)
    assert gateway.primary_ip4.address.startswith(GATEWAY_IP)
    assert set(gateway.interfaces) == {"lan", "wan"}
    assert gateway.custom_fields["unifi_mac"] == PRIMARY


def test_primary_unifi_mac_still_skips_the_placeholder_the_old_way(caplog):
    """The chassis MAC keeps the existing managed-role skip, not the new path."""
    netbox = FakeNetboxGateway()
    gateway = _gateway(netbox)

    with caplog.at_level(logging.INFO):
        summary = _run(netbox, _client(PRIMARY, name="edge-gateway"))

    assert summary.errors == []
    assert summary.devices_created == 0
    assert _owned_lines(caplog) == []
    assert any("not a managed client role" in record.getMessage() for record in caplog.records)
    assert "eth0" not in gateway.interfaces
    assert gateway.primary_ip4.address.startswith(GATEWAY_IP)
    assert PRIMARY not in netbox.clients_by_mac


def test_interface_mac_does_not_create_a_placeholder(caplog):
    netbox = FakeNetboxGateway()
    gateway = _gateway(netbox, interface_mac=SECONDARY)
    before = set(gateway.interfaces)

    with caplog.at_level(logging.INFO):
        summary = _run(netbox)
        again = _run(netbox)

    assert summary.errors == []
    assert again.errors == []
    assert summary.devices_created == 0
    assert again.devices_created == 0
    assert SECONDARY not in netbox.clients_by_mac
    assert netbox.cables == set()
    assert set(gateway.interfaces) == before
    assert "eth0" not in gateway.interfaces
    assert gateway.primary_ip4.address.startswith(GATEWAY_IP)
    assert len(_owned_lines(caplog)) == 2
    assert "interface MAC" in _owned_lines(caplog)[0]
    assert "edge-gateway" in _owned_lines(caplog)[0]
    assert "not creating a placeholder" in _owned_lines(caplog)[0]


def test_mac_address_object_on_an_interface_is_owned(caplog):
    netbox = FakeNetboxGateway()
    gateway = _gateway(netbox, interface_macs=[OBJECT_MAC])

    with caplog.at_level(logging.INFO):
        summary = _run(netbox, _client(OBJECT_MAC))

    assert summary.errors == []
    assert summary.devices_created == 0
    assert OBJECT_MAC not in netbox.clients_by_mac
    assert gateway.primary_ip4.address.startswith(GATEWAY_IP)
    assert "interface MAC" in _owned_lines(caplog)[0]
    assert netbox.cables == set()


def test_unifi_macs_field_does_not_create_a_placeholder(caplog):
    netbox = FakeNetboxGateway()
    gateway = _gateway(netbox, extra_field=f"{FIELD_MAC}, {OTHER_MAC}")

    with caplog.at_level(logging.INFO):
        summary = _run(netbox, _client(FIELD_MAC))

    assert summary.errors == []
    assert summary.devices_created == 0
    assert FIELD_MAC not in netbox.clients_by_mac
    assert gateway.custom_fields["unifi_mac"] == PRIMARY
    assert gateway.primary_ip4.address.startswith(GATEWAY_IP)
    assert "unifi_macs" in _owned_lines(caplog)[0]
    assert netbox.cables == set()


def test_semicolon_and_whitespace_lists_are_owned():
    netbox = FakeNetboxGateway()
    _gateway(netbox, extra_field=f"{FIELD_MAC};{OTHER_MAC}\n{OBJECT_MAC}")

    summary = _run(netbox, _client(OTHER_MAC, is_wired=False, switch_mac=None, switch_port=None))

    assert summary.errors == []
    assert summary.devices_created == 0
    assert OTHER_MAC not in netbox.clients_by_mac


def test_comma_separated_unifi_mac_is_owned_without_a_second_field(caplog):
    netbox = FakeNetboxGateway()
    gateway = _gateway(netbox)
    gateway.custom_fields["unifi_mac"] = f"{PRIMARY}, {SECONDARY}"

    with caplog.at_level(logging.INFO):
        summary = _run(netbox)

    assert summary.devices_created == 0
    assert SECONDARY not in netbox.clients_by_mac
    assert "unifi_macs" in _owned_lines(caplog)[0]
    # The stored primary string is left as the operator wrote it.
    assert gateway.custom_fields["unifi_mac"] == f"{PRIMARY}, {SECONDARY}"


def test_exact_unifi_mac_beats_another_devices_interface(caplog):
    netbox = FakeNetboxGateway()
    gateway = _gateway(netbox, interface_mac=SECONDARY)
    client, *_ = netbox.upsert_client_device(
        SECONDARY, "handset-1", "main", "unifi-client", "generic-network-client", "unifi-sync"
    )
    client.primary_ip4 = FakeIP("192.0.2.30")

    with caplog.at_level(logging.INFO):
        summary = _run(netbox, _client(SECONDARY, name="handset-1", is_wired=False, switch_mac=None, switch_port=None))

    assert summary.errors == []
    assert summary.devices_created == 0
    assert len(netbox.clients_by_mac) == 1
    assert netbox.clients_by_mac[SECONDARY] is client
    assert client.primary_ip4.address.startswith(CLIENT_IP)
    assert gateway.primary_ip4.address.startswith(GATEWAY_IP)
    assert _owned_lines(caplog) == []


def test_owned_mac_is_global_across_sites(caplog):
    netbox = FakeNetboxGateway()
    gateway = _gateway(netbox, interface_mac=SECONDARY, site_slug="other")

    with caplog.at_level(logging.INFO):
        summary = _run(netbox)

    assert summary.devices_created == 0
    assert SECONDARY not in netbox.clients_by_mac
    assert gateway.site_slug == "other"
    assert "edge-gateway" in _owned_lines(caplog)[0]


def test_disabling_owned_macs_creates_the_placeholder_again():
    netbox = FakeNetboxGateway()
    _gateway(netbox, interface_mac=SECONDARY, extra_field=FIELD_MAC)

    summary = _run(netbox, _client(SECONDARY), match_owned_macs=False)

    assert summary.errors == []
    assert summary.devices_created == 1
    placeholder = netbox.clients_by_mac[SECONDARY]
    assert placeholder.name == SECONDARY.replace(":", "")
    assert placeholder.custom_fields["unifi_mac"] == SECONDARY


def test_dry_run_plans_no_placeholder_for_an_owned_mac(caplog):
    netbox = FakeNetboxGateway()
    _gateway(netbox, interface_mac=SECONDARY)

    with caplog.at_level(logging.INFO):
        summary = _run(netbox, dry_run=True)

    assert summary.devices_created == 0
    assert SECONDARY not in netbox.clients_by_mac
    assert netbox.cables == set()
    assert any(line.startswith("[dry-run] ") for line in _owned_lines(caplog))
    assert "would create device" not in caplog.text


def test_prefetch_recognizes_an_owned_mac_without_a_per_client_lookup():
    netbox = FakeNetboxGateway()
    netbox.seed_site("main", "Main")
    _gateway(netbox, extra_field=FIELD_MAC)
    counting_inner = netbox
    from .fakes import CountingNetboxGateway

    counting = CountingNetboxGateway(counting_inner)
    engine = SyncEngine(
        unifi=FakeUnifiClient(clients=[_client(FIELD_MAC)]),
        netbox=CachingNetboxGateway(counting),
        settings=make_settings(netbox_prefetch=True, dry_run=True),
    )
    summary = engine.run()

    assert summary.errors == []
    assert summary.devices_created == 0
    assert FIELD_MAC not in netbox.clients_by_mac
    assert counting.calls["find_client_device_by_mac"] == 0
    assert counting.calls["upsert_client_device"] == 0


def test_first_declared_device_wins_when_two_list_the_same_mac(caplog):
    netbox = FakeNetboxGateway()
    first = _gateway(netbox, name="gateway-a", extra_field=FIELD_MAC)
    second = _gateway(
        netbox,
        name="gateway-b",
        primary=OTHER_MAC,
        extra_field=FIELD_MAC,
    )

    with caplog.at_level(logging.INFO):
        summary = _run(netbox, _client(FIELD_MAC, is_wired=False, switch_mac=None, switch_port=None))

    assert summary.devices_created == 0
    assert FIELD_MAC not in netbox.clients_by_mac
    assert first.id != second.id
    assert "gateway-a" in caplog.text
    assert "ignoring gateway-b" in caplog.text


def test_adopted_device_matches_an_interface_mac_without_a_duplicate():
    netbox = FakeNetboxGateway()
    gateway = _gateway(netbox, interface_mac=SECONDARY)
    uni = UnifiSwitchDevice(
        mac=SECONDARY,
        name="edge-gateway",
        model="GW-LAB",
        device_type="ugw",
        serial="LABSERIAL01",
        ports=[UnifiPort(index=1, media="GE"), UnifiPort(index=2, media="GE")],
    )

    summary, _box = run_infra([uni], netbox=netbox, device_update_policy="sync")

    assert summary.errors == []
    assert summary.infra_created == 0
    assert gateway.custom_fields["unifi_mac"] == PRIMARY
    assert gateway.serial == "LABSERIAL01"
    assert list(netbox.infra_by_mac.values()).count(gateway) >= 1
    assert all(device.id == gateway.id for device in netbox.infra_by_mac.values())


def test_extra_interface_mac_does_not_change_uplink_cabling():
    netbox = FakeNetboxGateway()
    upstream = netbox.seed_switch("00:00:5e:00:53:02", "core-switch", ["Port 4"])
    upstream.custom_fields["unifi_mac"] = "00:00:5e:00:53:02"
    upstream.tags.append("unifi-sync")
    upstream.site_slug = "main"
    upstream.serial = "LABSERIAL02"
    gateway = _gateway(netbox, interface_mac=SECONDARY)
    for index in range(9):
        gateway.interfaces.setdefault(f"eth{index}", FakeInterface(gateway, f"eth{index}"))
    gateway.interfaces["eth8"].mac_address = OBJECT_MAC
    uni_switch = UnifiSwitchDevice(
        mac="00:00:5e:00:53:02",
        name="core-switch",
        model="USW-LAB",
        device_type="usw",
        serial="LABSERIAL02",
        ports=[UnifiPort(index=i, media="GE") for i in range(1, 5)],
    )
    uni_gw = UnifiSwitchDevice(
        mac=PRIMARY,
        name="edge-gateway",
        model="GW-LAB",
        device_type="ugw",
        serial="LABSERIAL10",
        ports=[UnifiPort(index=i, media="GE") for i in range(1, 10)],
        uplink_mac="00:00:5e:00:53:02",
        uplink_remote_port=4,
        uplink_local_port=2,
    )

    summary, _box = run_infra([uni_switch, uni_gw], netbox=netbox)

    assert summary.errors == []
    assert summary.uplink_cables_created == 1
    pair = tuple(sorted((gateway.interfaces["eth1"].id, upstream.interfaces["Port 4"].id)))
    assert netbox.cables == {pair}
    assert gateway.interfaces["eth8"].cable is None


def test_unifi_macs_still_finds_the_device_for_an_uplink():
    """A port MAC listed in unifi_macs is a MAC-quality match, so the uplink is cabled once."""
    netbox = FakeNetboxGateway()
    upstream = netbox.seed_switch("00:00:5e:00:53:02", "core-switch", ["Port 4"])
    upstream.custom_fields["unifi_mac"] = "00:00:5e:00:53:02"
    upstream.tags.append("unifi-sync")
    upstream.site_slug = "main"
    upstream.serial = "LABSERIAL02"
    gateway = _gateway(netbox, extra_field=SECONDARY)
    for index in range(9):
        gateway.interfaces.setdefault(f"eth{index}", FakeInterface(gateway, f"eth{index}"))
    uni_switch = UnifiSwitchDevice(
        mac="00:00:5e:00:53:02",
        name="core-switch",
        model="USW-LAB",
        device_type="usw",
        serial="LABSERIAL02",
        ports=[UnifiPort(index=i, media="GE") for i in range(1, 5)],
    )
    # UniFi reports the gateway under a port MAC, not the primary unifi_mac.
    uni_gw = UnifiSwitchDevice(
        mac=SECONDARY,
        name="edge-gateway",
        model="GW-LAB",
        device_type="ugw",
        serial="LABSERIAL10",
        ports=[UnifiPort(index=i, media="GE") for i in range(1, 10)],
        uplink_mac="00:00:5e:00:53:02",
        uplink_remote_port=4,
        uplink_local_port=2,
    )

    summary, _box = run_infra([uni_switch, uni_gw], netbox=netbox)

    assert summary.errors == []
    assert summary.infra_created == 0
    assert summary.uplink_cables_created == 1
    assert gateway.custom_fields["unifi_mac"] == PRIMARY
    pair = tuple(sorted((gateway.interfaces["eth1"].id, upstream.interfaces["Port 4"].id)))
    assert netbox.cables == {pair}


def test_unifi_macs_ranks_with_mac_matches_not_with_names():
    assert _switch_match_rank("unifi_macs custom field") == _switch_match_rank("interface MAC")
    assert _switch_match_rank("unifi_macs custom field") < _switch_match_rank("device name")
    assert _switch_match_rank("unifi_macs custom field") < _switch_match_rank("serial")


def test_field_beats_a_different_devices_interface():
    gateway = FakeDevice(name="edge-gateway", mac=PRIMARY)
    other = FakeDevice(name="other-gateway", mac=OTHER_MAC)
    other.interfaces["wan"] = FakeInterface(other, "wan", mac_address=FIELD_MAC)
    gateway.custom_fields["unifi_macs"] = FIELD_MAC
    index, conflicts = index_owned_macs(
        [gateway, other],
        [other.interfaces["wan"]],
        [],
    )

    assert index[FIELD_MAC][0] is gateway
    assert index[FIELD_MAC][1] == "unifi_macs"
    assert conflicts[FIELD_MAC] == ["other-gateway"]


def test_prefetched_index_answers_without_a_netbox_call():
    gateway = PynetboxGateway("https://netbox.example.com", "token")
    device = FakeDevice(name="edge-gateway", mac=PRIMARY)
    wan = FakeInterface(device, "wan", mac_address=SECONDARY)

    class MacObject:
        mac_address = OBJECT_MAC
        assigned_object_type = "dcim.interface"
        assigned_object = type("Assigned", (), {"device": device})()

    gateway.note_owned_mac_sources(
        [device],
        [wan],
        [MacObject()],
        complete=True,
        mac_objects_indexed=True,
    )

    class _ApiMustNotBeCalled:
        def __getattr__(self, name: str):
            raise AssertionError("owned MAC lookup must use the index")

    gateway.api = _ApiMustNotBeCalled()

    found = gateway.find_owned_mac_device(SECONDARY)
    assert found is not None
    assert found[0] is device
    assert found[1] == "interface MAC"
    obj = gateway.find_owned_mac_device(OBJECT_MAC)
    assert obj is not None and obj[0] is device and obj[1] == "interface MAC"
    assert gateway.find_owned_mac_device(CLIENT) is None
    # The primary address is not an "extra" and is not answered here.
    assert gateway.find_owned_mac_device(PRIMARY) is None
