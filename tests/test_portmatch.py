"""Port-name matching: templates, slot/module, Juniper, zero-pad, SFP."""

from __future__ import annotations

import pytest

from unifi_netbox_sync.models import UnifiPort
from unifi_netbox_sync.portmatch import (
    candidate_names,
    match_port,
    match_uplink_port,
    parse_model_templates,
    sfp_ordinal,
    templates_for_model,
)

DEFAULTS = [
    "Port {port}",
    "{port}",
    "GE{port}",
    "Gi{port}",
    "eth{port}",
    "Ethernet{port}",
    "GigabitEthernet{port}",
    "Port{port}",
    "port{port}",
]


def _candidates(port: int, sfp: int | None = None, model: list[str] | None = None, extra: list[str] | None = None):
    return candidate_names(model or [], DEFAULTS, port, sfp, extra or [])


def _pinned(port: int, sfp: int | None, model: list[str] | None) -> int:
    if not model or port is None:
        return 0
    return len(candidate_names(model, [], port, sfp, []))


def _match(names: list[str], port: int, sfp: int | None = None, model: list[str] | None = None, extra: list[str] | None = None):
    return match_port(
        names,
        _candidates(port, sfp, model, extra),
        port,
        sfp,
        allow_structural=not model,
        pinned_count=_pinned(port, sfp, model),
    )


def test_exact_template_still_wins_over_a_structural_name():
    outcome = _match(["Port 4", "Gi1/0/4"], 4)
    assert outcome.name == "Port 4"
    assert outcome.via == "template"


@pytest.mark.parametrize(
    "name",
    [
        "Port 4",
        "4",
        "GE4",
        "Gi4",
        "eth4",
        "Ethernet4",
        "GigabitEthernet4",
        "port 4",  # case
        "Port 04",  # zero-pad
        "GE04",
    ],
)
def test_default_templates_match_common_and_padded_names(name):
    outcome = _match([name], 4)
    assert outcome.name == name
    assert outcome.via == "template"


@pytest.mark.parametrize(
    "name",
    [
        "Gi1/0/4",
        "gi1/0/4",
        "GigabitEthernet1/0/4",
        "Te1/0/4",
        "1/1/4",
        "1/0/4",
        "Gi1/0/04",
        "ge-0/0/4",
        "xe-0/0/4",
        "et-0/0/4",
        "mge-0/0/4",
        "ge-0/0/04",
        "Ethernet1/4",
        "Gi0/4",
    ],
)
def test_structural_match_covers_slot_module_and_juniper(name):
    outcome = _match([name, "mgmt0"], 4)
    assert outcome.name == name
    assert outcome.via == "structural"


def test_structural_does_not_match_a_different_port_or_a_lag():
    assert _match(["Gi1/0/14", "ae4", "vlan4", "mgmt0"], 4).name is None
    assert _match(["Port 14"], 4).name is None
    assert _match(["Gi1/0/40"], 4).name is None


def test_two_stack_members_are_ambiguous():
    outcome = _match(["Gi1/0/4", "Gi2/0/4"], 4)
    assert outcome.name is None
    assert outcome.via == "ambiguous"
    assert set(outcome.ambiguous_names) == {"Gi1/0/4", "Gi2/0/4"}


def test_model_template_picks_one_stack_member_and_disables_guessing():
    model = ["Gi1/0/{port}"]
    pinned = _match(["Gi1/0/4", "Gi2/0/4"], 4, model=model)
    assert pinned.name == "Gi1/0/4"
    assert pinned.via == "template"

    # The pinned scheme does not match the other member, and structural
    # guessing is off, so a missing Gi1/0/4 is a miss rather than Gi2/0/4.
    missed = _match(["Gi2/0/4"], 4, model=model)
    assert missed.name is None
    assert missed.via == "none"


def test_model_template_zero_pad_still_matches():
    outcome = _match(["Gi1/0/04"], 4, model=["Gi1/0/{port}"])
    assert outcome.name == "Gi1/0/04"
    assert outcome.via == "template"


