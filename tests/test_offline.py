"""Offline marking uses last_seen, and status writes do not replace the record."""

from __future__ import annotations

import logging
import time
from types import SimpleNamespace
from unittest.mock import MagicMock

from unifi_netbox_sync.models import UnifiKnownClient
from unifi_netbox_sync.netbox_client import PynetboxGateway
from unifi_netbox_sync.staleness import offline_decision
from unifi_netbox_sync.sync import SyncEngine

from .fakes import FakeNetboxGateway, FakeUnifiClient, make_unifi_client
from .test_sync import make_settings

STALE_MAC = "99:88:77:66:55:44"
PHONE_MAC = "02:11:22:33:44:55"  # locally administered
DAY = 24 * 3600


def _device(netbox, mac, name="laptop"):
    device, *_ = netbox.upsert_client_device(
        mac, name, "main", "unifi-client", "generic-network-client", "unifi-sync"
    )
    return device


def _run(netbox, unifi, **settings):
    return SyncEngine(unifi=unifi, netbox=netbox, settings=make_settings(**settings)).run()


def test_idle_client_inside_the_grace_stays_active():
    netbox = FakeNetboxGateway()
    device = _device(netbox, STALE_MAC)
    summary = _run(
        netbox,
        FakeUnifiClient(
            clients=[],
            known=[UnifiKnownClient(mac=STALE_MAC, last_seen=time.time() - DAY)],
        ),
        stale_offline_grace_seconds=7 * DAY,
    )
    assert summary.stale_marked_offline == 0
    assert device.status.value == "active"


def test_last_seen_past_the_grace_is_marked_offline():
    netbox = FakeNetboxGateway()
    device = _device(netbox, STALE_MAC)
    summary = _run(
        netbox,
        FakeUnifiClient(
            clients=[],
            known=[UnifiKnownClient(mac=STALE_MAC, last_seen=time.time() - 8 * DAY)],
        ),
        stale_offline_grace_seconds=7 * DAY,
    )
    assert summary.stale_marked_offline == 1
    assert device.status.value == "offline"
    assert device.name == "laptop"
    assert device.custom_fields["unifi_mac"] == STALE_MAC


def test_known_client_with_no_timestamp_is_not_marked():
    netbox = FakeNetboxGateway()
    device = _device(netbox, STALE_MAC)
    summary = _run(
        netbox,
        FakeUnifiClient(clients=[], known=[UnifiKnownClient(mac=STALE_MAC, last_seen=None)]),
    )
    assert summary.stale_marked_offline == 0
    assert device.status.value == "active"


def test_randomized_mac_inside_grace_stays_and_past_grace_is_offline(caplog):
    netbox = FakeNetboxGateway()
    sleeping = _device(netbox, PHONE_MAC, "phone")
    summary = _run(
        netbox,
        FakeUnifiClient(
            clients=[],
            known=[UnifiKnownClient(mac=PHONE_MAC, last_seen=time.time() - 2 * 3600)],
        ),
        stale_offline_grace_seconds=DAY,
    )
    assert summary.stale_marked_offline == 0
    assert sleeping.status.value == "active"

    netbox = FakeNetboxGateway()
    rotated = _device(netbox, PHONE_MAC, "phone")
    with caplog.at_level(logging.INFO):
        summary = _run(
            netbox,
            FakeUnifiClient(
                clients=[],
                known=[UnifiKnownClient(mac=PHONE_MAC, last_seen=time.time() - 3 * DAY)],
            ),
            stale_offline_grace_seconds=DAY,
        )
    assert summary.stale_marked_offline == 1
    assert rotated.status.value == "offline"
    assert "Randomized MAC" in "\n".join(record.getMessage() for record in caplog.records)


