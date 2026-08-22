from __future__ import annotations

from unifi_netbox_sync.naming import mac_suffixed_name, sanitize_device_name


def test_sanitize_passes_through_normal_names():
    assert sanitize_device_name("laptop", "11:22:33:44:55:66") == "laptop"


def test_sanitize_strips_control_characters_and_collapses_whitespace():
    assert sanitize_device_name("lap\x00top  name\x1f", "11:22:33:44:55:66") == "laptop name"


def test_sanitize_falls_back_to_mac_when_name_empty():
    assert sanitize_device_name("   ", "11:22:33:44:55:66") == "112233445566"


def test_sanitize_truncates_to_netbox_max_length():
    long_name = "x" * 100
    result = sanitize_device_name(long_name, "11:22:33:44:55:66")
    assert len(result) == 64


def test_mac_suffixed_name_appends_last_four_hex_digits():
    assert mac_suffixed_name("laptop", "11:22:33:44:55:66") == "laptop-5566"


def test_mac_suffixed_name_truncates_base_to_fit():
    long_name = "x" * 64
    result = mac_suffixed_name(long_name, "11:22:33:44:55:66")
    assert result == ("x" * 59) + "-5566"
    assert len(result) == 64
