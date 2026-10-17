"""Match a UniFi port index to a NetBox interface name.

UniFi reports ``sw_port`` as an integer port index. NetBox interfaces are
often named with a slot and module (``Gi1/0/4``, ``1/1/4``), a Juniper-style
name (``ge-0/0/4``), a zero-padded number (``Port 04``), or an SFP ordinal
(``SFP 1`` for port 25). Exact ``{port}`` templates cannot produce those.

Matching is deliberately ordered:

1. Model-specific templates, then the global templates, then the UniFi
   port's own name. Comparison ignores case and leading zeros in numbers,
   so ``Gi1/0/{port}`` hits ``Gi1/0/04``. The first candidate that hits
   exactly one interface wins, which keeps an exact ``Port 4`` match ahead
   of a structural guess.
2. If that model has no templates of its own, a structural parse of the
   interface list. A hit is used only when exactly one interface parses to
   this port. Two stack members both ending in ``/4`` are a skip, not a guess.
   Model templates suppress this guess: once an operator pins a scheme, a
   unique-but-wrong member must not win.

SFP names are only considered when the controller's port table says this
index is an SFP. Otherwise copper port 1 would cable to ``SFP 1``.

Infra-to-infra uplinks call :func:`match_uplink_port`, which uses that
order and then two fallbacks:

3. An interface whose trailing number equals the port index, for any
   prefix (``SFP+ 49``, ``SFP 49``, ``SFP28 49``, ``GE49``, ``eth12``).
   Wireless and virtual interfaces are ignored. More than one remaining
   hit is a skip, not a guess.
4. When the device has exactly one physical wired interface, uplink port
   1 — or an uplink whose local port UniFi did not report — lands there
   (an access point whose only wired interface is ``eth0``).

Preference is exact candidate name, then the looser template and
structural matches, then the trailing number, then the single interface.
``PORT_MODEL_TEMPLATES`` stays an override: a matching model template wins,
and a model that lists templates does not take the structural guess or
fallbacks 3 and 4 for a known port.

Uplinks also shift a name family that contains index 0 (``eth0``/``eth1``,
``lan0``/``lan1``, or any other prefix): UniFi port ``N`` selects index
``N-1``. Families that start at 1 (``Port 1``, ``GE1``, ``Gi1``) stay on
``N``. A zero-based hit and a different 1-based hit for the same port are
ambiguous. Management names (``mgmt0``, ``fxp0``) are not a port family.
Client cables do not use that shift or the single-interface fallback, so a
leftover client record whose only interface is the placeholder ``eth0`` is
not port 1.
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import dataclass

from .devicetype_library import normalize_model

# Longer names first so ``gigabitethernet`` wins over ``ge`` / ``eth``.
_FAMILY = (
    "hundredgigabitethernet|fortygigabitethernet|twentyfivegige|"
    "tengigabitethernet|fivegigabitethernet|twogigabitethernet|"
    "gigabitethernet|fastethernet|ethernet|"
    "mge|twe|xe|ge|et|fe|gi|te|fa|eth|tf|tw|fi|fo|hu"
)

_THREE = re.compile(
    rf"^(?:(?:{_FAMILY})-)?(?:{_FAMILY})?(\d+)/(\d+)/(\d+)$",
    re.IGNORECASE,
)
_TWO = re.compile(
    rf"^(?:{_FAMILY})?(\d+)/(\d+)$",
    re.IGNORECASE,
)
_LABELED = re.compile(
    r"^(?:port|gigabitethernet|ethernet|ge|gi|eth)\s*0*(\d+)$",
    re.IGNORECASE,
)
_PADDED_BARE = re.compile(r"^0+(\d+)$")
_SFP = re.compile(
    r"^(?:qsfp28|qsfp\+|qsfp|sfp28|sfp\+|sfp|uplink)\s*0*(\d+)$",
    re.IGNORECASE,
)
_SFP_TEMPLATE = re.compile(r"\{sfp")
_TRAILING_NUMBER = re.compile(r"(\d+)\s*$")
_FAMILY_INDEX = re.compile(r"^(.*?)(\d+)\s*$")
# Radios, VLANs, LAGs, bridges, loopbacks, and tunnels are not uplink ports.
# ``po``/``ae``/``br``/``lo``/``tun`` require a digit so ``Port 1`` stays eligible.
_NON_PHYSICAL_NAME = re.compile(
    r"^(?:wlan|wifi|wireless|radio|vlan|lag|bond|bridge|loopback|virtual|tunnel|null)(?:\d|\b)"
    r"|^(?:ae|po|br|lo|tun)\d",
    re.IGNORECASE,
)
_VIRTUAL_TYPES = frozenset({"virtual", "lag", "bridge"})
# A trailing 0 here is a management port, not UniFi port 1.
_NOT_A_PORT_PREFIX = frozenset({"mgmt", "management", "fxp", "me"})


@dataclass(frozen=True)
class PortMatch:
    """Outcome of matching one UniFi port against one switch's interfaces."""

    name: str | None
    via: str  # "exact", "template", "structural", "numeric-suffix", "sole-wired", "ambiguous", "none"
    names_tried: tuple[str, ...]
    ambiguous_names: tuple[str, ...] = ()


