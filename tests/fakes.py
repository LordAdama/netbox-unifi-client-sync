from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field
from itertools import count

from unifi_netbox_sync.models import UnifiClient, UnifiKnownClient, UnifiSite, UnifiSwitchDevice

_id_counter = count(1)


class Choice:
    def __init__(self, value: str) -> None:
        self.value = value


class FakeDeviceRef:
    def __init__(self, id: int, name: str) -> None:
        self.id = id
        self.name = name


class FakeInterface:
    def __init__(self, device: "FakeDevice", name: str, wired: bool = True) -> None:
        self.id = next(_id_counter)
        self.device = FakeDeviceRef(device.id, device.name)
        self.name = name
        self.type = Choice("1000base-t" if wired else "ieee802.11ac")
        self.cable = None
        self.link_peers: list["FakeInterface"] = []


class FakeDevice:
    def __init__(self, name: str, mac: str | None = None, site_slug: str | None = None) -> None:
        self.id = next(_id_counter)
        self.name = name
        self.status = Choice("active")
        self.custom_fields = {"unifi_mac": mac} if mac else {}
        self.primary_ip4 = None
        self.tags: list[str] = []
        self.interfaces: dict[str, FakeInterface] = {}
        self.site_slug = site_slug
        self.serial: str | None = None
        self.device_type = None
        self.role_slug: str | None = None
        self.device_type_slug: str | None = None


class FakeSite:
    def __init__(self, slug: str, name: str | None = None) -> None:
        self.id = next(_id_counter)
        self.slug = slug
        self.name = name or slug
        self.custom_fields: dict[str, str] = {}

    def save(self) -> None:
        return None


class FakeIP:
    """Mirrors the shape the real gateway sets on device.primary_ip4."""

    def __init__(self, address: str) -> None:
        self.id = next(_id_counter)
        self.address = address if "/" in address else f"{address}/32"


class _NoSpec:
    interfaces: list = []