def test_sfp_ordinal_matches_sfp_name_and_not_copper_port_1():
    ports = [UnifiPort(index=i, media="GE") for i in range(1, 25)]
    ports.append(UnifiPort(index=25, media="SFP+", name="Uplink"))
    assert sfp_ordinal(ports, 25) == 1
    assert sfp_ordinal(ports, 1) is None

    sfp = _match(["SFP 1", "xe-0/0/25"], 25, sfp=1)
    # The absolute port and the SFP ordinal both parse, so we do not guess.
    assert sfp.via == "ambiguous"

    only_sfp = _match(["SFP1"], 25, sfp=1)
    assert only_sfp.name == "SFP1"
    assert only_sfp.via == "structural"

    copper = _match(["SFP 1"], 1, sfp=None)
    assert copper.name is None


def test_unifi_port_name_is_an_exact_candidate():
    outcome = _match(["Camera"], 4, extra=["Camera"])
    assert outcome.name == "Camera"
    assert outcome.via == "template"


def test_parse_model_templates_folds_model_names():
    table = parse_model_templates(
        "USW-Pro-48=Gi1/0/{port};SFP {sfp}|EX2300=ge-0/0/{port}"
    )
    assert templates_for_model(table, "uswpro48") == ["Gi1/0/{port}", "SFP {sfp}"]
    assert templates_for_model(table, "USW-Pro-48-PoE") == []
    assert templates_for_model(table, "EX2300") == ["ge-0/0/{port}"]


def test_parse_model_templates_rejects_a_bad_rule():
    with pytest.raises(SystemExit):
        parse_model_templates("USW-24")
    with pytest.raises(SystemExit):
        parse_model_templates("USW-24=Gi1/0/{port}|USW24=Port {port}")


def _uplink(names, port, sfp=None, model=None, extra=None, types=None):
    candidates = _candidates(port, sfp, model, extra) if port is not None else []
    return match_uplink_port(
        names,
        candidates,
        port,
        sfp,
        allow_fallback=not model,
        interface_types=types,
        pinned_count=_pinned(port, sfp, model) if port is not None else 0,
    )


@pytest.mark.parametrize("name", ["SFP+ 49", "SFP+49", "SFP 49", "SFP28 49"])
def test_uplink_trailing_number_matches_sfp_names_templates_do_not(name):
    outcome = _uplink([name, "mgmt0"], 49, sfp=None, extra=["cage-a"])
    assert outcome.name == name
    assert outcome.via == "numeric-suffix"


def test_uplink_port_template_still_beats_a_trailing_sfp_name():
    outcome = _uplink(["Port 49", "SFP+ 49"], 49, extra=["SFP+ 1"])
    assert outcome.name == "Port 49"
    assert outcome.via == "exact"


@pytest.mark.parametrize("name", ["GE49", "Gi49", "eth49", "Port 49"])
def test_uplink_exact_template_covers_the_usual_prefixes(name):
    outcome = _uplink([name], 49)
    assert outcome.name == name
    assert outcome.via == "exact"


def test_uplink_sfp_flag_uses_the_client_structural_match():
    outcome = _uplink(["SFP+ 49"], 49, sfp=1, extra=["SFP+ 1"])
    assert outcome.name == "SFP+ 49"
    assert outcome.via == "structural"


def test_uplink_sole_wired_interface_is_port_1_on_an_access_point():
    outcome = _uplink(["eth0", "wlan0", "wlan1", "vlan1"], 1)
    assert outcome.name == "eth0"
    assert outcome.via == "zero-based"


def test_uplink_unknown_local_port_uses_the_only_wired_interface():
    outcome = _uplink(["eth0", "wlan0"], None)
    assert outcome.name == "eth0"
    assert outcome.via == "sole-wired"


def test_uplink_sole_wired_does_not_apply_to_any_other_port():
    assert _uplink(["eth0", "wlan0"], 2).name is None


def test_uplink_access_point_eth0_eth1_maps_port_1_to_eth0():
    outcome = _uplink(["eth0", "eth1", "wlan0"], 1)
    assert outcome.name == "eth0"
    assert outcome.via == "zero-based"
    assert _match(["eth0", "eth1", "wlan0"], 1).name == "eth0"