def coerce_port(value: object) -> int | None:
    """UniFi ``sw_port`` as an int, or None when it is not a port index."""
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, float) and value.is_integer():
        return int(value)
    if isinstance(value, str) and value.strip().isdigit():
        return int(value.strip())
    return None


def parse_model_templates(raw: str) -> dict[str, list[str]]:
    """Parse ``PORT_MODEL_TEMPLATES``.

    Rules are separated by ``|``. Inside a rule, templates are separated by
    ``;`` because a template itself may contain commas or spaces
    (``Port {port}``). The model key is folded the same way device-type
    lookup folds part numbers, so ``USW-24-PoE`` and ``USW24POE`` are one
    entry.
    """
    text = raw.strip()
    if not text:
        return {}
    table: dict[str, list[str]] = {}
    for rule in text.split("|"):
        rule = rule.strip()
        if not rule:
            continue
        if "=" not in rule:
            raise SystemExit(
                f"Invalid PORT_MODEL_TEMPLATES entry {rule!r}; "
                "expected 'model=template;template'"
            )
        model, _, templates_raw = rule.partition("=")
        model = model.strip()
        templates = [part.strip() for part in templates_raw.split(";") if part.strip()]
        if not model or not templates:
            raise SystemExit(
                f"Invalid PORT_MODEL_TEMPLATES entry {rule!r}; "
                "expected 'model=template;template'"
            )
        key = normalize_model(model)
        if not key:
            raise SystemExit(f"Invalid PORT_MODEL_TEMPLATES model {model!r}")
        if key in table:
            raise SystemExit(
                f"PORT_MODEL_TEMPLATES model {model!r} is specified more than once"
            )
        table[key] = templates
    return table


def templates_for_model(table: dict[str, list[str]], model: str | None) -> list[str]:
    if not model:
        return []
    return list(table.get(normalize_model(model), []))


def sfp_ordinal(ports: Sequence[object], port_number: int) -> int | None:
    """1-based SFP position of ``port_number``, or None when it is not an SFP.

    Ordinals follow port index order. On a 24-port switch whose SFP cages
    are indexes 25 and 26, port 25 is SFP 1. Copper ports return None so a
    name like ``SFP 1`` cannot be selected for them.
    """
    sfp_ports = [
        port
        for port in ports
        if "sfp" in str(getattr(port, "media", "") or "").lower()
    ]
    sfp_ports.sort(key=lambda port: int(getattr(port, "index")))
    for ordinal, port in enumerate(sfp_ports, start=1):
        if int(getattr(port, "index")) == port_number:
            return ordinal
    return None


def is_uplink_port(ports: Sequence[object], port_number: int) -> bool:
    return any(
        int(getattr(port, "index")) == port_number and bool(getattr(port, "is_uplink", False))
        for port in ports
    )


def numeric_key(name: str) -> str:
    """Case-fold and strip leading zeros from every number in ``name``.

    ``Gi1/0/04``, ``gi1/0/4`` and a template that rendered ``Gi1/0/4`` are
    the same key. The surrounding text is kept, so ``Gi1/0/4`` does not
    equal ``Gi2/0/4`` or ``4``.
    """
    return re.sub(r"\d+", lambda match: str(int(match.group())), name.strip().lower())


def format_template(template: str, port: int, sfp: int | None) -> str | None:
    """Render one template. SFP templates are skipped when this port is not an SFP."""
    if sfp is None and _SFP_TEMPLATE.search(template):
        return None
    values = {
        "port": port,
        "port2": f"{port:02d}",
        "port3": f"{port:03d}",
        "sfp": "" if sfp is None else sfp,
        "sfp2": "" if sfp is None else f"{sfp:02d}",
    }
    try:
        rendered = template.format(**values)
    except (IndexError, KeyError, ValueError):
        return None
    rendered = rendered.strip()
    return rendered or None


