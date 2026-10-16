"""UniFi controller model codes -> product SKUs, and power-device detection.

The controller's ``model`` field is a short code (``US24``, ``U7PG2``,
``US24PRO2``). devicetype-library YAML is indexed by the product SKU
(``US-24``, ``UAP-AC-Pro``, ``USW-Pro-24``). Normalizing punctuation catches
the cases that only differ by dashes. Everything else is listed here.

Codes and SKUs are Ubiquiti product identifiers, not site or customer data.
"""

from __future__ import annotations


def _normalize(value: str) -> str:
    # Local import: devicetype_library imports this module while loading.
    from .devicetype_library import normalize_model

    return normalize_model(value)

# Controller code (normalized) -> SKUs to try, most specific library part
# number first. A code that already normalizes onto a part number still works
# without an entry; these are the ones that do not.
_MODEL_ALIASES: dict[str, tuple[str, ...]] = {
    # Switches. US24 normalizes onto US-24, but older inventories used USW-24-G1.
    "US24": ("US-24", "USW-24-G1"),
    "US48": ("US-48", "US-48-G1"),
    "US24P250": ("US-24-250W",),
    "US24P500": ("US-24-500W",),
    "US48P500": ("US-48-500W",),
    "US48P750": ("US-48-750W",),
    "US16P150": ("US-16-150W",),
    "US8": ("US-8",),
    "US8P60": ("US-8-60W",),
    "US8P150": ("US-8-150W",),
    "S216150": ("US-16-150W",),
    "S224250": ("US-24-250W",),
    "S224500": ("US-24-500W",),
    "S248500": ("US-48-500W",),
    "S248750": ("US-48-750W",),
    "S28150": ("US-8-150W",),
    "US24PRO": ("USW-Pro-24-PoE",),
    "US24PRO2": ("USW-Pro-24",),
    "US48PRO": ("USW-Pro-48-PoE",),
    "US48PRO2": ("USW-Pro-48",),
    "USWPRO24": ("USW-Pro-24",),
    "USWPRO48": ("USW-Pro-48",),
    "USWPRO24POE": ("USW-Pro-24-PoE",),
    "USWPRO48POE": ("USW-Pro-48-PoE",),
    "USL24": ("USW-24", "USW-24-G2"),
    "USL48": ("USW-48", "USW-48-G2"),
    "USL24P": ("USW-24-PoE",),
    "USL48P": ("USW-48-PoE",),
    "USL16P": ("USW-16-PoE",),
    "USL16LP": ("USW-Lite-16-PoE",),
    "USL8LP": ("USW-Lite-8-PoE",),
    "USL8A": ("USW-Aggregation",),
    "US24PL2": ("US-L2-24-PoE", "USW-24-PoE"),
    "US48PL2": ("US-L2-48-PoE", "USW-48-PoE"),
    "USMINI": ("USW-Flex-Mini",),
    "USF5P": ("USW-Flex",),
    "USFXG": ("USW-Flex-XG",),
    "USAGGPRO": ("USW-Pro-Aggregation",),
    "USXG": ("US-16-XG",),
    "US6XG150": ("US-XG-6POE",),
    "US68P": ("USW-Enterprise-8-PoE",),
    "US624P": ("USW-Enterprise-24-PoE",),
    "US648P": ("USW-Enterprise-48-PoE",),
    "USC8P450": ("USW-Industrial",),
    "USPM16": ("USW-Pro-Max-16",),
    "USPM16P": ("USW-Pro-Max-16-PoE",),
    "USPM24": ("USW-Pro-Max-24",),
    "USPM24P": ("USW-Pro-Max-24-PoE",),
    "USPM48": ("USW-Pro-Max-48",),
    "USPM48P": ("USW-Pro-Max-48-PoE",),
    "USWPROMAX16": ("USW-Pro-Max-16",),
    "USWPROMAX16POE": ("USW-Pro-Max-16-PoE",),
    "USWPROMAX24": ("USW-Pro-Max-24",),
    "USWPROMAX24POE": ("USW-Pro-Max-24-PoE",),
    "USWPROMAX48": ("USW-Pro-Max-48",),
    "USWPROMAX48POE": ("USW-Pro-Max-48-PoE",),
    # Access points. U7PG2 is the AC Pro, not a Wi-Fi 7 AP.
    "U7PG2": ("UAP-AC-Pro", "UAP-AC-PRO"),
    "U7P": ("UAP-AC-Pro", "UAP-AC-PRO"),
    "U7LR": ("UAP-AC-LR",),
    "U7LT": ("UAP-AC-Lite",),
    "U7HD": ("UAP-AC-HD",),
    "U7IW": ("UAP-AC-IW",),
    "U7MSH": ("UAP-AC-M",),
    "U7MP": ("UAP-AC-M-Pro",),
    "U7NHD": ("UAP-nanoHD",),
    "U7PRO": ("U7-Pro",),
    "UAL6": ("U6-Lite",),
    "UALR6": ("U6-LR",),
    "UALR6V2": ("U6-LR",),
    "UALR6V3": ("U6-LR",),
    "U6ENT": ("U6-Enterprise",),
    "U6EXT": ("U6-Extender",),
    "U6IW": ("U6-IW",),
    "U6M": ("U6-Mesh",),
    "UAP6": ("U6-LR",),
    "UAP6MP": ("U6-Pro",),
    "UFLHD": ("UAP-FlexHD",),
    "UHDIW": ("UAP-IW-HD",),
    # Gateways
    "UDMPRO": ("UDM-Pro",),
    "UDMPROSE": ("UDM-SE",),
    "UDM": ("UDM",),
    "UDR": ("UDR",),
    "UXGPRO": ("UXG-Pro",),
    "UGW3": ("USG",),
    "UGW4": ("USG-PRO-4",),
    # Power. USP-RPS normalizes directly; USP-PDU-Pro does not (USPPDUP vs USPPDUPRO).
    "USPPDUP": ("USP-PDU-Pro",),
    "USPPDUHD": ("USP-PDU-HD",),
    "USPRPS": ("USP-RPS",),
    "USPRPSP": ("USP-RPS-Pro",),
    "UP1": ("USP-Plug",),
    "UP6": ("USP-Strip",),
    "USPPLUG": ("USP-Plug",),
    "USPSTRIP": ("USP-Strip",),
    "UPS2U": ("UPS-2U-US", "UPS-2U-EU"),
    "UPS2UUS": ("UPS-2U-US",),
    "UPS2UEU": ("UPS-2U-EU",),
}

