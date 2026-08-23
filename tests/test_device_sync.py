from __future__ import annotations

from unifi_netbox_sync.caching import CachingNetboxGateway
from unifi_netbox_sync.devicetype_library import (
    DeviceTypeLibrary,
    netbox_interface_type,
    normalize_model,
    spec_from_unifi_device,
)
from unifi_netbox_sync.models import UnifiPort, UnifiSwitchDevice
from unifi_netbox_sync.sync import SyncEngine

from .fakes import FakeNetboxGateway, FakeUnifiClient, make_unifi_client
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


def _run(devices, clients=(), **overrides):
    netbox = FakeNetboxGateway()
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
