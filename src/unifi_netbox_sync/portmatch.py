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
``PORT_MODEL_TEMPLATES`` stays an override: a model that lists templates
does not take the structural guess or fallbacks 3 and 4 for a known port.
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
# Radios, VLANs, LAGs, bridges, loopbacks, and tunnels are not uplink ports.
# ``po``/``ae``/``br``/``lo``/``tun`` require a digit so ``Port 1`` stays eligible.
_NON_PHYSICAL_NAME = re.compile(
    r"^(?:wlan|wifi|wireless|radio|vlan|lag|bond|bridge|loopback|virtual|tunnel|null)(?:\d|\b)"
    r"|^(?:ae|po|br|lo|tun)\d",
    re.IGNORECASE,
)
_VIRTUAL_TYPES = frozenset({"virtual", "lag", "bridge"})


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


def match_port(
    interface_names: Sequence[str],
    candidates: Sequence[str],
    port: int | None,
    sfp_index: int | None,
    *,
    allow_structural: bool,
) -> PortMatch:
    """Pick the interface name for this UniFi port, or say why not."""
    tried = tuple(candidates)
    grouped: dict[str, list[str]] = {}
    for name in interface_names:
        grouped.setdefault(numeric_key(name), []).append(name)

    for candidate in candidates:
        hits = grouped.get(numeric_key(candidate), [])
        if len(hits) == 1:
            return PortMatch(name=hits[0], via="template", names_tried=tried)
        if len(hits) > 1:
            return PortMatch(
                name=None,
                via="ambiguous",
                names_tried=tried,
                ambiguous_names=tuple(hits),
            )

    if port is None or not allow_structural:
        return PortMatch(name=None, via="none", names_tried=tried)

    structural: list[str] = []
    for name in interface_names:
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
    """True for radios, virtual interfaces, and subinterfaces.

    Uplink fallbacks ignore these so ``wlan1`` is not port 1 and a bridge
    or VLAN is not the device's only wired interface. ``eth0`` and
    ``SFP+ 49`` are physical.
    """
    kind = (iface_type or "").strip().lower()
    if kind in _VIRTUAL_TYPES or kind.startswith("ieee802.11"):
        return True
    text = (name or "").strip()
    if not text or "." in text:
        return True
    return _NON_PHYSICAL_NAME.match(text) is not None


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


def _exact_candidate_match(interface_names: Sequence[str], candidates: Sequence[str]) -> PortMatch | None:
    """First candidate that equals one interface, case-insensitively.

    Leading zeros stay significant, so ``Port 49`` hits ``Port 49`` and not
    ``Port 049``. A candidate that equals more than one interface is ambiguous.
    Returns None when no candidate equals any interface.
    """
    grouped: dict[str, list[str]] = {}
    for name in interface_names:
        grouped.setdefault(name.strip().lower(), []).append(name)
    tried = tuple(candidates)
    for candidate in candidates:
        hits = grouped.get(candidate.strip().lower(), [])
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
) -> PortMatch:
    """Resolve one end of an infra-to-infra uplink.

    Order, first unique hit wins:

    1. Exact candidate name (model templates, then global templates, then
       the UniFi port name). Comparison ignores case and keeps leading zeros.
    2. The same looser template match and structural parse client cabling
       uses (:func:`match_port`).
    3. If the model has no templates of its own, an interface whose trailing
       number equals ``port``. Any prefix qualifies (``SFP+ ``, ``SFP28 ``,
       ``Port ``, ``GE``, ``Gi``, ``eth``). Two hits are ambiguous.
    4. If ``port`` is 1, or UniFi did not report a local port, and exactly
       one physical wired interface remains, that interface.

    Wireless and virtual interfaces are ignored in every step. A model
    listed in ``PORT_MODEL_TEMPLATES`` sets ``allow_fallback`` false and
    therefore skips steps 3 and 4 for a known port; step 4 still applies
    when the local port itself is unknown, because a template cannot be
    rendered without a port index.
    """
    tried = tuple(candidates)
    physical = _physical_names(interface_names, interface_types)

    if port is not None:
        exact = _exact_candidate_match(physical, candidates)
        if exact is not None:
            return exact
        templated = match_port(
            physical,
            candidates,
            port,
            sfp_index,
            allow_structural=allow_fallback,
        )
        if templated.name is not None or templated.via == "ambiguous" or not allow_fallback:
            return templated
        suffix = [name for name in physical if trailing_number(name) == port]
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
