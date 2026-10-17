"""Engine-level cable matching, shared-port guard, and the skip report."""

from __future__ import annotations

import csv

from unifi_netbox_sync.models import UnifiPort, UnifiSwitchDevice
from unifi_netbox_sync.portmatch import parse_model_templates
from unifi_netbox_sync.sync import SyncEngine

from .fakes import FakeNetboxGateway, FakeUnifiClient, make_unifi_client
from .test_sync import make_settings

SWITCH_MAC = "aa:bb:cc:dd:ee:01"


def _switch(ports, model="USW-24-POE", name="core-switch"):
    return UnifiSwitchDevice(
        mac=SWITCH_MAC, name=name, model=model, device_type="usw", ports=ports
    )


def _run(netbox, clients, devices=None, **settings):
    return SyncEngine(
        unifi=FakeUnifiClient(clients=clients, devices=devices or []),
        netbox=netbox,
        settings=make_settings(**settings),
    ).run()


def test_slot_module_juniper_and_zero_pad_names_get_cables():
    for name in ("Gi1/0/4", "1/1/4", "ge-0/0/4", "ge-0/0/04", "Port 04"):
        netbox = FakeNetboxGateway()
        netbox.seed_switch(SWITCH_MAC, "sw", [name, "mgmt0"])
        summary = _run(
            netbox,
            [make_unifi_client(mac="11:22:33:44:55:66", is_wired=True, switch_mac=SWITCH_MAC, switch_port=4)],
        )
        assert summary.cables_created == 1, name
        assert netbox.switches[SWITCH_MAC].interfaces[name].cable is not None


def test_client_cable_does_not_shift_zero_based_names_or_use_a_lone_eth0():
    """Client cables stay on the template match.

    A leftover client record whose only interface is the placeholder eth0
    must not gain a cable, and eth0/eth1 must not be shifted down by one.
    Port 1 still hits eth1 through the eth{port} template. Port 2 hits nothing.
    """
    netbox = FakeNetboxGateway()
    netbox.seed_switch(SWITCH_MAC, "sw", ["eth0"])
    summary = _run(
        netbox,
        [make_unifi_client(mac="11:22:33:44:55:66", is_wired=True, switch_mac=SWITCH_MAC, switch_port=1)],
    )
    assert summary.cables_created == 0
    assert summary.cables_skipped == 1
    assert netbox.switches[SWITCH_MAC].interfaces["eth0"].cable is None

    netbox = FakeNetboxGateway()
    netbox.seed_switch(SWITCH_MAC, "sw", ["eth0", "eth1", "wlan0"])
    summary = _run(
        netbox,
        [make_unifi_client(mac="11:22:33:44:55:67", is_wired=True, switch_mac=SWITCH_MAC, switch_port=1)],
    )
    assert summary.cables_created == 1
    switch = netbox.switches[SWITCH_MAC]
    assert switch.interfaces["eth1"].cable is not None
    assert switch.interfaces["eth0"].cable is None

    netbox = FakeNetboxGateway()
    netbox.seed_switch(SWITCH_MAC, "sw", ["eth0", "eth1"])
    summary = _run(
        netbox,
        [make_unifi_client(mac="11:22:33:44:55:68", is_wired=True, switch_mac=SWITCH_MAC, switch_port=2)],
    )
    assert summary.cables_created == 0
    assert summary.cables_skipped == 1
    switch = netbox.switches[SWITCH_MAC]
    assert switch.interfaces["eth0"].cable is None
    assert switch.interfaces["eth1"].cable is None


def test_exact_port_name_still_preferred_when_a_structural_name_also_exists():
    netbox = FakeNetboxGateway()
    netbox.seed_switch(SWITCH_MAC, "sw", ["Port 4", "Gi1/0/4"])
    _run(
        netbox,
        [make_unifi_client(mac="11:22:33:44:55:66", is_wired=True, switch_mac=SWITCH_MAC, switch_port=4)],
    )
    switch = netbox.switches[SWITCH_MAC]
    assert switch.interfaces["Port 4"].cable is not None
    assert switch.interfaces["Gi1/0/4"].cable is None


def test_sfp_ordinal_matches_and_copper_port_does_not_take_the_sfp_name():
    ports = [UnifiPort(index=i, media="GE") for i in range(1, 25)]
    ports.append(UnifiPort(index=25, media="SFP+", name="SFP 1"))
    netbox = FakeNetboxGateway()
    netbox.seed_switch(SWITCH_MAC, "sw", ["SFP1"])
    summary = _run(
        netbox,
        [make_unifi_client(mac="11:22:33:44:55:66", is_wired=True, switch_mac=SWITCH_MAC, switch_port=25)],
        devices=[_switch(ports)],
    )
    assert summary.cables_created == 1
    assert summary.cables_skipped == 0

    netbox = FakeNetboxGateway()
    netbox.seed_switch(SWITCH_MAC, "sw", ["SFP 1"])
    summary = _run(
        netbox,
        [make_unifi_client(mac="11:22:33:44:55:67", is_wired=True, switch_mac=SWITCH_MAC, switch_port=1)],
        devices=[_switch(ports)],
    )
    assert summary.cables_created == 0
    assert summary.cable_skips[0].reason == "port_not_found"


