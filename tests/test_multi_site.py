from __future__ import annotations

from unifi_netbox_sync.sync import SyncEngine

from .fakes import FakeNetboxGateway, FakeUnifiClient, make_unifi_client
from .test_sync import make_settings


class RaisingUnifiClient(FakeUnifiClient):
    """Fake whose get_clients() raises for one specific site, to simulate a
    site-level failure (e.g. the controller returning an error) alongside a
    healthy site."""

    def __init__(self, by_site, raise_for_site: str, exc: Exception) -> None:
        super().__init__(by_site=by_site)
        self._raise_for_site = raise_for_site
        self._exc = exc

    def get_clients(self, site: str):
        if site == self._raise_for_site:
            raise self._exc
        return super().get_clients(site)


def test_site_pairs_defaults_to_single_pair():
    settings = make_settings()
    pairs = settings.site_pairs()

    assert len(pairs) == 1
    assert pairs[0].unifi_site == "default"
    assert pairs[0].netbox_site_slug == "main"


def test_site_map_overrides_single_pair():
    settings = make_settings(site_map={"hq": "headquarters", "branch": "branch-office"})
    pairs = settings.site_pairs()

    assert {(p.unifi_site, p.netbox_site_slug) for p in pairs} == {
        ("hq", "headquarters"),
        ("branch", "branch-office"),
    }


def test_multi_site_creates_devices_in_their_own_netbox_site():
    netbox = FakeNetboxGateway()
    hq_client = make_unifi_client(mac="11:11:11:11:11:11", name="hq-laptop", is_wired=False)
    branch_client = make_unifi_client(mac="22:22:22:22:22:22", name="branch-laptop", is_wired=False)
    unifi = FakeUnifiClient(by_site={"hq": [hq_client], "branch": [branch_client]})
    engine = SyncEngine(
        unifi=unifi,
        netbox=netbox,
        settings=make_settings(site_map={"hq": "headquarters", "branch": "branch-office"}),
    )

    summary = engine.run()

    assert summary.clients_seen == 2
    assert summary.devices_created == 2
    assert netbox.clients_by_mac["11:11:11:11:11:11"].site_slug == "headquarters"
    assert netbox.clients_by_mac["22:22:22:22:22:22"].site_slug == "branch-office"


def test_same_name_in_different_sites_is_not_a_collision():
    netbox = FakeNetboxGateway()
    hq_client = make_unifi_client(mac="11:11:11:11:11:11", name="laptop", is_wired=False)
    branch_client = make_unifi_client(mac="22:22:22:22:22:22", name="laptop", is_wired=False)
    unifi = FakeUnifiClient(by_site={"hq": [hq_client], "branch": [branch_client]})
    engine = SyncEngine(
        unifi=unifi,
        netbox=netbox,
        settings=make_settings(site_map={"hq": "headquarters", "branch": "branch-office"}),
    )

    engine.run()

    # Same name is fine in two different NetBox sites — no MAC-suffix disambiguation needed.
    assert netbox.clients_by_mac["11:11:11:11:11:11"].name == "laptop"
    assert netbox.clients_by_mac["22:22:22:22:22:22"].name == "laptop"


def test_stale_marking_is_scoped_per_site_not_global():
    """Regression guard: a device from one NetBox site must never be marked
    offline just because a different site's client list didn't mention its MAC."""
    netbox = FakeNetboxGateway()
    netbox.upsert_client_device(
        "11:11:11:11:11:11", "hq-laptop", "headquarters", "unifi-client", "generic-network-client", "unifi-sync"
    )
    netbox.upsert_client_device(
        "22:22:22:22:22:22", "branch-laptop", "branch-office", "unifi-client", "generic-network-client", "unifi-sync"
    )
    # This run only re-reports the "branch" client; "hq" is still active but simply
    # wasn't part of this run's clients for its own site.
    branch_client = make_unifi_client(mac="22:22:22:22:22:22", name="branch-laptop", is_wired=False)
    hq_client = make_unifi_client(mac="11:11:11:11:11:11", name="hq-laptop", is_wired=False)
    unifi = FakeUnifiClient(by_site={"hq": [hq_client], "branch": [branch_client]})
    engine = SyncEngine(
        unifi=unifi,
        netbox=netbox,
        settings=make_settings(site_map={"hq": "headquarters", "branch": "branch-office"}),
    )

    summary = engine.run()

    assert summary.stale_marked_offline == 0
    assert netbox.clients_by_mac["11:11:11:11:11:11"].status.value == "active"
    assert netbox.clients_by_mac["22:22:22:22:22:22"].status.value == "active"


