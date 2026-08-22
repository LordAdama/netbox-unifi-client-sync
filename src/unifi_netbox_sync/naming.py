from __future__ import annotations

import re

# NetBox's dcim.Device.name is a CharField(max_length=64). Names are also
# required to be unique within (site, tenant) when set; this tool never sets
# a tenant, so in practice that's unique-per-site among the devices it
# manages.
MAX_DEVICE_NAME_LENGTH = 64

_CONTROL_CHARS = re.compile(r"[\x00-\x1f\x7f]")
_WHITESPACE_RUN = re.compile(r"\s+")


def sanitize_device_name(raw: str, mac: str) -> str:
    """Produce a deterministic, NetBox-safe device name from a UniFi client name."""
    name = _CONTROL_CHARS.sub("", raw)
    name = _WHITESPACE_RUN.sub(" ", name).strip()
    if not name:
        name = mac.replace(":", "")
    return name[:MAX_DEVICE_NAME_LENGTH]


def mac_suffixed_name(name: str, mac: str) -> str:
    """Disambiguate a name that collides with a different device, deterministically."""
    suffix = "-" + mac.replace(":", "")[-4:]
    return name[: MAX_DEVICE_NAME_LENGTH - len(suffix)] + suffix