class FakeNetboxGateway:
    """In-memory double for NetboxGateway used in unit tests."""

    def __init__(self) -> None:
        # Real NetBox is a server: concurrent callers are its problem, not the
        # client's. This double keeps state in a dict, so it needs its own lock
        # to stay honest under parallel site workers — without it, a reader
        # iterating clients_by_mac while another worker inserts raises, and
        # parallel tests would only pass by luck.
        self._lock = threading.RLock()
        self.switches: dict[str, FakeDevice] = {}
        self.switches_without_mac: list[FakeDevice] = []
        self.created_device_types: set[str] = set()
        self.device_type_specs: dict = {}
        self.created_roles: set[str] = set()
        self.infra_by_mac: dict[str, FakeDevice] = {}
        self.clients_by_mac: dict[str, FakeDevice] = {}
        self.cables: set[tuple[int, int]] = set()
        self.ensure_prerequisites_called = False
        # By default a site "exists" for any slug a test uses; add a slug
        # here to simulate it being absent until ensure_site() creates it.
        self.missing_sites: set[str] = set()
        self.created_sites: set[str] = set()
        # Sites that genuinely exist, with real names. Tests that don't
        # seed any keep the permissive default below.
        self.existing_sites: dict[str, FakeSite] = {}
        self.cable_tags: dict[tuple[int, int], list[str]] = {}
        self.site_identity_field_ready = False

    def seed_switch(self, mac: str, name: str, ports: list[str]) -> FakeDevice:
        device = FakeDevice(name=name)
        for port_name in ports:
            device.interfaces[port_name] = FakeInterface(device, port_name)
        self.switches[mac] = device
        return device

    # -- NetboxGateway protocol -----------------------------------------

    def ensure_prerequisites(self, role_slug, manufacturer_slug, device_type_slug, tag_slug) -> None:
        self.ensure_prerequisites_called = True

    def find_switch_device_by_mac(self, mac: str, hints=None):
        device = self.switches.get(mac)
        if device is not None:
            return device
        # Mirror the real gateway's name/serial fallbacks, which are the only
        # way to reach a switch whose interfaces carry no MAC address.
        if hints is not None:
            for candidate in list(self.switches.values()) + self.switches_without_mac:
                if hints.name and candidate.name == hints.name:
                    return candidate
                if hints.serial and candidate.serial == hints.serial:
                    return candidate
        return None

    def seed_unmatchable_switch(self, name: str, ports: list[str], serial: str | None = None):
        """A switch present in NetBox but NOT findable by interface MAC — the
        common real-world case (switch ports rarely have mac_address set)."""
        device = FakeDevice(name=name)
        device.serial = serial
        for port_name in ports:
            device.interfaces[port_name] = FakeInterface(device, port_name)
        self.switches_without_mac.append(device)
        return device

    def ensure_client_device_type(self, manufacturer_name: str) -> str:
        from unifi_netbox_sync.naming import slugify

        slug = slugify(manufacturer_name)
        if not slug:
            raise ValueError(f"unusable manufacturer {manufacturer_name!r}")
        type_slug = f"{slug}-client"
        with self._lock:
            self.created_device_types.add(type_slug)
        return type_slug

    def ensure_device_type_from_spec(self, spec):
        with self._lock:
            self.created_device_types.add(spec.slug)
            self.device_type_specs[spec.slug] = spec
        return spec.slug

    def ensure_device_role(self, role_slug: str) -> None:
        with self._lock:
            self.created_roles.add(role_slug)

    def upsert_infrastructure_device(self, mac, name, site_slug, role_slug, device_type_slug,
                                     tag_slug, serial="", update_policy="sync"):
        with self._lock:
            existing = self.infra_by_mac.get(mac)
            if existing is None and name:
                for candidate in list(self.infra_by_mac.values()) + self.switches_without_mac:
                    if candidate.name == name and candidate.site_slug in (site_slug, None):
                        existing = candidate
                        break
            if existing is not None and tag_slug not in existing.tags:
                return existing, False, False
            if existing is not None:
                if update_policy == "create-only":
                    return existing, False, False
                updated = existing.name != name
                existing.name = name
                return existing, False, updated
            device = FakeDevice(name=name, mac=mac, site_slug=site_slug)
            device.serial = serial
            device.role_slug = role_slug
            device.device_type_slug = device_type_slug
            device.tags.append(tag_slug)
            # NetBox instantiates interfaces from the device type's templates.
            for iface_name, _t in self.device_type_specs.get(device_type_slug, _NoSpec()).interfaces:
                device.interfaces[iface_name] = FakeInterface(device, iface_name)
            self.infra_by_mac[mac] = device
            # Findable by the cable code exactly like a hand-created switch.
            self.switches[mac] = device
            return device, True, False

    def forget_switch(self, mac: str) -> None:
        pass

    def find_interface_by_name_candidates(self, device: FakeDevice, candidates: list[str]):
        for name in candidates:
            if name in device.interfaces:
                return device.interfaces[name]
        return None

    def list_device_interfaces(self, device: FakeDevice):
        return list(device.interfaces.values())

    def find_client_device_by_mac(self, mac: str):
        with self._lock:
            return self.clients_by_mac.get(mac)

    def device_name_taken_by_other(self, name: str, site_slug: str, mac: str) -> bool:
        # Real NetBox 400s on a filter naming a site that doesn't exist, rather
        # than returning an empty list. Mirrored here so the fake can't pass
        # where production would fail.
        if self.find_site(site_slug) is None:
            return False
        with self._lock:
            entries = list(self.clients_by_mac.items())
        return any(
            device.name == name and device.site_slug == site_slug
            for other_mac, device in entries
            if other_mac != mac
        )

    def seed_site(self, slug: str, name: str | None = None, unifi_site_id: str = "") -> FakeSite:
        site = FakeSite(slug, name)
        if unifi_site_id:
            site.custom_fields["unifi_site_id"] = unifi_site_id
        self.existing_sites[slug] = site
        return site

    def find_site(self, site_slug: str):
        if site_slug in self.existing_sites:
            return self.existing_sites[site_slug]
        if site_slug in self.missing_sites and site_slug not in self.created_sites:
            return None
        if self.existing_sites and site_slug not in self.created_sites:
            # Once a test seeds sites explicitly, only those exist.
            return None
        return FakeSite(site_slug)

    def list_sites(self):
        return list(self.existing_sites.values())

    def resolve_existing_site(self, desired_slug: str, label: str, mode: str = "normalized", unifi_site_id: str = ""):
        from unifi_netbox_sync.naming import normalize_key
        from unifi_netbox_sync.netbox_client import site_unifi_id

        if unifi_site_id:
            holders = [site for site in self.list_sites() if site_unifi_id(site) == unifi_site_id]
            if len(holders) == 1:
                return holders[0]
            if len(holders) > 1:
                return None

        def allowed(site) -> bool:
            current = site_unifi_id(site)
            return not current or not unifi_site_id or current == unifi_site_id

        exact = self.find_site(desired_slug)
        if exact is not None and allowed(exact):
            return exact
        if mode == "slug":
            return None
        for site in self.list_sites():
            if label and site.name == label and allowed(site):
                return site
        if mode != "normalized":
            return None
        wanted = {normalize_key(desired_slug), normalize_key(label)} - {""}
        for site in self.list_sites():
            if not allowed(site):
                continue
            if {normalize_key(site.slug), normalize_key(site.name)} & wanted:
                return site
        return None

    def ensure_site_identity_field(self) -> None:
        self.site_identity_field_ready = True

    def remember_site_link(self, site, unifi_site_id: str) -> bool:
        from unifi_netbox_sync.netbox_client import site_unifi_id as stored_id

        if not unifi_site_id:
            return False
        current = stored_id(site)
        if current:
            return False
        for other in self.list_sites():
            if other.id == site.id:
                continue
            if stored_id(other) == unifi_site_id:
                return False
        site.custom_fields["unifi_site_id"] = unifi_site_id
        return True

    def ensure_site(self, site_slug: str, policy: str, name: str = "", unifi_site_id: str = ""):
        with self._lock:
            site = self.find_site(site_slug)
            if site is not None:
                return site, False
            if policy != "create":
                raise LookupError(f"NetBox site '{site_slug}' does not exist (SITE_POLICY={policy!r})")
            created = FakeSite(site_slug, name or site_slug.replace("-", " ").title())
            if unifi_site_id:
                created.custom_fields["unifi_site_id"] = unifi_site_id
            self.existing_sites[site_slug] = created
            self.created_sites.add(site_slug)
            return created, True

    def upsert_client_device(
        self,
        mac,
        name,
        site_slug,
        role_slug,
        device_type_slug,
        tag_slug,
        update_policy="sync",
        existing=None,
        existing_looked_up=False,
    ):
        with self._lock:
            existing = existing if existing_looked_up else self.clients_by_mac.get(mac)
            if existing:
                if tag_slug not in existing.tags:
                    return existing, False, True, False
                would_change = existing.name != name or existing.status.value != "active"
                if update_policy == "create-only":
                    return existing, False, would_change, False
                updated = False
                if existing.name != name:
                    existing.name = name
                    updated = True
                if existing.status.value != "active":
                    existing.status = Choice("active")
                    updated = True
                return existing, False, False, updated
            device = FakeDevice(name=name, mac=mac, site_slug=site_slug)
            device.role_slug = role_slug
            device.tags.append(tag_slug)
            self.clients_by_mac[mac] = device
            return device, True, False, False

    def ensure_interface(self, device: FakeDevice, name: str, wired: bool):
        with self._lock:
            iface = device.interfaces.get(name)
            if iface is None:
                iface = FakeInterface(device, name, wired=wired)
                device.interfaces[name] = iface
            return iface

    def assign_ip(
        self,
        device: FakeDevice,
        interface: FakeInterface,
        ip: str,
        status: str = "active",
        managed_tag: str | None = None,
    ) -> bool:
        current = getattr(device.primary_ip4, "address", None)
        if current and current.split("/")[0] == ip.split("/")[0]:
            return False
        device.primary_ip4 = FakeIP(ip)
        return True

    def ensure_cable(
        self,
        interface_a: FakeInterface,
        interface_b: FakeInterface,
        conflict_policy: str,
        tag_slug: str = "unifi-sync",
    ):
        with self._lock:
            pair = tuple(sorted((interface_a.id, interface_b.id)))
            if interface_a.cable == pair or interface_b.cable == pair:
                return False, None

            doomed: list[tuple[int, int]] = []
            for iface in (interface_a, interface_b):
                existing = iface.cable
                if not existing or existing == pair:
                    continue
                if conflict_policy != "replace":
                    return False, "interface already cabled elsewhere"
                if tag_slug not in self.cable_tags.get(existing, []):
                    return False, f"cable is not tagged {tag_slug}"
                doomed.append(existing)
            for existing in doomed:
                self.cables.discard(existing)
                self.cable_tags.pop(existing, None)
            interface_a.cable = pair
            interface_b.cable = pair
            interface_a.link_peers = [interface_b]
            interface_b.link_peers = [interface_a]
            self.cables.add(pair)
            self.cable_tags[pair] = [tag_slug]
        return True, None

    def list_synced_client_devices(self, tag_slug: str, site_slug: str, role_slug: str | None = None):
        if self.find_site(site_slug) is None:
            return []
        with self._lock:
            devices = list(self.clients_by_mac.values()) + list(self.infra_by_mac.values())
        selected = []
        for device in devices:
            if tag_slug not in device.tags or device.site_slug != site_slug:
                continue
            if role_slug is not None and device.role_slug != role_slug:
                continue
            selected.append(device)
        return selected

    def mark_offline(self, device: FakeDevice) -> None:
        device.status = Choice("offline")


