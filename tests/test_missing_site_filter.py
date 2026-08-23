from __future__ import annotations

import logging
from unittest.mock import MagicMock

import pynetbox
import pytest

from unifi_netbox_sync.caching import CachingNetboxGateway
from unifi_netbox_sync.models import UnifiSite, UnifiSwitchDevice
from unifi_netbox_sync.netbox_client import PynetboxGateway
from unifi_netbox_sync.sync import SyncEngine

from .fakes import FakeNetboxGateway, FakeUnifiClient, make_unifi_client
from .test_sync import make_settings


def _clients(n=3):
    return [
        make_unifi_client(mac=f"11:22:33:44:55:{i:02x}", name=f"client-{i}", is_wired=False)
        for i in range(n)
    ]


# -- the reported failure ----------------------------------------------


def test_dry_run_against_sites_that_do_not_exist_yet_reports_a_plan():
    """The reported bug: --dry-run with SITE_POLICY=create over discovered
    sites errored on every client, because the name-uniqueness filter names a
    site NetBox doesn't have yet and NetBox 400s rather than returning [].

    This is the case dry-run exists for, so it must produce a plan.
    """
    netbox = FakeNetboxGateway()
    netbox.missing_sites.add("acme-s-depot-north-yard")
    engine = SyncEngine(
        unifi=FakeUnifiClient(
            by_site={"north": _clients()},
            sites=[UnifiSite(name="north", description="Acme's Depot - North Yard")],
        ),
        netbox=CachingNetboxGateway(netbox),
        settings=make_settings(sync_all_sites=True, site_policy="create", dry_run=True),
    )

    summary = engine.run()

    assert summary.errors == [], "a preview of a not-yet-created site must not error"
    assert summary.clients_seen == 3
    assert summary.devices_created == 3  # all planned
    assert netbox.clients_by_mac == {}  # still a dry run


def test_real_run_creates_the_site_then_syncs_into_it():
    netbox = FakeNetboxGateway()
    netbox.missing_sites.add("head-office")
    engine = SyncEngine(
        unifi=FakeUnifiClient(
            by_site={"s1": _clients()}, sites=[UnifiSite(name="s1", description="Head Office")]
        ),
        netbox=CachingNetboxGateway(netbox),
        settings=make_settings(sync_all_sites=True, site_policy="create"),
    )

    summary = engine.run()

    assert summary.errors == []
    assert summary.sites_created == 1
    assert summary.devices_created == 3


def test_name_collision_check_still_works_once_the_site_exists():
    """The guard must not disable collision detection for real sites."""
    netbox = FakeNetboxGateway()
    netbox.upsert_client_device(
        "aa:aa:aa:aa:aa:aa", "laptop", "main", "unifi-client", "generic-network-client", "unifi-sync"
    )
    engine = SyncEngine(
        unifi=FakeUnifiClient(clients=[make_unifi_client(mac="11:22:33:44:55:66", name="laptop", is_wired=False)]),
        netbox=CachingNetboxGateway(netbox),
        settings=make_settings(),
    )

    engine.run()

    assert netbox.clients_by_mac["11:22:33:44:55:66"].name == "laptop-5566"


# -- gateway-level: the actual NetBox behavior --------------------------


def _gateway_with(api):
    gateway = PynetboxGateway.__new__(PynetboxGateway)
    gateway.api = api
    import threading

    gateway._memo_lock = threading.Lock()
    gateway._device_type_memo = {}
    gateway._role_memo = {}
    gateway._site_memo = {}
    gateway._client_type_memo = {}
    gateway._infra_type_memo = {}
    gateway._site_missing = set()
    gateway._warned = set()
    return gateway


def test_gateway_does_not_query_devices_for_a_missing_site():
    api = MagicMock()
    api.dcim.sites.get.return_value = None  # site absent

    gateway = _gateway_with(api)
    taken = gateway.device_name_taken_by_other("laptop", "nope", "11:22:33:44:55:66")

    assert taken is False
    api.dcim.devices.filter.assert_not_called(), "must not issue the filter NetBox would reject"


