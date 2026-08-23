from __future__ import annotations

import logging

from unifi_netbox_sync.caching import CachingNetboxGateway
from unifi_netbox_sync.models import UnifiSite
from unifi_netbox_sync.naming import normalize_key, slugify
from unifi_netbox_sync.sync import SyncEngine

from .fakes import FakeNetboxGateway, FakeUnifiClient, make_unifi_client
from .test_sync import make_settings


def _client(n=1):
    return make_unifi_client(mac=f"11:22:33:44:55:{n:02x}", name=f"c{n}", is_wired=False)


def _run(netbox, sites, by_site, **overrides):
    engine = SyncEngine(
        unifi=FakeUnifiClient(by_site=by_site, sites=sites),
        netbox=CachingNetboxGateway(netbox),
        settings=make_settings(sync_all_sites=True, site_policy="create", **overrides),
    )
    return engine.run()


def test_normalize_key_folds_punctuation():
    assert normalize_key("Acme's Depot - North Yard") == "acmesdepotnorthyard"
    assert normalize_key("acmes-depot-north-yard") == "acmesdepotnorthyard"
    assert normalize_key("acme-s-depot-north-yard") == "acmesdepotnorthyard"


def test_apostrophe_slug_links_to_the_existing_site_instead_of_duplicating():
    """The reported case. "Acme's Depot - North Yard" slugifies to
    `acme-s-depot-north-yard`, but NetBox already holds the site as
    `acmes-depot-north-yard`. Exact-slug matching would create a duplicate."""
    label = "Acme's Depot - North Yard"
    assert slugify(label) == "acme-s-depot-north-yard"  # the guess that misses

    netbox = FakeNetboxGateway()
    existing = netbox.seed_site("acmes-depot-north-yard", "Acme's Depot - North Yard")

    summary = _run(netbox, [UnifiSite("north", label)], {"north": [_client()]})

    assert summary.sites_created == 0, "must link, not create a second site"
    assert netbox.created_sites == set()
    assert netbox.clients_by_mac["11:22:33:44:55:01"].site_slug == existing.slug


def test_matching_site_is_never_modified():
    netbox = FakeNetboxGateway()
    existing = netbox.seed_site("acmes-depot-north-yard", "Acme's Depot - North Yard")
    before = (existing.slug, existing.name, existing.id)

    _run(netbox, [UnifiSite("north", "Acme's Depot - North Yard")], {"north": [_client()]})

    assert (existing.slug, existing.name, existing.id) == before


def test_inventory_is_added_to_an_already_populated_site():
    """Devices already in the NetBox site stay; the UniFi clients join them."""
    netbox = FakeNetboxGateway()
    netbox.seed_site("acmes-depot-north-yard", "Acme's Depot - North Yard")
    netbox.upsert_client_device(
        "99:99:99:99:99:99",
        "pre-existing-server",
        "acmes-depot-north-yard",
        "unifi-client",
        "generic-network-client",
        "other-tag",
    )

    _run(netbox, [UnifiSite("north", "Acme's Depot - North Yard")], {"north": [_client()]})

    names = {d.name for d in netbox.clients_by_mac.values()}
    assert "pre-existing-server" in names, "existing inventory must survive"
    assert "c1" in names, "the UniFi client is added alongside it"


def test_exact_name_match_when_slug_is_unrelated():
    netbox = FakeNetboxGateway()
    netbox.seed_site("hq-01", "Head Office")

    summary = _run(netbox, [UnifiSite("s1", "Head Office")], {"s1": [_client()]})

    assert summary.sites_created == 0
    assert netbox.clients_by_mac["11:22:33:44:55:01"].site_slug == "hq-01"


def test_no_match_still_creates_under_create_policy():
    netbox = FakeNetboxGateway()
    netbox.seed_site("somewhere-else", "Somewhere Else")

    summary = _run(netbox, [UnifiSite("s1", "Brand New Site")], {"s1": [_client()]})

    assert summary.sites_created == 1
    assert "brand-new-site" in netbox.created_sites


def test_unrelated_sites_are_not_linked_together():
    """Folding punctuation must not make different sites collide."""
    netbox = FakeNetboxGateway()
    netbox.seed_site("north-depot", "North Depot")

    summary = _run(netbox, [UnifiSite("s1", "South Depot")], {"s1": [_client()]})

    assert summary.sites_created == 1
    assert "south-depot" in netbox.created_sites


def test_slug_mode_restores_exact_matching_only():
    netbox = FakeNetboxGateway()
    netbox.seed_site("acmes-depot-north-yard", "Acme's Depot - North Yard")

    summary = _run(
        netbox,
        [UnifiSite("north", "Acme's Depot - North Yard")],
        {"north": [_client()]},
        site_match="slug",
    )

    assert summary.sites_created == 1, "opt-out must behave as before"


def test_two_unifi_sites_linking_to_one_netbox_site_share_a_group():
    """Resolution happens before grouping, so the stale-marking union still
    covers both — the property that would silently break if two pairs landed
    on the same NetBox site in separate groups."""
    netbox = FakeNetboxGateway()
    netbox.seed_site("shared", "Shared Site")
    netbox.upsert_client_device(
        "aa:aa:aa:aa:aa:01", "from-a", "shared", "unifi-client", "generic-network-client", "unifi-sync"
    )
    netbox.upsert_client_device(
        "bb:bb:bb:bb:bb:02", "from-b", "shared", "unifi-client", "generic-network-client", "unifi-sync"
    )

    summary = _run(
        netbox,
        [UnifiSite("a", "Shared Site"), UnifiSite("b", "Shared-Site")],
        {
            "a": [make_unifi_client(mac="aa:aa:aa:aa:aa:01", name="from-a", is_wired=False)],
            "b": [make_unifi_client(mac="bb:bb:bb:bb:bb:02", name="from-b", is_wired=False)],
        },
    )

    assert summary.stale_marked_offline == 0, "neither pair may stale the other's clients"
    assert netbox.clients_by_mac["aa:aa:aa:aa:aa:01"].status.value == "active"
    assert netbox.clients_by_mac["bb:bb:bb:bb:bb:02"].status.value == "active"


def test_link_is_logged(caplog):
    netbox = FakeNetboxGateway()
    netbox.seed_site("acmes-depot-north-yard", "Acme's Depot - North Yard")

    with caplog.at_level(logging.INFO):
        _run(netbox, [UnifiSite("north", "Acme's Depot - North Yard")], {"north": [_client()]})

    messages = "\n".join(r.getMessage() for r in caplog.records)
    assert "Linking UniFi site" in messages
    assert "acmes-depot-north-yard" in messages


def test_dry_run_links_without_writing():
    netbox = FakeNetboxGateway()
    netbox.seed_site("acmes-depot-north-yard", "Acme's Depot - North Yard")

    summary = _run(
        netbox, [UnifiSite("north", "Acme's Depot - North Yard")], {"north": [_client()]}, dry_run=True
    )

    assert summary.sites_created == 0
    assert summary.errors == []
    assert netbox.clients_by_mac == {}
