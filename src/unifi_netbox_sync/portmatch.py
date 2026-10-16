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


@dataclass(frozen=True)
class PortMatch:
    """Outcome of matching one UniFi port against one switch's interfaces."""

    name: str | None
    via: str  # "template", "structural", "ambiguous", "none"
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
