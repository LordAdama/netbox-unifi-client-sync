from __future__ import annotations

import logging
import re
import threading
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, Protocol

import pynetbox
import requests

from .devicetype_library import normalize_model
from .naming import normalize_key, slugify

logger = logging.getLogger(__name__)

CLIENT_INTERFACE_NAME = "eth0"
UNIFI_MAC_FIELD = "unifi_mac"
# Stored on the NetBox site so a later rename of the site's name or slug still
# resolves to the same UniFi site. Matching on the name alone is what created
# duplicate sites and re-created names an operator had changed.
UNIFI_SITE_ID_FIELD = "unifi_site_id"

# "IP address … is configured as the primary IP for device Pixel-10a and cannot be reassigned"
_PRIMARY_DEVICE_RE = re.compile(
    r"device\s+(.+?)(?:\s+and\b|\s*\(|\s*$)",
    re.IGNORECASE,
)


class TimeoutSession(requests.Session):
    """requests.Session that applies a default timeout to every call.

    pynetbox issues ``session.get/post/...`` without a timeout argument, so a
    stalled NetBox connection blocks the worker forever. ``Session.request``
    is the one choke point those verbs share.
    """

    def __init__(self, timeout: float) -> None:
        super().__init__()
        self.timeout = timeout

    def request(self, method: str, url: str, **kwargs: Any) -> requests.Response:
        kwargs.setdefault("timeout", self.timeout)
        return super().request(method, url, **kwargs)


def device_has_tag(obj: Any, tag_slug: str) -> bool:
    """True if a pynetbox record or test double carries ``tag_slug``.

    Real NetBox objects expose ``tags`` as records (or dicts) with a slug.
    The in-memory fake stores plain strings. Both count.
    """
    for tag in getattr(obj, "tags", None) or []:
        if isinstance(tag, str):
            if tag == tag_slug:
                return True
            continue
        slug = getattr(tag, "slug", None)
        if slug is None and isinstance(tag, dict):
            slug = tag.get("slug")
        if slug == tag_slug:
            return True
    return False


def is_primary_ip_conflict(exc: BaseException) -> bool:
    """NetBox rejects moving an IP that is some device's primary address."""
    blob = f"{exc} {getattr(exc, 'error', '')}".lower()
    if "primary" not in blob:
        return False
    return any(token in blob for token in ("reassign", "designated", "configured as the primary"))


def device_name_from_primary_error(exc: BaseException) -> str | None:
    match = _PRIMARY_DEVICE_RE.search(f"{exc} {getattr(exc, 'error', '')}")
    if not match:
        return None
    name = match.group(1).strip().strip("'\"")
    return name or None


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

    def ensure_site(
        self, site_slug: str, policy: str, name: str = "", unifi_site_id: str = ""
    ) -> tuple[Any, bool]: ...

    def ensure_site_identity_field(self) -> None: ...

    def remember_site_link(self, site: Any, unifi_site_id: str) -> bool: ...

    def list_sites(self) -> list[Any]: ...

    def resolve_existing_site(
        self, desired_slug: str, label: str, mode: str = "normalized", unifi_site_id: str = ""
    ) -> Any | None: ...

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

    def assign_ip(
        self, device: Any, interface: Any, ip: str, status: str = "active", managed_tag: str | None = None
    ) -> bool: ...

    def ensure_cable(
        self, interface_a: Any, interface_b: Any, conflict_policy: str, tag_slug: str = "unifi-sync"
    ) -> tuple[bool, str | None]: ...

    def list_synced_client_devices(
        self, tag_slug: str, site_slug: str, role_slug: str | None = None
    ) -> list[Any]: ...

    def mark_offline(self, device: Any) -> None: ...


