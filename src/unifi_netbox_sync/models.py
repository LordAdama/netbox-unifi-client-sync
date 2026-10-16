from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class UnifiPort:
    """One physical port on a UniFi device, from its port_table."""

    index: int
    name: str = ""
    media: str = ""  # "GE", "SFP", "SFP+"
    max_speed: int = 0  # Mbps
    is_uplink: bool = False

    @property
    def netbox_name(self) -> str:
        """Stable "Port N" naming.

        Deliberately not the operator-assigned UniFi port name: that can be
        renamed at will, whereas cable matching keys off port index via
        PORT_NAME_TEMPLATES. The UniFi name goes in the description instead.
        """
        return f"Port {self.index}"


@dataclass
class UnifiOutlet:
    """One toggleable outlet on a UniFi PDU or UPS, from its outlet_table."""

    index: int
    name: str = ""


@dataclass
class UnifiSwitchDevice:
    """A UniFi network infrastructure device (switch, AP, gateway, PDU, UPS)."""

    mac: str
    name: str
    model: str
    device_type: str  # "usw", "uap", "ugw", ...
    serial: str | None = None
    ip: str | None = None
    version: str | None = None
    adopted: bool = True
    ports: list[UnifiPort] = field(default_factory=list)
    # Product shortname from the controller ("UPS23", "USP-PDU-Pro"), when present.
    shortname: str = ""
    outlets: list[UnifiOutlet] = field(default_factory=list)

    @property
    def is_switch(self) -> bool:
        return self.device_type == "usw"


@dataclass
class UnifiClient:
    """A normalized UniFi client (wired or wireless)."""

    mac: str
    name: str
    ip: str | None
    is_wired: bool
    switch_mac: str | None = None
    switch_port: int | None = None
    ap_mac: str | None = None
    essid: str | None = None
    # Manufacturer as resolved by the controller from the MAC's OUI, e.g.
    # "Apple, Inc.". Empty for MACs the controller can't attribute (including
    # randomized/private client MACs).
    oui: str | None = None
    # Unix seconds from the controller, when the record included one.
    last_seen: float | None = None

    @property
    def display_name(self) -> str:
        return self.name or self.mac.replace(":", "")


@dataclass(frozen=True)
class UnifiKnownClient:
    """A client the controller remembers, including ones not associated now.

    ``last_seen`` is unix seconds, or None when the record has no usable
    timestamp (missing or zero). None is not the same as "never seen": the
    controller still knows the MAC.
    """

    mac: str
    last_seen: float | None = None


@dataclass
class UnifiSite:
    """A site on the controller.

    `name` is the opaque id used in API paths (e.g. "7xk2p9qr"); `description`
    is the human label shown in the UI (e.g. "Head Office"). NetBox slugs are
    derived from the description, since the id is meaningless to operators.
    """

    name: str
    description: str = ""
    # Controller ``_id`` (Mongo ObjectId). Immutable; ``name`` is the short id
    # used in API paths and is what older releases stored in unifi_site_id.
    internal_id: str = ""

    @property
    def label(self) -> str:
        return self.description or self.name


@dataclass
class SitePair:
    """One UniFi site synced into one NetBox site."""

    unifi_site: str
    netbox_site_slug: str
    # Controller ``_id`` when the site list was readable. Empty for a SITE_MAP
    # entry we could not enrich. Not part of equality for callers that only
    # compare the two slugs.
    unifi_internal_id: str = ""


@dataclass(frozen=True)
class CableSkip:
    """One cable that was not created, for the per-run report."""

    unifi_site: str
    netbox_site: str
    switch: str
    switch_mac: str
    unifi_port: str
    names_tried: str
    netbox_interfaces: str
    reason: str


@dataclass
class ClientSyncResult:
    mac: str
    name: str
    device_created: bool = False
    device_updated: bool = False
    ip_assigned: bool = False
    cable_created: bool = False
    cable_skipped_reason: str | None = None
    # Structured row for the per-run cable skip report. Set whenever
    # cable_skipped_reason is set.
    cable_skip: CableSkip | None = None
    device_update_skipped_reason: str | None = None
    # Set when that specific aspect was inspected and already correct, so no
    # write was issued for it.
    ip_unchanged: bool = False
    cable_unchanged: bool = False
    # True when nothing at all was written for this client.
    unchanged: bool = False
    # True when this client was answered from the in-memory index without
    # the per-object write path. Not a NetBox outcome; timing only.
    fast_path: bool = False
    error: str | None = None


@dataclass
class SiteSyncStats:
    """Per-site timing and counts, for spotting which site is expensive."""

    unifi_site: str
    netbox_site_slug: str
    clients_seen: int = 0
    devices_created: int = 0
    devices_updated: int = 0
    cables_created: int = 0
    errors: int = 0
    duration_seconds: float = 0.0


@dataclass
class SyncSummary:
    clients_seen: int = 0
    devices_created: int = 0
    devices_updated: int = 0
    devices_update_skipped: int = 0
    cables_created: int = 0
    cables_skipped: int = 0
    stale_marked_offline: int = 0
    sites_created: int = 0
    # Adopted UniFi infrastructure seen / created (SYNC_UNIFI_DEVICES)
    devices_synced: int = 0
    infra_created: int = 0
    # Legacy unifi-client devices converted in place (MERGE_CLIENT_INFRA).
    infra_merged: int = 0
    # Devices moved onto the site where they are adopted now.
    infra_moved: int = 0
    # MAC/serial matches that were not created again (inventory or other site).
    infra_duplicates_avoided: int = 0
    infra_type_library: int = 0
    infra_type_generated: int = 0
    # Work that was inspected and correctly required no write. High numbers
    # here are the healthy steady state, not a problem.
    clients_unchanged: int = 0
    ips_unchanged: int = 0
    cables_unchanged: int = 0
    cache_hits: int = 0
    cache_misses: int = 0
    # Clients answered from the prefetched index without the write path.
    clients_fast_path: int = 0
    # Wall time spent bulk-loading NetBox before per-client work.
    prefetch_seconds: float = 0.0
    # Lookups served from that bulk index (device, name, interface, cable).
    prefetch_hits: int = 0
    duration_seconds: float = 0.0
    errors: list[str] | None = None
    client_results: list[ClientSyncResult] | None = None
    site_stats: list[SiteSyncStats] | None = None
    cable_skips: list[CableSkip] | None = None

    def __post_init__(self) -> None:
        if self.errors is None:
            self.errors = []
        if self.client_results is None:
            self.client_results = []
        if self.site_stats is None:
            self.site_stats = []
        if self.cable_skips is None:
            self.cable_skips = []
