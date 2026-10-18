"""MAC addresses a single NetBox device may own.

``unifi_mac`` stays a single exact address. Extra addresses live in
``unifi_macs`` (commas, semicolons, or whitespace) or on the device's
interfaces: the interface ``mac_address`` field, and NetBox MAC address
objects assigned to those interfaces.
"""

from __future__ import annotations

import re
from typing import Any

# Primary join. Exact string match, one address. Do not overload this with a list.
PRIMARY_MAC_FIELD = "unifi_mac"
# Additional addresses for the same device. The primary address stays in unifi_mac.
EXTRA_MACS_FIELD = "unifi_macs"

_COLON = re.compile(r"^[0-9a-f]{2}(?::[0-9a-f]{2}){5}$")
_HEX12 = re.compile(r"^[0-9a-f]{12}$")
_SPLIT = re.compile(r"[,;\s]+")

# How an address was claimed. Explicit lists outrank an address observed on a port.
VIA_FIELD = "unifi_macs"
VIA_INTERFACE = "interface MAC"


def normalize_mac(value: object) -> str:
    """Lowercase colon form, or ``""`` when ``value`` is not a MAC address.

    Accepts ``aa:bb:cc:dd:ee:ff``, dashes, and a 12-digit hex string.
    """
    if value is None:
        return ""
    text = str(value).strip().lower().replace("-", ":")
    if not text:
        return ""
    if _COLON.fullmatch(text):
        return text
    compact = re.sub(r"[^0-9a-f]", "", text)
    if _HEX12.fullmatch(compact):
        return ":".join(compact[i : i + 2] for i in range(0, 12, 2))
    return ""


def mac_text(value: object) -> str:
    """MAC text from a string, a dict, or an object with ``mac_address``."""
    if value is None:
        return ""
    if isinstance(value, str):
        return normalize_mac(value)
    if isinstance(value, dict):
        for key in ("mac_address", "mac", "address"):
            if value.get(key):
                return mac_text(value.get(key))
        return ""
    nested = getattr(value, "mac_address", None)
    if nested is not None and nested is not value:
        return mac_text(nested)
    return ""


def macs_from_text(value: object) -> list[str]:
    """MAC addresses in a custom-field value, in order, duplicates removed.

    Separators are commas, semicolons, and whitespace. One address with no
    separator is that address, which is what ``unifi_mac`` already stores.
    """
    if value is None:
        return []
    text = str(value).strip()
    if not text:
        return []
    found: list[str] = []
    for part in _SPLIT.split(text):
        mac = normalize_mac(part)
        if mac and mac not in found:
            found.append(mac)
    return found


def declared_extra_macs(custom_fields: dict | None) -> list[str]:
    """Addresses declared besides the single primary ``unifi_mac``.

    A ``unifi_mac`` value that is exactly one address is the primary identity
    and is not returned here. A value that lists more than one address is
    treated as additional ownership so an existing comma-separated field still
    matches, without changing the exact one-address comparison.
    """
    fields = custom_fields or {}
    primary = macs_from_text(fields.get(PRIMARY_MAC_FIELD))
    found: list[str] = []
    if len(primary) > 1:
        found.extend(primary)
    for mac in macs_from_text(fields.get(EXTRA_MACS_FIELD)):
        if mac not in found:
            found.append(mac)
    return found


def interface_macs(interface: object) -> list[str]:
    """Hardware addresses recorded on one interface.

    ``mac_addresses`` is read only when it is a list or tuple. A count or a
    related manager is left to the MAC address object query.
    """
    found: list[str] = []

    def add(value: object) -> None:
        mac = mac_text(value)
        if mac and mac not in found:
            found.append(mac)

    add(getattr(interface, "mac_address", None))
    add(getattr(interface, "primary_mac_address", None))
    listed = getattr(interface, "mac_addresses", None)
    if isinstance(listed, (list, tuple)):
        for item in listed:
            add(item)
    return found


def _device_id(value: object) -> Any:
    if value is None:
        return None
    if isinstance(value, dict):
        return value.get("id")
    return getattr(value, "id", None)


def _nested(value: object, name: str) -> object:
    if value is None:
        return None
    if isinstance(value, dict):
        return value.get(name)
    return getattr(value, name, None)


def mac_object_claim(obj: object) -> tuple[str, Any] | None:
    """``(mac, device id)`` when ``obj`` is a MAC address assigned to an interface.

    Virtual-machine interfaces are skipped: they are not a dcim device this
    sync can claim.
    """
    if obj is None or isinstance(obj, str):
        return None
    assigned_type = _nested(obj, "assigned_object_type")
    if assigned_type:
        text = str(assigned_type)
        if text.startswith("virtualization."):
            return None
        if "interface" not in text:
            return None
    mac = mac_text(obj)
    if not mac:
        return None
    assigned = _nested(obj, "assigned_object")
    device = _nested(assigned, "device") if assigned is not None else None
    dev_id = _device_id(device)
    if dev_id is None:
        return None
    return mac, dev_id


def index_owned_macs(
    devices: list[Any],
    interfaces: list[Any] | None = None,
    mac_objects: list[Any] | None = None,
) -> tuple[dict[str, tuple[Any, str]], dict[str, list[str]]]:
    """Map a normalized MAC to ``(device, via)``.

    Explicit ``unifi_macs`` (and a multi-value ``unifi_mac``) is claimed
    before interface addresses, so an operator's list wins when the two
    disagree. The first device in ``devices`` wins when two devices list the
    same address. ``conflicts`` maps a MAC to the names of the devices that
    were not chosen.

    A device is resolved from an interface or MAC object only when its id is
    in ``devices``. Callers that need a device the list does not contain have
    to add it first.
    """
    by_id: dict[Any, Any] = {}
    ordered: list[Any] = []
    for device in devices:
        dev_id = _device_id(device)
        if dev_id is not None and dev_id in by_id:
            continue
        if dev_id is not None:
            by_id[dev_id] = device
        ordered.append(device)

    index: dict[str, tuple[Any, str]] = {}
    conflicts: dict[str, list[str]] = {}

    def claim(mac: str, device: Any, via: str) -> None:
        if not mac or device is None:
            return
        current = index.get(mac)
        if current is None:
            index[mac] = (device, via)
            return
        current_device, _via = current
        current_id = _device_id(current_device)
        new_id = _device_id(device)
        if current_device is device or (current_id is not None and current_id == new_id):
            return
        name = str(getattr(device, "name", "") or new_id or "")
        bucket = conflicts.setdefault(mac, [])
        if name and name not in bucket:
            bucket.append(name)

    for device in ordered:
        fields = getattr(device, "custom_fields", None)
        if not isinstance(fields, dict):
            fields = {}
        for mac in declared_extra_macs(fields):
            claim(mac, device, VIA_FIELD)

    for iface in interfaces or []:
        device = by_id.get(_device_id(_nested(iface, "device")))
        if device is None:
            continue
        for mac in interface_macs(iface):
            claim(mac, device, VIA_INTERFACE)

    for obj in mac_objects or []:
        parsed = mac_object_claim(obj)
        if parsed is None:
            continue
        mac, dev_id = parsed
        device = by_id.get(dev_id)
        if device is None:
            continue
        claim(mac, device, VIA_INTERFACE)

    return index, conflicts
