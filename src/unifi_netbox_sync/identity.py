"""When a same-name NetBox device is the UniFi device being synced.

Name is the weakest join. It is accepted when the NetBox device looks like
the same chassis: manufacturer is Ubiquiti or was not recorded, and a serial
or ``unifi_mac`` that both sides have does not disagree. A recorded
manufacturer other than Ubiquiti is a different device that happens to share
a name.
"""

from __future__ import annotations

import re
from typing import Any

_UNIFI_VENDOR = re.compile(r"ubiquiti|ubnt", re.IGNORECASE)
_TOKEN = re.compile(r"[^a-z0-9]")


def _text(value: object) -> str:
    if isinstance(value, str):
        return value.strip()
    if value is None:
        return ""
    for attr in ("slug", "name", "display"):
        nested = getattr(value, attr, None)
        if isinstance(nested, str) and nested.strip():
            return nested.strip()
    return ""


def manufacturer_labels(device: Any) -> list[str]:
    """Manufacturer name and slug recorded on ``device``, if any.

    NetBox keeps the manufacturer on the device type. A string
    ``manufacturer`` attribute is accepted for callers that already flattened
    it. An empty list means the vendor was not loaded.
    """
    found: list[str] = []

    def add(value: object) -> None:
        text = _text(value)
        if text and text not in found:
            found.append(text)

    add(getattr(device, "manufacturer", None))
    device_type = getattr(device, "device_type", None)
    if device_type is not None and not isinstance(device_type, str):
        add(getattr(device_type, "manufacturer", None))
    return found


def is_unifi_vendor(device: Any) -> bool | None:
    """True for Ubiquiti, False for another recorded vendor, None if unknown.

    Unknown stays a name-match candidate so a device whose manufacturer was
    not included in the API response still matches the way it did before.
    """
    labels = manufacturer_labels(device)
    if not labels:
        return None
    if any(_UNIFI_VENDOR.search(label) for label in labels):
        return True
    return False


def allows_number_only_ports(device: Any) -> bool:
    """Trailing-number and structural port guesses apply to this device.

    Those guesses map a UniFi port index onto whatever interface ends in that
    number. That is safe for a Ubiquiti device and for a device with no
    manufacturer recorded. It is not safe for another vendor: a shared name
    must not cable UniFi ports onto that device's interfaces.
    """
    return is_unifi_vendor(device) is not False


def _norm(value: object) -> str:
    if value is None:
        return ""
    return _TOKEN.sub("", str(value).strip().lower())


def _custom_field(device: Any, name: str) -> str:
    raw = (getattr(device, "custom_fields", None) or {}).get(name) or ""
    return str(raw).strip()


def name_fallback_conflict(device: Any, *, mac: str, serial: str) -> str:
    """Why ``device`` must not be linked by name, or ``""`` when the name is enough.

    Serial and ``unifi_mac`` are checked only when both sides have a value.
    A missing serial does not reject a same-vendor name match.
    """
    if is_unifi_vendor(device) is False:
        labels = ", ".join(manufacturer_labels(device)) or "unknown"
        return f"manufacturer is {labels}, not Ubiquiti"
    theirs = _norm(getattr(device, "serial", None))
    ours = _norm(serial)
    if theirs and ours and theirs != ours:
        return "serial differs"
    stored = _norm(_custom_field(device, "unifi_mac"))
    wanted = _norm(mac)
    if stored and wanted and stored != wanted:
        return "unifi_mac differs"
    return ""
