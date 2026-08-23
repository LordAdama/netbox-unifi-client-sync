from __future__ import annotations

import threading
from dataclasses import dataclass, field
from itertools import count

from unifi_netbox_sync.models import UnifiClient, UnifiSwitchDevice

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


class FakeSite:
    def __init__(self, slug: str) -> None:
        self.id = next(_id_counter)
        self.slug = slug
        self.name = slug


class FakeIP:
    """Mirrors the shape the real gateway sets on device.primary_ip4."""

    def __init__(self, address: str) -> None:
        self.id = next(_id_counter)
        self.address = address if "/" in address else f"{address}/32"


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
        self.clients_by_mac: dict[str, FakeDevice] = {}
        self.cables: set[tuple[int, int]] = set()
        self.ensure_prerequisites_called = False
        # By default a site "exists" for any slug a test uses; add a slug
        # here to simulate it being absent until ensure_site() creates it.
        self.missing_sites: set[str] = set()
        self.created_sites: set[str] = set()

    def seed_switch(self, mac: str, name: str, ports: list[str]) -> FakeDevice:
        device = FakeDevice(name=name)
        for port_name in ports:
            device.interfaces[port_name] = FakeInterface(device, port_name)
        self.switches[mac] = device
        return device

    # -- NetboxGateway protocol -----------------------------------------

    def ensure_prerequisites(self, role_slug, manufacturer_slug, device_type_slug, tag_slug) -> None:
        self.ensure_prerequisites_called = True

    def find_switch_device_by_mac(self, mac: str):
        return self.switches.get(mac)

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
        with self._lock:
            entries = list(self.clients_by_mac.items())
        return any(
            device.name == name and device.site_slug == site_slug
            for other_mac, device in entries
            if other_mac != mac
        )

    def find_site(self, site_slug: str):
        if site_slug in self.missing_sites and site_slug not in self.created_sites:
            return None
        return FakeSite(site_slug)

    def ensure_site(self, site_slug: str, policy: str):
        with self._lock:
            site = self.find_site(site_slug)
            if site is not None:
                return site, False
            if policy != "create":
                raise LookupError(f"NetBox site '{site_slug}' does not exist (SITE_POLICY={policy!r})")
            self.created_sites.add(site_slug)
            return self.find_site(site_slug), True

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

    def assign_ip(self, device: FakeDevice, interface: FakeInterface, ip: str, status: str = "active") -> bool:
        current = getattr(device.primary_ip4, "address", None)
        if current and current.split("/")[0] == ip.split("/")[0]:
            return False
        device.primary_ip4 = FakeIP(ip)
        return True

    def ensure_cable(self, interface_a: FakeInterface, interface_b: FakeInterface, conflict_policy: str):
        with self._lock:
            pair = tuple(sorted((interface_a.id, interface_b.id)))
            if interface_a.cable or interface_b.cable:
                if pair in self.cables:
                    return False, None
                if conflict_policy != "replace":
                    return False, "interface already cabled elsewhere"
            interface_a.cable = pair
            interface_b.cable = pair
            interface_a.link_peers = [interface_b]
            interface_b.link_peers = [interface_a]
            self.cables.add(pair)
        return True, None

    def list_synced_client_devices(self, tag_slug: str, site_slug: str):
        with self._lock:
            devices = list(self.clients_by_mac.values())
        return [d for d in devices if tag_slug in d.tags and d.site_slug == site_slug]

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
) -> UnifiClient:
    return UnifiClient(
        mac=mac,
        name=name,
        ip=ip,
        is_wired=is_wired,
        switch_mac=switch_mac,
        switch_port=switch_port,
    )


class FakeUnifiClient:
    def __init__(
        self,
        clients: list[UnifiClient] | None = None,
        devices: list[UnifiSwitchDevice] | None = None,
        by_site: dict[str, list[UnifiClient]] | None = None,
    ) -> None:
        self._clients = clients or []
        self._devices = devices or []
        self._by_site = by_site

    def get_clients(self, site: str) -> list[UnifiClient]:
        if self._by_site is not None:
            return self._by_site.get(site, [])
        return self._clients

    def get_devices(self, site: str) -> list[UnifiSwitchDevice]:
        return self._devices