class CountingNetboxGateway:
    """Wraps a gateway and counts calls per method.

    Stands in for "number of NetBox API round-trips" in tests: every method
    here is one or more HTTP calls against a real NetBox, so holding these
    counts down is the whole point of the caching and no-op work.
    """

    _METHODS = (
        "ensure_prerequisites",
        "find_switch_device_by_mac",
        "find_interface_by_name_candidates",
        "list_device_interfaces",
        "find_client_device_by_mac",
        "device_name_taken_by_other",
        "find_site",
        "ensure_site",
        "upsert_client_device",
        "ensure_interface",
        "assign_ip",
        "ensure_cable",
        "list_synced_client_devices",
        "mark_offline",
    )

    def __init__(self, inner) -> None:
        self._inner = inner
        self.calls: dict[str, int] = dict.fromkeys(self._METHODS, 0)

    def __getattr__(self, name):
        attr = getattr(self._inner, name)
        if name not in self._METHODS:
            return attr

        def counted(*args, **kwargs):
            self.calls[name] += 1
            return attr(*args, **kwargs)

        return counted

    @property
    def total(self) -> int:
        return sum(self.calls.values())


def make_unifi_client(
    mac: str,
    name: str = "",
    ip: str | None = "10.0.0.5",
    is_wired: bool = True,
    switch_mac: str | None = None,
    switch_port: int | None = None,
    oui: str | None = None,
) -> UnifiClient:
    return UnifiClient(
        mac=mac,
        name=name,
        ip=ip,
        is_wired=is_wired,
        switch_mac=switch_mac,
        switch_port=switch_port,
        oui=oui,
    )