# Controller codes that are PDUs, RPS units, smart plugs, or UPS hardware even
# when the controller types them as "usw" and sends no outlet_table.
_POWER_CODES = {
    "USPPDUP",
    "USPPDUHD",
    "USPRPS",
    "USPRPSP",
    "UP1",
    "UP6",
    "USPPLUG",
    "USPSTRIP",
    "UPS2U",
    "UPS2UUS",
    "UPS2UEU",
}

_GATEWAY_TYPES = {"ugw", "udm", "uxg"}
_POWER_TYPES = {"usp", "ups", "pdu"}

# AC outlets the controller under-reports. UP6 / USP-Strip is six switched
# outlets; some firmware sends one outlet_table row (or none), and the
# generated type would otherwise keep the single-outlet fallback.
_KNOWN_OUTLET_COUNTS = {
    "UP6": 6,
    "USPSTRIP": 6,
}


def candidate_keys(model_code: str) -> list[str]:
    """Normalized lookup keys for a controller model code, most specific first."""
    normalized = _normalize(model_code)
    if not normalized:
        return []
    keys = [normalized]
    for sku in _MODEL_ALIASES.get(normalized, ()):
        folded = _normalize(sku)
        if folded and folded not in keys:
            keys.append(folded)
    return keys


def _is_gateway_code(code: str) -> bool:
    """True for UXG, UGW, and UDM model codes.

    ``UDMB`` is the BeaconHD AP, not a Dream Machine. ``UDR`` (Dream Router)
    does not share those prefixes; its controller type is ``udm``, and the
    code is listed so a missing type still classifies it as a gateway.
    """
    if not code or code.startswith("UDMB"):
        return False
    return code == "UDR" or code.startswith(("UXG", "UGW", "UDM"))


def is_gateway_device(
    model: str,
    device_type: str = "",
    shortname: str = "",
) -> bool:
    """True for UXG, UGW, and UDM gateways, including UXG-Pro.

    Gateways report an ``outlet_table`` (UXG-Pro sends a single row). That
    table must not classify them as PDUs.
    """
    dtype = (device_type or "").strip().lower()
    if dtype in _GATEWAY_TYPES:
        return True
    return any(_is_gateway_code(token) for token in (_normalize(model), _normalize(shortname)))


def known_outlet_count(model: str, shortname: str = "") -> int:
    """Physical AC outlets when the controller sends a short ``outlet_table``.

    Zero means "trust the table". UP6 / USP-Strip is six.
    """
    for token in (_normalize(model), _normalize(shortname)):
        count = _KNOWN_OUTLET_COUNTS.get(token)
        if count:
            return count
    return 0


def is_power_device(
    model: str,
    device_type: str = "",
    shortname: str = "",
    outlet_count: int = 0,
) -> bool:
    """True for USP PDUs, USP-RPS, and USW-DA / UPS hardware.

    UniFi reports several of these with ``type: usw``. An ``outlet_table``
    (toggleable outlets) is decisive for those. It is not decisive for a
    gateway: UXG-Pro and other UXG/UGW/UDM devices report a row too.
    ``USWDA*`` is the UPS-tower family (shortname ``UPS23`` and similar),
    not a switch.
    """
    if is_gateway_device(model, device_type, shortname):
        return False
    if outlet_count > 0:
        return True
    dtype = (device_type or "").strip().lower()
    if dtype in _POWER_TYPES:
        return True
    code = _normalize(model)
    short = _normalize(shortname)
    if code in _POWER_CODES or short in _POWER_CODES:
        return True
    if code.startswith("USWDA") or short.startswith("USWDA"):
        return True
    if "PDU" in code or code.startswith("USPRPS") or code.startswith("UPS"):
        return True
    if short.startswith("UPS") or "PDU" in short:
        return True
    return False


def device_is_power(device: object) -> bool:
    return is_power_device(
        getattr(device, "model", "") or "",
        getattr(device, "device_type", "") or "",
        getattr(device, "shortname", "") or "",
        len(getattr(device, "outlets", None) or []),
    )


def device_kind(device: object) -> str:
    """``switch``, ``ap``, ``gateway``, ``power``, or ``other``."""
    if is_gateway_device(
        getattr(device, "model", "") or "",
        getattr(device, "device_type", "") or "",
        getattr(device, "shortname", "") or "",
    ):
        return "gateway"
    if device_is_power(device):
        return "power"
    dtype = (getattr(device, "device_type", "") or "").strip().lower()
    if dtype == "uap":
        return "ap"
    if dtype == "usw":
        return "switch"
    return "other"
