from __future__ import annotations

from unifi_netbox_sync.model_map import (
    candidate_keys,
    device_kind,
    is_power_device,
    known_outlet_count,
)
from unifi_netbox_sync.models import UnifiOutlet, UnifiSwitchDevice


def test_candidate_keys_include_library_skus():
    assert candidate_keys("US24PRO2")[0] == "US24PRO2"
    assert "USWPRO24" in candidate_keys("US24PRO2")
    assert "UAPACPRO" in candidate_keys("U7PG2")
    assert "USPPDUPRO" in candidate_keys("USPPDUP")
    assert candidate_keys("US24")[0] == "US24"


def test_power_detection_covers_pdu_rps_and_ups_towers():
    assert is_power_device("USPPDUP", "usw")
    assert is_power_device("USWDA23", "usw", shortname="UPS23")
    assert is_power_device("USPRPS", "usw")
    assert is_power_device("US24", "usw", outlet_count=2)
    assert is_power_device("USPSTRIP", "usp")


def test_pro_xg_switches_are_not_power_devices():
    assert is_power_device("USPXG8P", "usw") is False
    assert is_power_device("USPXG24", "usw", shortname="USW-Pro-XG-24") is False
    assert is_power_device("US24", "usw") is False
    assert is_power_device("USW24POE", "usw") is False


def test_device_kind_checks_power_before_switch():
    pdu = UnifiSwitchDevice(mac="aa", name="pdu", model="USPPDUP", device_type="usw")
    switch = UnifiSwitchDevice(mac="aa", name="sw", model="US24", device_type="usw")
    ap = UnifiSwitchDevice(mac="aa", name="ap", model="U7PG2", device_type="uap")
    gateway = UnifiSwitchDevice(mac="aa", name="gw", model="UDMPRO", device_type="udm")
    xg = UnifiSwitchDevice(mac="aa", name="xg", model="USPXG8P", device_type="usw")

    assert device_kind(pdu) == "power"
    assert device_kind(switch) == "switch"
    assert device_kind(ap) == "ap"
    assert device_kind(gateway) == "gateway"
    assert device_kind(xg) == "switch"


def test_gateways_with_an_outlet_table_stay_gateways():
    """UXG-Pro reports one outlet. That must not select the PDU role."""
    one = [UnifiOutlet(index=1, name="Outlet 1")]
    for model, dtype in (("UXGPRO", "uxg"), ("UDMPRO", "udm"), ("UGW4", "ugw"), ("UDR", "udm")):
        assert is_power_device(model, dtype, outlet_count=1) is False
        device = UnifiSwitchDevice(
            mac="aa", name="gw", model=model, device_type=dtype, outlets=one
        )
        assert device_kind(device) == "gateway"

    # A missing type still follows the model family. UDMB is an AP.
    assert device_kind(UnifiSwitchDevice(mac="aa", name="gw", model="UXGPRO", device_type="")) == "gateway"
    assert device_kind(UnifiSwitchDevice(mac="aa", name="beacon", model="UDMB", device_type="uap")) == "ap"
    # A switch that really has outlets is still power.
    assert is_power_device("US24", "usw", outlet_count=2) is True


def test_up6_strip_is_known_to_have_six_outlets():
    assert known_outlet_count("UP6") == 6
    assert known_outlet_count("USP-Strip") == 6
    assert known_outlet_count("USW24POE", "USP-Strip") == 6
    assert known_outlet_count("USPPDUP") == 0
