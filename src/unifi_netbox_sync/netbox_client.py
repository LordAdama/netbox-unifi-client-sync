from __future__ import annotations

import logging
import threading
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, Protocol

import pynetbox

from .devicetype_library import normalize_model
from .naming import slugify

logger = logging.getLogger(__name__)

CLIENT_INTERFACE_NAME = "eth0"


@dataclass
class SwitchHints:
    """What UniFi knows about a switch, used to find it in NetBox when the
    interface-MAC join (which needs `mac_address` populated on switch ports)
    comes up empty — as it does in most deployments."""

    name: str | None = None
    serial: str | None = None
    model: str | None = None


class NetboxGateway(Protocol):
    """Interface the sync engine depends on. Lets tests substitute a fake."""

    def ensure_prerequisites(
        self, role_slug: str, manufacturer_slug: str, device_type_slug: str, tag_slug: str
    ) -> None: ...

    def find_switch_device_by_mac(self, mac: str, hints: SwitchHints | None = None) -> Any | None: ...

    def ensure_client_device_type(self, manufacturer_name: str) -> str: ...

    def ensure_device_type_from_spec(self, spec: Any) -> str: ...

    def ensure_device_role(self, role_slug: str) -> None: ...

    def upsert_infrastructure_device(
        self,
        mac: str,
        name: str,
        site_slug: str,
        role_slug: str,
        device_type_slug: str,
        tag_slug: str,
        serial: str = "",
        update_policy: str = "sync",
    ) -> tuple[Any, bool, bool]: ...

    def find_interface_by_name_candidates(self, device: Any, candidates: list[str]) -> Any | None: ...

    def find_client_device_by_mac(self, mac: str) -> Any | None: ...

    def device_name_taken_by_other(self, name: str, site_slug: str, mac: str) -> bool: ...
    """Is `name` used by a device other than `mac` in this site?

    Implementations must return False for a site that does not exist: an
    absent site holds no devices, so no name in it can be taken. NetBox's
    own filters answer an unknown site slug with a 400 rather than an empty
    list, so this has to be handled rather than passed through — it is the
    normal state under --dry-run with SITE_POLICY=create.
    """

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
        existing: Any | None = None,
        existing_looked_up: bool = False,
    ) -> tuple[Any, bool, bool, bool]: ...

    def ensure_interface(self, device: Any, name: str, wired: bool) -> Any: ...

    def list_device_interfaces(self, device: Any) -> list[Any]: ...

    def assign_ip(self, device: Any, interface: Any, ip: str, status: str) -> bool: ...

    def ensure_cable(self, interface_a: Any, interface_b: Any, conflict_policy: str) -> tuple[bool, str | None]: ...

    def list_synced_client_devices(self, tag_slug: str, site_slug: str) -> list[Any]: ...

    def mark_offline(self, device: Any) -> None: ...


