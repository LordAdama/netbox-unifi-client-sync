from __future__ import annotations

import logging
from unittest.mock import MagicMock

from unifi_netbox_sync.caching import CachingNetboxGateway
from unifi_netbox_sync.devicetype_library import (
    DeviceTypeLibrary,
    DeviceTypeSpec,
    netbox_interface_type,
    normalize_model,
    spec_from_unifi_device,
)
from unifi_netbox_sync.models import UnifiOutlet, UnifiPort, UnifiSite, UnifiSwitchDevice
from unifi_netbox_sync.netbox_client import PynetboxGateway
from unifi_netbox_sync.sync import SyncEngine

from .fakes import FakeIP, FakeNetboxGateway, FakeUnifiClient, make_unifi_client
from .test_sync import make_settings

SWITCH_MAC = "aa:bb:cc:dd:ee:01"


def _switch(ports: int = 8, **kw):
    return UnifiSwitchDevice(
        mac=SWITCH_MAC,
        name="core-switch",
        model=kw.pop("model", "USW24POE"),
        device_type=kw.pop("device_type", "usw"),
        serial=kw.pop("serial", "F09FC2AAAA01"),
        ports=[UnifiPort(index=i, media="GE", max_speed=1000) for i in range(1, ports + 1)],
        **kw,
    )


def _run(devices, clients=(), netbox=None, **overrides):
    netbox = netbox or FakeNetboxGateway()
    engine = SyncEngine(
        unifi=FakeUnifiClient(clients=list(clients), devices=devices),
        netbox=CachingNetboxGateway(netbox),
        settings=make_settings(sync_unifi_devices=True, **overrides),
    )
    return engine.run(), netbox


# -- helpers -----------------------------------------------------------


def test_normalize_model_folds_punctuation_and_case():
    assert normalize_model("USW-24-PoE") == "USW24POE"
    assert normalize_model("US-24-250W") == "US24250W"


def test_interface_type_mapping():
    assert netbox_interface_type("GE", 1000) == "1000base-t"
    assert netbox_interface_type("GE", 2500) == "2.5gbase-t"
    assert netbox_interface_type("SFP+", 10000) == "10gbase-x-sfpp"
    assert netbox_interface_type("SFP", 1000) == "1000base-x-sfp"
    assert netbox_interface_type("", 0) == "1000base-t"  # sane default


def test_spec_from_controller_uses_port_table():
    spec = spec_from_unifi_device(_switch(ports=4))

    assert [name for name, _ in spec.interfaces] == ["Port 1", "Port 2", "Port 3", "Port 4"]
    assert spec.part_number == "USW24POE"


def test_spec_gives_a_portless_device_somewhere_to_terminate():
    ap = UnifiSwitchDevice(mac="aa:11", name="ap", model="U7PG2", device_type="uap", ports=[])

    spec = spec_from_unifi_device(ap)

    assert spec.interfaces == [("Port 1", "1000base-t")]


# -- device creation ---------------------------------------------------


def test_adopted_devices_are_created_in_netbox():
    summary, netbox = _run([_switch()])

    assert summary.devices_synced == 1
    assert summary.infra_created == 1
    device = netbox.infra_by_mac[SWITCH_MAC]
    assert device.name == "core-switch"
    assert device.serial == "F09FC2AAAA01"
    assert len(device.interfaces) == 8


def test_unadopted_devices_are_ignored():
    summary, netbox = _run([_switch(adopted=False)])

    assert summary.devices_synced == 0
    assert netbox.infra_by_mac == {}


def test_device_sync_is_off_by_default():
    netbox = FakeNetboxGateway()
    engine = SyncEngine(
        unifi=FakeUnifiClient(clients=[], devices=[_switch()]),
        netbox=CachingNetboxGateway(netbox),
        settings=make_settings(),  # sync_unifi_devices defaults to False
    )

    summary = engine.run()

    assert summary.devices_synced == 0
    assert netbox.infra_by_mac == {}


def test_device_types_get_per_type_roles():
    devices = [
        _switch(device_type="usw"),
        UnifiSwitchDevice(mac="bb:01", name="ap-1", model="U7PG2", device_type="uap"),
        UnifiSwitchDevice(mac="cc:01", name="gw", model="UDMPRO", device_type="udm"),
        UnifiSwitchDevice(mac="dd:01", name="odd", model="XYZ", device_type="weird"),
    ]

    _, netbox = _run(devices)

    assert {"switch", "wireless-ap", "router", "network-device"} <= netbox.created_roles


