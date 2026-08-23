from __future__ import annotations

import threading

from unifi_netbox_sync.caching import CachingNetboxGateway
from unifi_netbox_sync.sync import SyncEngine

from .fakes import FakeNetboxGateway, FakeUnifiClient, make_unifi_client
from .test_sync import make_settings

SITES = 8
CLIENTS_PER_SITE = 6


def _many_sites():
    """8 UniFi sites -> 8 distinct NetBox sites, 6 clients each."""
    site_map = {f"unifi-{i}": f"netbox-{i}" for i in range(SITES)}
    by_site = {
        f"unifi-{i}": [
            make_unifi_client(
                mac=f"11:22:33:{i:02x}:55:{c:02x}",
                name=f"site{i}-client{c}",
                ip=f"10.{i}.0.{c}",
                is_wired=False,
            )
            for c in range(CLIENTS_PER_SITE)
        ]
        for i in range(SITES)
    }
    return site_map, by_site


def _run(max_workers: int):
    site_map, by_site = _many_sites()
    netbox = FakeNetboxGateway()
    engine = SyncEngine(
        unifi=FakeUnifiClient(by_site=by_site),
        netbox=CachingNetboxGateway(netbox),
        settings=make_settings(site_map=site_map, max_workers=max_workers),
    )
    return engine.run(), netbox


def test_parallel_matches_sequential_results():
    sequential, seq_nb = _run(max_workers=1)
    parallel, par_nb = _run(max_workers=4)

    assert parallel.clients_seen == sequential.clients_seen == SITES * CLIENTS_PER_SITE
    assert parallel.devices_created == sequential.devices_created == SITES * CLIENTS_PER_SITE
    assert parallel.errors == sequential.errors == []
    assert len(par_nb.clients_by_mac) == len(seq_nb.clients_by_mac)


def test_parallel_places_every_device_in_its_own_site():
    summary, netbox = _run(max_workers=4)

    assert summary.errors == []
    for i in range(SITES):
        for c in range(CLIENTS_PER_SITE):
            device = netbox.clients_by_mac[f"11:22:33:{i:02x}:55:{c:02x}"]
            assert device.site_slug == f"netbox-{i}"


def test_parallel_reports_per_site_stats_for_every_pair():
    summary, _ = _run(max_workers=4)

    assert len(summary.site_stats) == SITES
    assert {s.netbox_site_slug for s in summary.site_stats} == {f"netbox-{i}" for i in range(SITES)}
    assert all(s.clients_seen == CLIENTS_PER_SITE for s in summary.site_stats)


def test_pairs_sharing_a_netbox_site_run_in_the_same_worker():
    """Two UniFi sites mapped to one NetBox site must not be split across
    threads — that's what keeps the stale-marking union and per-site writes
    free of races."""
    seen_threads: dict[str, set[str]] = {}
    lock = threading.Lock()

    class ThreadRecordingUnifi(FakeUnifiClient):
        def get_clients(self, site: str):
            with lock:
                seen_threads.setdefault(site, set()).add(threading.current_thread().name)
            return super().get_clients(site)

    site_map = {"a1": "shared", "a2": "shared", "b1": "other"}
    by_site = {
        "a1": [make_unifi_client(mac="11:11:11:11:11:11", is_wired=False)],
        "a2": [make_unifi_client(mac="22:22:22:22:22:22", is_wired=False)],
        "b1": [make_unifi_client(mac="33:33:33:33:33:33", is_wired=False)],
    }
    engine = SyncEngine(
        unifi=ThreadRecordingUnifi(by_site=by_site),
        netbox=CachingNetboxGateway(FakeNetboxGateway()),
        settings=make_settings(site_map=site_map, max_workers=4),
    )
    engine.run()

    assert seen_threads["a1"] == seen_threads["a2"], "shared-NetBox-site pairs must share a worker"


def test_parallel_isolates_a_failing_site():
    site_map, by_site = _many_sites()
    netbox = FakeNetboxGateway()
    netbox.missing_sites.add("netbox-3")  # required but absent
    engine = SyncEngine(
        unifi=FakeUnifiClient(by_site=by_site),
        netbox=CachingNetboxGateway(netbox),
        settings=make_settings(site_map=site_map, max_workers=4, site_policy="require"),
    )

    summary = engine.run()

    assert len(summary.errors) == 1
    assert "netbox-3" in summary.errors[0]
    # Every other site still synced.
    assert summary.devices_created == (SITES - 1) * CLIENTS_PER_SITE


def test_worker_count_is_clamped_to_group_count():
    """More workers than groups must not error or spawn idle threads."""
    site_map = {"only": "one-site"}
    engine = SyncEngine(
        unifi=FakeUnifiClient(by_site={"only": [make_unifi_client(mac="11:11:11:11:11:11", is_wired=False)]}),
        netbox=CachingNetboxGateway(FakeNetboxGateway()),
        settings=make_settings(site_map=site_map, max_workers=32),
    )

    summary = engine.run()

    assert summary.clients_seen == 1
    assert summary.errors == []
