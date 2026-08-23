from __future__ import annotations

import logging
from typing import Any, Protocol

import pynetbox

logger = logging.getLogger(__name__)

CLIENT_INTERFACE_NAME = "eth0"


class NetboxGateway(Protocol):
    """Interface the sync engine depends on. Lets tests substitute a fake."""

    def ensure_prerequisites(
        self, role_slug: str, manufacturer_slug: str, device_type_slug: str, tag_slug: str
    ) -> None: ...

    def find_switch_device_by_mac(self, mac: str) -> Any | None: ...

    def find_interface_by_name_candidates(self, device: Any, candidates: list[str]) -> Any | None: ...

    def find_client_device_by_mac(self, mac: str) -> Any | None: ...

    def device_name_taken_by_other(self, name: str, site_slug: str, mac: str) -> bool: ...

    def find_site(self, site_slug: str) -> Any | None: ...

    def ensure_site(self, site_slug: str, policy: str) -> tuple[Any, bool]: ...

    def upsert_client_device(
        self,
        mac: str,
        name: str,
        site_slug: str,
        role_slug: str,
        device_type_slug: str,
        tag_slug: str,
        update_policy: str = "sync",
    ) -> tuple[Any, bool, bool]: ...

    def ensure_interface(self, device: Any, name: str, wired: bool) -> Any: ...

    def assign_ip(self, device: Any, interface: Any, ip: str, status: str) -> bool: ...

    def ensure_cable(self, interface_a: Any, interface_b: Any, conflict_policy: str) -> tuple[bool, str | None]: ...

    def list_synced_client_devices(self, tag_slug: str, site_slug: str) -> list[Any]: ...

    def mark_offline(self, device: Any) -> None: ...