def test_rerun_does_not_recreate_devices():
    netbox = FakeNetboxGateway()
    settings = make_settings(sync_unifi_devices=True)
    for _ in range(2):
        SyncEngine(
            unifi=FakeUnifiClient(clients=[], devices=[_switch()]),
            netbox=CachingNetboxGateway(netbox),
            settings=settings,
        ).run()

    assert len(netbox.infra_by_mac) == 1


def test_dry_run_creates_nothing():
    summary, netbox = _run([_switch()], dry_run=True)

    assert netbox.infra_by_mac == {}
    assert netbox.created_device_types == set()
    assert summary.infra_created == 0


# -- the payoff: cables work without pre-populating NetBox -------------


def test_creating_the_switch_makes_cables_work_in_the_same_run():
    """The whole point: with an empty NetBox, one run should create the
    switch, its ports, the client, and the cable between them."""
    client = make_unifi_client(
        mac="11:22:33:44:55:66",
        name="laptop",
        is_wired=True,
        switch_mac=SWITCH_MAC,
        switch_port=3,
    )

    summary, netbox = _run([_switch()], clients=[client])

    assert summary.infra_created == 1
    assert summary.devices_created == 1  # the client
    assert summary.cables_created == 1, "cable should land on the just-created switch"
    assert summary.cables_skipped == 0
    assert netbox.infra_by_mac[SWITCH_MAC].interfaces["Port 3"].cable is not None


def test_created_switch_ports_match_the_default_port_templates():
    """Interfaces are named "Port N", which the default PORT_NAME_TEMPLATES
    already resolve — so cabling needs no extra configuration."""
    _, netbox = _run([_switch(ports=3)])

    assert set(netbox.infra_by_mac[SWITCH_MAC].interfaces) == {"Port 1", "Port 2", "Port 3"}


# -- devicetype-library ------------------------------------------------


def _write_library(tmp_path):
    vendor = tmp_path / "device-types" / "Ubiquiti"
    vendor.mkdir(parents=True)
    (vendor / "USW-24-PoE.yaml").write_text(
        "manufacturer: Ubiquiti\n"
        "model: UniFi Switch 24 PoE Gen2\n"
        "slug: ubiquiti-unifi-switch-24-poe-gen2\n"
        "part_number: USW-24-POE\n"
        "u_height: 1\n"
        "is_full_depth: false\n"
        "interfaces:\n"
        "  - name: Port 1\n"
        "    type: 1000base-t\n"
        "  - name: Port 2\n"
        "    type: 1000base-t\n"
    )
    return tmp_path


def test_library_matches_on_normalized_part_number(tmp_path):
    library = DeviceTypeLibrary(str(_write_library(tmp_path)))

    spec = library.lookup("USW24POE")  # controller reports it without dashes

    assert spec is not None
    assert spec.model == "UniFi Switch 24 PoE Gen2"
    assert spec.slug == "ubiquiti-unifi-switch-24-poe-gen2"
    assert spec.u_height == 1


def test_library_metadata_is_used_when_it_matches(tmp_path):
    _, netbox = _run([_switch(model="USW24POE")], devicetype_library_path=str(_write_library(tmp_path)))

    assert "ubiquiti-unifi-switch-24-poe-gen2" in netbox.created_device_types


def test_unmatched_model_still_syncs_from_the_controller(tmp_path):
    _, netbox = _run(
        [_switch(model="SOMETHINGNEW", ports=5)],
        devicetype_library_path=str(_write_library(tmp_path)),
    )

    device = netbox.infra_by_mac[SWITCH_MAC]
    assert len(device.interfaces) == 5, "falls back to the controller's real port list"


def test_missing_library_path_is_not_fatal():
    library = DeviceTypeLibrary("/nonexistent/devicetype-library")
    assert library.lookup("USW24POE") is None


def test_no_library_configured_returns_nothing():
    assert DeviceTypeLibrary(None).lookup("USW24POE") is None


# -- legacy client merge ------------------------------------------------


def test_legacy_client_on_another_real_site_is_merged_and_moved():
    """A real site still pulls a legacy client onto itself. Inventory does not."""
    netbox = FakeNetboxGateway()
    legacy = netbox.seed_legacy_client(SWITCH_MAC, site_slug="branch")
    legacy_id = legacy.id

    summary, _ = _run([_switch()], netbox=netbox, device_update_policy="create-only")

    assert summary.infra_merged == 1
    assert summary.infra_moved == 1
    assert summary.infra_created == 0
    assert legacy.id == legacy_id
    assert legacy.site_slug == "main"
    assert legacy.role_slug == "switch"


