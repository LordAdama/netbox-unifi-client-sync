from __future__ import annotations

from dataclasses import dataclass


@dataclass
class UnifiSwitchDevice:
    """A UniFi network infrastructure device (switch, AP, gateway)."""

    mac: str
    name: str
    model: str
    device_type: str  # "usw", "uap", "ugw", ...

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

    @property
    def display_name(self) -> str:
        return self.name or self.mac.replace(":", "")


@dataclass
class SitePair:
    """One UniFi site synced into one NetBox site."""

    unifi_site: str
    netbox_site_slug: str


@dataclass
class ClientSyncResult:
    mac: str
    name: str
    device_created: bool = False
    device_updated: bool = False
    ip_assigned: bool = False
    cable_created: bool = False
    cable_skipped_reason: str | None = None
    device_update_skipped_reason: str | None = None
    # Set when that specific aspect was inspected and already correct, so no
    # write was issued for it.
    ip_unchanged: bool = False
    cable_unchanged: bool = False
    # True when nothing at all was written for this client.
    unchanged: bool = False
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
    # Work that was inspected and correctly required no write. High numbers
    # here are the healthy steady state, not a problem.
    clients_unchanged: int = 0
    ips_unchanged: int = 0
    cables_unchanged: int = 0
    cache_hits: int = 0
    cache_misses: int = 0
    duration_seconds: float = 0.0
    errors: list[str] | None = None
    client_results: list[ClientSyncResult] | None = None
    site_stats: list[SiteSyncStats] | None = None

    def __post_init__(self) -> None:
        if self.errors is None:
            self.errors = []
        if self.client_results is None:
            self.client_results = []
        if self.site_stats is None:
            self.site_stats = []