def test_empty_history_while_clients_are_active_does_not_offline_anyone(caplog):
    netbox = FakeNetboxGateway()
    idle = _device(netbox, STALE_MAC, "idle-laptop")
    with caplog.at_level(logging.WARNING):
        summary = _run(
            netbox,
            FakeUnifiClient(
                clients=[make_unifi_client(mac="11:22:33:44:55:01", name="active", is_wired=False)],
                known=[],
            ),
        )
    assert summary.stale_marked_offline == 0
    assert idle.status.value == "active"
    assert summary.devices_created == 1
    assert "no last_seen history" in "\n".join(record.getMessage() for record in caplog.records)


def test_history_read_failure_skips_offline_marking():
    netbox = FakeNetboxGateway()
    device = _device(netbox, STALE_MAC)
    summary = _run(
        netbox,
        FakeUnifiClient(clients=[], known_error=LookupError("stat/alluser failed")),
    )
    assert summary.stale_marked_offline == 0
    assert device.status.value == "active"
    assert summary.errors == []


def test_offline_decision_table():
    now = 1_000_000.0
    known = {"aa:bb:cc:dd:ee:01": now - 10, "aa:bb:cc:dd:ee:02": None}
    assert offline_decision("aa:bb:cc:dd:ee:01", known, now, 30).mark is False
    assert offline_decision("aa:bb:cc:dd:ee:01", known, now, 5).reason == "expired"
    assert offline_decision("aa:bb:cc:dd:ee:02", known, now, 30).reason == "no_last_seen"
    assert offline_decision("ff:ff:ff:ff:ff:ff", known, now, 30).reason == "not_known"


def test_mark_offline_patches_status_and_nothing_else(monkeypatch):
    captured: dict = {}

    class FakeRequest:
        def __init__(self, **kwargs):
            captured["init"] = kwargs

        def patch(self, data):
            captured["patch"] = dict(data)
            return {"id": 7, "status": {"value": "offline"}, "name": "laptop", "comments": "keep"}

    monkeypatch.setattr("unifi_netbox_sync.netbox_client.Request", FakeRequest)
    gateway = PynetboxGateway.__new__(PynetboxGateway)
    gateway.api = SimpleNamespace(
        token="token",
        http_session=object(),
        dcim=SimpleNamespace(devices=SimpleNamespace(url="https://netbox.example.com/api/dcim/devices")),
    )
    device = MagicMock()
    device.id = 7
    device.name = "laptop"
    device.comments = "operator note"

    gateway.mark_offline(device)

    assert captured["patch"] == {"status": "offline"}
    device.save.assert_not_called()
    device._parse_values.assert_called_once()


def test_status_reactivation_patches_only_the_changed_fields(monkeypatch):
    captured: dict = {}

    class FakeRequest:
        def __init__(self, **kwargs):
            captured["init"] = kwargs

        def patch(self, data):
            captured["patch"] = dict(data)
            return {"id": 3}

    monkeypatch.setattr("unifi_netbox_sync.netbox_client.Request", FakeRequest)
    gateway = PynetboxGateway.__new__(PynetboxGateway)
    gateway.api = SimpleNamespace(
        token="token",
        http_session=object(),
        dcim=SimpleNamespace(devices=SimpleNamespace(url="https://netbox.example.com/api/dcim/devices")),
    )
    device = SimpleNamespace(
        id=3,
        name="hand-named",
        status=SimpleNamespace(value="offline"),
        device_type=SimpleNamespace(slug="generic-network-client"),
        comments="keep me",
        tags=["unifi-sync"],
        custom_fields={"unifi_mac": "11:22:33:44:55:66"},
        _parse_values=lambda result: None,
    )
    gateway.find_site = lambda slug: SimpleNamespace(id=1)  # type: ignore[method-assign]

    _device, created, skipped, updated = gateway.upsert_client_device(
        "11:22:33:44:55:66",
        "hand-named",
        "main",
        "unifi-client",
        "generic-network-client",
        "unifi-sync",
        update_policy="sync",
        existing=device,
        existing_looked_up=True,
    )

    assert created is False
    assert skipped is False
    assert updated is True
    assert captured["patch"] == {"status": "active"}
    assert "comments" not in captured["patch"]
    assert "name" not in captured["patch"]
