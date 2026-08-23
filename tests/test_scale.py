from __future__ import annotations

from unifi_netbox_sync.caching import CachingNetboxGateway
from unifi_netbox_sync.sync import SyncEngine

from .fakes import CountingNetboxGateway, FakeNetboxGateway, FakeUnifiClient, make_unifi_client
from .test_sync import make_settings

SWITCH_MAC = "aa:bb:cc:dd:ee:01"
PORTS = 48


def _switch_full_of_clients(netbox: FakeNetboxGateway):
    """One 48-port switch with a wired client on every port."""
    netbox.seed_switch(SWITCH_MAC, "core-switch", [str(p) for p in range(1, PORTS + 1)])
    return [
        make_unifi_client(
            mac=f"11:22:33:44:55:{port:02x}",
            name=f"client-{port}",
            ip=f"10.0.0.{port}",
            is_wired=True,
            switch_mac=SWITCH_MAC,
            switch_port=port,
        )
        for port in range(1, PORTS + 1)
    ]


def _engine(netbox, clients, **overrides):
    return SyncEngine(
        unifi=FakeUnifiClient(clients=clients), netbox=netbox, settings=make_settings(**overrides)
    )


def test_switch_lookup_happens_once_per_switch_not_once_per_client():
    """48 clients behind one switch should resolve that switch once."""
    netbox = FakeNetboxGateway()
    clients = _switch_full_of_clients(netbox)

    uncached = CountingNetboxGateway(netbox)
    _engine(uncached, clients).run()

    fresh = FakeNetboxGateway()
    clients2 = _switch_full_of_clients(fresh)
    counting = CountingNetboxGateway(fresh)
    _engine(CachingNetboxGateway(counting), clients2).run()

    assert uncached.calls["find_switch_device_by_mac"] == PORTS
    assert counting.calls["find_switch_device_by_mac"] == 1
    # Per-port lookups collapse into a single interface list for the switch.
    assert uncached.calls["find_interface_by_name_candidates"] == PORTS
    assert counting.calls["find_interface_by_name_candidates"] == 0
    assert counting.calls["list_device_interfaces"] == 1


def test_steady_state_rerun_writes_nothing():
    """The second run over an unchanged network must issue no writes at all."""
    netbox = FakeNetboxGateway()
    clients = _switch_full_of_clients(netbox)
    _engine(CachingNetboxGateway(netbox), clients).run()

    counting = CountingNetboxGateway(netbox)
    summary = _engine(CachingNetboxGateway(counting), clients).run()

    assert summary.devices_created == 0
    assert summary.devices_updated == 0
    assert summary.cables_created == 0
    assert summary.clients_unchanged == PORTS
    assert summary.ips_unchanged == PORTS
    assert summary.cables_unchanged == PORTS
    # The genuinely-write-shaped calls must not fire at all.
    assert counting.calls["assign_ip"] == 0
    assert counting.calls["ensure_cable"] == 0
    assert counting.calls["mark_offline"] == 0


def test_steady_state_skips_the_name_collision_query():
    """A device already holding its own name needs no uniqueness lookup."""
    netbox = FakeNetboxGateway()
    clients = _switch_full_of_clients(netbox)
    _engine(CachingNetboxGateway(netbox), clients).run()

    counting = CountingNetboxGateway(netbox)
    _engine(CachingNetboxGateway(counting), clients).run()

    assert counting.calls["device_name_taken_by_other"] == 0


def test_cache_cuts_calls_on_top_of_noop_short_circuiting():
    """Cache contribution measured in isolation.

    Both arms run the current engine (so both already benefit from the no-op
    short-circuits); the only difference is whether the cache is present.
    """
    baseline_nb = FakeNetboxGateway()
    baseline_clients = _switch_full_of_clients(baseline_nb)
    _engine(CountingNetboxGateway(baseline_nb), baseline_clients).run()  # populate
    baseline_steady = CountingNetboxGateway(baseline_nb)
    _engine(baseline_steady, baseline_clients).run()

    tuned_nb = FakeNetboxGateway()
    tuned_clients = _switch_full_of_clients(tuned_nb)
    _engine(CachingNetboxGateway(tuned_nb), tuned_clients).run()  # populate
    tuned_steady = CountingNetboxGateway(tuned_nb)
    _engine(CachingNetboxGateway(tuned_steady), tuned_clients).run()

    assert tuned_steady.total < baseline_steady.total * 0.7, (
        f"expected >30% fewer calls from caching alone, "
        f"got {tuned_steady.total} vs {baseline_steady.total}"
    )


def test_steady_state_calls_per_client_stay_bounded():
    """Absolute guard: a no-change run must not creep back up in chattiness.

    The floor is one device lookup + one upsert probe + one interface lookup
    per client, plus a handful of fixed per-run/per-switch calls.
    """
    netbox = FakeNetboxGateway()
    clients = _switch_full_of_clients(netbox)
    _engine(CachingNetboxGateway(netbox), clients).run()

    counting = CountingNetboxGateway(netbox)
    _engine(CachingNetboxGateway(counting), clients).run()

    per_client = counting.total / PORTS
    assert per_client <= 3.5, f"{per_client:.1f} gateway calls per client on a no-change run"


def test_cache_does_not_hide_newly_created_client_devices():
    """Client-device lookups must stay uncached, or a create in the same run
    would be invisible to the code that follows it."""
    netbox = FakeNetboxGateway()
    cached = CachingNetboxGateway(netbox)
    client = make_unifi_client(mac="11:22:33:44:55:66", name="laptop", is_wired=False)

    assert cached.find_client_device_by_mac("11:22:33:44:55:66") is None
    _engine(cached, [client]).run()
    assert cached.find_client_device_by_mac("11:22:33:44:55:66") is not None


def test_cache_does_not_pin_a_missing_site_created_later():
    """find_site must not cache a None, or SITE_POLICY=create would break."""
    netbox = FakeNetboxGateway()
    netbox.missing_sites.add("main")
    cached = CachingNetboxGateway(netbox)

    assert cached.find_site("main") is None
    client = make_unifi_client(mac="11:22:33:44:55:66", is_wired=False)
    summary = _engine(cached, [client], site_policy="create").run()

    assert summary.sites_created == 1
    assert cached.find_site("main") is not None
