from __future__ import annotations

from unifi_netbox_sync.naming import slugify
from unifi_netbox_sync.oui import OuiLookup, is_locally_administered, oui_prefix
from unifi_netbox_sync.sync import SyncEngine

from .fakes import FakeNetboxGateway, FakeUnifiClient, make_unifi_client
from .test_sync import make_settings


def _run(clients, netbox=None, **overrides):
    netbox = netbox or FakeNetboxGateway()
    engine = SyncEngine(
        unifi=FakeUnifiClient(clients=clients), netbox=netbox, settings=make_settings(**overrides)
    )
    return engine.run(), netbox


def test_slugify_handles_real_vendor_strings():
    assert slugify("Apple, Inc.") == "apple-inc"
    assert slugify("Intel Corporate") == "intel-corporate"
    assert slugify("Ubiquiti Inc") == "ubiquiti-inc"
    assert slugify("  Cisco Systems, Inc  ") == "cisco-systems-inc"
    assert slugify("!!!") == ""


def test_client_gets_a_device_type_for_its_reported_vendor():
    client = make_unifi_client(mac="00:11:22:33:44:55", name="mac-mini", is_wired=False, oui="Apple, Inc.")

    _, netbox = _run([client])

    assert "apple-inc-client" in netbox.created_device_types


def test_clients_of_different_vendors_get_different_device_types():
    clients = [
        make_unifi_client(mac="00:11:22:33:44:01", is_wired=False, oui="Apple, Inc."),
        make_unifi_client(mac="00:11:22:33:44:02", is_wired=False, oui="Intel Corporate"),
    ]

    _, netbox = _run(clients)

    assert {"apple-inc-client", "intel-corporate-client"} <= netbox.created_device_types


def test_unknown_vendor_falls_back_to_the_configured_generic_type():
    client = make_unifi_client(mac="00:11:22:33:44:55", is_wired=False, oui=None)

    _, netbox = _run([client])

    assert netbox.created_device_types == set()


def test_oui_lookup_can_be_disabled():
    client = make_unifi_client(mac="00:11:22:33:44:55", is_wired=False, oui="Apple, Inc.")

    _, netbox = _run([client], use_oui_manufacturer=False)

    assert netbox.created_device_types == set()


def test_unusable_vendor_name_falls_back_instead_of_failing():
    client = make_unifi_client(mac="00:11:22:33:44:55", is_wired=False, oui="!!!")

    summary, netbox = _run([client])

    assert summary.errors == []
    assert summary.devices_created == 1
    assert netbox.created_device_types == set()


def test_randomized_mac_is_recognised_as_locally_administered():
    # Second-least-significant bit of the first octet set => randomized.
    assert is_locally_administered("02:11:22:33:44:55")
    assert is_locally_administered("da:a1:19:00:00:01")  # typical iOS private address
    assert not is_locally_administered("00:11:22:33:44:55")
    assert not is_locally_administered("f0:9f:c2:00:00:01")  # Ubiquiti


def test_oui_prefix_extraction():
    assert oui_prefix("aa:bb:cc:dd:ee:ff") == "AABBCC"


def test_offline_lookup_reads_ieee_format(tmp_path):
    oui_file = tmp_path / "oui.txt"
    oui_file.write_text(
        "OUI/MA-L                                                    Organization\n"
        "\n"
        "00-1B-63   (hex)\t\tApple, Inc.\n"
        "F0-9F-C2   (hex)\t\tUbiquiti Networks Inc.\n"
    )
    lookup = OuiLookup(str(oui_file))

    assert lookup.lookup("00:1b:63:11:22:33") == "Apple, Inc."
    assert lookup.lookup("f0:9f:c2:11:22:33") == "Ubiquiti Networks Inc."
    assert lookup.lookup("aa:aa:aa:11:22:33") is None


def test_offline_lookup_reads_wireshark_manuf_format(tmp_path):
    manuf = tmp_path / "manuf"
    manuf.write_text("# comment\n00:1B:63\tApple\tApple, Inc.\n3C:5A:B4\tGoogle\n")
    lookup = OuiLookup(str(manuf))

    assert lookup.lookup("00:1b:63:11:22:33") == "Apple, Inc."
    assert lookup.lookup("3c:5a:b4:11:22:33") == "Google"


def test_missing_oui_file_is_not_fatal():
    lookup = OuiLookup("/nonexistent/path/oui.txt")
    assert lookup.lookup("00:1b:63:11:22:33") is None


def test_offline_file_fills_in_when_controller_reports_no_vendor(tmp_path):
    oui_file = tmp_path / "oui.txt"
    oui_file.write_text("00-1B-63   (hex)\t\tApple, Inc.\n")
    client = make_unifi_client(mac="00:1b:63:aa:bb:cc", is_wired=False, oui=None)

    _, netbox = _run([client], oui_file=str(oui_file))

    assert "apple-inc-client" in netbox.created_device_types


def test_randomized_mac_is_not_attributed_to_a_vendor(tmp_path):
    """A locally-administered MAC has no owning vendor, so the offline table
    must not be consulted for it — the OUI bytes are meaningless there."""
    oui_file = tmp_path / "oui.txt"
    oui_file.write_text("02-11-22   (hex)\t\tSomeone Wrong\n")
    client = make_unifi_client(mac="02:11:22:33:44:55", is_wired=False, oui=None)

    _, netbox = _run([client], oui_file=str(oui_file))

    assert netbox.created_device_types == set()