def test_legacy_client_is_merged_in_place_under_create_only():
    """Older releases stored switches as client devices named by MAC."""
    netbox = FakeNetboxGateway()
    legacy = netbox.seed_legacy_client(SWITCH_MAC, site_slug="main")
    legacy_id = legacy.id

    summary, _ = _run([_switch()], netbox=netbox, device_update_policy="create-only")

    assert summary.infra_merged == 1
    assert summary.infra_created == 0
    device = netbox.infra_by_mac[SWITCH_MAC]
    assert device.id == legacy_id
    assert device.name == "core-switch"
    assert device.role_slug == "switch"
    assert device.serial == "F09FC2AAAA01"
    assert "eth0" not in device.interfaces
    assert set(device.interfaces) == {f"Port {i}" for i in range(1, 9)}
    assert SWITCH_MAC not in netbox.clients_by_mac


def test_legacy_eth0_ip_and_cable_move_onto_the_new_interface():
    netbox = FakeNetboxGateway()
    legacy = netbox.seed_legacy_client(SWITCH_MAC)
    eth0 = legacy.interfaces["eth0"]
    eth0.ips.append(FakeIP("10.1.2.3"))
    peer = netbox.seed_switch("11:22:33:44:55:66", "uplink", ["Port 1"]).interfaces["Port 1"]
    eth0.cable = object()
    eth0.link_peers = [peer]
    peer.link_peers = [eth0]
    peer.cable = eth0.cable

    _run([_switch(ports=2)], netbox=netbox)

    device = netbox.infra_by_mac[SWITCH_MAC]
    assert "eth0" not in device.interfaces
    port = device.interfaces["Port 1"]
    assert [ip.address for ip in port.ips] == ["10.1.2.3/32"]
    assert port.cable is eth0.cable
    assert peer.link_peers == [port]


def test_uap_library_eth0_is_kept_on_merge(tmp_path):
    vendor = tmp_path / "device-types" / "Ubiquiti"
    vendor.mkdir(parents=True)
    (vendor / "UAP-AC-Pro.yaml").write_text(
        "manufacturer: Ubiquiti\n"
        "model: UniFi AP-AC-Pro\n"
        "slug: ubiquiti-unifi-ap-ac-pro\n"
        "part_number: UAP-AC-PRO\n"
        "interfaces:\n"
        "  - name: eth0\n"
        "    type: 1000base-t\n"
    )
    netbox = FakeNetboxGateway()
    legacy = netbox.seed_legacy_client("aa:bb:cc:dd:ee:02")
    ap = UnifiSwitchDevice(mac="aa:bb:cc:dd:ee:02", name="ap-1", model="U7PG2", device_type="uap")

    _run([ap], netbox=netbox, devicetype_library_path=str(tmp_path))

    assert "eth0" in legacy.interfaces
    assert legacy.role_slug == "wireless-ap"
    assert legacy.device_type_slug == "ubiquiti-unifi-ap-ac-pro"


def test_dry_run_logs_the_merge_and_writes_nothing(caplog):
    netbox = FakeNetboxGateway()
    legacy = netbox.seed_legacy_client(SWITCH_MAC)

    with caplog.at_level(logging.INFO):
        summary, _ = _run([_switch()], netbox=netbox, dry_run=True)

    assert summary.infra_created == 0
    assert summary.infra_merged == 0
    assert legacy.role_slug == "unifi-client"
    assert netbox.infra_by_mac == {}
    assert "would merge" in caplog.text
    assert "legacy client device" in caplog.text


def test_merge_disabled_leaves_the_client_and_does_not_duplicate():
    netbox = FakeNetboxGateway()
    legacy = netbox.seed_legacy_client(SWITCH_MAC)
    legacy_id = legacy.id

    summary, _ = _run([_switch()], netbox=netbox, merge_client_infra=False)

    assert summary.infra_created == 0
    assert summary.infra_merged == 0
    assert summary.infra_duplicates_avoided == 1
    assert legacy.id == legacy_id
    assert legacy.role_slug == "unifi-client"
    assert legacy.name == SWITCH_MAC.replace(":", "")
    assert netbox.infra_by_mac == {}


def test_merge_disabled_does_not_convert_under_sync_policy():
    netbox = FakeNetboxGateway()
    legacy = netbox.seed_legacy_client(SWITCH_MAC)
    engine = SyncEngine(
        unifi=FakeUnifiClient(devices=[_switch()]),
        netbox=CachingNetboxGateway(netbox),
        settings=make_settings(
            sync_unifi_devices=True,
            merge_client_infra=False,
            device_update_policy="sync",
        ),
    )
    summary = engine.run()

    assert summary.infra_created == 0
    assert summary.infra_merged == 0
    assert legacy.role_slug == "unifi-client"
    assert legacy.name == SWITCH_MAC.replace(":", "")
    assert netbox.infra_by_mac == {}


