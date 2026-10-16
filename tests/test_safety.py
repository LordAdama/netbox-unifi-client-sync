"""Production-safety behavior: site identity, dry-run, managed objects, IP conflicts."""

from __future__ import annotations

import logging
from types import SimpleNamespace
from unittest.mock import MagicMock

import pynetbox
import pytest
import requests

from unifi_netbox_sync.cli import parse_args
from unifi_netbox_sync.config import Settings
from unifi_netbox_sync.health import write_success_marker
from unifi_netbox_sync.models import UnifiSite, UnifiSwitchDevice
from unifi_netbox_sync.netbox_client import (
    PynetboxGateway,
    TimeoutSession,
    device_name_from_primary_error,
    is_primary_ip_conflict,
)
from unifi_netbox_sync.sync import SyncEngine
from unifi_netbox_sync.unifi_client import UnifiClientAPI

from .fakes import FakeInterface, FakeIP, FakeNetboxGateway, FakeUnifiClient, make_unifi_client
from .test_sync import make_settings


def test_from_env_defaults_are_non_destructive(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("UNIFI_HOST", "https://unifi.example.com")
    monkeypatch.setenv("UNIFI_API_KEY", "secret-key")
    monkeypatch.setenv("NETBOX_URL", "https://netbox.example.com")
    monkeypatch.setenv("NETBOX_TOKEN", "token")
    monkeypatch.setenv("NETBOX_SITE_SLUG", "main")
    monkeypatch.delenv("DRY_RUN", raising=False)
    monkeypatch.delenv("DEVICE_UPDATE_POLICY", raising=False)
    monkeypatch.delenv("SITE_POLICY", raising=False)
    monkeypatch.delenv("CABLE_CONFLICT_POLICY", raising=False)

    settings = Settings.from_env()

    assert settings.dry_run is True
    assert settings.device_update_policy == "create-only"
    assert settings.site_policy == "require"
    assert settings.cable_conflict_policy == "skip"
    assert settings.netbox_timeout == 30.0
    assert settings.unifi_timeout == 30.0


def test_apply_flag_overrides_dry_run_default():
    args = parse_args(["--apply"])
    assert args.apply is True
    assert args.dry_run is False
    with pytest.raises(SystemExit):
        parse_args(["--apply", "--dry-run"])


def test_success_marker_is_written_atomically(tmp_path):
    path = tmp_path / "nested" / "last-sync-ok"
    write_success_marker(str(path))
    assert path.is_file()
    assert not path.with_name(path.name + ".tmp").exists()
    text = path.read_text()
    assert "T" in text  # ISO-8601 timestamp


def test_renamed_site_is_found_by_unifi_id_and_not_renamed_back():
    netbox = FakeNetboxGateway()
    site = netbox.seed_site("ops-renamed-it", "Operations", unifi_site_id="7xk2p9qr")
    before = (site.slug, site.name, site.id)

    summary = SyncEngine(
        unifi=FakeUnifiClient(
            sites=[UnifiSite("7xk2p9qr", "North Warehouse")],
            by_site={"7xk2p9qr": [make_unifi_client(mac="11:22:33:44:55:01", name="laptop", is_wired=False)]},
        ),
        netbox=netbox,
        settings=make_settings(sync_all_sites=True, site_policy="create"),
    ).run()

    assert summary.sites_created == 0
    assert summary.errors == []
    assert (site.slug, site.name, site.id) == before
    assert netbox.clients_by_mac["11:22:33:44:55:01"].site_slug == "ops-renamed-it"
    assert "north-warehouse" not in netbox.created_sites


def test_name_match_records_unifi_id_without_touching_the_name():
    netbox = FakeNetboxGateway()
    site = netbox.seed_site("acmes-depot-north-yard", "Acme's Depot - North Yard")

    SyncEngine(
        unifi=FakeUnifiClient(
            sites=[UnifiSite("north", "Acme's Depot - North Yard")],
            by_site={"north": [make_unifi_client(mac="11:22:33:44:55:01", name="c1", is_wired=False)]},
        ),
        netbox=netbox,
        settings=make_settings(sync_all_sites=True, site_policy="create"),
    ).run()

    assert site.custom_fields["unifi_site_id"] == "north"
    assert site.name == "Acme's Depot - North Yard"
    assert site.slug == "acmes-depot-north-yard"
    assert netbox.created_sites == set()


def test_dry_run_logs_intended_changes_and_writes_nothing(caplog):
    netbox = FakeNetboxGateway()
    netbox.seed_switch("aa:bb:cc:dd:ee:01", "switch-1", ["Port 1"])
    stale, *_ = netbox.upsert_client_device(
        "99:88:77:66:55:44", "old-laptop", "main", "unifi-client", "generic-network-client", "unifi-sync"
    )
    client = make_unifi_client(
        mac="11:22:33:44:55:66",
        name="laptop",
        ip="10.9.9.9",
        is_wired=True,
        switch_mac="aa:bb:cc:dd:ee:01",
        switch_port=1,
    )
    with caplog.at_level(logging.INFO):
        summary = SyncEngine(
            unifi=FakeUnifiClient(clients=[client]),
            netbox=netbox,
            settings=make_settings(dry_run=True),
        ).run()

    text = "\n".join(record.getMessage() for record in caplog.records)
    assert "[dry-run] would create device 'laptop'" in text
    assert "[dry-run] would assign IP 10.9.9.9" in text
    assert "[dry-run] would create cable" in text
    assert "[dry-run] would mark device 'old-laptop'" in text
    assert summary.devices_created == 1
    assert summary.cables_created == 1
    assert summary.stale_marked_offline == 1
    assert "11:22:33:44:55:66" not in netbox.clients_by_mac
    assert stale.status.value == "active"


def test_untagged_device_is_not_modified_even_when_sync_is_on():
    netbox = FakeNetboxGateway()
    device, *_ = netbox.upsert_client_device(
        "11:22:33:44:55:66", "hand-renamed", "main", "unifi-client", "generic-network-client", "unifi-sync"
    )
    device.tags.clear()

    summary = SyncEngine(
        unifi=FakeUnifiClient(
            clients=[make_unifi_client(mac="11:22:33:44:55:66", name="from-unifi", ip="10.1.1.9", is_wired=False)]
        ),
        netbox=netbox,
        settings=make_settings(device_update_policy="sync"),
    ).run()

    assert device.name == "hand-renamed"
    assert device.primary_ip4 is None
    assert summary.devices_updated == 0
    assert summary.devices_update_skipped == 1
    assert summary.errors == []


def test_stale_marking_ignores_infrastructure_and_untagged_devices():
    netbox = FakeNetboxGateway()
    client, *_ = netbox.upsert_client_device(
        "11:22:33:44:55:66", "old-laptop", "main", "unifi-client", "generic-network-client", "unifi-sync"
    )
    manual, *_ = netbox.upsert_client_device(
        "22:22:22:22:22:22", "manual-laptop", "main", "unifi-client", "generic-network-client", "unifi-sync"
    )
    manual.tags.clear()
    switch, _, _ = netbox.upsert_infrastructure_device(
        "aa:bb:cc:dd:ee:01", "core-switch", "main", "switch", "usw", "unifi-sync", serial="ABC"
    )

    summary = SyncEngine(
        unifi=FakeUnifiClient(clients=[]),
        netbox=netbox,
        settings=make_settings(),
    ).run()

    assert summary.stale_marked_offline == 1
    assert client.status.value == "offline"
    assert manual.status.value == "active"
    assert switch.status.value == "active"


def test_replace_does_not_delete_an_untagged_cable():
    netbox = FakeNetboxGateway()
    switch = netbox.seed_switch("aa:bb:cc:dd:ee:01", "switch-1", ["Port 1"])
    port = switch.interfaces["Port 1"]
    other = FakeInterface(switch, "somewhere-else")
    pair = tuple(sorted((port.id, other.id)))
    port.cable = pair
    other.cable = pair
    port.link_peers = [other]
    netbox.cables.add(pair)
    netbox.cable_tags[pair] = []

    summary = SyncEngine(
        unifi=FakeUnifiClient(
            clients=[
                make_unifi_client(
                    mac="11:22:33:44:55:66",
                    is_wired=True,
                    switch_mac="aa:bb:cc:dd:ee:01",
                    switch_port=1,
                )
            ]
        ),
        netbox=netbox,
        settings=make_settings(cable_conflict_policy="replace"),
    ).run()

    assert summary.cables_created == 0
    assert pair in netbox.cables
    assert "not tagged" in (summary.client_results[0].cable_skipped_reason or "")


def test_missing_switch_warns_and_does_not_create_a_device(caplog):
    netbox = FakeNetboxGateway()
    with caplog.at_level(logging.WARNING):
        summary = SyncEngine(
            unifi=FakeUnifiClient(
                clients=[
                    make_unifi_client(
                        mac="11:22:33:44:55:66",
                        is_wired=True,
                        switch_mac="aa:bb:cc:dd:ee:99",
                        switch_port=1,
                    )
                ]
            ),
            netbox=netbox,
            settings=make_settings(),
        ).run()

    assert summary.cables_skipped == 1
    assert summary.devices_created == 1  # the client, not a switch
    assert netbox.infra_by_mac == {}
    assert "No infrastructure device will be created" in "\n".join(r.getMessage() for r in caplog.records)


def test_switch_with_no_interfaces_warns_and_creates_nothing(caplog):
    netbox = FakeNetboxGateway()
    netbox.seed_switch("aa:bb:cc:dd:ee:01", "bare-switch", [])
    with caplog.at_level(logging.WARNING):
        summary = SyncEngine(
            unifi=FakeUnifiClient(
                clients=[
                    make_unifi_client(
                        mac="11:22:33:44:55:66",
                        is_wired=True,
                        switch_mac="aa:bb:cc:dd:ee:01",
                        switch_port=3,
                    )
                ],
                devices=[
                    UnifiSwitchDevice(mac="aa:bb:cc:dd:ee:01", name="bare-switch", model="USW", device_type="usw")
                ],
            ),
            netbox=netbox,
            settings=make_settings(),
        ).run()

    assert summary.cables_skipped == 1
    assert netbox.infra_by_mac == {}
    text = "\n".join(record.getMessage() for record in caplog.records)
    assert "has no interfaces" in text
    assert "No device or interface will be created" in text


def test_one_site_error_does_not_stop_another_site():
    class RaiseForSite(FakeNetboxGateway):
        def upsert_client_device(self, mac, name, site_slug, *args, **kwargs):
            if site_slug == "bad":
                raise TypeError("boom in bad")
            return super().upsert_client_device(mac, name, site_slug, *args, **kwargs)

    netbox = RaiseForSite()
    summary = SyncEngine(
        unifi=FakeUnifiClient(
            by_site={
                "good-unifi": [make_unifi_client(mac="11:22:33:44:55:01", name="ok", is_wired=False)],
                "bad-unifi": [make_unifi_client(mac="11:22:33:44:55:02", name="nope", is_wired=False)],
            }
        ),
        netbox=netbox,
        settings=make_settings(site_map={"good-unifi": "good", "bad-unifi": "bad"}),
    ).run()

    assert any("boom in bad" in error for error in summary.errors)
    assert netbox.clients_by_mac["11:22:33:44:55:01"].site_slug == "good"
    assert "11:22:33:44:55:02" not in netbox.clients_by_mac


def test_unifi_requests_use_the_configured_timeout():
    captured: dict[str, object] = {}

    class Response:
        status_code = 200

        def raise_for_status(self) -> None:
            return None

        def json(self) -> dict:
            return {"data": []}

    client = UnifiClientAPI(host="https://unifi.example.com", api_key="k", timeout=7)
    client._authenticated = True

    def fake_get(url, timeout=None):
        captured["url"] = url
        captured["timeout"] = timeout
        return Response()

    client.session.get = fake_get
    assert client.get_sites() == []
    assert captured["timeout"] == 7


def test_netbox_session_has_a_timeout():
    gateway = PynetboxGateway("https://netbox.example.com", "token", timeout=12)
    assert isinstance(gateway.api.http_session, TimeoutSession)
    assert gateway.api.http_session.timeout == 12
    assert gateway.api.http_session.verify is True


def _session_that_records(monkeypatch, outcomes: list, delays: list):
    """Parent ``Session.request`` stand-in. ``outcomes`` are raised or returned."""
    session = TimeoutSession(30, sleep=delays.append)
    seen: list[tuple[str, float | None]] = []

    def wrapped(self, method, url, **kwargs):
        seen.append((method, kwargs.get("timeout")))
        outcome = outcomes.pop(0)
        if isinstance(outcome, BaseException):
            raise outcome
        return outcome

    monkeypatch.setattr(requests.Session, "request", wrapped)
    return session, seen


def test_netbox_read_retries_a_timeout_then_succeeds(monkeypatch):
    delays: list[float] = []
    response = MagicMock(status_code=200)
    session, seen = _session_that_records(
        monkeypatch,
        [requests.exceptions.ReadTimeout("timed out"), requests.exceptions.ConnectionError("reset"), response],
        delays,
    )

    got = session.get("https://netbox.example.com/api/dcim/devices/")

    assert got is response
    assert [method for method, _ in seen] == ["GET", "GET", "GET"]
    assert all(timeout == 30 for _, timeout in seen)
    assert delays == [1.0, 2.0]


def test_netbox_read_gives_up_after_three_attempts(monkeypatch):
    delays: list[float] = []
    session, seen = _session_that_records(
        monkeypatch,
        [requests.exceptions.ReadTimeout("timed out")] * 3,
        delays,
    )

    with pytest.raises(requests.exceptions.ReadTimeout):
        session.get("https://netbox.example.com/api/dcim/devices/")

    assert len(seen) == 3
    assert delays == [1.0, 2.0]


def test_netbox_write_is_not_retried_on_timeout(monkeypatch):
    delays: list[float] = []
    session, seen = _session_that_records(
        monkeypatch,
        [requests.exceptions.ReadTimeout("timed out")],
        delays,
    )

    with pytest.raises(requests.exceptions.ReadTimeout):
        session.post("https://netbox.example.com/api/dcim/devices/", json={"name": "sw"})

    assert len(seen) == 1
    assert delays == []


def test_netbox_read_does_not_retry_an_http_error(monkeypatch):
    delays: list[float] = []
    response = MagicMock(status_code=500)
    session, seen = _session_that_records(monkeypatch, [response], delays)

    got = session.get("https://netbox.example.com/api/dcim/devices/")

    assert got.status_code == 500
    assert len(seen) == 1
    assert delays == []


def test_missing_site_id_field_falls_back_to_name_matching(caplog):
    """A dry-run before the custom field exists must still link by name."""
    import threading

    response = MagicMock()
    response.status_code = 400
    response.reason = "Bad Request"
    response.url = "https://netbox.example.com/api/dcim/sites/"
    response.request = MagicMock(body=b"")
    response.text = "unknown custom field"
    response.json.return_value = {"cf_unifi_site_id": ["unknown"]}

    matched = SimpleNamespace(slug="hq-01", name="Head Office", custom_fields={})
    api = MagicMock()
    api.dcim.sites.get.return_value = None
    api.dcim.sites.filter.side_effect = [pynetbox.RequestError(response), [matched]]

    gateway = PynetboxGateway.__new__(PynetboxGateway)
    gateway.api = api
    gateway._memo_lock = threading.Lock()
    gateway._site_memo = {}
    gateway._all_sites = None

    with caplog.at_level(logging.WARNING):
        found = gateway.resolve_existing_site("head-office", "Head Office", "name", "7xk2p9qr")

    assert found is matched
    assert "falling back to name/slug matching" in "\n".join(r.getMessage() for r in caplog.records)


def _sites_api(sites):
    """NetBox stand-in whose cf_unifi_site_id filter is ignored.

    That is what the production dry-run hit: the custom field does not exist
    yet, NetBox drops the unknown parameter, and the unfiltered site list
    comes back with Site A first.
    """
    api = MagicMock()

    def sites_filter(**kwargs):
        if any(key.startswith("cf_") for key in kwargs):
            return list(sites)
        if "name" in kwargs:
            return [site for site in sites if site.name == kwargs["name"]]
        return list(sites)

    def sites_get(slug=None, **kwargs):
        return next((site for site in sites if site.slug == slug), None)

    api.dcim.sites.filter.side_effect = sites_filter
    api.dcim.sites.get.side_effect = sites_get
    api.dcim.sites.all.return_value = list(sites)
    return api


def _resolver(api) -> PynetboxGateway:
    import threading

    gateway = PynetboxGateway.__new__(PynetboxGateway)
    gateway.api = api
    gateway._memo_lock = threading.Lock()
    gateway._site_memo = {}
    gateway._all_sites = None
    gateway._unifi_site_id_filter_unusable = False
    return gateway


def test_ignored_site_id_filter_does_not_bind_every_unifi_site_to_site_a(caplog):
    """An ignored custom-field filter must not attach every UniFi site to Site A.

    UniFi id ``siteaid1`` really is Site A. The others must not be.
    """
    site_a = SimpleNamespace(id=1, slug="site-a", name="Site A", custom_fields={})
    site_b = SimpleNamespace(id=2, slug="site-b", name="Site B", custom_fields={})
    site_c = SimpleNamespace(id=3, slug="site-c", name="Site C", custom_fields={})
    gateway = _resolver(_sites_api([site_a, site_b, site_c]))

    with caplog.at_level(logging.INFO):
        assert gateway.resolve_existing_site("site-c", "Site C", "normalized", "sitecid3") is site_c
        assert gateway.resolve_existing_site("site-b", "Site B", "normalized", "default") is site_b
        assert gateway.resolve_existing_site("other-site", "Other Site", "normalized", "siteoid4") is None
        assert gateway.resolve_existing_site("site-a", "Site A", "normalized", "siteaid1") is site_a

    text = "\n".join(record.getMessage() for record in caplog.records)
    assert "via unifi_site_id" not in text
    assert "filter is being ignored" in text
    assert site_a.custom_fields == {}


class _CollapseToSiteA(FakeNetboxGateway):
    """Every lookup returns the first NetBox site, which is the failure mode."""

    def resolve_existing_site(self, desired_slug, label, mode="normalized", unifi_site_id="", **_kwargs):
        return self.existing_sites["site-a"]


def test_every_unifi_site_resolving_to_site_a_is_a_conflict(caplog):
    netbox = _CollapseToSiteA()
    site = netbox.seed_site("site-a", "Site A")
    sites = [
        UnifiSite("sitecid3", "Site C"),
        UnifiSite("default", "Site B"),
        UnifiSite("siteaid1", "Site A"),
    ]
    by_site = {
        "sitecid3": [make_unifi_client(mac="11:22:33:44:55:01", name="site-c-laptop", ip="10.1.1.1", is_wired=False)],
        "default": [make_unifi_client(mac="11:22:33:44:55:02", name="site-b-laptop", ip="10.2.2.2", is_wired=False)],
        "siteaid1": [make_unifi_client(mac="11:22:33:44:55:03", name="site-a-laptop", ip="10.3.3.3", is_wired=False)],
    }
    with caplog.at_level(logging.INFO):
        summary = SyncEngine(
            unifi=FakeUnifiClient(sites=sites, by_site=by_site),
            netbox=netbox,
            settings=make_settings(sync_all_sites=True, site_policy="create", dry_run=False),
        ).run()

    text = "\n".join(record.getMessage() for record in caplog.records)
    assert summary.devices_created == 0
    assert summary.clients_seen == 0
    assert netbox.clients_by_mac == {}
    assert site.custom_fields == {}
    assert "would record unifi_site_id" not in text
    assert any("sitecid3" in error and "siteaid1" in error and "site-a" in error for error in summary.errors)


def test_only_the_matching_site_is_linked_when_the_filter_is_honest():
    """Only the UniFi site whose name is Site A links when that is the only NetBox site."""
    netbox = FakeNetboxGateway()
    site = netbox.seed_site("site-a", "Site A")
    sites = [
        UnifiSite("sitecid3", "Site C"),
        UnifiSite("default", "Site B"),
        UnifiSite("siteaid1", "Site A"),
    ]
    by_site = {
        "sitecid3": [make_unifi_client(mac="11:22:33:44:55:01", name="site-c-laptop", is_wired=False)],
        "default": [make_unifi_client(mac="11:22:33:44:55:02", name="site-b-laptop", is_wired=False)],
        "siteaid1": [make_unifi_client(mac="11:22:33:44:55:03", name="site-a-laptop", is_wired=False)],
    }
    summary = SyncEngine(
        unifi=FakeUnifiClient(sites=sites, by_site=by_site),
        netbox=netbox,
        settings=make_settings(sync_all_sites=True, site_policy="require"),
    ).run()

    assert site.custom_fields["unifi_site_id"] == "siteaid1"
    assert netbox.clients_by_mac["11:22:33:44:55:03"].site_slug == "site-a"
    assert "11:22:33:44:55:01" not in netbox.clients_by_mac
    assert "11:22:33:44:55:02" not in netbox.clients_by_mac
    assert summary.devices_created == 1
    assert any("site-c" in error for error in summary.errors)
    assert any("site-b" in error for error in summary.errors)


def test_stored_unifi_site_id_is_never_overwritten_or_copied():
    netbox = FakeNetboxGateway()
    site_a = netbox.seed_site("site-a", "Site A", unifi_site_id="siteaid1")
    site_b = netbox.seed_site("site-b", "Site B")

    assert netbox.remember_site_link(site_a, "sitecid3") is False
    assert netbox.remember_site_link(site_b, "siteaid1") is False
    assert site_a.custom_fields["unifi_site_id"] == "siteaid1"
    assert site_b.custom_fields == {}


def test_create_only_skips_ip_changes_on_existing_untagged_devices(caplog):
    netbox = FakeNetboxGateway()
    device, *_ = netbox.upsert_client_device(
        "11:22:33:44:55:66", "hand-named", "main", "unifi-client", "generic-network-client", "unifi-sync"
    )
    device.tags.clear()
    device.primary_ip4 = FakeIP("10.1.1.1")
    client = make_unifi_client(mac="11:22:33:44:55:66", name="from-unifi", ip="10.9.9.9", is_wired=False)

    with caplog.at_level(logging.INFO):
        dry = SyncEngine(
            unifi=FakeUnifiClient(clients=[client]),
            netbox=netbox,
            settings=make_settings(dry_run=True, device_update_policy="create-only"),
        ).run()
    text = "\n".join(record.getMessage() for record in caplog.records)
    assert "would assign IP" not in text
    assert dry.devices_created == 0
    assert device.primary_ip4.address.startswith("10.1.1.1")

    applied = SyncEngine(
        unifi=FakeUnifiClient(clients=[client]),
        netbox=netbox,
        settings=make_settings(device_update_policy="create-only"),
    ).run()
    assert applied.devices_updated == 0
    assert device.name == "hand-named"
    assert device.primary_ip4.address.startswith("10.1.1.1")


def test_create_only_still_assigns_ip_on_a_tagged_device():
    netbox = FakeNetboxGateway()
    netbox.upsert_client_device(
        "11:22:33:44:55:66", "laptop", "main", "unifi-client", "generic-network-client", "unifi-sync"
    )
    client = make_unifi_client(mac="11:22:33:44:55:66", name="laptop", ip="10.9.9.9", is_wired=False)
    SyncEngine(
        unifi=FakeUnifiClient(clients=[client]),
        netbox=netbox,
        settings=make_settings(device_update_policy="create-only"),
    ).run()
    assert netbox.clients_by_mac["11:22:33:44:55:66"].primary_ip4.address.startswith("10.9.9.9")


def test_primary_conflict_detection_names_the_device():
    message = (
        "The request failed with code 400 Bad Request: "
        "{'__all__': ['IP address 10.1.1.1/32 is configured as the primary IP for device "
        "phone-01 and cannot be reassigned']}"
    )
    exc = Exception(message)
    assert is_primary_ip_conflict(exc)
    assert device_name_from_primary_error(exc) == "phone-01"
    assert is_primary_ip_conflict(Exception("name: this field is required")) is False


def _request_error(body: str, status: int = 400) -> pynetbox.RequestError:
    response = MagicMock()
    response.status_code = status
    response.reason = "Bad Request"
    response.url = "https://netbox.example.com/api/ipam/ip-addresses/5/"
    response.request = MagicMock(body=b"")
    response.text = body
    response.json.return_value = {"__all__": [body]}
    return pynetbox.RequestError(response)


def _gateway(api) -> PynetboxGateway:
    gateway = PynetboxGateway.__new__(PynetboxGateway)
    gateway.api = api
    return gateway


def test_primary_ip_on_a_managed_device_is_cleared_then_reassigned():
    api = MagicMock()
    ip = MagicMock()
    ip.id = 5
    ip.address = "10.1.1.1/32"
    ip.assigned_object = SimpleNamespace(id=99)
    ip.save.side_effect = [
        _request_error(
            "IP address 10.1.1.1/32 is configured as the primary IP for device phone-01 "
            "and cannot be reassigned"
        ),
        None,
    ]
    api.ipam.ip_addresses.filter.return_value = [ip]

    holder = MagicMock()
    holder.name = "phone-01"
    holder.tags = [SimpleNamespace(slug="unifi-sync")]
    holder.primary_ip4 = ip
    api.dcim.devices.filter.return_value = [holder]

    target = SimpleNamespace(name="new-phone", primary_ip4=None, save=MagicMock())
    changed = _gateway(api).assign_ip(target, SimpleNamespace(id=7), "10.1.1.1", managed_tag="unifi-sync")

    assert changed is True
    assert holder.primary_ip4 is None
    holder.save.assert_called_once()
    assert ip.assigned_object_id == 7
    assert ip.save.call_count == 2
    assert target.primary_ip4 == 5
    target.save.assert_called_once()


def test_primary_ip_on_an_unmanaged_device_is_left_alone():
    api = MagicMock()
    ip = MagicMock()
    ip.id = 5
    ip.address = "10.1.1.1/32"
    ip.assigned_object = SimpleNamespace(id=99)
    ip.save.side_effect = _request_error(
        "IP address 10.1.1.1/32 is configured as the primary IP for device phone-01 "
        "and cannot be reassigned"
    )
    api.ipam.ip_addresses.filter.return_value = [ip]

    holder = MagicMock()
    holder.name = "phone-01"
    holder.tags = [SimpleNamespace(slug="hand-edited")]
    api.dcim.devices.filter.return_value = [holder]

    target = SimpleNamespace(name="new-phone", primary_ip4=None, save=MagicMock())
    changed = _gateway(api).assign_ip(target, SimpleNamespace(id=7), "10.1.1.1", managed_tag="unifi-sync")

    assert changed is False
    holder.save.assert_not_called()
    target.save.assert_not_called()


def test_non_primary_ip_error_still_raises():
    api = MagicMock()
    ip = MagicMock()
    ip.id = 5
    ip.address = "10.1.1.1/32"
    ip.assigned_object = None
    ip.save.side_effect = _request_error("name: this field is required")
    api.ipam.ip_addresses.filter.return_value = [ip]

    with pytest.raises(pynetbox.RequestError):
        _gateway(api).assign_ip(SimpleNamespace(name="phone", primary_ip4=None, save=MagicMock()), SimpleNamespace(id=7), "10.1.1.1")


def test_primary_holder_can_be_found_by_the_name_in_the_error():
    api = MagicMock()
    ip = MagicMock()
    ip.id = 5
    ip.address = "10.1.1.1/32"
    ip.assigned_object = SimpleNamespace(id=99)
    ip.save.side_effect = [
        _request_error(
            "IP address 10.1.1.1/32 is configured as the primary IP for device phone-01 "
            "and cannot be reassigned"
        ),
        None,
    ]
    api.ipam.ip_addresses.filter.return_value = [ip]

    holder = MagicMock()
    holder.name = "phone-01"
    holder.tags = [SimpleNamespace(slug="unifi-sync")]

    def devices_filter(**kwargs):
        if "primary_ip4_id" in kwargs:
            return []
        if kwargs.get("name") == "phone-01":
            return [holder]
        return []

    api.dcim.devices.filter.side_effect = devices_filter

    changed = _gateway(api).assign_ip(
        SimpleNamespace(name="new-phone", primary_ip4=None, save=MagicMock()),
        SimpleNamespace(id=7),
        "10.1.1.1",
        managed_tag="unifi-sync",
    )

    assert changed is True
    assert holder.primary_ip4 is None
    holder.save.assert_called_once()