class PynetboxGateway:
    """NetBox gateway backed by the real pynetbox API client."""

    def __init__(self, url: str, token: str, verify_ssl: bool = True) -> None:
        self.api = pynetbox.api(url, token=token)
        self.api.http_session.verify = verify_ssl
        # Per-run memo for objects that are constant for the whole run but are
        # looked up inside upsert_client_device (i.e. once per client, which
        # is pure waste). Not exposed on NetboxGateway, so CachingNetboxGateway
        # can't reach them — hence the local memo. Guarded by a lock because
        # parallel site workers share one gateway instance.
        self._memo_lock = threading.Lock()
        self._device_type_memo: dict[str, Any] = {}
        self._role_memo: dict[str, Any] = {}
        self._site_memo: dict[str, Any] = {}
        self._client_type_memo: dict[str, str] = {}
        self._infra_type_memo: dict[str, str] = {}
        self._site_missing: set[str] = set()
        self._warned: set[str] = set()

    def _memoized(self, memo: dict[str, Any], key: str, fetch: Callable[[], Any]) -> Any:
        with self._memo_lock:
            if key in memo:
                return memo[key]
        value = fetch()
        with self._memo_lock:
            memo[key] = value
        return value

    def _get_device_type(self, slug: str) -> Any:
        return self._memoized(
            self._device_type_memo, slug, lambda: self.api.dcim.device_types.get(slug=slug)
        )

    def _get_role(self, slug: str) -> Any:
        return self._memoized(self._role_memo, slug, lambda: self.api.dcim.device_roles.get(slug=slug))

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

    def find_switch_device_by_mac(self, mac: str, hints: SwitchHints | None = None) -> Any | None:
        """Locate the NetBox device for a UniFi switch.

        Matching on an *interface* MAC alone (the original strategy) fails in
        most real deployments: switch interfaces usually have no `mac_address`
        populated at all, and where they do, a port's MAC is not the chassis
        MAC that UniFi reports as `sw_mac`. So we try several joins in
        descending order of precision, and say plainly which one worked.
        """
        for strategy, finder in (
            ("interface MAC", lambda: self._switch_by_interface_mac(mac)),
            ("unifi_mac custom field", lambda: self._first(self.api.dcim.devices.filter(cf_unifi_mac=mac))),
            ("serial", lambda: self._switch_by_serial(mac, hints)),
            ("device name", lambda: self._switch_by_name(hints)),
        ):
            device = finder()
            if device is not None:
                logger.info("Matched UniFi switch %s to NetBox device %r via %s", mac, device.name, strategy)
                return device

        # The operator-facing warning is emitted by the sync engine, which
        # owns that guidance for every gateway implementation.
        logger.debug("No NetBox device matched UniFi switch %s by any strategy", mac)
        return None

    @staticmethod
    def _first(results: Any) -> Any | None:
        return next(iter(results), None)

    def _switch_by_interface_mac(self, mac: str) -> Any | None:
        matches = list(self.api.dcim.interfaces.filter(mac_address=mac))
        if not matches:
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
        return self.api.dcim.devices.get(iface.device.id)

    def _switch_by_serial(self, mac: str, hints: SwitchHints | None) -> Any | None:
        # UniFi imports commonly use the MAC as the serial, in either
        # colon-separated or bare form; the controller's own serial too.
        candidates = [mac, mac.replace(":", ""), mac.upper(), mac.replace(":", "").upper()]
        if hints and hints.serial:
            candidates.insert(0, hints.serial)
        for serial in candidates:
            device = self._first(self.api.dcim.devices.filter(serial=serial))
            if device is not None:
                return device
        return None

    def _switch_by_name(self, hints: SwitchHints | None) -> Any | None:
        if not hints or not hints.name:
            return None
        return self._first(self.api.dcim.devices.filter(name=hints.name))

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
        # NetBox validates ?site=<slug> against existing sites and answers a
        # unknown slug with 400 "Select a valid choice", not an empty list. A
        # site that doesn't exist holds no devices, so nothing can be taken —
        # and this is the normal case under --dry-run with SITE_POLICY=create,
        # where the sites would only have been created by a real run.
        if not self._site_exists(site_slug):
            return False
        try:
            existing = next(iter(self.api.dcim.devices.filter(name=name, site=site_slug)), None)
        except pynetbox.RequestError as exc:
            if getattr(exc, "req", None) is not None and exc.req.status_code == 400:
                # A validation error means this NetBox can't answer the query
                # at all (version differences in the device filters). Treat the
                # name as free: a genuine duplicate then fails loudly on create
                # for that one device, rather than every client dying here.
                self._warn_once(
                    f"name-filter-400:{site_slug}",
                    "NetBox rejected the device name-uniqueness filter for site '%s' (%s); "
                    "assuming names are free. A real collision will surface when the device "
                    "is created.",
                    site_slug,
                    exc,
                )
                return False
            raise
        if existing is None:
            return False
        return (existing.custom_fields or {}).get("unifi_mac") != mac

    def _site_exists(self, site_slug: str) -> bool:
        """Whether the site is present, with the negative result cached.

        find_site deliberately caches only hits, so a site that is missing
        would be re-queried for every client. Misses are tracked separately
        here and cleared by ensure_site when it creates one.
        """
        with self._memo_lock:
            if site_slug in self._site_memo:
                return True
            if site_slug in self._site_missing:
                return False
        exists = self.find_site(site_slug) is not None
        if not exists:
            with self._memo_lock:
                self._site_missing.add(site_slug)
        return exists

    def _warn_once(self, key: str, message: str, *args: Any) -> None:
        with self._memo_lock:
            if key in self._warned:
                return
            self._warned.add(key)
        logger.warning(message, *args)

    def list_synced_client_devices(self, tag_slug: str, site_slug: str) -> list[Any]:
        # Scoped to one NetBox site: with multiple sites synced in a single
        # run, an unscoped query here would see every site's tagged devices
        # and could mark a device stale based on another site's client list.
        # A site that doesn't exist holds nothing to mark stale, and filtering
        # on an unknown slug is a 400 rather than an empty list.
        if not self._site_exists(site_slug):
            return []
        return list(self.api.dcim.devices.filter(tag=tag_slug, site=site_slug))

    def find_site(self, site_slug: str) -> Any | None:
        # Memoized here, not only in CachingNetboxGateway: upsert_client_device
        # calls this internally (once per client), which bypasses the decorator
        # entirely. Only hits are cached — a site missing now may be created
        # later in the run under SITE_POLICY=create.
        with self._memo_lock:
            if site_slug in self._site_memo:
                return self._site_memo[site_slug]
        site = self.api.dcim.sites.get(slug=site_slug)
        if site is not None:
            with self._memo_lock:
                self._site_memo[site_slug] = site
        return site

    def list_device_interfaces(self, device: Any) -> list[Any]:
        return list(self.api.dcim.interfaces.filter(device_id=device.id))

    def ensure_device_type_from_spec(self, spec: Any) -> str:
        """Find or create a NetBox device type, with its interface templates.

        Matching is by slug, then by normalized part number, so a device type
        already imported from devicetype-library is reused rather than
        duplicated. Interface *templates* are used (not per-device
        interfaces): NetBox instantiates them automatically on every device of
        this type, which is both idiomatic and far fewer API calls.
        """
        with self._memo_lock:
            if spec.slug in self._infra_type_memo:
                return self._infra_type_memo[spec.slug]

        existing = self.api.dcim.device_types.get(slug=spec.slug)
        if existing is None and spec.part_number:
            wanted = normalize_model(spec.part_number)
            existing = next(
                (
                    dt
                    for dt in self.api.dcim.device_types.filter(manufacturer=slugify(spec.manufacturer))
                    if normalize_model(getattr(dt, "part_number", "") or "") == wanted
                ),
                None,
            )

        if existing is not None:
            resolved = existing.slug
            logger.info("Reusing NetBox device type %r for %s", resolved, spec.part_number or spec.model)
        else:
            manufacturer = self._ensure_manufacturer(spec.manufacturer)
            created = self.api.dcim.device_types.create(
                manufacturer=manufacturer.id,
                model=spec.model,
                slug=spec.slug,
                part_number=spec.part_number,
                u_height=spec.u_height,
                is_full_depth=spec.is_full_depth,
            )
            resolved = created.slug
            if spec.interfaces:
                self.api.dcim.interface_templates.create(
                    [
                        {"device_type": created.id, "name": name, "type": iface_type}
                        for name, iface_type in spec.interfaces
                    ]
                )
            logger.info(
                "Created NetBox device type %r with %d interface template(s)",
                spec.model,
                len(spec.interfaces),
            )

        with self._memo_lock:
            self._infra_type_memo[spec.slug] = resolved
        return resolved

    def _ensure_manufacturer(self, name: str) -> Any:
        slug = slugify(name)
        manufacturer = self.api.dcim.manufacturers.get(slug=slug)
        if manufacturer is None:
            manufacturer = self.api.dcim.manufacturers.create(name=name, slug=slug)
            logger.info("Created NetBox manufacturer %r", name)
        return manufacturer

    def upsert_infrastructure_device(
        self,
        mac: str,
        name: str,
        site_slug: str,
        role_slug: str,
        device_type_slug: str,
        tag_slug: str,
        serial: str = "",
        update_policy: str = "sync",
    ) -> tuple[Any, bool, bool]:
        """Create or update an adopted UniFi device. Returns (device, created, updated).

        The unifi_mac custom field is always set, which makes this device
        findable by the exact-match strategy the cable code tries second — so
        devices created here wire up reliably regardless of naming.
        """
        device = next(iter(self.api.dcim.devices.filter(cf_unifi_mac=mac)), None)
        if device is None and name:
            device = next(iter(self.api.dcim.devices.filter(name=name, site=site_slug)), None)
        if device is None and serial:
            device = next(iter(self.api.dcim.devices.filter(serial=serial)), None)

        site = self.find_site(site_slug)
        if site is None:
            raise LookupError(f"NetBox site '{site_slug}' does not exist")

        if device is None:
            device = self.api.dcim.devices.create(
                name=name,
                device_type=self._get_device_type(device_type_slug).id,
                role=self._get_role(role_slug).id,
                site=site.id,
                serial=serial,
                status="active",
                custom_fields={"unifi_mac": mac},
                tags=[{"slug": tag_slug}],
            )
            return device, True, False

        if update_policy == "create-only":
            return device, False, False

        updated = False
        if name and device.name != name:
            device.name = name
            updated = True
        if serial and (device.serial or "") != serial:
            device.serial = serial
            updated = True
        if (device.custom_fields or {}).get("unifi_mac") != mac:
            device.custom_fields = {**(device.custom_fields or {}), "unifi_mac": mac}
            updated = True
        if updated:
            device.save()
        return device, False, updated

    def ensure_device_role(self, role_slug: str) -> None:
        self._ensure_device_role(role_slug)

    def ensure_client_device_type(self, manufacturer_name: str) -> str:
        """Get (creating if needed) a client device type for this manufacturer.

        NetBox models the manufacturer on the *device type*, not the device, so
        showing a client's real vendor means one zero-U device type per vendor.
        Memoized: a run with 500 Apple clients creates this once.
        """
        slug = slugify(manufacturer_name)
        if not slug:
            raise ValueError(f"Manufacturer name {manufacturer_name!r} does not yield a usable slug")
        type_slug = f"{slug}-client"

        with self._memo_lock:
            if type_slug in self._client_type_memo:
                return self._client_type_memo[type_slug]

        if self.api.dcim.device_types.get(slug=type_slug) is None:
            manufacturer = self.api.dcim.manufacturers.get(slug=slug)
            if manufacturer is None:
                manufacturer = self.api.dcim.manufacturers.create(name=manufacturer_name, slug=slug)
                logger.info("Created NetBox manufacturer %r", manufacturer_name)
            self.api.dcim.device_types.create(
                manufacturer=manufacturer.id,
                model=f"{manufacturer_name} Client",
                slug=type_slug,
                u_height=0,
                is_full_depth=False,
            )
            logger.info("Created NetBox device type %r", f"{manufacturer_name} Client")

        with self._memo_lock:
            self._client_type_memo[type_slug] = type_slug
        return type_slug

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
        with self._memo_lock:
            self._site_memo[site_slug] = site
            self._site_missing.discard(site_slug)
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
        existing: Any | None = None,
        existing_looked_up: bool = False,
    ) -> tuple[Any, bool, bool, bool]:
        """Returns (device, created, update_skipped_by_policy, updated).

        `updated` is True only when fields were actually written — an existing
        device that already matched reports False, so callers can distinguish
        "seen" from "changed".

        `existing` lets the caller pass a device it has already fetched, so we
        don't repeat the by-MAC lookup. `existing_looked_up` distinguishes
        "caller looked and found nothing" from "caller didn't look" — without
        it, a None `existing` would force a redundant query on every new device.
        """
        device = existing if existing_looked_up else self.find_client_device_by_mac(mac)
        site = self.find_site(site_slug)
        if site is None:
            # Defensive: SyncEngine.run() already resolves the site via
            # ensure_site() before any client is synced, so this shouldn't
            # be reachable in normal use.
            raise LookupError(f"NetBox site '{site_slug}' does not exist")

        if device is None:
            # Only needed on the create path — memoized so this costs at most
            # one lookup each per run rather than one per client.
            device_type = self._get_device_type(device_type_slug)
            role = self._get_role(role_slug)
            device = self.api.dcim.devices.create(
                name=name,
                device_type=device_type.id,
                role=role.id,
                site=site.id,
                status="active",
                custom_fields={"unifi_mac": mac},
                tags=[{"slug": tag_slug}],
            )
            return device, True, False, False

        # Correcting the device type is how an existing estate picks up real
        # manufacturers instead of staying on the generic type it was created
        # with. Compared by slug so it's a no-op once already right.
        current_type = getattr(getattr(device, "device_type", None), "slug", None)
        type_differs = current_type is not None and current_type != device_type_slug

        would_change = device.name != name or device.status.value != "active" or type_differs
        if update_policy == "create-only":
            return device, False, would_change, False

        updated = False
        if device.name != name:
            device.name = name
            updated = True
        if device.status.value != "active":
            device.status = "active"
            updated = True
        if type_differs:
            device.device_type = self._get_device_type(device_type_slug).id
            logger.info(
                "Updating %s device type %s -> %s", device.name, current_type, device_type_slug
            )
            updated = True
        if updated:
            device.save()
        return device, False, False, updated

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