def test_untagged_mac_match_is_left_alone():
    netbox = FakeNetboxGateway()
    legacy = netbox.seed_legacy_client(SWITCH_MAC)
    legacy.tags.clear()

    summary, _ = _run([_switch()], netbox=netbox)

    assert summary.infra_created == 0
    assert legacy.role_slug == "unifi-client"
    assert netbox.infra_by_mac == {}


def test_merged_switch_is_not_rewritten_when_it_also_appears_as_a_client():
    netbox = FakeNetboxGateway()
    netbox.seed_legacy_client(SWITCH_MAC)
    client = make_unifi_client(mac=SWITCH_MAC, name="core-switch", is_wired=False)

    summary, _ = _run([_switch()], clients=[client], netbox=netbox)

    device = netbox.infra_by_mac[SWITCH_MAC]
    assert device.role_slug == "switch"
    assert summary.devices_created == 0
    assert SWITCH_MAC not in netbox.clients_by_mac


# -- inventory / staging sites ------------------------------------------


OTHER_MAC = "aa:bb:cc:dd:ee:09"


def _device(mac, name, serial, model="USW24POE", device_type="usw", **kw):
    return UnifiSwitchDevice(
        mac=mac,
        name=name,
        model=model,
        device_type=device_type,
        serial=serial,
        ports=[UnifiPort(index=1, media="GE", max_speed=1000)],
        **kw,
    )


def _two_sites(devices_by_site, site_map, **overrides):
    netbox = overrides.pop("netbox", None) or FakeNetboxGateway()
    engine = SyncEngine(
        unifi=FakeUnifiClient(devices_by_site=devices_by_site, sites=overrides.pop("sites", None)),
        netbox=CachingNetboxGateway(netbox),
        settings=make_settings(
            sync_unifi_devices=True,
            site_map=site_map,
            netbox_site_slug="",
            **overrides,
        ),
    )
    return engine.run(), netbox


def test_inventory_site_merges_a_legacy_client_in_place_without_moving_it():
    """A tagged client on another site is converted, not skipped and not moved."""
    netbox = FakeNetboxGateway()
    legacy = netbox.seed_legacy_client(OTHER_MAC, site_slug="hq")
    legacy_id = legacy.id
    switch = _device(OTHER_MAC, "spare-switch", "SER-INV-9")

    summary, _ = _two_sites(
        {"staging": [switch]},
        {"staging": "warehouse"},
        inventory_site_ids=("staging",),
        device_update_policy="create-only",
        merge_client_infra=True,
        netbox=netbox,
    )

    assert summary.infra_merged == 1
    assert summary.infra_created == 0
    assert summary.infra_moved == 0
    assert summary.infra_duplicates_avoided == 0
    assert legacy.id == legacy_id
    assert legacy.site_slug == "hq"
    assert legacy.name == "spare-switch"
    assert legacy.role_slug == "switch"
    assert legacy.device_type_slug == "ubiquiti-usw24poe"
    assert legacy.status.value == "inventory"
    assert "eth0" not in legacy.interfaces
    assert netbox.infra_by_mac[OTHER_MAC].id == legacy_id


def test_inventory_legacy_merge_is_planned_and_not_written_on_dry_run(caplog):
    netbox = FakeNetboxGateway()
    legacy = netbox.seed_legacy_client(OTHER_MAC, site_slug="hq")
    switch = _device(OTHER_MAC, "spare-switch", "SER-INV-9")

    with caplog.at_level(logging.INFO):
        summary, _ = _two_sites(
            {"staging": [switch]},
            {"staging": "warehouse"},
            inventory_site_ids=("staging",),
            device_update_policy="create-only",
            dry_run=True,
            netbox=netbox,
        )

    assert summary.infra_merged == 0
    assert summary.infra_created == 0
    assert legacy.role_slug == "unifi-client"
    assert legacy.site_slug == "hq"
    assert netbox.infra_by_mac == {}
    merge_lines = [r.getMessage() for r in caplog.records if "would merge" in r.getMessage()]
    assert len(merge_lines) == 1
    assert "site='hq'" in merge_lines[0]
    assert "not creating a duplicate and not moving it" not in caplog.text