def test_stale_marking_only_affects_devices_in_the_missing_site():
    netbox = FakeNetboxGateway()
    netbox.upsert_client_device(
        "11:11:11:11:11:11", "hq-old", "headquarters", "unifi-client", "generic-network-client", "unifi-sync"
    )
    netbox.upsert_client_device(
        "22:22:22:22:22:22", "branch-laptop", "branch-office", "unifi-client", "generic-network-client", "unifi-sync"
    )
    # "hq" now reports zero clients (the old one disconnected); "branch" still has its client.
    branch_client = make_unifi_client(mac="22:22:22:22:22:22", name="branch-laptop", is_wired=False)
    unifi = FakeUnifiClient(by_site={"hq": [], "branch": [branch_client]})
    engine = SyncEngine(
        unifi=unifi,
        netbox=netbox,
        settings=make_settings(site_map={"hq": "headquarters", "branch": "branch-office"}),
    )

    summary = engine.run()

    assert summary.stale_marked_offline == 1
    assert netbox.clients_by_mac["11:11:11:11:11:11"].status.value == "offline"
    assert netbox.clients_by_mac["22:22:22:22:22:22"].status.value == "active"


def test_shared_netbox_site_stale_marking_uses_union_of_both_pairs():
    """Regression guard for the case two UniFi sites map to the same NetBox
    site: without unioning seen_macs across both pairs first, each pair's
    stale pass would see the *other* pair's device (same NetBox site, via
    list_synced_client_devices) as unseen and wrongly mark it offline."""
    netbox = FakeNetboxGateway()
    netbox.upsert_client_device(
        "11:11:11:11:11:11", "device-a", "shared-site", "unifi-client", "generic-network-client", "unifi-sync"
    )
    netbox.upsert_client_device(
        "22:22:22:22:22:22", "device-b", "shared-site", "unifi-client", "generic-network-client", "unifi-sync"
    )
    client_a = make_unifi_client(mac="11:11:11:11:11:11", name="device-a", is_wired=False)
    client_b = make_unifi_client(mac="22:22:22:22:22:22", name="device-b", is_wired=False)
    unifi = FakeUnifiClient(by_site={"site-a": [client_a], "site-b": [client_b]})
    engine = SyncEngine(
        unifi=unifi,
        netbox=netbox,
        settings=make_settings(site_map={"site-a": "shared-site", "site-b": "shared-site"}),
    )

    summary = engine.run()

    assert netbox.clients_by_mac["11:11:11:11:11:11"].status.value == "active"
    assert netbox.clients_by_mac["22:22:22:22:22:22"].status.value == "active"
    assert summary.stale_marked_offline == 0


def test_shared_netbox_site_skips_stale_marking_if_one_pair_fails():
    netbox = FakeNetboxGateway()
    netbox.upsert_client_device(
        "11:11:11:11:11:11", "device-a", "shared-site", "unifi-client", "generic-network-client", "unifi-sync"
    )
    netbox.upsert_client_device(
        "22:22:22:22:22:22", "device-b", "shared-site", "unifi-client", "generic-network-client", "unifi-sync"
    )
    client_a = make_unifi_client(mac="11:11:11:11:11:11", name="device-a", is_wired=False)
    unifi = RaisingUnifiClient(
        by_site={"site-a": [client_a]}, raise_for_site="site-b", exc=LookupError("simulated failure")
    )
    engine = SyncEngine(
        unifi=unifi,
        netbox=netbox,
        settings=make_settings(site_map={"site-a": "shared-site", "site-b": "shared-site"}),
    )

    summary = engine.run()

    assert len(summary.errors) == 1
    # site-b's failure means we don't know its clients, so stale-marking for
    # "shared-site" must be skipped entirely rather than acting on partial data.
    assert netbox.clients_by_mac["11:11:11:11:11:11"].status.value == "active"
    assert netbox.clients_by_mac["22:22:22:22:22:22"].status.value == "active"
    assert summary.stale_marked_offline == 0