def candidate_names(
    model_templates: Sequence[str],
    global_templates: Sequence[str],
    port: int,
    sfp: int | None,
    extra: Sequence[str] = (),
) -> list[str]:
    """Names to try, model templates first, duplicates removed."""
    seen: set[str] = set()
    names: list[str] = []
    for template in list(model_templates) + list(global_templates):
        rendered = format_template(template, port, sfp)
        if not rendered:
            continue
        key = rendered.lower()
        if key in seen:
            continue
        seen.add(key)
        names.append(rendered)
    for name in extra:
        text = (name or "").strip()
        if not text or text.lower() in seen:
            continue
        seen.add(text.lower())
        names.append(text)
    return names


def model_candidate_count(
    model_templates: Sequence[str], port: int | None, sfp: int | None
) -> int:
    """How many leading ``candidate_names`` entries came from model templates."""
    if not model_templates or port is None:
        return 0
    return len(candidate_names(model_templates, [], port, sfp, ()))


def _copper_port(name: str) -> int | None:
    raw = name.strip()
    three = _THREE.fullmatch(raw)
    if three:
        return int(three.group(3))
    two = _TWO.fullmatch(raw)
    if two:
        return int(two.group(2))
    labeled = _LABELED.fullmatch(raw)
    if labeled:
        return int(labeled.group(1))
    padded = _PADDED_BARE.fullmatch(raw)
    if padded:
        return int(padded.group(1))
    return None


def _sfp_number(name: str) -> int | None:
    match = _SFP.fullmatch(name.strip())
    if not match:
        return None
    return int(match.group(1))


def _family_index(name: str) -> tuple[str, int] | None:
    """Prefix and trailing index of a wired interface name.

    ``eth0`` is ``("eth", 0)``, ``lan1`` is ``("lan", 1)``, ``Port 4`` is
    ``("port", 4)``, and ``Gi1/0/4`` is ``("gi1/0/", 4)``. Wireless and
    virtual names are not a family: callers skip those.
    """
    match = _FAMILY_INDEX.match((name or "").strip())
    if not match:
        return None
    return match.group(1).strip().lower(), int(match.group(2))


def zero_based_prefixes(interface_names: Sequence[str]) -> set[str]:
    """Prefixes whose wired names include index 0.

    ``eth0`` makes every ``eth*`` name zero-based. ``Port 1`` does not,
    because that family never contains ``Port 0``. ``wlan0`` is ignored.
    """
    numbers: dict[str, set[int]] = {}
    for name in interface_names:
        if is_wireless_or_virtual(name):
            continue
        parsed = _family_index(name)
        if parsed is None:
            continue
        prefix, number = parsed
        numbers.setdefault(prefix, set()).add(number)
    return {
        prefix
        for prefix, found in numbers.items()
        if 0 in found and prefix not in _NOT_A_PORT_PREFIX
    }


def zero_based_hits(
    interface_names: Sequence[str], port: int | None, prefixes: set[str]
) -> list[str]:
    """Wired interfaces at index ``port - 1`` in a zero-based family."""
    if port is None or port < 1 or not prefixes:
        return []
    target = port - 1
    hits: list[str] = []
    for name in interface_names:
        if is_wireless_or_virtual(name):
            continue
        parsed = _family_index(name)
        if parsed is None:
            continue
        prefix, number = parsed
        if prefix in prefixes and number == target:
            hits.append(name)
    return hits


def _shifted_name(name: str, prefixes: set[str], port: int | None) -> bool:
    """True when ``name`` is the 1-based index of a zero-based family.

    ``eth1`` is shifted for UniFi port 1 once ``eth0`` exists. ``Port 1``
    is not, and neither is ``eth1`` when the family has no index 0.
    """
    if port is None or port < 1 or not prefixes:
        return False
    parsed = _family_index(name)
    if parsed is None:
        return False
    prefix, number = parsed
    return prefix in prefixes and number != port - 1