def test_inventory_site_still_skips_a_non_legacy_device_on_another_site(caplog):
    netbox = FakeNetboxGateway()
    parked = netbox.seed_legacy_client(OTHER_MAC, site_slug="hq", role_slug="switch")
    parked.name = "floor-switch"
    parked.device_type_slug = "already-there"
    switch = _device(OTHER_MAC, "spare-switch", "SER-INV-9")

    with caplog.at_level(logging.INFO):
        summary, _ = _two_sites(
            {"staging": [switch]},
            {"staging": "warehouse"},
            inventory_site_ids=("staging",),
            device_update_policy="create-only",
            netbox=netbox,
        )

    assert summary.infra_duplicates_avoided == 1
    assert summary.infra_merged == 0
    assert summary.infra_created == 0
    assert summary.infra_moved == 0
    assert parked.site_slug == "hq"
    assert parked.role_slug == "switch"
    assert parked.device_type_slug == "already-there"
    assert parked.name == "floor-switch"
    assert parked.status.value == "active"
    assert "not creating a duplicate and not moving it" in caplog.text


def test_inventory_legacy_client_is_left_alone_when_merge_is_disabled():
    netbox = FakeNetboxGateway()
    legacy = netbox.seed_legacy_client(OTHER_MAC, site_slug="hq")
    switch = _device(OTHER_MAC, "spare-switch", "SER-INV-9")

    summary, _ = _two_sites(
        {"staging": [switch]},
        {"staging": "warehouse"},
        inventory_site_ids=("staging",),
        merge_client_infra=False,
        netbox=netbox,
    )

    assert summary.infra_created == 0
    assert summary.infra_merged == 0
    assert summary.infra_duplicates_avoided == 1
    assert legacy.role_slug == "unifi-client"
    assert legacy.site_slug == "hq"
    assert netbox.infra_by_mac == {}


def test_inventory_site_does_not_duplicate_a_device_on_a_real_site():
    real = _device(OTHER_MAC, "spare-switch", "SER-INV-1")
    summary, netbox = _two_sites(
        {"north": [real], "staging": [real]},
        {"north": "hq", "staging": "warehouse"},
        inventory_site_ids=("staging",),
    )

    assert summary.infra_created == 1
    assert summary.infra_duplicates_avoided == 1
    assert len(netbox.infra_by_mac) == 1
    device = netbox.infra_by_mac[OTHER_MAC]
    assert device.site_slug == "hq"
    assert device.status.value == "active"


def test_inventory_observation_never_pulls_a_device_off_a_real_site():
    real = _device(OTHER_MAC, "floor-switch", "SER-INV-2")
    summary, netbox = _two_sites(
        {"staging": [real], "north": [real]},
        {"staging": "warehouse", "north": "hq"},
        inventory_site_ids=("staging",),
        device_update_policy="create-only",
    )

    assert len(netbox.infra_by_mac) == 1
    device = netbox.infra_by_mac[OTHER_MAC]
    assert device.site_slug == "hq"
    assert device.status.value == "active"
    assert summary.infra_moved == 1


def test_device_created_on_an_inventory_site_uses_inventory_status():
    staging = _device(OTHER_MAC, "shelf-switch", "SER-INV-3")
    summary, netbox = _two_sites(
        {"staging": [staging]},
        {"staging": "warehouse"},
        inventory_site_ids=("staging",),
        sites=[UnifiSite(name="staging", description="Staging", internal_id="64aabbccddeeff0011223344")],
    )

    device = netbox.infra_by_mac[OTHER_MAC]
    assert summary.infra_created == 1
    assert device.status.value == "inventory"
    assert device.site_slug == "warehouse"
    assert device.custom_fields["unifi_site_name"] == "staging"
    assert device.custom_fields["unifi_site_internal_id"] == "64aabbccddeeff0011223344"


def test_inventory_device_moves_to_the_real_site_under_create_only():
    netbox = FakeNetboxGateway()
    parked = netbox.seed_legacy_client(OTHER_MAC, site_slug="warehouse", role_slug="switch")
    parked.serial = "SER-INV-4"
    parked.custom_fields["unifi_site_name"] = "staging"
    parked.status = parked.status  # already active; the move sets the real-site status
    device = _device(OTHER_MAC, "floor-switch", "SER-INV-4")

    engine = SyncEngine(
        unifi=FakeUnifiClient(devices=[device]),
        netbox=CachingNetboxGateway(netbox),
        settings=make_settings(
            sync_unifi_devices=True,
            inventory_site_ids=("staging",),
            device_update_policy="create-only",
        ),
    )
    summary = engine.run()

    assert summary.infra_moved == 1
    assert summary.infra_created == 0
    assert parked.site_slug == "main"
    assert parked.status.value == "active"
    assert parked.id == netbox.infra_by_mac[OTHER_MAC].id


