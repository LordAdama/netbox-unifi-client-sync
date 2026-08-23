from __future__ import annotations

import logging
import threading
from typing import Any

from .netbox_client import NetboxGateway

logger = logging.getLogger(__name__)


class CachingNetboxGateway:
    """Run-scoped read cache in front of any NetboxGateway.

    Only caches lookups whose answers are stable for the lifetime of one sync
    run. Deliberately NOT cached:

    - ``find_client_device_by_mac`` — this tool creates and mutates client
      devices as it goes, so a cached miss would go stale the moment we
      create one.
    - ``device_name_taken_by_other`` — names change as devices are created
      during the run.
    - ``ensure_cable`` / ``list_synced_client_devices`` — these need live
      cable/status state to stay correct.

    Switch devices and their interfaces *are* cached: this tool never mutates
    switches, and the identity of a switch port doesn't change mid-run. Cable
    state hangs off those interfaces, but ``ensure_cable`` re-reads it by id
    on the real gateway rather than trusting the cached object.

    Thread-safe: parallel site workers share one instance (see SyncEngine).
    """

    def __init__(self, inner: NetboxGateway) -> None:
        self._inner = inner
        self._lock = threading.Lock()
        self._sites: dict[str, Any] = {}
        self._switches: dict[str, Any] = {}
        self._switch_ports: dict[tuple[Any, tuple[str, ...]], Any] = {}
        self._device_interfaces: dict[Any, dict[str, Any]] = {}
        self._prereqs_done: set[tuple[str, str, str, str]] = set()
        self.hits = 0
        self.misses = 0

    # -- cached ----------------------------------------------------------

    def ensure_prerequisites(
        self, role_slug: str, manufacturer_slug: str, device_type_slug: str, tag_slug: str
    ) -> None:
        key = (role_slug, manufacturer_slug, device_type_slug, tag_slug)
        with self._lock:
            if key in self._prereqs_done:
                self.hits += 1
                return
        # Deliberately outside the lock: this makes several network calls, and
        # holding the lock across them would serialize every site worker.
        # Worst case two workers race and both run it — ensure_prerequisites
        # is idempotent, so that costs a duplicate call, not correctness.
        self._inner.ensure_prerequisites(role_slug, manufacturer_slug, device_type_slug, tag_slug)
        with self._lock:
            self._prereqs_done.add(key)
            self.misses += 1

    def find_site(self, site_slug: str) -> Any | None:
        with self._lock:
            if site_slug in self._sites:
                self.hits += 1
                return self._sites[site_slug]
        site = self._inner.find_site(site_slug)
        with self._lock:
            # Only cache a hit: a missing site may be created later in the run
            # (SITE_POLICY=create), and caching the None would hide it.
            if site is not None:
                self._sites[site_slug] = site
            self.misses += 1
        return site

    def find_switch_device_by_mac(self, mac: str, hints: Any | None = None) -> Any | None:
        # Keyed on the MAC alone: hints are derived from it, so they can't vary
        # for a given MAC within a run.
        with self._lock:
            if mac in self._switches:
                self.hits += 1
                return self._switches[mac]
        device = self._inner.find_switch_device_by_mac(mac, hints)
        with self._lock:
            # Negative results are cached too: a switch absent from NetBox at
            # the start of a run won't appear mid-run, and re-querying for
            # every client behind that switch is exactly the cost we're here
            # to remove.
            self._switches[mac] = device
            self.misses += 1
        return device

    def find_interface_by_name_candidates(self, device: Any, candidates: list[str]) -> Any | None:
        """Resolve a switch port, fetching the switch's interfaces once.

        The uncached path costs one lookup per *candidate name* per client —
        with the default four templates and 48 clients on a switch, up to 192
        round-trips to answer 48 questions about one device. Instead we pull
        the device's interfaces in a single list call the first time we're
        asked about it, then match every later candidate in memory.
        """
        device_id = getattr(device, "id", None)
        by_name = self._interfaces_for(device, device_id)
        if by_name is None:
            # Inner gateway can't list interfaces; fall back to per-name lookup.
            key = (device_id, tuple(candidates))
            with self._lock:
                if key in self._switch_ports:
                    self.hits += 1
                    return self._switch_ports[key]
            iface = self._inner.find_interface_by_name_candidates(device, candidates)
            with self._lock:
                self._switch_ports[key] = iface
                self.misses += 1
            return iface

        for name in candidates:
            if name in by_name:
                return by_name[name]
        return None

    def _interfaces_for(self, device: Any, device_id: Any) -> dict[str, Any] | None:
        with self._lock:
            if device_id in self._device_interfaces:
                self.hits += 1
                return self._device_interfaces[device_id]
        lister = getattr(self._inner, "list_device_interfaces", None)
        if lister is None:
            return None
        by_name = {iface.name: iface for iface in lister(device)}
        with self._lock:
            self._device_interfaces[device_id] = by_name
            self.misses += 1
        return by_name

    # -- pass-through ----------------------------------------------------

    def ensure_site(self, site_slug: str, policy: str) -> tuple[Any, bool]:
        site, created = self._inner.ensure_site(site_slug, policy)
        with self._lock:
            self._sites[site_slug] = site
        return site, created

    def find_client_device_by_mac(self, mac: str) -> Any | None:
        return self._inner.find_client_device_by_mac(mac)

    def device_name_taken_by_other(self, name: str, site_slug: str, mac: str) -> bool:
        return self._inner.device_name_taken_by_other(name, site_slug, mac)

    def upsert_client_device(
        self,
        mac: str,
        name: str,
        site_slug: str,
        role_slug: str,
        device_type_slug: str,
        tag_slug: str,
        update_policy: str = "sync",
        existing: Any | None = None,
        existing_looked_up: bool = False,
    ) -> tuple[Any, bool, bool, bool]:
        return self._inner.upsert_client_device(
            mac,
            name,
            site_slug,
            role_slug,
            device_type_slug,
            tag_slug,
            update_policy,
            existing,
            existing_looked_up,
        )

    def ensure_interface(self, device: Any, name: str, wired: bool) -> Any:
        return self._inner.ensure_interface(device, name, wired)

    def list_device_interfaces(self, device: Any) -> list[Any]:
        return self._inner.list_device_interfaces(device)

    def forget_switch(self, mac: str) -> None:
        """Drop a cached switch lookup, after creating the switch mid-run.

        The cache stores negative results deliberately (see
        find_switch_device_by_mac), which would otherwise keep a
        just-created switch invisible for the rest of the run."""
        with self._lock:
            self._switches.pop(mac, None)

    def ensure_device_type_from_spec(self, spec: Any) -> str:
        return self._inner.ensure_device_type_from_spec(spec)

    def ensure_device_role(self, role_slug: str) -> None:
        self._inner.ensure_device_role(role_slug)

    def upsert_infrastructure_device(self, *args: Any, **kwargs: Any) -> tuple[Any, bool, bool]:
        return self._inner.upsert_infrastructure_device(*args, **kwargs)

    def ensure_client_device_type(self, manufacturer_name: str) -> str:
        # Already memoized inside PynetboxGateway, which is where the
        # create-if-missing has to be atomic anyway.
        return self._inner.ensure_client_device_type(manufacturer_name)

    def assign_ip(self, device: Any, interface: Any, ip: str, status: str = "active") -> bool:
        return self._inner.assign_ip(device, interface, ip, status)

    def ensure_cable(self, interface_a: Any, interface_b: Any, conflict_policy: str) -> tuple[bool, str | None]:
        return self._inner.ensure_cable(interface_a, interface_b, conflict_policy)

    def list_synced_client_devices(self, tag_slug: str, site_slug: str) -> list[Any]:
        return self._inner.list_synced_client_devices(tag_slug, site_slug)

    def mark_offline(self, device: Any) -> None:
        self._inner.mark_offline(device)

    def log_stats(self) -> None:
        total = self.hits + self.misses
        if total:
            logger.info(
                "NetBox lookup cache: %d hits, %d misses (%.0f%% avoided)",
                self.hits,
                self.misses,
                100.0 * self.hits / total,
            )
