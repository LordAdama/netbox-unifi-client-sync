from __future__ import annotations

import os
import re
from dataclasses import dataclass, field

from .models import SitePair
from .portmatch import parse_model_templates


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


def _positive_float(raw: str, name: str) -> float:
    try:
        value = float(raw)
    except ValueError:
        raise SystemExit(f"{name} must be a number of seconds, got {raw!r}") from None
    if value <= 0:
        raise SystemExit(f"{name} must be > 0, got {value}")
    return value


_DURATION = re.compile(r"^(\d+(?:\.\d+)?)([smhd])?$")
_DURATION_UNITS = {"s": 1, "m": 60, "h": 3600, "d": 86400}


def _duration_seconds(raw: str, name: str) -> float:
    """Parse ``7d``, ``24h``, ``30m``, ``90s``, or a bare number of seconds.

    Zero is allowed and means "no grace": a client missing from the active
    list is eligible as soon as its last_seen is in the past. Negative values
    and bare words are rejected.
    """
    text = raw.strip().lower()
    match = _DURATION.fullmatch(text)
    if not match:
        raise SystemExit(
            f"{name} must be a duration like 7d, 24h, 30m, or a number of seconds, got {raw!r}"
        ) from None
    seconds = float(match.group(1)) * _DURATION_UNITS[match.group(2) or "s"]
    if seconds < 0:
        raise SystemExit(f"{name} must be >= 0, got {raw!r}")
    return seconds


_NETBOX_DEVICE_STATUSES = {
    "offline",
    "active",
    "planned",
    "staged",
    "failed",
    "inventory",
    "decommissioning",
}


def _device_status(raw: str, name: str) -> str:
    value = raw.strip().lower()
    if value not in _NETBOX_DEVICE_STATUSES:
        allowed = ", ".join(sorted(_NETBOX_DEVICE_STATUSES))
        raise SystemExit(f"{name} must be one of {allowed}, got {raw!r}")
    return value


def _csv_tokens(raw: str) -> tuple[str, ...]:
    return tuple(part.strip() for part in raw.split(",") if part.strip())


def _managed_client_roles_from_env() -> tuple[str, ...]:
    """Roles whose placeholder interface and IP the sync may write.

    Unset ``MANAGED_CLIENT_ROLE_SLUGS`` follows ``CLIENT_DEVICE_ROLE_SLUG``.
    An explicit list replaces that default; include the client role in it
    when new client devices should keep being managed on later passes.
    """
    client_role = (os.environ.get("CLIENT_DEVICE_ROLE_SLUG") or "unifi-client").strip() or "unifi-client"
    raw = os.environ.get("MANAGED_CLIENT_ROLE_SLUGS")
    if raw is None or not raw.strip():
        return (client_role,)
    return _csv_tokens(raw) or (client_role,)


def _positive_int(raw: str, name: str) -> int:
    try:
        value = int(raw)
    except ValueError:
        raise SystemExit(f"{name} must be an integer, got {raw!r}") from None
    if value < 1:
        raise SystemExit(f"{name} must be >= 1, got {value}")
    return value