def test_create_only_does_not_move_between_two_real_sites():
    netbox = FakeNetboxGateway()
    parked = netbox.seed_legacy_client(OTHER_MAC, site_slug="branch", role_slug="switch")
    parked.serial = "SER-REAL-1"
    device = _device(OTHER_MAC, "floor-switch", "SER-REAL-1")
    engine = SyncEngine(
        unifi=FakeUnifiClient(devices=[device]),
        netbox=CachingNetboxGateway(netbox),
        settings=make_settings(sync_unifi_devices=True, device_update_policy="create-only"),
    )
    summary = engine.run()

    assert summary.infra_created == 0
    assert summary.infra_duplicates_avoided == 1
    assert parked.site_slug == "branch"
    assert netbox.infra_by_mac == {}


def test_sync_policy_moves_a_device_between_real_sites():
    netbox = FakeNetboxGateway()
    parked = netbox.seed_legacy_client(OTHER_MAC, site_slug="branch", role_slug="switch")
    parked.serial = "SER-REAL-2"
    device = _device(OTHER_MAC, "floor-switch", "SER-REAL-2")
    engine = SyncEngine(
        unifi=FakeUnifiClient(devices=[device]),
        netbox=CachingNetboxGateway(netbox),
        settings=make_settings(sync_unifi_devices=True, device_update_policy="sync"),
    )
    summary = engine.run()

    assert summary.infra_moved == 1
    assert parked.site_slug == "main"
    assert parked.name == "floor-switch"


def test_same_name_at_another_site_is_not_a_match():
    netbox = FakeNetboxGateway()
    other = netbox.seed_unmatchable_switch("core-switch", ["Port 1"], serial="DIFFERENT")
    other.site_slug = "branch"
    other.tags.append("unifi-sync")
    other.role_slug = "switch"

    summary, _ = _run([_switch(serial="F09FC2AAAA01")], netbox=netbox)

    assert summary.infra_created == 1
    assert netbox.infra_by_mac[SWITCH_MAC].site_slug == "main"
    assert other.site_slug == "branch"


def test_serial_match_across_sites_avoids_a_duplicate(caplog):
    netbox = FakeNetboxGateway()
    other = netbox.seed_unmatchable_switch("old-name", ["Port 1"], serial="F09FC2AAAA01")
    other.site_slug = "branch"
    other.tags.append("unifi-sync")
    other.role_slug = "switch"
    engine = SyncEngine(
        unifi=FakeUnifiClient(devices=[_switch()]),
        netbox=CachingNetboxGateway(netbox),
        settings=make_settings(sync_unifi_devices=True, device_update_policy="create-only"),
    )
    with caplog.at_level(logging.INFO):
        summary = engine.run()

    assert summary.infra_created == 0
    assert summary.infra_duplicates_avoided == 1
    assert "cross-site match avoided" in caplog.text
    assert other.site_slug == "branch"


# -- roles and device types ---------------------------------------------


def test_existing_role_slug_is_reused_not_created(caplog):
    netbox = FakeNetboxGateway()
    netbox.seed_role("access-switch")
    engine = SyncEngine(
        unifi=FakeUnifiClient(devices=[_switch()]),
        netbox=CachingNetboxGateway(netbox),
        settings=make_settings(sync_unifi_devices=True, unifi_switch_role_slug="access-switch"),
    )
    engine.run()

    assert "access-switch" not in netbox.created_roles
    assert netbox.infra_by_mac[SWITCH_MAC].role_slug == "access-switch"

    api = MagicMock()
    api.dcim.device_roles.get.return_value = MagicMock(slug="access-switch")
    gateway = PynetboxGateway.__new__(PynetboxGateway)
    gateway.api = api
    with caplog.at_level(logging.INFO):
        gateway.ensure_device_role("access-switch")
    assert "Reusing existing NetBox device role" in caplog.text
    api.dcim.device_roles.create.assert_not_called()


def test_device_type_is_reused_by_normalized_part_number():
    netbox = FakeNetboxGateway()
    netbox.seed_device_type(
        DeviceTypeSpec(
            manufacturer="Ubiquiti",
            model="UniFi Switch 24 PoE",
            slug="already-imported",
            part_number="USW-24-POE",
            interfaces=[("Port 1", "1000base-t")],
        )
    )
    engine = SyncEngine(
        unifi=FakeUnifiClient(devices=[_switch(model="USW24POE", ports=8)]),
        netbox=CachingNetboxGateway(netbox),
        settings=make_settings(sync_unifi_devices=True),
    )
    engine.run()

    device = netbox.infra_by_mac[SWITCH_MAC]
    assert device.device_type_slug == "already-imported"
    assert "already-imported" in netbox.reused_device_types
    assert "ubiquiti-usw24poe" not in netbox.created_device_types


