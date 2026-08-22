from __future__ import annotations

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
    def __init__(self, name: str, mac: str | None = None) -> None:
        self.id = next(_id_counter)
        self.name = name
        self.status = Choice("active")
        self.custom_fields = {"unifi_mac": mac} if mac else {}
        self.primary_ip4 = None
        self.tags: list[str] = []
        self.interfaces: dict[str, FakeInterface] = {}


class FakeSite:
    def __init__(self, slug: str) -> None:
        self.id = next(_id_counter)
        self.slug = slug
        self.name = slug


class FakeNetboxGateway:
    """In-memory double for NetboxGateway used in unit tests."""

    def __init__(self) -> None:
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

    def find_client_device_by_mac(self, mac: str):
        return self.clients_by_mac.get(mac)

    def device_name_taken_by_other(self, name: str, site_slug: str, mac: str) -> bool:
        return any(device.name == name for other_mac, device in self.clients_by_mac.items() if other_mac != mac)

    def find_site(self, site_slug: str):
        if site_slug in self.missing_sites and site_slug not in self.created_sites:
            return None
        return FakeSite(site_slug)

    def ensure_site(self, site_slug: str, policy: str):
        site = self.find_site(site_slug)
        if site is not None:
            return site, False
        if policy != "create":
            raise LookupError(f"NetBox site '{site_slug}' does not exist (SITE_POLICY={policy!r})")
        self.created_sites.add(site_slug)
        return self.find_site(site_slug), True

    def upsert_client_device(self, mac, name, site_slug, role_slug, device_type_slug, tag_slug, update_policy="sync"):
        existing = self.clients_by_mac.get(mac)
        if existing:
            would_change = existing.name != name
            if update_policy == "create-only":
                return existing, False, would_change
            existing.name = name
            return existing, False, False
        device = FakeDevice(name=name, mac=mac)
        device.tags.append(tag_slug)
        self.clients_by_mac[mac] = device
        return device, True, False

    def ensure_interface(self, device: FakeDevice, name: str, wired: bool):
        iface = device.interfaces.get(name)
        if iface is None:
            iface = FakeInterface(device, name, wired=wired)
            device.interfaces[name] = iface
        return iface

    def assign_ip(self, device: FakeDevice, interface: FakeInterface, ip: str, status: str = "active") -> bool:
        if device.primary_ip4 == ip:
            return False
        device.primary_ip4 = ip
        return True

    def ensure_cable(self, interface_a: FakeInterface, interface_b: FakeInterface, conflict_policy: str):
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

    def list_synced_client_devices(self, tag_slug: str):
        return [d for d in self.clients_by_mac.values() if tag_slug in d.tags]

    def mark_offline(self, device: FakeDevice) -> None:
        device.status = Choice("offline")


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
    def __init__(self, clients: list[UnifiClient], devices: list[UnifiSwitchDevice] | None = None) -> None:
        self._clients = clients
        self._devices = devices or []

    def get_clients(self, site: str) -> list[UnifiClient]:
        return self._clients

    def get_devices(self, site: str) -> list[UnifiSwitchDevice]:
        return self._devices
