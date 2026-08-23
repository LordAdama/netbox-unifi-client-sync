from __future__ import annotations

import logging

import pytest

from unifi_netbox_sync.config import Settings
from unifi_netbox_sync.models import UnifiSite
from unifi_netbox_sync.sync import SyncEngine

from .fakes import FakeNetboxGateway, FakeUnifiClient, make_unifi_client
from .test_sync import make_settings

REQUIRED_ENV = {
    "UNIFI_HOST": "https://192.168.1.1",
    "UNIFI_API_KEY": "key",
    "NETBOX_URL": "https://netbox.example.com",
    "NETBOX_TOKEN": "token",
}


@pytest.fixture(autouse=True)
def _isolated_env(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    for key in ("NETBOX_SITE_SLUG", "SITE_MAP", "UNIFI_USERNAME", "UNIFI_PASSWORD"):
        monkeypatch.delenv(key, raising=False)
    for key, value in REQUIRED_ENV.items():
        monkeypatch.setenv(key, value)


def _client(n: int):
    return make_unifi_client(mac=f"11:22:33:44:55:{n:02x}", name=f"c{n}", is_wired=False)


def _engine(sites, by_site, **overrides):
    netbox = FakeNetboxGateway()
    options = {"sync_all_sites": True, "site_policy": "create", **overrides}
    engine = SyncEngine(
        unifi=FakeUnifiClient(by_site=by_site, sites=sites),
        netbox=netbox,
        settings=make_settings(**options),
    )
    return engine, netbox


# -- config parsing ----------------------------------------------------


def test_star_enables_discovery(monkeypatch):
    monkeypatch.setenv("SITE_MAP", "*")

    settings = Settings.from_env()

    assert settings.sync_all_sites is True
    assert settings.site_map == {}


def test_star_can_be_combined_with_explicit_overrides(monkeypatch):
    monkeypatch.setenv("SITE_MAP", "*,default:head-office")

    settings = Settings.from_env()

    assert settings.sync_all_sites is True
    assert settings.site_map == {"default": "head-office"}


def test_star_satisfies_the_site_requirement_without_netbox_site_slug(monkeypatch):
    monkeypatch.setenv("SITE_MAP", "*")
    Settings.from_env()  # must not raise


def test_no_site_config_at_all_is_still_rejected(monkeypatch):
    with pytest.raises(SystemExit):
        Settings.from_env()


# -- discovery ---------------------------------------------------------


def test_every_controller_site_is_synced():
    sites = [UnifiSite(name="s1", description="Head Office"), UnifiSite(name="s2", description="Branch")]
    engine, netbox = _engine(sites, {"s1": [_client(1)], "s2": [_client(2)]})

    summary = engine.run()

    assert summary.clients_seen == 2
    assert netbox.clients_by_mac["11:22:33:44:55:01"].site_slug == "head-office"
    assert netbox.clients_by_mac["11:22:33:44:55:02"].site_slug == "branch"


def test_slug_comes_from_the_human_description_not_the_opaque_id():
    """UniFi site ids look like '7xk2p9qr' — useless as a NetBox slug."""
    sites = [UnifiSite(name="7xk2p9qr", description="Head Office")]
    engine, netbox = _engine(sites, {"7xk2p9qr": [_client(1)]})

    engine.run()

    assert netbox.clients_by_mac["11:22:33:44:55:01"].site_slug == "head-office"


def test_site_without_a_description_falls_back_to_its_id():
    sites = [UnifiSite(name="default", description="")]
    engine, netbox = _engine(sites, {"default": [_client(1)]})

    engine.run()

    assert netbox.clients_by_mac["11:22:33:44:55:01"].site_slug == "default"


def test_explicit_entry_overrides_the_derived_slug():
    sites = [UnifiSite(name="s1", description="Head Office"), UnifiSite(name="s2", description="Branch")]
    engine, netbox = _engine(
        sites, {"s1": [_client(1)], "s2": [_client(2)]}, site_map={"s1": "hq-custom"}
    )

    engine.run()

    assert netbox.clients_by_mac["11:22:33:44:55:01"].site_slug == "hq-custom"
    assert netbox.clients_by_mac["11:22:33:44:55:02"].site_slug == "branch"  # still derived


def test_colliding_descriptions_warn_but_still_sync(caplog):
    sites = [UnifiSite(name="s1", description="Office"), UnifiSite(name="s2", description="Office")]
    engine, netbox = _engine(sites, {"s1": [_client(1)], "s2": [_client(2)]})

    with caplog.at_level(logging.WARNING):
        summary = engine.run()

    assert summary.clients_seen == 2
    assert summary.errors == []
    assert "both map to NetBox site" in "\n".join(r.getMessage() for r in caplog.records)


def test_a_controller_reporting_no_sites_is_an_explicit_error():
    engine, _ = _engine([], {})

    with pytest.raises(LookupError, match="reported no sites"):
        engine.run()


def test_require_policy_hint_is_logged_when_discovering(caplog):
    sites = [UnifiSite(name="s1", description="Head Office")]
    engine, _ = _engine(sites, {"s1": [_client(1)]}, site_policy="require")

    with caplog.at_level(logging.INFO):
        engine.run()

    assert "SITE_POLICY=create" in "\n".join(r.getMessage() for r in caplog.records)


def test_discovered_sites_are_created_under_create_policy():
    sites = [UnifiSite(name="s1", description="Head Office"), UnifiSite(name="s2", description="Branch")]
    netbox = FakeNetboxGateway()
    netbox.missing_sites.update({"head-office", "branch"})
    engine = SyncEngine(
        unifi=FakeUnifiClient(by_site={"s1": [_client(1)], "s2": [_client(2)]}, sites=sites),
        netbox=netbox,
        settings=make_settings(sync_all_sites=True, site_policy="create"),
    )

    summary = engine.run()

    assert summary.sites_created == 2
    assert netbox.created_sites == {"head-office", "branch"}


def test_discovery_works_with_parallel_workers():
    sites = [UnifiSite(name=f"s{i}", description=f"Site {i}") for i in range(6)]
    by_site = {f"s{i}": [_client(i)] for i in range(6)}
    engine, netbox = _engine(sites, by_site, max_workers=4)

    summary = engine.run()

    assert summary.clients_seen == 6
    assert summary.errors == []
    assert len({d.site_slug for d in netbox.clients_by_mac.values()}) == 6


def test_dry_run_discovers_without_writing():
    sites = [UnifiSite(name="s1", description="Head Office")]
    engine, netbox = _engine(sites, {"s1": [_client(1)]}, dry_run=True)

    summary = engine.run()

    assert summary.clients_seen == 1
    assert netbox.clients_by_mac == {}