def test_sites_created_counts_multiple_sites():
    netbox = FakeNetboxGateway()
    netbox.missing_sites.add("headquarters")
    netbox.missing_sites.add("branch-office")
    hq_client = make_unifi_client(mac="11:11:11:11:11:11", is_wired=False)
    branch_client = make_unifi_client(mac="22:22:22:22:22:22", is_wired=False)
    unifi = FakeUnifiClient(by_site={"hq": [hq_client], "branch": [branch_client]})
    engine = SyncEngine(
        unifi=unifi,
        netbox=netbox,
        settings=make_settings(
            site_map={"hq": "headquarters", "branch": "branch-office"}, site_policy="create"
        ),
    )

    summary = engine.run()

    assert summary.sites_created == 2


def test_aggregate_summary_sums_across_sites():
    netbox = FakeNetboxGateway()
    netbox.seed_switch("aa:bb:cc:dd:ee:01", "hq-switch", ["1"])
    hq_client = make_unifi_client(
        mac="11:11:11:11:11:11", name="hq-laptop", is_wired=True,
        switch_mac="aa:bb:cc:dd:ee:01", switch_port=1,
    )
    branch_client = make_unifi_client(mac="22:22:22:22:22:22", name="branch-laptop", is_wired=False)
    unifi = FakeUnifiClient(by_site={"hq": [hq_client], "branch": [branch_client]})
    engine = SyncEngine(
        unifi=unifi,
        netbox=netbox,
        settings=make_settings(site_map={"hq": "headquarters", "branch": "branch-office"}),
    )

    summary = engine.run()

    assert summary.clients_seen == 2
    assert summary.devices_created == 2
    assert summary.cables_created == 1  # only the wired hq client got cabled
    assert len(summary.client_results) == 2


def test_one_broken_site_does_not_block_the_others():
    netbox = FakeNetboxGateway()
    netbox.missing_sites.add("branch-office")  # this site is required but missing
    hq_client = make_unifi_client(mac="11:11:11:11:11:11", name="hq-laptop", is_wired=False)
    branch_client = make_unifi_client(mac="22:22:22:22:22:22", name="branch-laptop", is_wired=False)
    unifi = FakeUnifiClient(by_site={"hq": [hq_client], "branch": [branch_client]})
    engine = SyncEngine(
        unifi=unifi,
        netbox=netbox,
        settings=make_settings(
            site_map={"hq": "headquarters", "branch": "branch-office"}, site_policy="require"
        ),
    )

    summary = engine.run()  # must not raise

    assert netbox.clients_by_mac["11:11:11:11:11:11"].site_slug == "headquarters"  # hq still synced
    assert "22:22:22:22:22:22" not in netbox.clients_by_mac  # branch was skipped
    assert len(summary.errors) == 1
    assert "branch" in summary.errors[0] and "branch-office" in summary.errors[0]


def test_dry_run_works_across_multiple_sites():
    netbox = FakeNetboxGateway()
    netbox.missing_sites.add("branch-office")  # "branch" site doesn't exist yet
    hq_client = make_unifi_client(mac="11:11:11:11:11:11", name="hq-laptop", is_wired=False)
    branch_client = make_unifi_client(mac="22:22:22:22:22:22", name="branch-laptop", is_wired=False)
    unifi = FakeUnifiClient(by_site={"hq": [hq_client], "branch": [branch_client]})
    engine = SyncEngine(
        unifi=unifi,
        netbox=netbox,
        settings=make_settings(
            site_map={"hq": "headquarters", "branch": "branch-office"},
            site_policy="create",
            dry_run=True,
        ),
    )

    summary = engine.run()  # must not raise, and must not write anything

    assert summary.clients_seen == 2
    assert summary.devices_created == 2  # planned
    assert netbox.clients_by_mac == {}  # nothing actually written
    assert "branch-office" not in netbox.created_sites