def match_port(
    interface_names: Sequence[str],
    candidates: Sequence[str],
    port: int | None,
    sfp_index: int | None,
    *,
    allow_structural: bool,
    pinned_count: int = 0,
    zero_based: bool = False,
) -> PortMatch:
    """Pick the interface name for this UniFi port, or say why not.

    Client cables call this with ``zero_based`` false. Templates and
    structural names then behave as they did before the uplink shift, so a
    device whose only interface is the placeholder ``eth0`` does not become
    port 1, and there is no single-interface fallback.

    Uplink matching passes ``zero_based`` true. ``pinned_count`` is how many
    leading ``candidates`` came from ``PORT_MODEL_TEMPLATES``. Those are
    tried first and win even when the device's own names are zero-based.
    Every other family that contains index 0 maps port ``N`` to index
    ``N-1``; families that start at 1 keep ``N``.
    """
    tried = tuple(candidates)
    grouped: dict[str, list[str]] = {}
    for name in interface_names:
        grouped.setdefault(numeric_key(name), []).append(name)

    def first_unique(
        pool: Sequence[str], prefixes: set[str], *, shift: bool
    ) -> PortMatch | None:
        for candidate in pool:
            hits = list(grouped.get(numeric_key(candidate), []))
            if shift:
                hits = [hit for hit in hits if not _shifted_name(hit, prefixes, port)]
            if len(hits) == 1:
                return PortMatch(name=hits[0], via="template", names_tried=tried)
            if len(hits) > 1:
                return PortMatch(
                    name=None,
                    via="ambiguous",
                    names_tried=tried,
                    ambiguous_names=tuple(hits),
                )
        return None

    pinned = max(0, pinned_count)
    prefixes: set[str] = set()
    if zero_based:
        if pinned:
            # An explicit model template beats the zero-based correction.
            chosen = first_unique(candidates[:pinned], set(), shift=False)
            if chosen is not None:
                return chosen

        prefixes = zero_based_prefixes(interface_names) if port is not None else set()
        zero_hits = zero_based_hits(interface_names, port, prefixes)
        templated = first_unique(candidates, prefixes, shift=True)
        if templated is not None and templated.via == "ambiguous":
            return templated
        template_name = templated.name if templated is not None else None
        if template_name and zero_hits:
            distinct = list(dict.fromkeys([template_name, *zero_hits]))
            if len(distinct) > 1:
                return PortMatch(
                    name=None,
                    via="ambiguous",
                    names_tried=tried,
                    ambiguous_names=tuple(distinct),
                )
            return templated
        if template_name:
            return templated
        if len(zero_hits) == 1:
            return PortMatch(name=zero_hits[0], via="zero-based", names_tried=tried)
        if len(zero_hits) > 1:
            return PortMatch(
                name=None,
                via="ambiguous",
                names_tried=tried,
                ambiguous_names=tuple(zero_hits),
            )
    else:
        templated = first_unique(candidates, set(), shift=False)
        if templated is not None:
            return templated

    if port is None or not allow_structural:
        return PortMatch(name=None, via="none", names_tried=tried)

    structural: list[str] = []
    for name in interface_names:
        parsed = _family_index(name)
        if parsed is not None and parsed[0] in prefixes:
            # Already decided above: this family uses N-1, and nothing matched.
            continue
        copper = _copper_port(name)
        sfp_number = _sfp_number(name)
        sfp_hit = sfp_index is not None and sfp_number is not None and (
            sfp_number == sfp_index or sfp_number == port
        )
        if copper == port or sfp_hit:
            structural.append(name)
    if len(structural) == 1:
        return PortMatch(name=structural[0], via="structural", names_tried=tried)
    if len(structural) > 1:
        return PortMatch(
            name=None,
            via="ambiguous",
            names_tried=tried,
            ambiguous_names=tuple(structural),
        )
    return PortMatch(name=None, via="none", names_tried=tried)


def trailing_number(name: str) -> int | None:
    """Integer at the end of an interface name, or None when there is none.

    ``SFP+ 49``, ``SFP28 49``, ``GE49`` and ``eth0`` yield 49, 49, 49 and 0.
    Leading zeros are not significant: ``Port 04`` is 4.
    """
    match = _TRAILING_NUMBER.search((name or "").strip())
    if not match:
        return None
    return int(match.group(1))


def is_wireless_or_virtual(name: str, iface_type: str = "") -> bool:
    """True for radios, virtual interfaces, subinterfaces, and management ports.

    Uplink fallbacks ignore these so ``wlan1`` is not port 1, a bridge or
    VLAN is not the device's only wired interface, and ``mgmt0`` is not
    UniFi port 1. ``eth0`` and ``SFP+ 49`` are physical.
    """
    kind = (iface_type or "").strip().lower()
    if kind in _VIRTUAL_TYPES or kind.startswith("ieee802.11"):
        return True
    text = (name or "").strip()
    if not text or "." in text:
        return True
    if _NON_PHYSICAL_NAME.match(text) is not None:
        return True
    parsed = _family_index(text)
    return parsed is not None and parsed[0] in _NOT_A_PORT_PREFIX


def _physical_names(
    interface_names: Sequence[str],
    interface_types: Sequence[str] | None,
) -> list[str]:
    physical: list[str] = []
    for index, name in enumerate(interface_names):
        iface_type = ""
        if interface_types is not None and index < len(interface_types):
            iface_type = interface_types[index] or ""
        if not is_wireless_or_virtual(name, iface_type):
            physical.append(name)
    return physical


