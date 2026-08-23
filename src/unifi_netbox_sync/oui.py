from __future__ import annotations

import logging
import re
import threading

logger = logging.getLogger(__name__)

# Locally-administered / randomized MACs carry no vendor meaning: the second
# least-significant bit of the first octet is the "locally administered" flag.
# Modern phones randomize per-SSID, so attributing these to a vendor would be
# actively wrong rather than merely unknown.
_LOCALLY_ADMINISTERED_MASK = 0b10


def is_locally_administered(mac: str) -> bool:
    try:
        first_octet = int(mac.split(":")[0], 16)
    except (ValueError, IndexError):
        return False
    return bool(first_octet & _LOCALLY_ADMINISTERED_MASK)


def oui_prefix(mac: str) -> str:
    """First three octets, uppercase hex, no separators — e.g. 'AABBCC'."""
    return "".join(mac.split(":")[:3]).upper()


class OuiLookup:
    """Offline OUI -> vendor lookup, for MACs the controller didn't attribute.

    Parses either the IEEE `oui.txt` format::

        AA-BB-CC   (hex)		Vendor Name

    or the Wireshark `manuf` format::

        AA:BB:CC	Short	Vendor Name

    Loaded lazily on first miss and cached, so configuring a file you never
    need costs nothing. Absent a file this returns None for everything, which
    simply leaves the manufacturer as the configured fallback.
    """

    _IEEE = re.compile(r"^\s*([0-9A-Fa-f]{2})[-:]([0-9A-Fa-f]{2})[-:]([0-9A-Fa-f]{2})\s+\(hex\)\s+(.+?)\s*$")
    _MANUF = re.compile(r"^\s*([0-9A-Fa-f]{2}):([0-9A-Fa-f]{2}):([0-9A-Fa-f]{2})\s+(\S+)(?:\s+(.*?))?\s*$")

    def __init__(self, path: str | None) -> None:
        self.path = path
        self._lock = threading.Lock()
        self._table: dict[str, str] | None = None

    def _load(self) -> dict[str, str]:
        table: dict[str, str] = {}
        if not self.path:
            return table
        try:
            with open(self.path, encoding="utf-8", errors="replace") as handle:
                for line in handle:
                    if not line.strip() or line.lstrip().startswith("#"):
                        continue
                    ieee = self._IEEE.match(line)
                    if ieee:
                        a, b, c, vendor = ieee.groups()
                        table[f"{a}{b}{c}".upper()] = vendor.strip()
                        continue
                    manuf = self._MANUF.match(line)
                    if manuf:
                        a, b, c, short, long_name = manuf.groups()
                        table[f"{a}{b}{c}".upper()] = (long_name or short).strip()
        except OSError as exc:
            logger.warning("Could not read OUI file %s: %s", self.path, exc)
            return {}
        logger.info("Loaded %d OUI entries from %s", len(table), self.path)
        return table

    def lookup(self, mac: str) -> str | None:
        if not self.path:
            return None
        with self._lock:
            if self._table is None:
                self._table = self._load()
            return self._table.get(oui_prefix(mac))