class PynetboxGateway:
    """NetBox gateway backed by the real pynetbox API client."""

    def __init__(self, url: str, token: str, verify_ssl: bool = True, timeout: float = 30.0) -> None:
        self.api = pynetbox.api(url, token=token)
        # pynetbox never passes timeout= into requests, so a single stalled
        # NetBox call hangs the sync (and, in the long-running container, the
        # healthcheck) until something kills the process. Replace the session
        # before any call. Auth is attached per request from api.token, not
        # from the session, so swapping the session does not drop the token.
        session = TimeoutSession(timeout)
        session.verify = verify_ssl
        session.headers.update(self.api.http_session.headers)
        self.api.http_session = session
        self.timeout = timeout
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
        self._all_sites: list[Any] | None = None

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

        self._ensure_custom_field(UNIFI_MAC_FIELD, "UniFi MAC", ["dcim.device"])
        self.ensure_site_identity_field()

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

    def _ensure_custom_field(self, name: str, label: str, object_types: list[str]) -> None:
        if self.api.extras.custom_fields.get(name=name):
            return
        self.api.extras.custom_fields.create(
            object_types=object_types,
            type="text",
            name=name,
            label=label,
            filter_logic="exact",
        )

    def ensure_site_identity_field(self) -> None:
        """Create the site custom field that stores the UniFi site id.

        Called before site linking, which is earlier than ensure_prerequisites,
        so a renamed site can be found on this run rather than the next one.
        """
        self._ensure_custom_field(UNIFI_SITE_ID_FIELD, "UniFi Site ID", ["dcim.site"])

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

    def list_synced_client_devices(
        self, tag_slug: str, site_slug: str, role_slug: str | None = None
    ) -> list[Any]:
        # Scoped to one NetBox site and, when given, the client role. The sync
        # tag is also applied to infrastructure devices this tool creates;
        # without the role filter, stale-marking would offline those switches
        # because their MACs are not in the client list. A site that doesn't
        # exist holds nothing to mark stale, and filtering on an unknown slug
        # is a 400 rather than an empty list.
        if not self._site_exists(site_slug):
            return []
        filters: dict[str, Any] = {"tag": tag_slug, "site": site_slug}
        if role_slug:
            filters["role"] = role_slug
        return list(self.api.dcim.devices.filter(**filters))

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

    def list_sites(self) -> list[Any]:
        """Every NetBox site, fetched once per run for name matching."""
        with self._memo_lock:
            if self._all_sites is not None:
                return self._all_sites
        sites = list(self.api.dcim.sites.all())
        with self._memo_lock:
            self._all_sites = sites
        return sites

    def resolve_existing_site(
        self, desired_slug: str, label: str, mode: str = "normalized", unifi_site_id: str = ""
    ) -> Any | None:
        """Find a NetBox site that already represents this UniFi site.

        Tried in order, each an exact comparison (never fuzzy, so two genuinely
        different sites can't be bound together):

        1. custom field ``unifi_site_id`` == the UniFi site id. This survives
           a rename of the NetBox site's name and slug.
        2. slug == desired_slug
        3. name == the UniFi site's label
        4. slug or name folded to alphanumerics matches either of the above

        Step 4 is what stops a duplicate when punctuation differs: "Acme's
        Depot - North Yard" slugifies to `acme-s-depot-north-yard`, which
        would miss an existing `acmes-depot-north-yard` on an exact match.

        Name and slug matching still runs when the id is not stored yet, which
        is what lets two UniFi sites that fold to one NetBox site share it.
        An id that is already stored is never overwritten, so a rename in
        NetBox keeps pointing at the UniFi site it was linked to.

        Read-only. The matched site's name and slug are never modified.
        """
        if unifi_site_id:
            # The custom field may not exist yet (a dry-run, or the ensure call
            # failed). A 400 here must fall through to name/slug matching
            # rather than abandoning the site.
            try:
                linked = self._first(
                    self.api.dcim.sites.filter(**{f"cf_{UNIFI_SITE_ID_FIELD}": unifi_site_id})
                )
            except pynetbox.RequestError as exc:
                logger.warning(
                    "Could not look up sites by %s (%s); falling back to name/slug matching",
                    UNIFI_SITE_ID_FIELD,
                    exc,
                )
                linked = None
            if linked is not None:
                logger.info(
                    "Matched UniFi site %s to NetBox site %r (%r) via %s",
                    unifi_site_id,
                    linked.slug,
                    getattr(linked, "name", ""),
                    UNIFI_SITE_ID_FIELD,
                )
                return linked

        exact = self.find_site(desired_slug)
        if exact is not None:
            return exact
        if mode == "slug":
            return None

        by_name = self._first(self.api.dcim.sites.filter(name=label)) if label else None
        if by_name is not None:
            return by_name
        if mode != "normalized":
            return None

        wanted = {normalize_key(desired_slug), normalize_key(label)} - {""}
        for site in self.list_sites():
            candidates = {normalize_key(site.slug or ""), normalize_key(site.name or "")}
            if candidates & wanted:
                return site
        return None

    def remember_site_link(self, site: Any, unifi_site_id: str) -> bool:
        """Record the UniFi site id on an existing site, if it has none.

        This is the only write site-linking performs. Name, slug, status, and
        an id that is already set are left alone, so a rename made in NetBox
        is never reverted and a site is never rebound to a different UniFi site.
        """
        if not unifi_site_id:
            return False
        current = ((getattr(site, "custom_fields", None) or {}).get(UNIFI_SITE_ID_FIELD) or "")
        if current:
            return False
        site.custom_fields = {**(site.custom_fields or {}), UNIFI_SITE_ID_FIELD: unifi_site_id}
        site.save()
        logger.info(
            "Recorded %s=%r on NetBox site %r (%r); name and slug were not changed",
            UNIFI_SITE_ID_FIELD,
            unifi_site_id,
            site.slug,
            getattr(site, "name", ""),
        )
        return True

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

        if device is not None and not device_has_tag(device, tag_slug):
            logger.warning(
                "UniFi device %s matches NetBox device %r, which is not tagged %r; "
                "leaving it unchanged and not creating a duplicate",
                mac,
                device.name,
                tag_slug,
            )
            return device, False, False

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

    def ensure_site(
        self, site_slug: str, policy: str, name: str = "", unifi_site_id: str = ""
    ) -> tuple[Any, bool]:
        """Resolve the NetBox site, honoring SITE_POLICY.

        "create" makes a minimal site if missing; any other value (the
        default, "require") raises. An *existing* site is returned as-is:
        its name, slug, and status are never written, including when the
        caller has a different name in mind. That is what keeps a rename
        made in NetBox from being reverted.
        """
        site = self.find_site(site_slug)
        if site is not None:
            return site, False
        if policy != "create":
            raise LookupError(
                f"NetBox site '{site_slug}' does not exist (SITE_POLICY={policy!r}); "
                "set SITE_POLICY=create to let this tool create it, or create it yourself"
            )
        payload: dict[str, Any] = {
            "name": name or site_slug.replace("-", " ").title(),
            "slug": site_slug,
            "status": "active",
        }
        if unifi_site_id:
            payload["custom_fields"] = {UNIFI_SITE_ID_FIELD: unifi_site_id}
        site = self.api.dcim.sites.create(**payload)
        with self._memo_lock:
            self._site_memo[site_slug] = site
            self._site_missing.discard(site_slug)
            self._all_sites = None
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

        if device is not None and not device_has_tag(device, tag_slug):
            # An operator removed the sync tag (or the unifi_mac was set on a
            # device this tool did not create). Leave every field alone.
            logger.warning(
                "Not updating NetBox device %r (%s): it is not tagged %r",
                device.name,
                mac,
                tag_slug,
            )
            return device, False, True, False

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

    def assign_ip(
        self,
        device: Any,
        interface: Any,
        ip: str,
        status: str = "active",
        managed_tag: str | None = None,
    ) -> bool:
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
        else:
            assigned = getattr(ip_obj, "assigned_object", None)
            assigned_id = getattr(assigned, "id", None) if assigned else None
            if assigned_id != interface.id:
                if not self._move_ip(ip_obj, interface, managed_tag, device):
                    return False
                changed = True

        primary = getattr(device, "primary_ip4", None)
        primary_id = getattr(primary, "id", None) if primary else None
        if primary_id != getattr(ip_obj, "id", None):
            device.primary_ip4 = ip_obj.id
            device.save()
            changed = True
        return changed

    def _move_ip(self, ip_obj: Any, interface: Any, managed_tag: str | None, target_device: Any) -> bool:
        """Point ``ip_obj`` at ``interface``.

        NetBox refuses the save when the address is still some device's primary
        IP (the "Pixel-10a" error). If that device is one this sync manages,
        clear the primary first and retry. If it is not, leave the address
        where it is and let the client sync continue.
        """
        ip_obj.assigned_object_type = "dcim.interface"
        ip_obj.assigned_object_id = interface.id
        try:
            ip_obj.save()
            return True
        except pynetbox.RequestError as exc:
            if not is_primary_ip_conflict(exc):
                raise
            if not self._clear_primary_holders(ip_obj, managed_tag, exc):
                logger.warning(
                    "Skipping reassignment of %s onto %s: NetBox refused because it is a "
                    "primary IP (%s). The rest of this client is unchanged.",
                    getattr(ip_obj, "address", ip_obj),
                    getattr(target_device, "name", target_device),
                    exc,
                )
                return False
            ip_obj.assigned_object_type = "dcim.interface"
            ip_obj.assigned_object_id = interface.id
            ip_obj.save()
            return True

    def _primary_holders(self, ip_obj: Any, exc: BaseException) -> list[Any]:
        try:
            found = list(self.api.dcim.devices.filter(primary_ip4_id=ip_obj.id))
        except pynetbox.RequestError as filter_exc:
            logger.warning("Could not list devices by primary_ip4_id (%s)", filter_exc)
            found = []
        if found:
            return found
        name = device_name_from_primary_error(exc)
        if not name:
            return []
        try:
            return list(self.api.dcim.devices.filter(name=name))
        except pynetbox.RequestError as filter_exc:
            logger.warning(
                "Could not look up device %r named in the primary-IP error (%s)", name, filter_exc
            )
            return []

    def _clear_primary_holders(self, ip_obj: Any, managed_tag: str | None, exc: BaseException) -> bool:
        holders = self._primary_holders(ip_obj, exc)
        if not holders:
            return False
        for holder in holders:
            if not managed_tag or not device_has_tag(holder, managed_tag):
                logger.warning(
                    "Not clearing primary IP %s on %r: that device is not tagged %r, so it is left untouched",
                    getattr(ip_obj, "address", ""),
                    getattr(holder, "name", holder),
                    managed_tag,
                )
                return False
        for holder in holders:
            logger.info(
                "Clearing primary IPv4 on %r so %s can move to its current interface",
                holder.name,
                getattr(ip_obj, "address", ip_obj),
            )
            holder.primary_ip4 = None
            holder.save()
        return True

    def _delete_cable_if_managed(self, cable_ref: Any, tag_slug: str, where: str) -> tuple[bool, str | None]:
        """Delete ``cable_ref`` only when it carries the sync tag.

        ``replace`` is opt-in, and even then it must not remove a cable an
        operator created by hand. Cables this tool creates are tagged.
        """
        cable = self.api.dcim.cables.get(getattr(cable_ref, "id", cable_ref))
        if cable is None:
            return True, None
        if not device_has_tag(cable, tag_slug):
            return False, (
                f"{where} is already cabled by a cable that is not tagged {tag_slug!r}; "
                "leaving that manual cable in place"
            )
        cable.delete()
        return True, None

    def ensure_cable(
        self, interface_a: Any, interface_b: Any, conflict_policy: str, tag_slug: str = "unifi-sync"
    ) -> tuple[bool, str | None]:
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
            deleted, reason = self._delete_cable_if_managed(
                interface_a.cable,
                tag_slug,
                f"interface {interface_a.device.name}:{interface_a.name}",
            )
            if not deleted:
                return False, reason

        interface_b = self.api.dcim.interfaces.get(interface_b.id)
        if interface_b.cable:
            if conflict_policy != "replace":
                return False, (
                    f"interface {interface_b.device.name}:{interface_b.name} is already cabled "
                    "to something else"
                )
            deleted, reason = self._delete_cable_if_managed(
                interface_b.cable,
                tag_slug,
                f"interface {interface_b.device.name}:{interface_b.name}",
            )
            if not deleted:
                return False, reason

        self.api.dcim.cables.create(
            a_terminations=[{"object_type": "dcim.interface", "object_id": interface_a.id}],
            b_terminations=[{"object_type": "dcim.interface", "object_id": interface_b.id}],
            status="connected",
            tags=[{"slug": tag_slug}],
        )
        return True, None

    def mark_offline(self, device: Any) -> None:
        device.status = "offline"
        device.save()