def test_ambiguous_stack_members_are_not_cabled():
    netbox = FakeNetboxGateway()
    netbox.seed_switch(SWITCH_MAC, "sw", ["Gi1/0/4", "Gi2/0/4"])
    summary = _run(
        netbox,
        [make_unifi_client(mac="11:22:33:44:55:66", is_wired=True, switch_mac=SWITCH_MAC, switch_port=4)],
    )
    assert summary.cables_created == 0
    assert summary.cable_skips[0].reason == "port_ambiguous"
    assert netbox.switches[SWITCH_MAC].interfaces["Gi1/0/4"].cable is None


def test_model_template_selects_the_configured_member():
    netbox = FakeNetboxGateway()
    netbox.seed_switch(SWITCH_MAC, "sw", ["Gi1/0/4", "Gi2/0/4"])
    summary = _run(
        netbox,
        [make_unifi_client(mac="11:22:33:44:55:66", is_wired=True, switch_mac=SWITCH_MAC, switch_port=4)],
        devices=[_switch([], model="USW-Pro-48")],
        port_model_templates=parse_model_templates("USW-Pro-48=Gi1/0/{port}"),
    )
    assert summary.cables_created == 1
    assert netbox.switches[SWITCH_MAC].interfaces["Gi1/0/4"].cable is not None
    assert netbox.switches[SWITCH_MAC].interfaces["Gi2/0/4"].cable is None


def test_clients_sharing_a_port_do_not_get_a_cable():
    netbox = FakeNetboxGateway()
    netbox.seed_switch(SWITCH_MAC, "sw", ["Port 5", "Port 3"])
    summary = _run(
        netbox,
        [
            make_unifi_client(mac="11:22:33:44:55:01", name="a", is_wired=True, switch_mac=SWITCH_MAC, switch_port=5),
            make_unifi_client(mac="11:22:33:44:55:02", name="b", is_wired=True, switch_mac=SWITCH_MAC, switch_port=5),
            make_unifi_client(mac="11:22:33:44:55:03", name="c", is_wired=True, switch_mac=SWITCH_MAC, switch_port=3),
        ],
    )
    switch = netbox.switches[SWITCH_MAC]
    assert switch.interfaces["Port 5"].cable is None
    assert switch.interfaces["Port 3"].cable is not None
    assert summary.cables_created == 1
    shared = [skip for skip in summary.cable_skips if skip.reason == "shared_port"]
    assert len(shared) == 2
    assert {skip.unifi_port for skip in shared} == {"5"}


def test_uplink_port_is_not_cabled_even_for_a_single_client():
    netbox = FakeNetboxGateway()
    netbox.seed_switch(SWITCH_MAC, "sw", ["Port 26"])
    summary = _run(
        netbox,
        [make_unifi_client(mac="11:22:33:44:55:01", is_wired=True, switch_mac=SWITCH_MAC, switch_port=26)],
        devices=[_switch([UnifiPort(index=26, media="SFP+", is_uplink=True)])],
    )
    assert summary.cables_created == 0
    assert summary.cable_skips[0].reason == "uplink_port"
    assert netbox.switches[SWITCH_MAC].interfaces["Port 26"].cable is None


def test_skip_report_lists_every_cable_and_counts_reasons(tmp_path):
    report = tmp_path / "skips.csv"
    netbox = FakeNetboxGateway()
    netbox.seed_switch(SWITCH_MAC, "core-switch", ["mgmt0", "ae0"])
    summary = _run(
        netbox,
        [
            make_unifi_client(mac="11:22:33:44:55:01", is_wired=True, switch_mac=SWITCH_MAC, switch_port=4),
            make_unifi_client(mac="11:22:33:44:55:02", is_wired=True, switch_mac="aa:bb:cc:dd:ee:99", switch_port=1),
        ],
        cable_skip_report=str(report),
    )
    assert summary.cables_skipped == 2
    reasons = {skip.reason for skip in summary.cable_skips}
    assert reasons == {"port_not_found", "switch_not_found"}

    with report.open(newline="", encoding="utf-8") as handle:
        rows = list(csv.DictReader(handle))
    assert [row["reason"] for row in rows] == ["port_not_found", "switch_not_found"]
    port_row = rows[0]
    assert port_row["site"] == "default"
    assert port_row["netbox_site"] == "main"
    assert port_row["switch"] == "core-switch"
    assert port_row["switch_mac"] == SWITCH_MAC
    assert port_row["unifi_port"] == "4"
    assert "Port 4" in port_row["names_tried"]
    assert "mgmt0" in port_row["netbox_interfaces"]
    assert "ae0" in port_row["netbox_interfaces"]