class FakeUnifiClient:
    def __init__(
        self,
        clients: list[UnifiClient] | None = None,
        devices: list[UnifiSwitchDevice] | None = None,
        by_site: dict[str, list[UnifiClient]] | None = None,
        devices_by_site: dict[str, list[UnifiSwitchDevice]] | None = None,
        sites: list[UnifiSite] | None = None,
        known: list[UnifiKnownClient] | None = None,
        known_by_site: dict[str, list[UnifiKnownClient]] | None = None,
        known_error: BaseException | None = None,
    ) -> None:
        self._clients = clients or []
        self._devices = devices or []
        self._by_site = by_site
        self._devices_by_site = devices_by_site
        self._sites = sites
        # None means "derive from the active clients, seen just now". An
        # explicit list, including [], is what the controller returned.
        self._known = known
        self._known_by_site = known_by_site
        self._known_error = known_error

    def get_sites(self) -> list[UnifiSite]:
        if self._sites is not None:
            return self._sites
        # Default: infer from whatever per-site client data the test supplied.
        return [UnifiSite(name=name) for name in (self._by_site or {})]

    def get_clients(self, site: str) -> list[UnifiClient]:
        if self._by_site is not None:
            return self._by_site.get(site, [])
        return self._clients

    def get_known_clients(self, site: str) -> list[UnifiKnownClient]:
        if self._known_error is not None:
            raise self._known_error
        if self._known_by_site is not None:
            return list(self._known_by_site.get(site, []))
        if self._known is not None:
            return list(self._known)
        now = time.time()
        return [UnifiKnownClient(mac=client.mac, last_seen=now) for client in self.get_clients(site)]

    def get_devices(self, site: str) -> list[UnifiSwitchDevice]:
        if self._devices_by_site is not None:
            return self._devices_by_site.get(site, [])
        return self._devices
