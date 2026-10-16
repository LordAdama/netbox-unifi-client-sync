from __future__ import annotations

from unifi_netbox_sync.caching import CachingNetboxGateway
from unifi_netbox_sync.models import UnifiSite
from unifi_netbox_sync.sync import SyncEngine

from .fakes import FakeNetboxGateway, FakeUnifiClient, make_unifi_client
from .test_sync import make_settings

INTERNAL = "64aabbccddeeff0011223344"


def _client():
    return make_unifi_client(mac="11:22:33:44:55:01", name="laptop", is_wired=False)


def _run_all(netbox, sites, **overrides):
    by_site = {site.name: [_client()] for site in sites}
    engine = SyncEngine(
        unifi=FakeUnifiClient(by_site=by_site, sites=sites),
        netbox=CachingNetboxGateway(netbox),
        settings=make_settings(sync_all_sites=True, site_policy="create", **overrides),
    )
    return engine.run()


def test_internal_id_matches_after_the_unifi_description_is_renamed():
    netbox = FakeNetboxGateway()
    existing = netbox.seed_site("kept-slug", "Old Office")
    existing.custom_fields["unifi_site_internal_id"] = INTERNAL
    existing.custom_fields["unifi_site_id"] = "north"
    existing.custom_fields["unifi_site_name"] = "north"

    summary = _run_all(
        netbox,
        [UnifiSite(name="north", description="Completely Different Name", internal_id=INTERNAL)],
    )

    assert summary.sites_created == 0
    assert netbox.created_sites == set()
    assert netbox.clients_by_mac["11:22:33:44:55:01"].site_slug == "kept-slug"
    assert existing.name == "Old Office"
    assert existing.slug == "kept-slug"


def test_legacy_unifi_site_id_is_backfilled_without_being_rewritten():
    """unifi_site_id already stores the short API name. A rename of the
    UniFi description must not change that value; the companion fields are
    filled from it and from the controller _id."""
    netbox = FakeNetboxGateway()
    existing = netbox.seed_site("kept-slug", "Old Office", unifi_site_id="north")

    _run_all(
        netbox,
        [UnifiSite(name="north", description="Renamed Office", internal_id=INTERNAL)],
    )

    assert existing.custom_fields["unifi_site_id"] == "north"
    assert existing.custom_fields["unifi_site_name"] == "north"
    assert existing.custom_fields["unifi_site_internal_id"] == INTERNAL
    assert existing.name == "Old Office"
    assert netbox.clients_by_mac["11:22:33:44:55:01"].site_slug == "kept-slug"


def test_site_map_still_pins_the_slug_when_nothing_matches():
    netbox = FakeNetboxGateway()
    netbox.seed_site("unrelated", "Unrelated")
    engine = SyncEngine(
        unifi=FakeUnifiClient(
            by_site={"north": [_client()]},
            sites=[UnifiSite(name="north", description="North Office", internal_id=INTERNAL)],
        ),
        netbox=CachingNetboxGateway(netbox),
        settings=make_settings(
            site_map={"north": "pinned-slug"},
            netbox_site_slug="",
            site_policy="create",
        ),
    )

    summary = engine.run()

    assert summary.sites_created == 1
    assert "pinned-slug" in netbox.created_sites
    created = netbox.existing_sites["pinned-slug"]
    assert created.custom_fields["unifi_site_id"] == "north"
    assert created.custom_fields["unifi_site_name"] == "north"
    assert created.custom_fields["unifi_site_internal_id"] == INTERNAL
    assert netbox.clients_by_mac["11:22:33:44:55:01"].site_slug == "pinned-slug"


def test_stored_identity_wins_over_the_site_map_slug():
    netbox = FakeNetboxGateway()
    existing = netbox.seed_site("already-here", "Already Here", unifi_site_id="north")
    engine = SyncEngine(
        unifi=FakeUnifiClient(
            by_site={"north": [_client()]},
            sites=[UnifiSite(name="north", description="North Office", internal_id=INTERNAL)],
        ),
        netbox=CachingNetboxGateway(netbox),
        settings=make_settings(
            site_map={"north": "wanted-slug"},
            netbox_site_slug="",
            site_policy="create",
        ),
    )

    summary = engine.run()

    assert summary.sites_created == 0
    assert "wanted-slug" not in netbox.created_sites
    assert netbox.clients_by_mac["11:22:33:44:55:01"].site_slug == "already-here"
    assert existing.custom_fields["unifi_site_id"] == "north"
    assert existing.custom_fields["unifi_site_name"] == "north"
    assert existing.custom_fields["unifi_site_internal_id"] == INTERNAL
