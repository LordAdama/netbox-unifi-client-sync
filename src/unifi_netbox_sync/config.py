from __future__ import annotations

import os
from dataclasses import dataclass, field

from .models import SitePair


def _bool(value: str) -> bool:
    return value.strip().lower() in ("1", "true", "yes", "on")


# Tried in order against the switch's real interface names. Covers the naming
# used by the NetBox devicetype-library UniFi profiles ("Port 1"), bare-number
# imports ("1"), and the common vendor abbreviations. All candidates are
# matched against one cached interface list, so a long list costs nothing
# extra at runtime.
DEFAULT_PORT_NAME_TEMPLATES = [
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


def _positive_int(raw: str, name: str) -> int:
    try:
        value = int(raw)
    except ValueError:
        raise SystemExit(f"{name} must be an integer, got {raw!r}") from None
    if value < 1:
        raise SystemExit(f"{name} must be >= 1, got {value}")
    return value


def _load_dotenv(path: str = ".env") -> None:
    """Load KEY=VALUE lines from `path` into os.environ (without overriding
    variables already set in the real environment).

    This deliberately does NOT shell out to `source`/`.` — several default
    values here (PORT_NAME_TEMPLATES, SITE_MAP) contain spaces and commas,
    which bash's word-splitting mangles if you `source .env` directly. A
    plain KEY=VALUE parser sidesteps that entirely, and is also what Docker's
    --env-file does (line-oriented, no shell semantics).
    """
    try:
        with open(path, encoding="utf-8") as f:
            lines = f.readlines()
    except FileNotFoundError:
        return
    for line in lines:
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        os.environ.setdefault(key, value)


def _parse_site_map(raw: str) -> tuple[dict[str, str], bool]:
    """Parse SITE_MAP into (explicit pairs, discover-all flag).

    Accepts "unifi_site:netbox_slug" pairs, the bare token "*" meaning "every
    site the controller reports", or both — so you can sync everything while
    still pinning individual sites to a particular NetBox slug:

        SITE_MAP=*
        SITE_MAP=*,default:head-office
    """
    site_map: dict[str, str] = {}
    discover_all = False
    for entry in raw.split(","):
        entry = entry.strip()
        if not entry:
            continue
        if entry == "*":
            discover_all = True
            continue
        if ":" not in entry:
            raise SystemExit(
                f"Invalid SITE_MAP entry {entry!r}; expected 'unifi_site:netbox_site_slug' or '*'"
            )
        unifi_site, netbox_slug = (part.strip() for part in entry.split(":", 1))
        if not unifi_site or not netbox_slug:
            raise SystemExit(
                f"Invalid SITE_MAP entry {entry!r}; expected 'unifi_site:netbox_site_slug' or '*'"
            )
        site_map[unifi_site] = netbox_slug
    return site_map, discover_all


@dataclass
class Settings:
    unifi_host: str
    unifi_username: str = ""
    unifi_password: str = ""
    unifi_api_key: str | None = None
    # Single-site convenience fields, used only when site_map is empty.
    unifi_site: str = "default"
    netbox_site_slug: str = ""
    # UniFi site name -> NetBox site slug, for syncing more than one UniFi
    # site in a single run. Takes precedence over unifi_site/netbox_site_slug
    # when non-empty. See Settings.site_pairs().
    site_map: dict[str, str] = field(default_factory=dict)
    # Discover every site from the controller instead of listing them. Entries
    # in site_map still win for the sites they name, so you can sync
    # everything while pinning a few slugs explicitly.
    sync_all_sites: bool = False
    unifi_is_udm: bool = True
    unifi_verify_ssl: bool = False

    netbox_url: str = ""
    netbox_token: str = ""
    netbox_verify_ssl: bool = True
    netbox_ip_status: str = "active"

    dry_run: bool = False
    log_level: str = "INFO"
    log_format: str = "text"
    sync_tag: str = "unifi-sync"
    client_device_role_slug: str = "unifi-client"
    client_manufacturer_slug: str = "generic"
    client_device_type_slug: str = "generic-network-client"
    port_name_templates: list[str] = field(
        default_factory=lambda: DEFAULT_PORT_NAME_TEMPLATES.copy()
    )
    cable_conflict_policy: str = "skip"
    mark_stale_offline: bool = True
    metrics_file: str | None = None
    # "require" (default, safe): fail if a site slug doesn't already exist.
    # "create": create a minimal site if missing; an existing site's
    # attributes are never modified either way. Any other value behaves as
    # "require", matching cable_conflict_policy's fail-safe-default style.
    # Applies to every site in site_map the same way.
    site_policy: str = "require"
    # "sync" (default): keep an existing client device's name/status in
    # sync with UniFi. "create-only": set them once at creation and never
    # touch them again, so a NetBox admin's manual edits stick. Interfaces,
    # IPs, and cables are unaffected by this — it only governs the device's
    # name/status fields. Any other value behaves as "sync".
    device_update_policy: str = "sync"
    # Site groups synced concurrently. 1 (default) keeps the original strictly
    # sequential behavior. Raise it for many sites; the ceiling is whatever
    # your NetBox and UniFi controller tolerate, not this tool.
    max_workers: int = 1
    # Give each client a device type carrying its real manufacturer, resolved
    # from the MAC OUI (the controller reports this per client). Falls back to
    # client_manufacturer_slug / client_device_type_slug when the vendor is
    # unknown or the MAC is randomized.
    use_oui_manufacturer: bool = True
    # Optional IEEE oui.txt or Wireshark manuf file, consulted only for MACs
    # the controller did not attribute.
    oui_file: str | None = None

    def site_pairs(self) -> list[SitePair]:
        if self.site_map:
            return [SitePair(unifi_site=u, netbox_site_slug=n) for u, n in self.site_map.items()]
        return [SitePair(unifi_site=self.unifi_site, netbox_site_slug=self.netbox_site_slug)]

    @classmethod
    def from_env(cls) -> "Settings":
        _load_dotenv()
        try:
            unifi_host = os.environ["UNIFI_HOST"]
            netbox_url = os.environ["NETBOX_URL"]
            netbox_token = os.environ["NETBOX_TOKEN"]
        except KeyError as exc:
            raise SystemExit(f"Missing required environment variable: {exc}") from exc

        unifi_api_key = os.environ.get("UNIFI_API_KEY") or None
        unifi_username = os.environ.get("UNIFI_USERNAME", "")
        unifi_password = os.environ.get("UNIFI_PASSWORD", "")
        if not unifi_api_key and not (unifi_username and unifi_password):
            raise SystemExit(
                "Provide either UNIFI_API_KEY, or both UNIFI_USERNAME and UNIFI_PASSWORD"
            )

        site_map, sync_all_sites = _parse_site_map(os.environ.get("SITE_MAP", ""))
        netbox_site_slug = os.environ.get("NETBOX_SITE_SLUG", "")
        if not site_map and not sync_all_sites and not netbox_site_slug:
            raise SystemExit(
                "Provide NETBOX_SITE_SLUG (single site), SITE_MAP (explicit pairs), "
                "or SITE_MAP=* (every site on the controller)"
            )

        templates_raw = os.environ.get("PORT_NAME_TEMPLATES", "")
        port_name_templates = [t.strip() for t in templates_raw.split(",") if t.strip()] or (
            DEFAULT_PORT_NAME_TEMPLATES.copy()
        )

        return cls(
            unifi_host=unifi_host.rstrip("/"),
            unifi_username=unifi_username,
            unifi_password=unifi_password,
            unifi_api_key=unifi_api_key,
            unifi_site=os.environ.get("UNIFI_SITE", "default"),
            netbox_site_slug=netbox_site_slug,
            site_map=site_map,
            sync_all_sites=sync_all_sites,
            unifi_is_udm=_bool(os.environ.get("UNIFI_IS_UDM", "true")),
            unifi_verify_ssl=_bool(os.environ.get("UNIFI_VERIFY_SSL", "false")),
            netbox_url=netbox_url.rstrip("/"),
            netbox_token=netbox_token,
            netbox_verify_ssl=_bool(os.environ.get("NETBOX_VERIFY_SSL", "true")),
            netbox_ip_status=os.environ.get("NETBOX_IP_STATUS", "active"),
            dry_run=_bool(os.environ.get("DRY_RUN", "false")),
            log_level=os.environ.get("LOG_LEVEL", "INFO"),
            log_format=os.environ.get("LOG_FORMAT", "text"),
            sync_tag=os.environ.get("SYNC_TAG", "unifi-sync"),
            client_device_role_slug=os.environ.get("CLIENT_DEVICE_ROLE_SLUG", "unifi-client"),
            client_manufacturer_slug=os.environ.get("CLIENT_MANUFACTURER_SLUG", "generic"),
            client_device_type_slug=os.environ.get("CLIENT_DEVICE_TYPE_SLUG", "generic-network-client"),
            port_name_templates=port_name_templates,
            cable_conflict_policy=os.environ.get("CABLE_CONFLICT_POLICY", "skip"),
            mark_stale_offline=_bool(os.environ.get("MARK_STALE_OFFLINE", "true")),
            metrics_file=os.environ.get("METRICS_FILE") or None,
            site_policy=os.environ.get("SITE_POLICY", "require"),
            device_update_policy=os.environ.get("DEVICE_UPDATE_POLICY", "sync"),
            max_workers=_positive_int(os.environ.get("MAX_WORKERS", "1"), "MAX_WORKERS"),
            use_oui_manufacturer=_bool(os.environ.get("USE_OUI_MANUFACTURER", "true")),
            oui_file=os.environ.get("OUI_FILE") or None,
        )