def _exact_candidate_match(
    interface_names: Sequence[str],
    candidates: Sequence[str],
    skip: object = None,
) -> PortMatch | None:
    """First candidate that equals one interface, case-insensitively.

    Leading zeros stay significant, so ``Port 49`` hits ``Port 49`` and not
    ``Port 049``. A candidate that equals more than one interface is ambiguous.
    ``skip`` drops shifted zero-based names such as ``eth1`` for port 1.
    Returns None when no candidate equals any interface.
    """
    grouped: dict[str, list[str]] = {}
    for name in interface_names:
        grouped.setdefault(name.strip().lower(), []).append(name)
    tried = tuple(candidates)
    for candidate in candidates:
        hits = list(grouped.get(candidate.strip().lower(), []))
        if skip is not None:
            hits = [hit for hit in hits if not skip(hit)]  # type: ignore[operator]
        if len(hits) == 1:
            return PortMatch(name=hits[0], via="exact", names_tried=tried)
        if len(hits) > 1:
            return PortMatch(
                name=None,
                via="ambiguous",
                names_tried=tried,
                ambiguous_names=tuple(hits),
            )
    return None


def match_uplink_port(
    interface_names: Sequence[str],
    candidates: Sequence[str],
    port: int | None,
    sfp_index: int | None,
    *,
    allow_fallback: bool,
    interface_types: Sequence[str] | None = None,
    pinned_count: int = 0,
) -> PortMatch:
    """Resolve one end of an infra-to-infra uplink.

    Order, first unique hit wins:

    1. Exact model-template name, when ``PORT_MODEL_TEMPLATES`` rendered
       one. That override is not rewritten for zero-based families.
    2. Exact candidate name, then :func:`match_port` (templates, zero-based
       families, structural names). A family that contains index 0 maps
       port ``N`` to index ``N-1`` (``eth1`` is UniFi port 2). ``Port 1``,
       ``GE1`` and ``Gi1`` stay 1-based. Two different hits are ambiguous.
    3. If the model has no templates of its own, an interface whose trailing
       number equals ``port``, skipping families already treated as
       zero-based. Any other prefix qualifies (``SFP+ ``, ``SFP28 ``).
    4. If ``port`` is 1, or UniFi did not report a local port, and exactly
       one physical wired interface remains, that interface.

    Wireless and virtual interfaces are ignored. A model that lists
    templates skips steps 3 and 4 for a known port. Step 4 still applies
    when the local port itself is unknown.
    """
    tried = tuple(candidates)
    physical = _physical_names(interface_names, interface_types)
    pinned = max(0, pinned_count)

    if port is not None and pinned:
        pinned_hit = _exact_candidate_match(physical, list(candidates)[:pinned])
        if pinned_hit is not None:
            return pinned_hit

    prefixes = zero_based_prefixes(physical) if port is not None else set()
    zero_hits = zero_based_hits(physical, port, prefixes) if port is not None else []

    if port is not None:
        exact = _exact_candidate_match(
            physical,
            candidates,
            skip=lambda name: _shifted_name(name, prefixes, port),
        )
        if exact is not None and exact.via == "ambiguous":
            return exact
        if exact is not None and exact.name:
            distinct = list(dict.fromkeys([exact.name, *zero_hits]))
            if len(distinct) == 1:
                return exact
            return PortMatch(
                name=None,
                via="ambiguous",
                names_tried=tried,
                ambiguous_names=tuple(distinct),
            )

        templated = match_port(
            physical,
            candidates,
            port,
            sfp_index,
            allow_structural=allow_fallback,
            pinned_count=pinned,
            zero_based=True,
        )
        if templated.name is not None or templated.via == "ambiguous" or not allow_fallback:
            return templated
        suffix: list[str] = []
        for name in physical:
            parsed = _family_index(name)
            if parsed is not None and parsed[0] in prefixes:
                continue
            if trailing_number(name) == port:
                suffix.append(name)
        if len(suffix) == 1:
            return PortMatch(name=suffix[0], via="numeric-suffix", names_tried=tried)
        if len(suffix) > 1:
            return PortMatch(
                name=None,
                via="ambiguous",
                names_tried=tried,
                ambiguous_names=tuple(suffix),
            )
        if port != 1:
            return templated

    if (port is None or port == 1) and len(physical) == 1:
        return PortMatch(name=physical[0], via="sole-wired", names_tried=tried)
    return PortMatch(name=None, via="none", names_tried=tried)