class PynetboxGateway:
    """NetBox gateway backed by the real pynetbox API client."""

    def __init__(self, url: str, token: str, verify_ssl: bool = True) -> None:
        self.api = pynetbox.api(url, token=token)
        self.api.http_session.verify = verify_ssl

    # -- setup -----------------------------------------------------------

    def ensure_prerequisites(
        self, role_slug: str, manufacturer_slug: str, device_type_slug: str, tag_slug: str
    ) -> None:
        self.api.extras.tags.get(slug=tag_slug) or self.api.extras.tags.create(
            name=tag_slug, slug=tag_slug, color="2196f3"
        )

        if not self.api.extras.custom_fields.get(name="unifi_mac"):
            self.api.extras.custom_fields.create(
                object_types=["dcim.device"],
                type="text",
                name="unifi_mac",
                label="UniFi MAC",
                filter_logic="exact",
            )

        manufacturer = self.api.dcim.manufacturers.get(slug=manufacturer_slug)
        if not manufacturer:
            manufacturer = self.api.dcim.manufacturers.create(
                name=manufacturer_slug.replace("-", " ").title(), slug=manufacturer_slug
            )

        if not self.api.dcim.device_types.get(slug=device_type_slug):
            self.api.dcim.device_types.create(
                manufacturer=manufacturer.id,
                model=device_type_slug.replace("-", " ").title(),
                slug=device_type_slug,
                u_height=0,
                is_full_depth=False,
            )

        self._ensure_device_role(role_slug)

    def _ensure_device_role(self, role_slug: str) -> None:
        existing = self.api.dcim.device_roles.get(slug=role_slug)
        if existing:
            return
        self.api.dcim.device_roles.create(
            name=role_slug.replace("-", " ").title(), slug=role_slug, color="9e9e9e"
        )

    # -- lookups -----------------------------------------------------------

    def find_switch_device_by_mac(self, mac: str) -> Any | None:
        matches = list(self.api.dcim.interfaces.filter(mac_address=mac))
        if not matches:
            logger.debug("No NetBox interface found with mac_address=%s", mac)
            return None
        iface = matches[0]
        if len(matches) > 1:
            logger.warning(
                "MAC %s matched %d NetBox interfaces (%s); using %s:%s",
                mac,
                len(matches),
                ", ".join(f"{m.device.name}:{m.name}" for m in matches),
                iface.device.name,
                iface.name,
            )
        else:
            logger.info("Matched switch MAC %s to NetBox interface %s:%s", mac, iface.device.name, iface.name)
        return self.api.dcim.devices.get(iface.device.id)

    def find_interface_by_name_candidates(self, device: Any, candidates: list[str]) -> Any | None:
        for name in candidates:
            iface = self.api.dcim.interfaces.get(device_id=device.id, name=name)
            if iface:
                logger.info(
                    "Matched switch port on %s using name candidate %r -> interface %s",
                    device.name,
                    name,
                    iface.name,
                )
                return iface
        return None

    def find_client_device_by_mac(self, mac: str) -> Any | None:
        return next(iter(self.api.dcim.devices.filter(cf_unifi_mac=mac)), None)

    def device_name_taken_by_other(self, name: str, site_slug: str, mac: str) -> bool:
        existing = next(iter(self.api.dcim.devices.filter(name=name, site=site_slug)), None)
        if existing is None:
            return False
        return (existing.custom_fields or {}).get("unifi_mac") != mac

    def list_synced_client_devices(self, tag_slug: str, site_slug: str) -> list[Any]:
        # Scoped to one NetBox site: with multiple sites synced in a single
        # run, an unscoped query here would see every site's tagged devices
        # and could mark a device stale based on another site's client list.
        return list(self.api.dcim.devices.filter(tag=tag_slug, site=site_slug))

    def find_site(self, site_slug: str) -> Any | None:
        return self.api.dcim.sites.get(slug=site_slug)

    # -- mutations -----------------------------------------------------------

    def ensure_site(self, site_slug: str, policy: str) -> tuple[Any, bool]:
        """Resolve the NetBox site, honoring SITE_POLICY.

        "create" makes a minimal site if missing; any other value (the
        default, "require") raises. Either way, an *existing* site's
        attributes are never modified — this only ever creates, never
        updates.
        """
        site = self.find_site(site_slug)
        if site is not None:
            return site, False
        if policy != "create":
            raise LookupError(
                f"NetBox site '{site_slug}' does not exist (SITE_POLICY={policy!r}); "
                "set SITE_POLICY=create to let this tool create it, or create it yourself"
            )
        site = self.api.dcim.sites.create(
            name=site_slug.replace("-", " ").title(), slug=site_slug, status="active"
        )
        logger.info("Created NetBox site %r (SITE_POLICY=create)", site_slug)
        return site, True

    def upsert_client_device(
        self,
        mac: str,
        name: str,
        site_slug: str,
        role_slug: str,
        device_type_slug: str,
        tag_slug: str,
        update_policy: str = "sync",
    ) -> tuple[Any, bool, bool]:
        """Returns (device, created, update_skipped_by_policy)."""
        device = self.find_client_device_by_mac(mac)
        device_type = self.api.dcim.device_types.get(slug=device_type_slug)
        role = self.api.dcim.device_roles.get(slug=role_slug)
        site = self.find_site(site_slug)
        if site is None:
            # Defensive: SyncEngine.run() already resolves the site via
            # ensure_site() before any client is synced, so this shouldn't
            # be reachable in normal use.
            raise LookupError(f"NetBox site '{site_slug}' does not exist")

        if device is None:
            device = self.api.dcim.devices.create(
                name=name,
                device_type=device_type.id,
                role=role.id,
                site=site.id,
                status="active",
                custom_fields={"unifi_mac": mac},
                tags=[{"slug": tag_slug}],
            )
            return device, True, False

        would_change = device.name != name or device.status.value != "active"
        if update_policy == "create-only":
            return device, False, would_change

        updated = False
        if device.name != name:
            device.name = name
            updated = True
        if device.status.value != "active":
            device.status = "active"
            updated = True
        if updated:
            device.save()
        return device, False, False

    def ensure_interface(self, device: Any, name: str, wired: bool) -> Any:
        iface = self.api.dcim.interfaces.get(device_id=device.id, name=name)
        iface_type = "1000base-t" if wired else "ieee802.11ac"
        if iface is None:
            return self.api.dcim.interfaces.create(device=device.id, name=name, type=iface_type)
        if iface.type.value != iface_type:
            iface.type = iface_type
            iface.save()
        return iface

    def assign_ip(self, device: Any, interface: Any, ip: str, status: str = "active") -> bool:
        # NetBox has no signal for the client's real prefix length, VRF, or
        # tenant, so addresses land as bare /32s in the global table. If your
        # environment uses VRFs/tenants, extend this to look them up (e.g.
        # from the client's UniFi network/VLAN) before creating the IP.
        cidr = ip if "/" in ip else f"{ip}/32"
        ip_obj = next(iter(self.api.ipam.ip_addresses.filter(address=ip)), None)
        changed = False
        if ip_obj is None:
            ip_obj = self.api.ipam.ip_addresses.create(
                address=cidr,
                assigned_object_type="dcim.interface",
                assigned_object_id=interface.id,
                status=status,
            )
            changed = True
        elif not ip_obj.assigned_object or ip_obj.assigned_object.id != interface.id:
            ip_obj.assigned_object_type = "dcim.interface"
            ip_obj.assigned_object_id = interface.id
            ip_obj.save()
            changed = True

        if not device.primary_ip4 or device.primary_ip4.id != ip_obj.id:
            device.primary_ip4 = ip_obj.id
            device.save()
            changed = True
        return changed

    def ensure_cable(self, interface_a: Any, interface_b: Any, conflict_policy: str) -> tuple[bool, str | None]:
        interface_a = self.api.dcim.interfaces.get(interface_a.id)
        if interface_a.cable:
            peer = next(iter(interface_a.link_peers or []), None)
            if peer and peer.id == interface_b.id:
                return False, None
            if conflict_policy != "replace":
                return False, (
                    f"interface {interface_a.device.name}:{interface_a.name} is already cabled "
                    "to something else"
                )
            self.api.dcim.cables.get(interface_a.cable.id).delete()

        interface_b = self.api.dcim.interfaces.get(interface_b.id)
        if interface_b.cable:
            if conflict_policy != "replace":
                return False, (
                    f"interface {interface_b.device.name}:{interface_b.name} is already cabled "
                    "to something else"
                )
            self.api.dcim.cables.get(interface_b.cable.id).delete()

        self.api.dcim.cables.create(
            a_terminations=[{"object_type": "dcim.interface", "object_id": interface_a.id}],
            b_terminations=[{"object_type": "dcim.interface", "object_id": interface_b.id}],
            status="connected",
        )
        return True, None

    def mark_offline(self, device: Any) -> None:
        device.status = "offline"
        device.save()
