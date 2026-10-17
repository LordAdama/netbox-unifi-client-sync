"""Infra uplink cabling, the dry-run legacy eth0 report, and the wired name."""

from __future__ import annotations

import logging

from unifi_netbox_sync.models import UnifiPort, UnifiSwitchDevice
from unifi_netbox_sync.unifi_client import _parse_uplink

from .fakes import FakeNetboxGateway, make_unifi_client
from .test_device_sync import SWITCH_MAC, _run, _switch

DOWN_MAC = "aa:bb:cc:dd:ee:09"


def _downstream(**kw):
    return UnifiSwitchDevice(
        mac=DOWN_MAC,
        name="switch-02",
        model="USW24POE",
        device_type="usw",
        serial="F09FC2AAAA09",
        ports=[UnifiPort(index=i, media="GE", max_speed=1000) for i in range(1, 9)],
        uplink_mac=SWITCH_MAC,
        uplink_remote_port=kw.pop("remote", 5),
        uplink_local_port=kw.pop("local", 8),
    )


def test_parse_uplink_reads_wired_ports_only():
    assert _parse_uplink({"type": "wire", "uplink_mac": "AA:BB:CC:DD:EE:01", "uplink_remote_port": 14, "port_idx": 24}) == {
        "uplink_mac": "aa:bb:cc:dd:ee:01",
        "uplink_remote_port": 14,
        "uplink_local_port": 24,
    }
    assert _parse_uplink({"type": "wireless", "uplink_mac": "aa:bb:cc:dd:ee:01"}) == {}
    assert _parse_uplink({"type": "wire", "uplink_mac": "aa:bb:cc:dd:ee:01", "port_idx": 0})["uplink_local_port"] is None


def test_uplink_cable_joins_the_real_ports_on_both_switches():
    netbox = FakeNetboxGateway()

    summary, _ = _run([_switch(), _downstream()], netbox=netbox)

    assert summary.errors == []
    assert summary.uplink_cables_created == 1
    up = netbox.infra_by_mac[SWITCH_MAC]
    down = netbox.infra_by_mac[DOWN_MAC]
    pair = tuple(sorted((down.interfaces["Port 8"].id, up.interfaces["Port 5"].id)))
    assert netbox.cables == {pair}
    assert "eth0" not in down.interfaces

    again, _ = _run([_switch(), _downstream()], netbox=netbox)
    assert again.uplink_cables_created == 0
    assert again.uplink_cables_unchanged == 1


def test_uplink_dry_run_plans_and_writes_nothing(caplog):
    netbox = FakeNetboxGateway()
    netbox.seed_switch(SWITCH_MAC, "switch-01", [f"Port {i}" for i in range(1, 9)])
    netbox.seed_switch(DOWN_MAC, "switch-02", [f"Port {i}" for i in range(1, 9)])

    with caplog.at_level(logging.INFO):
        summary, _ = _run([_switch(), _downstream()], netbox=netbox, dry_run=True)

    assert summary.uplink_cables_created == 1
    assert netbox.cables == set()
    assert "would create uplink cable" in caplog.text


def test_uplink_without_local_port_is_skipped_not_guessed():
    netbox = FakeNetboxGateway()
    netbox.seed_switch(SWITCH_MAC, "switch-01", [f"Port {i}" for i in range(1, 9)])
    netbox.seed_switch(DOWN_MAC, "switch-02", [f"Port {i}" for i in range(1, 9)])
    device = _downstream()
    device.uplink_local_port = None

    summary, _ = _run([_switch(), device], netbox=netbox)

    assert summary.uplink_cables_created == 0
    assert summary.uplink_cables_skipped == 1
    assert netbox.cables == set()


def test_dry_run_reports_cabled_legacy_eth0_on_infra_without_writing(caplog):
    netbox = FakeNetboxGateway()
    # Untagged, hand-made record of a switch that still carries a client eth0.
    holder = netbox.seed_legacy_client(SWITCH_MAC, tag="other", name="core-switch")
    peer = netbox.seed_switch("11:22:33:44:55:66", "switch-03", ["Port 1"]).interfaces["Port 1"]
    eth0 = holder.interfaces["eth0"]
    eth0.cable = object()
    eth0.link_peers = [peer]

    with caplog.at_level(logging.INFO):
        summary, _ = _run([_switch()], netbox=netbox, dry_run=True)

    assert summary.legacy_eth0_found == 1
    assert "legacy eth0 on switch" in caplog.text
    assert "eth0" in holder.interfaces


def test_legacy_eth0_report_is_dry_run_only():
    netbox = FakeNetboxGateway()
    holder = netbox.seed_legacy_client(SWITCH_MAC, tag="other", name="core-switch")
    holder.interfaces["eth0"].cable = object()

    summary, _ = _run([_switch()], netbox=netbox)

    assert summary.legacy_eth0_found == 0
    assert "eth0" in holder.interfaces


def test_client_wired_interface_name_is_configurable():
    netbox = FakeNetboxGateway()
    client = make_unifi_client(mac="aa:bb:cc:00:00:01", name="printer-01", ip="10.0.0.5", is_wired=True)

    _run([], clients=[client], netbox=netbox, client_wired_interface_name="lan0")

    device = netbox.clients_by_mac["aa:bb:cc:00:00:01"]
    assert "lan0" in device.interfaces
    assert "eth0" not in device.interfaces


def test_wired_interface_name_defaults_to_eth0(monkeypatch):
    from unifi_netbox_sync.config import Settings

    monkeypatch.delenv("CLIENT_WIRED_INTERFACE_NAME", raising=False)
    assert Settings.client_wired_interface_name == "eth0"