def test_gateway_caches_the_missing_site_instead_of_asking_per_client():
    api = MagicMock()
    api.dcim.sites.get.return_value = None

    gateway = _gateway_with(api)
    for _ in range(25):
        gateway.device_name_taken_by_other("x", "nope", "11:22:33:44:55:66")

    assert api.dcim.sites.get.call_count == 1, "negative site result should be cached"


def test_gateway_degrades_when_netbox_rejects_the_filter(caplog):
    """Some NetBox versions may reject this filter for other reasons; one
    unanswerable query must not kill every client."""
    api = MagicMock()
    api.dcim.sites.get.return_value = object()  # site exists
    response = MagicMock()
    response.status_code = 400
    response.json.return_value = {"site": ["Select a valid choice."]}
    api.dcim.devices.filter.side_effect = pynetbox.RequestError(response)

    gateway = _gateway_with(api)
    with caplog.at_level(logging.WARNING):
        taken = gateway.device_name_taken_by_other("laptop", "main", "11:22:33:44:55:66")

    assert taken is False
    assert "assuming names are free" in "\n".join(r.getMessage() for r in caplog.records)


def test_gateway_still_raises_on_non_validation_errors():
    """A 500 is a real outage, not something to paper over."""
    api = MagicMock()
    api.dcim.sites.get.return_value = object()
    response = MagicMock()
    response.status_code = 500
    response.json.return_value = {"detail": "boom"}
    api.dcim.devices.filter.side_effect = pynetbox.RequestError(response)

    gateway = _gateway_with(api)
    with pytest.raises(pynetbox.RequestError):
        gateway.device_name_taken_by_other("laptop", "main", "11:22:33:44:55:66")


def test_gateway_skips_stale_query_for_a_missing_site():
    api = MagicMock()
    api.dcim.sites.get.return_value = None

    gateway = _gateway_with(api)

    assert gateway.list_synced_client_devices("unifi-sync", "nope") == []
    api.dcim.devices.filter.assert_not_called()


# -- dry-run diagnostics -----------------------------------------------


def test_dry_run_explains_why_a_cable_would_be_skipped(caplog):
    """A reported run showed '45 cables skipped' with no warning at all — the one
    number an operator needs explained."""
    netbox = FakeNetboxGateway()
    netbox.seed_switch("aa:bb:cc:dd:ee:01", "core-switch", ["xe-0/0/1", "xe-0/0/2"])
    client = make_unifi_client(
        mac="11:22:33:44:55:66",
        is_wired=True,
        switch_mac="aa:bb:cc:dd:ee:01",
        switch_port=1,
    )
    engine = SyncEngine(
        unifi=FakeUnifiClient(
            clients=[client],
            devices=[
                UnifiSwitchDevice(
                    mac="aa:bb:cc:dd:ee:01", name="core-switch", model="USW", device_type="usw"
                )
            ],
        ),
        netbox=CachingNetboxGateway(netbox),
        settings=make_settings(dry_run=True),
    )

    with caplog.at_level(logging.WARNING):
        summary = engine.run()

    assert summary.cables_skipped == 1
    messages = "\n".join(r.getMessage() for r in caplog.records)
    assert "xe-0/0/1" in messages, "dry-run must name the real interfaces too"
    assert "PORT_NAME_TEMPLATES" in messages


def test_dry_run_explains_a_missing_switch(caplog):
    client = make_unifi_client(
        mac="11:22:33:44:55:66", is_wired=True, switch_mac="aa:bb:cc:dd:ee:99", switch_port=1
    )
    engine = SyncEngine(
        unifi=FakeUnifiClient(clients=[client]),
        netbox=CachingNetboxGateway(FakeNetboxGateway()),
        settings=make_settings(dry_run=True),
    )

    with caplog.at_level(logging.WARNING):
        summary = engine.run()

    assert summary.cables_skipped == 1
    assert "No NetBox device found for UniFi switch" in "\n".join(
        r.getMessage() for r in caplog.records
    )
