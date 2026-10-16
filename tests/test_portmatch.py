"""Port-name matching: templates, slot/module, Juniper, zero-pad, SFP."""

from __future__ import annotations

import pytest

from unifi_netbox_sync.models import UnifiPort
from unifi_netbox_sync.portmatch import (
    candidate_names,
    match_port,
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


def _match(names: list[str], port: int, sfp: int | None = None, model: list[str] | None = None, extra: list[str] | None = None):
    return match_port(
        names,
        _candidates(port, sfp, model, extra),
        port,
        sfp,
        allow_structural=not model,
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