def test_library_aliases_map_controller_codes(tmp_path):
    vendor = tmp_path / "device-types" / "Ubiquiti"
    vendor.mkdir(parents=True)
    (vendor / "US-24.yaml").write_text(
        "manufacturer: Ubiquiti\nmodel: UniFi Switch 24\nslug: us-24\npart_number: US-24\n"
        "interfaces:\n  - name: Port 1\n    type: 1000base-t\n"
    )
    (vendor / "UAP-AC-Pro.yaml").write_text(
        "manufacturer: Ubiquiti\nmodel: UniFi AP-AC-Pro\nslug: uap-ac-pro\npart_number: UAP-AC-PRO\n"
        "interfaces:\n  - name: eth0\n    type: 1000base-t\n"
    )
    (vendor / "USW-Pro-24.yaml").write_text(
        "manufacturer: Ubiquiti\nmodel: UniFi Switch Pro 24\nslug: usw-pro-24\npart_number: USW-Pro-24\n"
        "interfaces:\n  - name: Port 1\n    type: 1000base-t\n"
    )
    (vendor / "USP-PDU-Pro.yaml").write_text(
        "manufacturer: Ubiquiti\nmodel: UniFi SmartPower PDU Pro\nslug: usp-pdu-pro\n"
        "part_number: USP-PDU-Pro\n"
        "interfaces:\n  - name: Port 21\n    type: 1000base-t\n"
        "power-ports:\n  - name: Input\n    type: nema-5-15p\n"
        "power-outlets:\n  - name: Outlet 1\n    type: nema-5-15r\n    power_port: Input\n"
    )
    library = DeviceTypeLibrary(str(tmp_path))

    assert library.lookup("US24").slug == "us-24"
    assert library.lookup("U7PG2").model == "UniFi AP-AC-Pro"
    assert library.lookup("US24PRO2").part_number == "USW-Pro-24"
    pdu = library.lookup("USPPDUP")
    assert pdu.slug == "usp-pdu-pro"
    assert [outlet.name for outlet in pdu.power_outlets] == ["Outlet 1"]
    assert pdu.power_outlets[0].type == "nema-5-15r"


def test_generated_ups_uses_power_outlets_and_the_pdu_role():
    ups = UnifiSwitchDevice(
        mac="aa:bb:cc:dd:ee:23",
        name="ups-1",
        model="USWDA23",
        device_type="usw",
        shortname="UPS23",
        ports=[UnifiPort(index=1, media="GE", max_speed=1000)],
        outlets=[UnifiOutlet(index=1, name="Outlet 1"), UnifiOutlet(index=2, name="Outlet 2")],
    )

    spec = spec_from_unifi_device(ups)
    assert [name for name, _ in spec.interfaces] == ["Port 1"]
    assert [outlet.name for outlet in spec.power_outlets] == ["Outlet 1", "Outlet 2"]
    assert spec.power_ports[0].name == "Input"

    summary, netbox = _run([ups])

    device = netbox.infra_by_mac["aa:bb:cc:dd:ee:23"]
    assert summary.infra_type_generated == 1
    assert device.role_slug == "pdu"
    assert set(device.power_outlets) == {"Outlet 1", "Outlet 2"}
    assert "Outlet 1" not in device.interfaces
    assert "Port 1" in device.interfaces


def test_library_power_templates_are_copied_onto_the_device(tmp_path):
    vendor = tmp_path / "device-types" / "Ubiquiti"
    vendor.mkdir(parents=True)
    (vendor / "USP-PDU-Pro.yaml").write_text(
        "manufacturer: Ubiquiti\nmodel: UniFi SmartPower PDU Pro\nslug: usp-pdu-pro\n"
        "part_number: USP-PDU-Pro\n"
        "interfaces:\n  - name: Port 21\n    type: 1000base-t\n"
        "power-ports:\n  - name: Input\n    type: nema-5-15p\n"
        "power-outlets:\n"
        "  - name: Outlet 1\n    type: nema-5-15r\n    power_port: Input\n"
        "  - name: USB-C\n    type: usb-c\n    power_port: Input\n"
    )
    pdu = UnifiSwitchDevice(
        mac="aa:bb:cc:dd:ee:30",
        name="pdu-1",
        model="USPPDUP",
        device_type="usw",
        outlets=[UnifiOutlet(index=1, name="ignored-by-library")],
        ports=[UnifiPort(index=1, media="GE", max_speed=1000)],
    )

    summary, netbox = _run([pdu], devicetype_library_path=str(tmp_path))

    device = netbox.infra_by_mac["aa:bb:cc:dd:ee:30"]
    assert summary.infra_type_library == 1
    assert device.role_slug == "pdu"
    assert device.device_type_slug == "usp-pdu-pro"
    assert set(device.interfaces) == {"Port 21"}
    assert device.power_outlets["Outlet 1"] == "nema-5-15r"
    assert device.power_ports["Input"] == "nema-5-15p"