def _non_negative_int(raw: str, name: str) -> int:
    try:
        value = int(raw)
    except ValueError:
        raise SystemExit(f"{name} must be an integer, got {raw!r}") from None
    if value < 0:
        raise SystemExit(f"{name} must be >= 0, got {value}")
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
    # Seconds for each NetBox HTTP call. pynetbox does not set a timeout of
    # its own; without this a stalled connection hangs the run indefinitely.
    netbox_timeout: float = 30.0
    unifi_timeout: float = 30.0
    # Bulk-load devices, interfaces, and names once per site instead of one
    # filtered GET per client. Results match the per-object lookups. Set
    # false to force the old call pattern.
    netbox_prefetch: bool = True
    # Skip the write path when the prefetched record already matches UniFi.
    # Same creates, updates, skips, and offline decisions as the full path.
    netbox_fast_path: bool = True
    # Page size for bulk GETs. NetBox caps this at MAX_PAGE_SIZE (often 1000).
    netbox_page_size: int = 1000
    # Cap on concurrent NetBox HTTP calls across site workers. 0 means no
    # extra cap. 429 responses are retried up to netbox_rate_limit_retries.
    netbox_max_inflight: int = 4
    netbox_rate_limit_retries: int = 5

    # Default on, so a container started from an unedited environment cannot
    # write to NetBox. Set DRY_RUN=false or pass --apply to write.
    dry_run: bool = True
    log_level: str = "INFO"
    log_format: str = "text"
    sync_tag: str = "unifi-sync"
    client_device_role_slug: str = "unifi-client"
    # Roles whose placeholder client interface and IP this sync may write.
    # Empty means "only client_device_role_slug". A device in any other role
    # is still matched by MAC, so no duplicate is created, but eth0/wlan0
    # and the address on it are left alone.
    managed_client_role_slugs: tuple[str, ...] = ()
    # Opt-out tag. A device carrying it is matched by MAC (no duplicate) and
    # does not get placeholder interface or IP changes, even if its role is
    # still a managed client role.
    sync_unmanaged_tag: str = "sync-unmanaged"
    client_manufacturer_slug: str = "generic"
    client_device_type_slug: str = "generic-network-client"
    # Interface name used for wired client devices. Wireless stays wlan0.
    client_wired_interface_name: str = "eth0"
    port_name_templates: list[str] = field(
        default_factory=lambda: DEFAULT_PORT_NAME_TEMPLATES.copy()
    )
    # UniFi model (folded) -> templates tried before port_name_templates.
    # Empty means every switch uses the global list plus structural matching.
    port_model_templates: dict[str, list[str]] = field(default_factory=dict)
    # Local CSV of every skipped cable. Unset writes nothing; the run still
    # logs one line per skip and a count by reason. Dry-run writes this file
    # too — it is not a NetBox change.
    cable_skip_report: str | None = None
    cable_conflict_policy: str = "skip"
    mark_stale_offline: bool = True
    # How long a client may be absent from the active list before it is
    # marked offline. Compared to UniFi last_seen, not to "missing from
    # /stat/sta right now". Default 7 days, so a laptop over a weekend or a
    # phone that slept is left active. 0 disables the grace.
    stale_offline_grace_seconds: float = 7 * 24 * 3600
    metrics_file: str | None = None
    # "require" (default, safe): fail if a site slug doesn't already exist.
    # "create": create a minimal site if missing; an existing site's
    # attributes are never modified either way. Any other value behaves as
    # "require", matching cable_conflict_policy's fail-safe-default style.
    # Applies to every site in site_map the same way.
    # How to find an existing NetBox site for a UniFi site:
    #   "normalized" (default) - exact slug, then exact name, then both folded
    #                            to alphanumerics so punctuation differences
    #                            (apostrophes, dashes) don't create duplicates
    #   "name"                 - exact slug, then exact name
    #   "slug"                 - exact slug only (the original behavior)
    # A matched site is linked to and never modified.
    site_match: str = "normalized"
    site_policy: str = "require"
    # "create-only" (default): set a managed device's name/status once, at
    # creation, and never overwrite them. "sync" keeps them matched to UniFi
    # and is opt-in because it reverts renames made in NetBox. Either value
    # only applies to devices carrying sync_tag; untagged objects are never
    # modified. Any value other than "create-only" behaves as "sync".
    device_update_policy: str = "create-only"
    # Site groups synced concurrently. 4 overlaps independent sites without
    # a stampede; NETBOX_MAX_INFLIGHT still caps in-flight HTTP. 1 is strictly
    # sequential. The ceiling is whatever NetBox and the controller tolerate.
    max_workers: int = 4
    # Give each client a device type carrying its real manufacturer, resolved
    # from the MAC OUI (the controller reports this per client). Falls back to
    # client_manufacturer_slug / client_device_type_slug when the vendor is
    # unknown or the MAC is randomized.
    use_oui_manufacturer: bool = True
    # Optional IEEE oui.txt or Wireshark manuf file, consulted only for MACs
    # the controller did not attribute.
    oui_file: str | None = None
    # Create adopted UniFi infrastructure (switches, APs, gateways) in NetBox
    # rather than requiring it to be there already. Off by default: creating
    # infrastructure is more invasive than creating clients.
    sync_unifi_devices: bool = False
    # Local clone of netbox-community/devicetype-library. When a model matches,
    # its interface, power-port, and power-outlet templates are used. Otherwise
    # the type is built from the controller port_table and outlet_table.
    devicetype_library_path: str | None = None
    # Fallback role for created UniFi devices whose kind isn't recognised.
    # Recognised kinds use the per-kind slugs below.
    unifi_device_role_slug: str = "network-device"
    # Per-kind role slugs. Defaults match the historical hardcoded roles so an
    # existing NetBox (switch / wireless-ap / router) keeps working. Point
    # these at roles you already have (access-switch, wireless-access-point,
    # gateway, pdu) and the tool reuses them instead of creating "switch".
    unifi_switch_role_slug: str = "switch"
    unifi_ap_role_slug: str = "wireless-ap"
    unifi_gateway_role_slug: str = "router"
    unifi_power_role_slug: str = "pdu"
    # Convert a legacy client device (role = client role, unifi_mac set) into
    # the adopted infrastructure device with the same MAC, in place. Independent
    # of DEVICE_UPDATE_POLICY: create-only still merges when this is true.
    # false leaves that client device untouched and does not create a second one.
    merge_client_infra: bool = True
    # A device owns more than its single unifi_mac: addresses listed in
    # unifi_macs, and addresses on its interfaces (mac_address or a MAC
    # address object). A UniFi client that presents one of those addresses
    # is not created as a second device. Exact unifi_mac matching is unchanged.
    # false keeps the previous single-field join.
    match_owned_macs: bool = True
    # When a same-site name is already held by a device this UniFi device must
    # not be linked to, create another device whose name ends in the MAC.
    # Off by default: a stale hand-made record, or two UniFi devices that share
    # a name, must not grow a duplicate on the first pass. The conflict is
    # still logged and counted.
    create_suffixed_on_name_conflict: bool = False
    # UniFi site ids (API name and/or internal _id) whose adopted devices are
    # inventory or failed-unit staging. Empty disables the behaviour.
    inventory_site_ids: tuple[str, ...] = ()
    # NetBox status for devices synced from an inventory site.
    inventory_device_status: str = "inventory"

    def site_pairs(self) -> list[SitePair]:
        if self.site_map:
            return [SitePair(unifi_site=u, netbox_site_slug=n) for u, n in self.site_map.items()]
        return [SitePair(unifi_site=self.unifi_site, netbox_site_slug=self.netbox_site_slug)]

    def managed_client_roles(self) -> tuple[str, ...]:
        """Role slugs whose placeholder interface and IP this sync may write.

        An explicit list wins. Otherwise the single configured client role is
        the whole set, so renaming ``CLIENT_DEVICE_ROLE_SLUG`` keeps new
        client devices managed without a second setting.
        """
        roles = tuple(slug.strip() for slug in self.managed_client_role_slugs if slug and slug.strip())
        if roles:
            return roles
        slug = (self.client_device_role_slug or "").strip()
        return (slug,) if slug else ()

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
            netbox_timeout=_positive_float(os.environ.get("NETBOX_TIMEOUT", "30"), "NETBOX_TIMEOUT"),
            unifi_timeout=_positive_float(os.environ.get("UNIFI_TIMEOUT", "30"), "UNIFI_TIMEOUT"),
            netbox_prefetch=_bool(os.environ.get("NETBOX_PREFETCH", "true")),
            netbox_fast_path=_bool(os.environ.get("NETBOX_FAST_PATH", "true")),
            netbox_page_size=_positive_int(
                os.environ.get("NETBOX_PAGE_SIZE", "1000"), "NETBOX_PAGE_SIZE"
            ),
            netbox_max_inflight=_non_negative_int(
                os.environ.get("NETBOX_MAX_INFLIGHT", "4"), "NETBOX_MAX_INFLIGHT"
            ),
            netbox_rate_limit_retries=_non_negative_int(
                os.environ.get("NETBOX_RATE_LIMIT_RETRIES", "5"), "NETBOX_RATE_LIMIT_RETRIES"
            ),
            dry_run=_bool(os.environ.get("DRY_RUN", "true")),
            log_level=os.environ.get("LOG_LEVEL", "INFO"),
            log_format=os.environ.get("LOG_FORMAT", "text"),
            sync_tag=os.environ.get("SYNC_TAG", "unifi-sync"),
            client_device_role_slug=(
                os.environ.get("CLIENT_DEVICE_ROLE_SLUG") or "unifi-client"
            ).strip()
            or "unifi-client",
            managed_client_role_slugs=_managed_client_roles_from_env(),
            sync_unmanaged_tag=(os.environ.get("SYNC_UNMANAGED_TAG") or "sync-unmanaged").strip()
            or "sync-unmanaged",
            client_manufacturer_slug=os.environ.get("CLIENT_MANUFACTURER_SLUG", "generic"),
            client_device_type_slug=os.environ.get("CLIENT_DEVICE_TYPE_SLUG", "generic-network-client"),
            client_wired_interface_name=(os.environ.get("CLIENT_WIRED_INTERFACE_NAME") or "eth0").strip() or "eth0",
            port_name_templates=port_name_templates,
            port_model_templates=parse_model_templates(os.environ.get("PORT_MODEL_TEMPLATES", "")),
            cable_skip_report=os.environ.get("CABLE_SKIP_REPORT") or None,
            cable_conflict_policy=os.environ.get("CABLE_CONFLICT_POLICY", "skip"),
            mark_stale_offline=_bool(os.environ.get("MARK_STALE_OFFLINE", "true")),
            stale_offline_grace_seconds=_duration_seconds(
                os.environ.get("STALE_OFFLINE_GRACE", "7d"), "STALE_OFFLINE_GRACE"
            ),
            metrics_file=os.environ.get("METRICS_FILE") or None,
            site_match=os.environ.get("SITE_MATCH", "normalized"),
            site_policy=os.environ.get("SITE_POLICY", "require"),
            device_update_policy=os.environ.get("DEVICE_UPDATE_POLICY", "create-only"),
            max_workers=_positive_int(os.environ.get("MAX_WORKERS", "4"), "MAX_WORKERS"),
            use_oui_manufacturer=_bool(os.environ.get("USE_OUI_MANUFACTURER", "true")),
            oui_file=os.environ.get("OUI_FILE") or None,
            sync_unifi_devices=_bool(os.environ.get("SYNC_UNIFI_DEVICES", "false")),
            devicetype_library_path=os.environ.get("DEVICETYPE_LIBRARY_PATH") or None,
            unifi_device_role_slug=os.environ.get("UNIFI_DEVICE_ROLE_SLUG", "network-device"),
            unifi_switch_role_slug=os.environ.get("UNIFI_SWITCH_ROLE_SLUG", "switch"),
            unifi_ap_role_slug=os.environ.get("UNIFI_AP_ROLE_SLUG", "wireless-ap"),
            unifi_gateway_role_slug=os.environ.get("UNIFI_GATEWAY_ROLE_SLUG", "router"),
            unifi_power_role_slug=os.environ.get("UNIFI_POWER_ROLE_SLUG", "pdu"),
            merge_client_infra=_bool(os.environ.get("MERGE_CLIENT_INFRA", "true")),
            match_owned_macs=_bool(os.environ.get("MATCH_OWNED_MACS", "true")),
            create_suffixed_on_name_conflict=_bool(
                os.environ.get("CREATE_SUFFIXED_ON_NAME_CONFLICT", "false")
            ),
            inventory_site_ids=_csv_tokens(os.environ.get("UNIFI_INVENTORY_SITES", "")),
            inventory_device_status=_device_status(
                os.environ.get("INVENTORY_DEVICE_STATUS", "inventory"),
                "INVENTORY_DEVICE_STATUS",
            ),
        )
