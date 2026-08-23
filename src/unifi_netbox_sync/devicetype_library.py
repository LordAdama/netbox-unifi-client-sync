from __future__ import annotations

import logging
import re
import threading
from dataclasses import dataclass, field
from pathlib import Path

logger = logging.getLogger(__name__)

# Speeds are Mbps as UniFi reports them; values are NetBox interface types.
_ETHERNET_BY_SPEED = {
    100: "100base-tx",
    1000: "1000base-t",
    2500: "2.5gbase-t",
    5000: "5gbase-t",
    10000: "10gbase-t",
}
_FIBRE_BY_SPEED = {
    1000: "1000base-x-sfp",
    10000: "10gbase-x-sfpp",
    25000: "25gbase-x-sfp28",
}


def netbox_interface_type(media: str, max_speed: int) -> str:
    """Best NetBox interface type for a UniFi port.

    Defaults to 1000base-t, which is right for the overwhelming majority of
    UniFi access ports and harmless where it isn't — the type is cosmetic for
    cable tracing, which is what this tool is for.
    """
    if media.upper().startswith("SFP"):
        return _FIBRE_BY_SPEED.get(max_speed, "1000base-x-sfp")
    return _ETHERNET_BY_SPEED.get(max_speed, "1000base-t")


def normalize_model(value: str) -> str:
    """Fold a model/part number for comparison: 'USW-24-PoE' -> 'USW24POE'."""
    return re.sub(r"[^A-Z0-9]", "", (value or "").upper())


@dataclass
class DeviceTypeSpec:
    """A NetBox device type to find or create, plus its interface templates."""

    manufacturer: str
    model: str
    slug: str
    part_number: str = ""
    u_height: float = 1
    is_full_depth: bool = False
    airflow: str = ""
    # (name, netbox type) pairs
    interfaces: list[tuple[str, str]] = field(default_factory=list)


class DeviceTypeLibrary:
    """Optional index over a local netbox-community/devicetype-library clone.

    Used to give created devices canonical model names, part numbers and rack
    heights instead of a bare UniFi model code. It is *not* the source of
    truth for ports: the controller's own port_table is, since it reflects the
    hardware actually in front of you.

    Loaded lazily and only for the Ubiquiti vendor directory, so pointing at a
    full clone costs one small directory scan on first use.
    """

    def __init__(self, path: str | None, vendor_dir: str = "Ubiquiti") -> None:
        self.path = path
        self.vendor_dir = vendor_dir
        self._lock = threading.Lock()
        self._by_key: dict[str, DeviceTypeSpec] | None = None

    def _candidate_dirs(self) -> list[Path]:
        root = Path(self.path or "")
        # Accept either the repo root or the device-types dir directly.
        return [
            root / "device-types" / self.vendor_dir,
            root / self.vendor_dir,
            root,
        ]

    def _load(self) -> dict[str, DeviceTypeSpec]:
        index: dict[str, DeviceTypeSpec] = {}
        if not self.path:
            return index
        try:
            import yaml
        except ImportError:
            logger.warning(
                "DEVICETYPE_LIBRARY_PATH is set but PyYAML is not installed; "
                "device types will be built from the controller's port table only"
            )
            return index

        directory = next((d for d in self._candidate_dirs() if d.is_dir()), None)
        if directory is None:
            logger.warning(
                "DEVICETYPE_LIBRARY_PATH %r has no %s device-types directory; "
                "device types will be built from the controller's port table only",
                self.path,
                self.vendor_dir,
            )
            return index

        for yaml_file in sorted(directory.glob("*.y*ml")):
            try:
                data = yaml.safe_load(yaml_file.read_text(encoding="utf-8"))
            except (OSError, Exception) as exc:  # noqa: BLE001 - a bad file must not stop the scan
                logger.debug("Skipping %s: %s", yaml_file.name, exc)
                continue
            if not isinstance(data, dict) or not data.get("model"):
                continue
            spec = DeviceTypeSpec(
                manufacturer=data.get("manufacturer") or self.vendor_dir,
                model=str(data["model"]),
                slug=str(data.get("slug") or ""),
                part_number=str(data.get("part_number") or ""),
                u_height=data.get("u_height", 1) or 0,
                is_full_depth=bool(data.get("is_full_depth", False)),
                airflow=str(data.get("airflow") or ""),
                interfaces=[
                    (str(i["name"]), str(i.get("type") or "1000base-t"))
                    for i in (data.get("interfaces") or [])
                    if isinstance(i, dict) and i.get("name")
                ],
            )
            # Index under every identifier a UniFi model code might match.
            for key in (spec.part_number, spec.model, spec.slug):
                normalized = normalize_model(key)
                if normalized:
                    index.setdefault(normalized, spec)

        logger.info("Indexed %d Ubiquiti device types from %s", len(index), directory)
        return index

    def lookup(self, model_code: str) -> DeviceTypeSpec | None:
        if not self.path:
            return None
        with self._lock:
            if self._by_key is None:
                self._by_key = self._load()
            return self._by_key.get(normalize_model(model_code))


def spec_from_unifi_device(device, manufacturer: str = "Ubiquiti") -> DeviceTypeSpec:
    """Build a device type straight from what the controller reports.

    The fallback when the library has no match (or isn't configured). Port
    names follow the "Port N" convention the library also uses, so cable
    matching works identically either way.
    """
    from .naming import slugify

    model = device.model or "Unknown"
    interfaces = [(p.netbox_name, netbox_interface_type(p.media, p.max_speed)) for p in device.ports]
    if not interfaces:
        # APs and gateways often report no port_table; they still need
        # somewhere to terminate an uplink cable.
        interfaces = [("Port 1", "1000base-t")]
    return DeviceTypeSpec(
        manufacturer=manufacturer,
        model=f"{manufacturer} {model}",
        slug=slugify(f"{manufacturer}-{model}"),
        part_number=model,
        u_height=0,
        is_full_depth=False,
        interfaces=interfaces,
    )