def test_uplink_gateway_eth_ports_are_zero_based():
    names = [f"eth{index}" for index in range(9)]
    outcome = _uplink(names, 2)
    assert outcome.name == "eth1"
    assert outcome.via == "zero-based"
    assert _uplink(names, 1).name == "eth0"
    assert _uplink(names, 9).name == "eth8"
    assert _match(names, 2).name == "eth1"


@pytest.mark.parametrize("name", ["Port 1", "GE1", "Gi1"])
def test_one_based_names_keep_the_unifi_index(name):
    outcome = _match([name], 1)
    assert outcome.name == name
    assert outcome.via == "template"
    assert _uplink([name], 1).name == name


def test_zero_based_lan_family_shifts_the_same_way():
    outcome = _uplink(["lan0", "lan1"], 2)
    assert outcome.name == "lan1"
    assert outcome.via == "zero-based"


def test_management_port_zero_is_not_unifi_port_1():
    assert _match(["mgmt0", "irb"], 1).name is None
    assert _uplink(["mgmt0"], 1).name is None


def test_zero_based_and_one_based_hits_are_ambiguous():
    outcome = _uplink(["eth0", "eth1", "Port 1"], 1)
    assert outcome.name is None
    assert outcome.via == "ambiguous"
    assert set(outcome.ambiguous_names) == {"eth0", "Port 1"}


def test_two_zero_based_families_are_ambiguous():
    outcome = _uplink(["eth0", "lan0"], 1)
    assert outcome.name is None
    assert outcome.via == "ambiguous"
    assert set(outcome.ambiguous_names) == {"eth0", "lan0"}


def test_model_template_beats_a_zero_based_family():
    pinned = _uplink(["eth0", "eth1", "Gi1/0/1"], 1, model=["Gi1/0/{port}"])
    assert pinned.name == "Gi1/0/1"
    forced = _uplink(["eth0", "eth1"], 1, model=["eth{port}"])
    assert forced.name == "eth1"


def test_uplink_virtual_type_does_not_count_as_a_wired_interface():
    outcome = _uplink(["eth0", "eth1"], 1, types=["1000base-t", "virtual"])
    assert outcome.name == "eth0"
    assert outcome.via == "zero-based"


def test_uplink_numeric_suffix_ambiguity_skips():
    outcome = _uplink(["SFP+ 49", "SFP28 49", "wlan49"], 49, sfp=None)
    assert outcome.name is None
    assert outcome.via == "ambiguous"
    assert set(outcome.ambiguous_names) == {"SFP+ 49", "SFP28 49"}


def test_uplink_model_template_overrides_numeric_suffix_and_sole_wired():
    pinned = _uplink(["SFP+ 49", "Gi1/0/49"], 49, model=["Gi1/0/{port}"])
    assert pinned.name == "Gi1/0/49"
    assert pinned.via == "exact"

    padded = _uplink(["Gi1/0/049", "SFP+ 49"], 49, model=["Gi1/0/{port}"])
    assert padded.name == "Gi1/0/049"
    assert padded.via == "template"

    missed = _uplink(["SFP+ 49"], 49, model=["Gi1/0/{port}"])
    assert missed.name is None
    assert missed.via == "none"

    # A name with no index still does not fall through to the single-interface
    # guess once a model template is set. eth0 is zero-based, so port 1 maps
    # there even when the pinned template does not name it.
    suppressed = _uplink(["uplink"], 1, model=["Gi1/0/{port}"])
    assert suppressed.name is None
    assert suppressed.via == "none"
    shifted = _uplink(["eth0", "wlan0"], 1, model=["Gi1/0/{port}"])
    assert shifted.name == "eth0"
    assert shifted.via == "zero-based"


def test_uplink_unknown_port_still_uses_the_only_wired_interface_when_a_model_is_pinned():
    outcome = _uplink(["eth0", "wlan0"], None, model=["Gi1/0/{port}"])
    assert outcome.name == "eth0"
    assert outcome.via == "sole-wired"


def test_uplink_template_beats_an_sfp_ordinal_on_port_1():
    outcome = _uplink(["Port 1", "SFP 1"], 1, sfp=None)
    assert outcome.name == "Port 1"
    assert outcome.via == "exact"