def test_uxg_pro_with_one_outlet_is_a_gateway_not_a_pdu():
    gateway = UnifiSwitchDevice(
        mac="aa:bb:cc:dd:ee:40",
        name="edge-gw",
        model="UXGPRO",
        device_type="uxg",
        ports=[UnifiPort(index=1, media="GE", max_speed=1000)],
        outlets=[UnifiOutlet(index=1, name="Outlet 1")],
    )

    spec = spec_from_unifi_device(gateway)
    assert spec.power_outlets == []
    assert [name for name, _ in spec.interfaces] == ["Port 1"]

    summary, netbox = _run([gateway])

    device = netbox.infra_by_mac["aa:bb:cc:dd:ee:40"]
    assert device.role_slug == "router"
    assert device.power_outlets == {}
    assert "Port 1" in device.interfaces
    assert summary.infra_created == 1


def test_up6_strip_gets_six_outlets_when_the_controller_sends_one():
    strip = UnifiSwitchDevice(
        mac="aa:bb:cc:dd:ee:61",
        name="bench-strip",
        model="UP6",
        device_type="uap",
        shortname="USP-Strip",
        outlets=[UnifiOutlet(index=1, name="Bench 1")],
    )

    spec = spec_from_unifi_device(strip)
    assert [outlet.name for outlet in spec.power_outlets] == ["Bench 1"] + [
        f"Outlet {i}" for i in range(2, 7)
    ]

    summary, netbox = _run([strip])

    device = netbox.infra_by_mac["aa:bb:cc:dd:ee:61"]
    assert device.role_slug == "pdu"
    assert list(device.power_outlets) == ["Bench 1"] + [f"Outlet {i}" for i in range(2, 7)]
    assert summary.infra_type_generated == 1


def test_up6_with_an_empty_outlet_table_still_has_six_outlets():
    strip = UnifiSwitchDevice(
        mac="aa:bb:cc:dd:ee:62",
        name="empty-strip",
        model="UP6",
        device_type="uap",
        outlets=[],
    )

    spec = spec_from_unifi_device(strip)

    assert [outlet.name for outlet in spec.power_outlets] == [f"Outlet {i}" for i in range(1, 7)]


def test_up6_keeps_a_complete_outlet_table():
    strip = UnifiSwitchDevice(
        mac="aa:bb:cc:dd:ee:63",
        name="full-strip",
        model="UP6",
        device_type="uap",
        outlets=[UnifiOutlet(index=i, name=f"Bench {i}") for i in range(1, 7)],
    )

    spec = spec_from_unifi_device(strip)

    assert [outlet.name for outlet in spec.power_outlets] == [f"Bench {i}" for i in range(1, 7)]


def test_library_outlet_list_wins_over_the_up6_count(tmp_path):
    vendor = tmp_path / "device-types" / "Ubiquiti"
    vendor.mkdir(parents=True)
    (vendor / "USP-Strip.yaml").write_text(
        "manufacturer: Ubiquiti\nmodel: UniFi SmartPower Strip\nslug: usp-strip\n"
        "part_number: USP-Strip\n"
        "power-ports:\n  - name: Input\n    type: nema-5-15p\n"
        "power-outlets:\n"
        "  - name: Outlet A\n    type: nema-5-15r\n    power_port: Input\n"
        "  - name: Outlet B\n    type: nema-5-15r\n    power_port: Input\n"
    )
    strip = UnifiSwitchDevice(
        mac="aa:bb:cc:dd:ee:64",
        name="library-strip",
        model="UP6",
        device_type="uap",
        outlets=[UnifiOutlet(index=1, name="ignored")],
    )

    _, netbox = _run([strip], devicetype_library_path=str(tmp_path))

    device = netbox.infra_by_mac["aa:bb:cc:dd:ee:64"]
    assert device.device_type_slug == "usp-strip"
    assert set(device.power_outlets) == {"Outlet A", "Outlet B"}
