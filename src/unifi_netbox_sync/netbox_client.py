from __future__ import annotations

import logging
import re
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, Protocol
from urllib.parse import urlsplit

import pynetbox
import requests
from pynetbox.core.query import Request

from .devicetype_library import normalize_model
from .models import UnifiPort
from .naming import normalize_key, slugify

logger = logging.getLogger(__name__)

CLIENT_INTERFACE_NAME = "eth0"
UNIFI_MAC_FIELD = "unifi_mac"
# unifi_site_id has always stored the UniFi *API site name* — the short id
# used in /s/{name}/ paths ("default", "7xk2p9qr"), not the Mongo ``_id``.
# That meaning is kept so existing NetBox data still matches. The two fields
# below split the two identifiers explicitly:
#   unifi_site_name        same short API name (backfilled from unifi_site_id)
#   unifi_site_internal_id the controller ``_id`` (24-hex ObjectId)
# Both are also applied to devices, recording which UniFi site last claimed
# the device. That is how an inventory/staging site can avoid adopting a
# device that already lives on a real site.
UNIFI_SITE_ID_FIELD = "unifi_site_id"
UNIFI_SITE_NAME_FIELD = "unifi_site_name"
UNIFI_SITE_INTERNAL_ID_FIELD = "unifi_site_internal_id"


def _custom_field(obj: Any, name: str) -> str:
    raw = (getattr(obj, "custom_fields", None) or {}).get(name) or ""
    return str(raw).strip()


def site_unifi_id(site: Any) -> str:
    """The UniFi API site name stored on a NetBox site, or "" when it has none.

    This is the historical ``unifi_site_id`` value: the short name used in
    controller URLs, not the internal ``_id``.
    """
    return _custom_field(site, UNIFI_SITE_ID_FIELD)


def site_unifi_name(site: Any) -> str:
    """Explicit copy of the API site name. Empty until backfilled or written."""
    return _custom_field(site, UNIFI_SITE_NAME_FIELD)


def site_unifi_internal_id(site: Any) -> str:
    """The controller ``_id`` stored on a NetBox site, or ""."""
    return _custom_field(site, UNIFI_SITE_INTERNAL_ID_FIELD)


def role_slug_of(device: Any) -> str:
    """Role slug from a pynetbox device or the in-memory fake."""
    direct = getattr(device, "role_slug", None)
    if isinstance(direct, str) and direct:
        return direct
    role = getattr(device, "role", None)
    if isinstance(role, str):
        return role
    slug = getattr(role, "slug", None)
    return str(slug or "")


def _custom_field_object_types(field: Any) -> set[str]:
    """Content-type strings already attached to a custom field."""
    found: set[str] = set()
    for item in getattr(field, "object_types", None) or []:
        if isinstance(item, str):
            found.add(item)
            continue
        app = getattr(item, "app_label", None)
        model = getattr(item, "model", None)
        if app and model:
            found.add(f"{app}.{model}")
            continue
        text = str(item).strip()
        if text:
            found.add(text)
    return found


def device_site_slug(device: Any) -> str:
    """Site slug from a pynetbox device or the in-memory fake."""
    direct = getattr(device, "site_slug", None)
    if isinstance(direct, str) and direct:
        return direct
    site = getattr(device, "site", None)
    if isinstance(site, str):
        return site
    slug = getattr(site, "slug", None)
    return str(slug or "")

# "IP address … is configured as the primary IP for device phone-01 and cannot be reassigned"
_PRIMARY_DEVICE_RE = re.compile(
    r"device\s+(.+?)(?:\s+and\b|\s*\(|\s*$)",
    re.IGNORECASE,
)


# Reads are safe to repeat. Writes are not: a timed-out POST may have
# succeeded, and retrying it could create a second object.
_READ_METHODS = frozenset({"GET", "HEAD", "OPTIONS"})
_RETRYABLE_READ_ERRORS = (
    requests.exceptions.Timeout,
    requests.exceptions.ConnectionError,
)
# One try plus two retries. Backoff is the pause before the next try.
DEFAULT_READ_ATTEMPTS = 3
DEFAULT_READ_BACKOFF = (1.0, 2.0)


class _Unlimited:
    """Stand-in for a semaphore when NETBOX_MAX_INFLIGHT is 0."""

    def __enter__(self) -> None:
        return None

    def __exit__(self, *exc: object) -> bool:
        return False


def _retry_delay(response: requests.Response, attempt: int) -> float:
    """Seconds to wait after HTTP 429. Honors Retry-After when it is numeric."""
    raw = (response.headers.get("Retry-After") or "").strip()
    try:
        parsed = float(raw)
    except ValueError:
        parsed = 0.5 * (2**attempt)
    return min(60.0, max(0.0, parsed))


class TimeoutSession(requests.Session):
    """requests.Session that applies a default timeout to every call.

    pynetbox issues ``session.get/post/...`` without a timeout argument, so a
    stalled NetBox connection blocks the worker forever. ``Session.request``
    is the one choke point those verbs share.

    A read that times out or fails to connect is retried a bounded number of
    times with backoff. POSTs, PUTs, PATCHes, and DELETEs are not retried
    after a timeout or a dropped connection.

    Also caps how many NetBox calls are in flight and retries HTTP 429 on
    every verb. A 429 means NetBox refused the call before applying it, so
    repeating it does not create a second object. The wait happens outside
    the in-flight cap so a backoff does not pin every worker.
    """

    def __init__(
        self,
        timeout: float,
        max_inflight: int = 4,
        rate_limit_retries: int = 5,
        *,
        read_attempts: int = DEFAULT_READ_ATTEMPTS,
        read_backoff: tuple[float, ...] = DEFAULT_READ_BACKOFF,
        sleep: Callable[[float], None] | None = None,
    ) -> None:
        super().__init__()
        self.timeout = timeout
        self.rate_limit_retries = rate_limit_retries
        self.read_attempts = max(1, read_attempts)
        self.read_backoff = read_backoff or (0.0,)
        self.requests_made = 0
        self._count_lock = threading.Lock()
        self._sleep = sleep or time.sleep
        if max_inflight <= 0:
            self._inflight: threading.BoundedSemaphore | _Unlimited = _Unlimited()
        else:
            self._inflight = threading.BoundedSemaphore(max_inflight)

    def request(self, method: str, url: str, **kwargs: Any) -> requests.Response:
        kwargs.setdefault("timeout", self.timeout)
        verb = method.upper()
        read_attempts = self.read_attempts if verb in _READ_METHODS else 1
        rate_retries = max(0, self.rate_limit_retries)
        response: requests.Response | None = None
        for rate_attempt in range(rate_retries + 1):
            response = self._request_once(verb, url, kwargs, read_attempts)
            if response.status_code != 429 or rate_attempt >= rate_retries:
                return response
            delay = _retry_delay(response, rate_attempt)
            logger.warning(
                "NetBox returned 429 for %s %s; retrying in %.1fs (%d/%d)",
                verb,
                urlsplit(url).path or url,
                delay,
                rate_attempt + 1,
                rate_retries,
            )
            if delay:
                self._sleep(delay)
        assert response is not None
        return response

    def _request_once(
        self, verb: str, url: str, kwargs: dict[str, Any], read_attempts: int
    ) -> requests.Response:
        """One logical call, repeating a read that times out or fails to connect."""
        for attempt in range(1, read_attempts + 1):
            try:
                with self._inflight:
                    response = super().request(verb, url, **kwargs)
                with self._count_lock:
                    self.requests_made += 1
                return response
            except _RETRYABLE_READ_ERRORS as exc:
                if attempt >= read_attempts:
                    raise
                delay = self.read_backoff[min(attempt - 1, len(self.read_backoff) - 1)]
                logger.warning(
                    "NetBox %s %s failed (%s); retrying in %.1fs (attempt %d/%d)",
                    verb,
                    url,
                    exc.__class__.__name__,
                    delay,
                    attempt + 1,
                    read_attempts,
                )
                if delay > 0:
                    self._sleep(delay)
        raise RuntimeError("NetBox request retry ended without a result")


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
    # Physical ports from the controller's port_table. Used to tell an SFP
    # cage from a copper port and to refuse cables onto an uplink.
    ports: tuple[UnifiPort, ...] = ()


class NetboxGateway(Protocol):
    """Interface the sync engine depends on. Lets tests substitute a fake."""

    def ensure_prerequisites(
        self, role_slug: str, manufacturer_slug: str, device_type_slug: str, tag_slug: str
    ) -> None: ...

    def find_switch_device_by_mac(self, mac: str, hints: SwitchHints | None = None) -> Any | None: ...

    def match_switch_device(
        self, mac: str, hints: SwitchHints | None = None
    ) -> tuple[Any | None, str]: ...

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
        **kwargs: Any,
    ) -> tuple[Any, bool, bool]: ...

    def find_interface_by_name_candidates(self, device: Any, candidates: list[str]) -> Any | None: ...

    def find_client_device_by_mac(self, mac: str) -> Any | None: ...

    def device_name_taken_by_other(self, name: str, site_slug: str, mac: str) -> bool: ...

    def find_device_name_conflict(self, name: str, site_slug: str, exclude_id: Any) -> Any | None: ...
    """Is `name` used by a device other than `mac` in this site?

    Implementations must return False for a site that does not exist: an
    absent site holds no devices, so no name in it can be taken. NetBox's
    own filters answer an unknown site slug with a 400 rather than an empty
    list, so this has to be handled rather than passed through — it is the
    normal state under --dry-run with SITE_POLICY=create.
    """

    def find_site(self, site_slug: str) -> Any | None: ...

    def ensure_site(
        self,
        site_slug: str,
        policy: str,
        name: str = "",
        unifi_site_id: str = "",
        unifi_internal_id: str = "",
    ) -> tuple[Any, bool]: ...

    def ensure_site_identity_field(self) -> None: ...

    def remember_site_link(
        self, site: Any, unifi_site_id: str, unifi_internal_id: str = ""
    ) -> bool: ...

    def list_sites(self) -> list[Any]: ...

    def resolve_existing_site(
        self,
        desired_slug: str,
        label: str,
        mode: str = "normalized",
        unifi_site_id: str = "",
        unifi_internal_id: str = "",
    ) -> Any | None: ...

    def find_unifi_device(
        self, mac: str, serial: str, name: str, site_slug: str
    ) -> tuple[Any | None, str]: ...

    def legacy_eth0_plan(self, device: Any, interface_names: list[str]) -> str: ...

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

    def __init__(
        self,
        url: str,
        token: str,
        verify_ssl: bool = True,
        timeout: float = 30.0,
        page_size: int = 1000,
        max_inflight: int = 4,
        rate_limit_retries: int = 5,
    ) -> None:
        self.api = pynetbox.api(url, token=token)
        # pynetbox never passes timeout= into requests, so a single stalled
        # NetBox call hangs the sync (and, in the long-running container, the
        # healthcheck) until something kills the process. Replace the session
        # before any call. Auth is attached per request from api.token, not
        # from the session, so swapping the session does not drop the token.
        session = TimeoutSession(timeout, max_inflight, rate_limit_retries)
        session.verify = verify_ssl
        session.headers.update(self.api.http_session.headers)
        self.api.http_session = session
        self.timeout = timeout
        self.page_size = page_size
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
        # Set when cf_unifi_site_id is not a real filter. NetBox then ignores
        # the parameter and returns every site; trusting the first one bound
        # every UniFi site to whichever site sorts first.
        self._unifi_site_id_filter_unusable = False
        # Filled by note_prefetched_* before per-client work. Hits skip the
        # filtered GET; misses fall through unless the index is complete.
        self._pref_by_mac: dict[str, Any] | None = None
        self._pref_by_serial: dict[str, Any] | None = None
        self._pref_by_name: dict[str, Any] | None = None
        self._pref_by_id: dict[Any, Any] = {}
        self._pref_mac_complete = False
        self._pref_ifaces_by_mac: dict[str, list[Any]] | None = None

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
        existing = self.api.extras.custom_fields.get(name=name)
        if existing:
            self._extend_custom_field_types(existing, object_types)
            return
        self.api.extras.custom_fields.create(
            object_types=object_types,
            type="text",
            name=name,
            label=label,
            description=label,
            filter_logic="exact",
        )

    def _extend_custom_field_types(self, field: Any, object_types: list[str]) -> None:
        """Add object types to a custom field that already exists.

        ``unifi_site_name`` starts on sites and is also stored on devices.
        A field created by an older release only lists ``dcim.site``.
        """
        current = _custom_field_object_types(field)
        missing = [item for item in object_types if item not in current]
        if not missing:
            return
        try:
            field.object_types = sorted(current | set(object_types))
            field.save()
            logger.info(
                "Extended custom field %r to %s",
                getattr(field, "name", ""),
                ", ".join(sorted(current | set(object_types))),
            )
        except Exception as exc:
            logger.warning(
                "Could not add %s to custom field %r (%s); device site fields may not filter until updated",
                ", ".join(missing),
                getattr(field, "name", ""),
                exc,
            )

    def ensure_site_identity_field(self) -> None:
        """Create the site custom fields that identify a UniFi site.

        Called before site linking, which is earlier than ensure_prerequisites,
        so a renamed site can be found on this run rather than the next one.

        ``unifi_site_id`` stays the API site name, which is what every
        existing install already stored. ``unifi_site_name`` is that same
        value under an explicit name (backfilled on the next link).
        ``unifi_site_internal_id`` is the controller ``_id``. The latter two
        also apply to devices.
        """
        self._ensure_custom_field(UNIFI_SITE_ID_FIELD, "UniFi Site ID", ["dcim.site"])
        self._ensure_custom_field(
            UNIFI_SITE_NAME_FIELD,
            "UniFi Site Name",
            ["dcim.site", "dcim.device"],
        )
        self._ensure_custom_field(
            UNIFI_SITE_INTERNAL_ID_FIELD,
            "UniFi Site Internal ID",
            ["dcim.site", "dcim.device"],
        )

    def _ensure_device_role(self, role_slug: str) -> None:
        existing = self.api.dcim.device_roles.get(slug=role_slug)
        if existing:
            # A configured slug that already exists is reused. Never create a
            # second role beside it.
            logger.info("Reusing existing NetBox device role %r", role_slug)
            return
        self.api.dcim.device_roles.create(
            name=role_slug.replace("-", " ").title(), slug=role_slug, color="9e9e9e"
        )
        logger.info("Created NetBox device role %r", role_slug)

    # -- lookups -----------------------------------------------------------

    def find_switch_device_by_mac(self, mac: str, hints: SwitchHints | None = None) -> Any | None:
        """Locate the NetBox device for a UniFi switch.

        Matching on an *interface* MAC alone (the original strategy) fails in
        most real deployments: switch interfaces usually have no `mac_address`
        populated at all, and where they do, a port's MAC is not the chassis
        MAC that UniFi reports as `sw_mac`. So we try several joins in
        descending order of precision, and say plainly which one worked.

        The interface-MAC join stays a live filter: the first hit has to be
        the one that filter returns, and a site-scoped prefetch can miss it.
        A complete ``unifi_mac`` index answers the custom-field join with no
        further GET. Serial and name stay live queries too, once per switch,
        because a partial index can hold a different device than the first
        row of the global filter.
        """
        device, _strategy = self.match_switch_device(mac, hints)
        return device

    def match_switch_device(
        self, mac: str, hints: SwitchHints | None = None
    ) -> tuple[Any | None, str]:
        """Like ``find_switch_device_by_mac``, plus the join that won.

        The strategy is ``interface MAC``, ``unifi_mac custom field``,
        ``serial``, or ``device name``. Uplink cabling uses it when two
        UniFi devices resolve to one NetBox device and the MAC match should
        be the one that is cabled.
        """
        for strategy, finder in (
            ("interface MAC", lambda: self._switch_by_interface_mac(mac)),
            ("unifi_mac custom field", lambda: self._switch_by_unifi_mac(mac)),
            ("serial", lambda: self._switch_by_serial(mac, hints)),
            ("device name", lambda: self._switch_by_name(hints)),
        ):
            device = finder()
            if device is not None:
                logger.info("Matched UniFi switch %s to NetBox device %r via %s", mac, device.name, strategy)
                return device, strategy

        # The operator-facing warning is emitted by the sync engine, which
        # owns that guidance for every gateway implementation.
        logger.debug("No NetBox device matched UniFi switch %s by any strategy", mac)
        return None, ""

    @staticmethod
    def _first(results: Any) -> Any | None:
        return next(iter(results), None)

    def _switch_by_interface_mac(self, mac: str) -> Any | None:
        # Always the live filter. A site-scoped interface prefetch can miss a
        # port MAC that lives on some other device, and the first hit has to
        # stay the one this filter would return.
        matches = list(self.api.dcim.interfaces.filter(mac_address=mac, limit=getattr(self, "page_size", 1000)))
        if not matches:
            return None
        return self._device_for_mac_interfaces(mac, matches)

    def _device_for_mac_interfaces(self, mac: str, matches: list[Any]) -> Any | None:
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
        device_id = getattr(getattr(iface, "device", None), "id", None)
        cached = self._pref_by_id.get(device_id) if device_id is not None else None
        if cached is not None:
            return cached
        if device_id is None:
            return None
        return self.api.dcim.devices.get(device_id)

    def _serial_candidates(self, mac: str, hints: SwitchHints | None) -> list[str]:
        # UniFi imports commonly use the MAC as the serial, in either
        # colon-separated or bare form; the controller's own serial too.
        candidates = [mac, mac.replace(":", ""), mac.upper(), mac.replace(":", "").upper()]
        if hints and hints.serial:
            candidates.insert(0, hints.serial)
        return candidates

    def _switch_by_serial(self, mac: str, hints: SwitchHints | None) -> Any | None:
        # Serial is a global join. A per-site index can hold a later device
        # than the one ``filter(serial=)`` returns first, so this stays a
        # live query. It runs once per switch, not once per client.
        for serial in self._serial_candidates(mac, hints):
            device = self._first(self.api.dcim.devices.filter(serial=serial, limit=getattr(self, "page_size", 1000)))
            if device is not None:
                return device
        return None

    def _switch_by_name(self, hints: SwitchHints | None) -> Any | None:
        if not hints or not hints.name:
            return None
        return self._first(self.api.dcim.devices.filter(name=hints.name, limit=getattr(self, "page_size", 1000)))

    def _switch_by_unifi_mac(self, mac: str) -> Any | None:
        index = self._pref_by_mac
        if index is not None and mac in index:
            return index[mac]
        if self._pref_mac_complete:
            return None
        return self._first(
            self.api.dcim.devices.filter(**{f"cf_{UNIFI_MAC_FIELD}": mac}, limit=getattr(self, "page_size", 1000))
        )

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
        return next(
            iter(self.api.dcim.devices.filter(**{f"cf_{UNIFI_MAC_FIELD}": mac}, limit=getattr(self, "page_size", 1000))),
            None,
        )

    def match_client_device(self, devices: list[Any], mac: str) -> Any | None:
        """First device whose ``unifi_mac`` equals ``mac``, in list order.

        Same answer as ``find_client_device_by_mac`` when ``devices`` is the
        full set that filter would scan, in NetBox's default order.
        """
        for device in devices:
            raw = (getattr(device, "custom_fields", None) or {}).get(UNIFI_MAC_FIELD)
            if raw is not None and str(raw) == mac:
                return device
        return None

    def device_name_taken_among(self, devices: list[Any], name: str, site_slug: str, mac: str) -> bool:
        """In-memory form of ``device_name_taken_by_other``.

        The first device at the site with this name wins, matching
        ``next(iter(filter(name=, site=)))``.
        """
        if not self._site_exists(site_slug):
            return False
        for device in devices:
            if getattr(device, "name", None) != name:
                continue
            if device_site_slug(device) != site_slug:
                continue
            stored = (getattr(device, "custom_fields", None) or {}).get(UNIFI_MAC_FIELD)
            return stored != mac
        return False

    def select_synced_clients(
        self, devices: list[Any], tag_slug: str, site_slug: str, role_slug: str | None
    ) -> list[Any]:
        """Devices from ``devices`` that ``list_synced_client_devices`` would return."""
        if not self._site_exists(site_slug):
            return []
        selected = []
        for device in devices:
            if not device_has_tag(device, tag_slug):
                continue
            if device_site_slug(device) != site_slug:
                continue
            if role_slug and role_slug_of(device) != role_slug:
                continue
            selected.append(device)
        return selected

    def bulk_devices_with_unifi_mac(self) -> list[Any] | None:
        """Every device that has ``unifi_mac`` set, or None if the query failed.

        An ignored filter returns the whole device table (NetBox drops unknown
        parameters and answers 200). That table is still a complete index: a
        MAC that is not on any record is not in NetBox. Pagination is followed
        to the end, so a short page is not treated as the full set.
        """
        try:
            devices = list(
                self.api.dcim.devices.filter(
                    **{f"cf_{UNIFI_MAC_FIELD}__empty": "false"},
                    limit=getattr(self, "page_size", 1000),
                )
            )
        except pynetbox.RequestError as exc:
            logger.warning(
                "Could not bulk-load devices by %s (%s); per-client lookups will be used",
                UNIFI_MAC_FIELD,
                exc,
            )
            return None
        empty = sum(1 for device in devices if not _custom_field(device, UNIFI_MAC_FIELD))
        if empty:
            logger.info(
                "NetBox ignored the %s empty-filter and returned the full device list "
                "(%d record(s), %d without the field); indexing that list",
                UNIFI_MAC_FIELD,
                len(devices),
                empty,
            )
        else:
            logger.info("Bulk-loaded %d NetBox device(s) with %s set", len(devices), UNIFI_MAC_FIELD)
        return devices

    def bulk_devices_at_site(self, site_slug: str) -> list[Any] | None:
        """Every device at ``site_slug``, or None if the query failed.

        An empty list means the site exists and holds nothing (or the site
        does not exist). None means "do not trust an index".
        """
        if not self._site_exists(site_slug):
            return []
        try:
            devices = list(self.api.dcim.devices.filter(site=site_slug, limit=getattr(self, "page_size", 1000)))
        except pynetbox.RequestError as exc:
            logger.warning(
                "Could not bulk-load devices at site %s (%s); per-client lookups will be used",
                site_slug,
                exc,
            )
            return None
        logger.info("Bulk-loaded %d NetBox device(s) at site %s", len(devices), site_slug)
        return devices

    def bulk_interfaces(self, device_ids: list[Any]) -> list[Any] | None:
        """Interfaces for ``device_ids``, in chunks so the query string stays bounded."""
        if not device_ids:
            return []
        found: list[Any] = []
        try:
            for start in range(0, len(device_ids), 50):
                batch = device_ids[start : start + 50]
                found.extend(
                    self.api.dcim.interfaces.filter(device_id=batch, limit=getattr(self, "page_size", 1000))
                )
        except pynetbox.RequestError as exc:
            logger.warning("Could not bulk-load interfaces (%s); per-device lookups will be used", exc)
            return None
        return found

    def note_prefetched_devices(self, devices: list[Any], *, mac_complete: bool) -> None:
        """Index devices for switch joins. ``mac_complete`` means every unifi_mac is here."""
        by_mac: dict[str, Any] = {}
        by_serial: dict[str, Any] = {}
        by_name: dict[str, Any] = {}
        by_id: dict[Any, Any] = {}
        for device in devices:
            device_id = getattr(device, "id", None)
            if device_id is not None and device_id not in by_id:
                by_id[device_id] = device
            raw = (getattr(device, "custom_fields", None) or {}).get(UNIFI_MAC_FIELD)
            if raw is not None and str(raw) not in by_mac:
                by_mac[str(raw)] = device
            serial = getattr(device, "serial", None) or ""
            if serial and serial not in by_serial:
                by_serial[str(serial)] = device
            name = getattr(device, "name", None) or ""
            if name and name not in by_name:
                by_name[str(name)] = device
        with self._memo_lock:
            self._pref_by_mac = by_mac
            self._pref_by_serial = by_serial
            self._pref_by_name = by_name
            self._pref_by_id.update(by_id)
            self._pref_mac_complete = mac_complete

    def note_prefetched_interfaces(self, interfaces: list[Any]) -> None:
        """Interface-MAC hits from the bulk load. A miss still queries NetBox."""
        by_mac: dict[str, list[Any]] = {}
        for iface in interfaces:
            raw = getattr(iface, "mac_address", None)
            if not raw:
                continue
            by_mac.setdefault(str(raw).lower(), []).append(iface)
        with self._memo_lock:
            self._pref_ifaces_by_mac = by_mac

    def find_device_name_conflict(self, name: str, site_slug: str, exclude_id: Any) -> Any | None:
        """Another device already named ``name`` at ``site_slug``, or None.

        Used before a legacy merge renames a client record, so a NetBox
        uniqueness rejection becomes a logged duplicate instead of an error.
        The prefetch index answers hits without a query; a miss there is not
        proof (it keeps one device per name), so it falls back to the filter.
        """
        if not name or not self._site_exists(site_slug):
            return None
        with self._memo_lock:
            cached = (self._pref_by_name or {}).get(name)
        if (
            cached is not None
            and getattr(cached, "id", None) != exclude_id
            and device_site_slug(cached) == site_slug
        ):
            return cached
        try:
            for device in self.api.dcim.devices.filter(name=name, site=site_slug):
                if getattr(device, "id", None) != exclude_id:
                    return device
        except pynetbox.RequestError as exc:
            if getattr(exc, "req", None) is not None and exc.req.status_code == 400:
                return None
            raise
        return None

    def device_name_taken_by_other(self, name: str, site_slug: str, mac: str) -> bool:
        # NetBox validates ?site=<slug> against existing sites and answers a
        # unknown slug with 400 "Select a valid choice", not an empty list. A
        # site that doesn't exist holds no devices, so nothing can be taken —
        # and this is the normal case under --dry-run with SITE_POLICY=create,
        # where the sites would only have been created by a real run.
        if not self._site_exists(site_slug):
            return False
        try:
            existing = next(
                iter(self.api.dcim.devices.filter(name=name, site=site_slug, limit=getattr(self, "page_size", 1000))),
                None,
            )
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
        filters["limit"] = getattr(self, "page_size", 1000)
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
        return list(self.api.dcim.interfaces.filter(device_id=device.id, limit=getattr(self, "page_size", 1000)))

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
        self,
        desired_slug: str,
        label: str,
        mode: str = "normalized",
        unifi_site_id: str = "",
        unifi_internal_id: str = "",
    ) -> Any | None:
        """Find a NetBox site that already represents this UniFi site.

        Tried in order, each an exact comparison (never fuzzy, so two genuinely
        different sites can't be bound together):

        1. ``unifi_site_internal_id`` == the controller ``_id``. This survives
           a rename of the UniFi description and of the NetBox name and slug.
        2. ``unifi_site_id`` or ``unifi_site_name`` == the API site name (the
           short id used in controller URLs). ``unifi_site_id`` is what older
           releases stored, and it still holds that short name.
        3. slug == desired_slug
        4. name == the UniFi site's label
        5. slug or name folded to alphanumerics matches either of the above

        Step 5 is what stops a duplicate when punctuation differs: "Acme's
        Depot - North Yard" slugifies to `acme-s-depot-north-yard`, which
        would miss an existing `acmes-depot-north-yard` on an exact match.

        Name and slug matching still runs when no id is stored yet. A site
        already bound to a *different* UniFi site is not a match: those
        fields are never overwritten and are never shared. Callers treat two
        UniFi sites that still land on one NetBox site as a conflict and skip
        both.

        Read-only. The matched site's name and slug are never modified.
        """
        if unifi_internal_id and not self._site_field_filter_unusable(UNIFI_SITE_INTERNAL_ID_FIELD):
            linked = self._match_site_by_field(UNIFI_SITE_INTERNAL_ID_FIELD, unifi_internal_id)
            if linked is not None:
                logger.info(
                    "Matched UniFi site %s to NetBox site %r (%r) via custom field %s",
                    unifi_internal_id,
                    linked.slug,
                    getattr(linked, "name", ""),
                    UNIFI_SITE_INTERNAL_ID_FIELD,
                )
                return linked

        if unifi_site_id and not getattr(self, "_unifi_site_id_filter_unusable", False):
            linked = self._match_site_by_unifi_id(unifi_site_id)
            if linked is not None:
                logger.info(
                    "Matched UniFi site %s to NetBox site %r (%r) via %s",
                    unifi_site_id,
                    linked.slug,
                    getattr(linked, "name", ""),
                    UNIFI_SITE_ID_FIELD,
                )
                return linked
            # Only when the id filter is a real filter that simply missed.
            # If NetBox ignored it (field not created yet), do not issue a
            # second custom-field query — that would consume the fallback
            # name lookup and bind the wrong site.
            if not getattr(self, "_unifi_site_id_filter_unusable", False) and not self._site_field_filter_unusable(
                UNIFI_SITE_NAME_FIELD
            ):
                linked = self._match_site_by_field(UNIFI_SITE_NAME_FIELD, unifi_site_id)
                if linked is not None:
                    logger.info(
                        "Matched UniFi site %s to NetBox site %r (%r) via custom field %s",
                        unifi_site_id,
                        linked.slug,
                        getattr(linked, "name", ""),
                        UNIFI_SITE_NAME_FIELD,
                    )
                    return linked

        exact = self.find_site(desired_slug)
        if exact is not None and self._site_id_allows(exact, unifi_site_id, unifi_internal_id):
            return exact
        if mode == "slug":
            return None

        by_name = self._first(self.api.dcim.sites.filter(name=label)) if label else None
        if by_name is not None and self._site_id_allows(by_name, unifi_site_id, unifi_internal_id):
            return by_name
        if mode != "normalized":
            return None

        wanted = {normalize_key(desired_slug), normalize_key(label)} - {""}
        for site in self.list_sites():
            if not self._site_id_allows(site, unifi_site_id, unifi_internal_id):
                continue
            candidates = {normalize_key(site.slug or ""), normalize_key(site.name or "")}
            if candidates & wanted:
                return site
        return None

    def _site_field_filter_unusable(self, field_name: str) -> bool:
        flags = getattr(self, "_site_field_filters_unusable", None)
        if flags is None:
            return False
        return field_name in flags

    def _mark_site_field_filter_unusable(self, field_name: str) -> None:
        flags = getattr(self, "_site_field_filters_unusable", None)
        if flags is None:
            flags = set()
            self._site_field_filters_unusable = flags
        flags.add(field_name)

    def _site_id_allows(self, site: Any, unifi_site_id: str, unifi_internal_id: str = "") -> bool:
        """False when the site is already bound to a different UniFi site."""
        current = site_unifi_id(site)
        if current and unifi_site_id and current != unifi_site_id:
            logger.info(
                "NetBox site %r (%r) is already linked to UniFi site %r; not also linking %r",
                getattr(site, "slug", ""),
                getattr(site, "name", ""),
                current,
                unifi_site_id,
            )
            return False
        current_name = site_unifi_name(site)
        if current_name and unifi_site_id and current_name != unifi_site_id:
            logger.info(
                "NetBox site %r (%r) is already linked to UniFi site %r via %s; not also linking %r",
                getattr(site, "slug", ""),
                getattr(site, "name", ""),
                current_name,
                UNIFI_SITE_NAME_FIELD,
                unifi_site_id,
            )
            return False
        current_internal = site_unifi_internal_id(site)
        if current_internal and unifi_internal_id and current_internal != unifi_internal_id:
            logger.info(
                "NetBox site %r (%r) is already linked to UniFi _id %r; not also linking %r",
                getattr(site, "slug", ""),
                getattr(site, "name", ""),
                current_internal,
                unifi_internal_id,
            )
            return False
        return True

    def _match_site_by_unifi_id(self, unifi_site_id: str) -> Any | None:
        """Return the one site whose stored id equals ``unifi_site_id``.

        NetBox ignores query parameters it does not know. On a dry-run the
        ``unifi_site_id`` custom field often does not exist yet, so
        ``?cf_unifi_site_id=<id>`` comes back 200 with *every* site, and the
        first record looks like a hit for every UniFi id. A result counts only
        when that site's own field equals the id we asked for.
        """
        try:
            iterator = iter(
                self.api.dcim.sites.filter(**{f"cf_{UNIFI_SITE_ID_FIELD}": unifi_site_id})
            )
            first = next(iterator, None)
            second = next(iterator, None) if first is not None else None
        except pynetbox.RequestError as exc:
            logger.warning(
                "Could not look up sites by %s (%s); falling back to name/slug matching",
                UNIFI_SITE_ID_FIELD,
                exc,
            )
            self._unifi_site_id_filter_unusable = True
            return None
        if first is None:
            return None
        if site_unifi_id(first) != unifi_site_id:
            logger.warning(
                "NetBox returned site %r (%r) for %s=%r but that site's field is %r. "
                "The filter is being ignored, which happens when the custom field does "
                "not exist yet (a dry-run does not create it). Ignoring this result and "
                "falling back to name/slug matching.",
                getattr(first, "slug", ""),
                getattr(first, "name", ""),
                UNIFI_SITE_ID_FIELD,
                unifi_site_id,
                site_unifi_id(first) or "<empty>",
            )
            self._unifi_site_id_filter_unusable = True
            return None
        if second is not None and site_unifi_id(second) == unifi_site_id:
            logger.error(
                "UniFi site %s is stored on more than one NetBox site (%r and %r); not choosing one",
                unifi_site_id,
                getattr(first, "slug", ""),
                getattr(second, "slug", ""),
            )
            return None
        return first

    def _match_site_by_field(self, field_name: str, value: str) -> Any | None:
        """Return the one site whose custom field equals ``value``.

        Same ignored-filter guard as ``unifi_site_id``: a dry-run before the
        field exists makes NetBox return every site, and the first is not a hit.
        """
        try:
            iterator = iter(self.api.dcim.sites.filter(**{f"cf_{field_name}": value}))
            first = next(iterator, None)
            second = next(iterator, None) if first is not None else None
        except pynetbox.RequestError as exc:
            logger.warning(
                "Could not look up sites by %s (%s); falling back to name/slug matching",
                field_name,
                exc,
            )
            self._mark_site_field_filter_unusable(field_name)
            return None
        if first is None:
            return None
        if _custom_field(first, field_name) != value:
            logger.warning(
                "NetBox returned site %r (%r) for %s=%r but that site's field is %r. "
                "The filter is being ignored, which happens when the custom field does "
                "not exist yet (a dry-run does not create it). Ignoring this result and "
                "falling back to name/slug matching.",
                getattr(first, "slug", ""),
                getattr(first, "name", ""),
                field_name,
                value,
                _custom_field(first, field_name) or "<empty>",
            )
            self._mark_site_field_filter_unusable(field_name)
            return None
        if second is not None and _custom_field(second, field_name) == value:
            logger.error(
                "UniFi value %s is stored on more than one NetBox site (%r and %r); not choosing one",
                value,
                getattr(first, "slug", ""),
                getattr(second, "slug", ""),
            )
            return None
        return first

    def remember_site_link(
        self, site: Any, unifi_site_id: str, unifi_internal_id: str = ""
    ) -> bool:
        """Record UniFi identifiers on an existing site, without renaming it.

        ``unifi_site_id`` keeps meaning "API site name" (the short id in
        controller URLs). It is never overwritten. When it is already set to
        that name, empty companion fields are filled:

        * ``unifi_site_name`` is backfilled from ``unifi_site_id`` (that is
          the value older releases stored there).
        * ``unifi_site_internal_id`` is set from the controller ``_id`` when
          we know it.

        Name, slug, and status are not written.
        """
        if not unifi_site_id and not unifi_internal_id:
            return False
        current = site_unifi_id(site)
        if current and unifi_site_id and current != unifi_site_id:
            logger.warning(
                "Not overwriting %s on NetBox site %r (%r): already %r, refusing %r",
                UNIFI_SITE_ID_FIELD,
                getattr(site, "slug", ""),
                getattr(site, "name", ""),
                current,
                unifi_site_id,
            )
            return False
        current_internal = site_unifi_internal_id(site)
        if current_internal and unifi_internal_id and current_internal != unifi_internal_id:
            logger.warning(
                "Not overwriting %s on NetBox site %r (%r): already %r, refusing %r",
                UNIFI_SITE_INTERNAL_ID_FIELD,
                getattr(site, "slug", ""),
                getattr(site, "name", ""),
                current_internal,
                unifi_internal_id,
            )
            return False
        if not current and unifi_site_id:
            for other in self.list_sites():
                if getattr(other, "id", None) == getattr(site, "id", None):
                    continue
                if site_unifi_id(other) == unifi_site_id or site_unifi_name(other) == unifi_site_id:
                    logger.warning(
                        "Not recording %s=%r on NetBox site %r; UniFi site %s is already recorded on %r",
                        UNIFI_SITE_ID_FIELD,
                        unifi_site_id,
                        getattr(site, "slug", ""),
                        unifi_site_id,
                        getattr(other, "slug", ""),
                    )
                    return False
        if unifi_internal_id and not current_internal:
            for other in self.list_sites():
                if getattr(other, "id", None) == getattr(site, "id", None):
                    continue
                if site_unifi_internal_id(other) == unifi_internal_id:
                    logger.warning(
                        "Not recording %s=%r on NetBox site %r; it is already recorded on %r",
                        UNIFI_SITE_INTERNAL_ID_FIELD,
                        unifi_internal_id,
                        getattr(site, "slug", ""),
                        getattr(other, "slug", ""),
                    )
                    return False

        fields = dict(site.custom_fields or {})
        changed = False
        # The short API name. Prefer the value already stored in unifi_site_id
        # so a backfill cannot disagree with history.
        short_name = current or unifi_site_id
        if short_name and not current:
            fields[UNIFI_SITE_ID_FIELD] = short_name
            changed = True
        if short_name and not site_unifi_name(site):
            fields[UNIFI_SITE_NAME_FIELD] = short_name
            changed = True
        if unifi_internal_id and not current_internal:
            fields[UNIFI_SITE_INTERNAL_ID_FIELD] = unifi_internal_id
            changed = True
        if not changed:
            return False
        site.custom_fields = fields
        site.save()
        with self._memo_lock:
            self._all_sites = None
        logger.info(
            "Recorded UniFi site identity on NetBox site %r (%r): %s=%r %s=%r %s=%r; "
            "name and slug were not changed",
            site.slug,
            getattr(site, "name", ""),
            UNIFI_SITE_ID_FIELD,
            fields.get(UNIFI_SITE_ID_FIELD, ""),
            UNIFI_SITE_NAME_FIELD,
            fields.get(UNIFI_SITE_NAME_FIELD, ""),
            UNIFI_SITE_INTERNAL_ID_FIELD,
            fields.get(UNIFI_SITE_INTERNAL_ID_FIELD, ""),
        )
        return True

    def ensure_device_type_from_spec(self, spec: Any) -> str:
        """Find or create a NetBox device type, with its interface templates.

        Matching is by slug, then by normalized part number, model, or slug,
        so a device type already imported from devicetype-library is reused
        rather than duplicated. Interface, power-port, and power-outlet
        *templates* are used (not per-device components): NetBox instantiates
        them automatically on every new device of this type.
        """
        with self._memo_lock:
            if spec.slug in self._infra_type_memo:
                return self._infra_type_memo[spec.slug]

        existing = self._find_device_type(spec)

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
            self._create_power_templates(created.id, spec)
            logger.info(
                "Created NetBox device type %r with %d interface template(s), "
                "%d power port template(s), %d power outlet template(s)",
                spec.model,
                len(spec.interfaces),
                len(getattr(spec, "power_ports", None) or []),
                len(getattr(spec, "power_outlets", None) or []),
            )

        with self._memo_lock:
            self._infra_type_memo[spec.slug] = resolved
        return resolved

    def _find_device_type(self, spec: Any) -> Any | None:
        existing = self.api.dcim.device_types.get(slug=spec.slug)
        if existing is not None:
            return existing
        keys = {
            normalize_model(getattr(spec, "part_number", "") or ""),
            normalize_model(getattr(spec, "model", "") or ""),
            normalize_model(getattr(spec, "slug", "") or ""),
        } - {""}
        if not keys:
            return None
        if spec.part_number:
            hit = self.api.dcim.device_types.get(part_number=spec.part_number)
            if hit is not None:
                return hit
        candidates = list(self.api.dcim.device_types.filter(manufacturer=slugify(spec.manufacturer)))
        for dt in candidates:
            ids = {
                normalize_model(getattr(dt, "part_number", "") or ""),
                normalize_model(getattr(dt, "model", "") or ""),
                normalize_model(getattr(dt, "slug", "") or ""),
            }
            if ids & keys:
                return dt
        return None

    def _create_power_templates(self, device_type_id: int, spec: Any) -> None:
        port_ids: dict[str, int] = {}
        for port in getattr(spec, "power_ports", None) or []:
            payload: dict[str, Any] = {
                "device_type": device_type_id,
                "name": port.name,
                "type": port.type,
            }
            if port.maximum_draw:
                payload["maximum_draw"] = port.maximum_draw
            created = self.api.dcim.power_port_templates.create(**payload)
            port_ids[port.name] = created.id
        outlets = []
        for outlet in getattr(spec, "power_outlets", None) or []:
            item: dict[str, Any] = {
                "device_type": device_type_id,
                "name": outlet.name,
                "type": outlet.type,
            }
            feed = port_ids.get(outlet.power_port)
            if feed is not None:
                item["power_port"] = feed
            outlets.append(item)
        if outlets:
            self.api.dcim.power_outlet_templates.create(outlets)

    def _ensure_manufacturer(self, name: str) -> Any:
        slug = slugify(name)
        manufacturer = self.api.dcim.manufacturers.get(slug=slug)
        if manufacturer is None:
            manufacturer = self.api.dcim.manufacturers.create(name=name, slug=slug)
            logger.info("Created NetBox manufacturer %r", name)
        return manufacturer

    def find_unifi_device(
        self, mac: str, serial: str, name: str, site_slug: str
    ) -> tuple[Any | None, str]:
        """Find a NetBox device for an adopted UniFi device.

        Order, most precise first:

        1. ``unifi_mac`` anywhere (cross-site).
        2. A device whose name *is* the MAC, anywhere. That is how older
           releases named infrastructure they had created as clients.
        3. Serial anywhere (cross-site).
        4. Name, and only inside ``site_slug``.

        Name is same-site on purpose: two sites may both have a device called
        "core-switch". MAC and serial are the chassis, so they are global and
        a hit means "do not create another one".
        """
        mac = (mac or "").lower()
        compact = mac.replace(":", "")
        by_mac = self._device_by_unifi_mac(mac) if mac else None
        if by_mac is not None:
            return by_mac, "mac"
        for candidate in (compact, mac):
            if not candidate:
                continue
            named = self._first(self.api.dcim.devices.filter(name=candidate))
            if named is not None:
                return named, "mac-name"
        if serial:
            by_serial = self._first(self.api.dcim.devices.filter(serial=serial))
            if by_serial is not None:
                return by_serial, "serial"
        if name and site_slug and name.lower() not in {compact, mac} and self._site_exists(site_slug):
            by_name = self._first(self.api.dcim.devices.filter(name=name, site=site_slug))
            if by_name is not None:
                return by_name, "name"
        return None, ""

    def _device_by_unifi_mac(self, mac: str) -> Any | None:
        if getattr(self, "_unifi_mac_filter_unusable", False):
            return None
        try:
            first = self._first(self.api.dcim.devices.filter(**{f"cf_{UNIFI_MAC_FIELD}": mac}))
        except pynetbox.RequestError as exc:
            logger.warning("Could not look up devices by %s (%s)", UNIFI_MAC_FIELD, exc)
            self._unifi_mac_filter_unusable = True
            return None
        if first is None:
            return None
        stored = _custom_field(first, UNIFI_MAC_FIELD).lower()
        if stored != mac.lower():
            logger.warning(
                "NetBox returned device %r for %s=%r but that device's field is %r; "
                "ignoring the filter",
                getattr(first, "name", ""),
                UNIFI_MAC_FIELD,
                mac,
                stored or "<empty>",
            )
            self._unifi_mac_filter_unusable = True
            return None
        return first

    def legacy_eth0_plan(self, device: Any, interface_names: list[str]) -> str:
        """What would happen to a legacy client ``eth0``, without writing."""
        if CLIENT_INTERFACE_NAME in set(interface_names):
            return "kept (eth0 is part of the device type)"
        interfaces = {iface.name: iface for iface in self.list_device_interfaces(device)}
        eth0 = interfaces.get(CLIENT_INTERFACE_NAME)
        if eth0 is None:
            return "absent"
        ips = self._interface_addresses(eth0)
        if ips is None:
            return "kept (could not tell whether eth0 has an IP)"
        cabled = bool(getattr(eth0, "cable", None))
        if not ips and not cabled:
            return "deleted (eth0 has no IP or cable)"
        target = interface_names[0] if interface_names else ""
        if not target:
            return "kept (eth0 has an IP or cable and there is no interface to move it to)"
        return f"migrated onto {target}"

    def _interface_addresses(self, interface: Any) -> list[Any] | None:
        try:
            return list(self.api.ipam.ip_addresses.filter(interface_id=interface.id))
        except Exception as exc:
            logger.warning(
                "Could not list IP addresses on interface %s (%s)",
                getattr(interface, "name", ""),
                exc,
            )
            return None

    def align_device_components(self, device: Any, spec: Any, tag_slug: str) -> str:
        """Add missing interfaces and power components, then retire a stale eth0.

        Used when an existing device changes type (a legacy client merge). A
        brand-new device gets its components from the device-type templates
        and does not need this. Returns what happened to eth0.
        """
        interfaces = {iface.name: iface for iface in self.api.dcim.interfaces.filter(device_id=device.id)}
        for name, iface_type in spec.interfaces:
            if name not in interfaces:
                created = self.api.dcim.interfaces.create(device=device.id, name=name, type=iface_type)
                interfaces[name] = created
        power_ports = {port.name: port for port in self.api.dcim.power_ports.filter(device_id=device.id)}
        for port in getattr(spec, "power_ports", None) or []:
            if port.name in power_ports:
                continue
            payload: dict[str, Any] = {"device": device.id, "name": port.name, "type": port.type}
            if port.maximum_draw:
                payload["maximum_draw"] = port.maximum_draw
            power_ports[port.name] = self.api.dcim.power_ports.create(**payload)
        outlet_names = {outlet.name for outlet in self.api.dcim.power_outlets.filter(device_id=device.id)}
        for outlet in getattr(spec, "power_outlets", None) or []:
            if outlet.name in outlet_names:
                continue
            payload = {"device": device.id, "name": outlet.name, "type": outlet.type}
            feed = power_ports.get(outlet.power_port)
            if feed is not None:
                payload["power_port"] = feed.id
            self.api.dcim.power_outlets.create(**payload)
            outlet_names.add(outlet.name)
        return self._retire_eth0(device, spec, interfaces, tag_slug)

    def _retire_eth0(self, device: Any, spec: Any, interfaces: dict[str, Any], tag_slug: str) -> str:
        template_names = [name for name, _ in spec.interfaces]
        if CLIENT_INTERFACE_NAME in template_names:
            return "kept"
        eth0 = interfaces.get(CLIENT_INTERFACE_NAME)
        if eth0 is None:
            return "absent"
        ips = self._interface_addresses(eth0)
        if ips is None:
            logger.warning(
                "Leaving eth0 on device %r; could not tell whether it has an IP",
                getattr(device, "name", ""),
            )
            return "kept"
        cabled = bool(getattr(eth0, "cable", None))
        if not ips and not cabled:
            eth0.delete()
            logger.info("Removed empty legacy eth0 from device %r", getattr(device, "name", ""))
            return "deleted"
        if not template_names:
            logger.info(
                "Keeping eth0 on device %r; it has an IP or cable and the new type has no interface",
                getattr(device, "name", ""),
            )
            return "kept"
        target_name = template_names[0]
        target = interfaces.get(target_name)
        if target is None:
            return "kept"
        if cabled and getattr(target, "cable", None):
            logger.info(
                "Keeping eth0 on device %r; %s is already cabled",
                getattr(device, "name", ""),
                target_name,
            )
            return "kept"
        for ip in ips:
            ip.assigned_object_type = "dcim.interface"
            ip.assigned_object_id = target.id
            ip.save()
        if cabled:
            peers = list(getattr(eth0, "link_peers", None) or [])
            cable = eth0.cable
            if cable is not None:
                cable.delete()
            for peer in peers:
                self.api.dcim.cables.create(
                    a_terminations=[{"object_type": "dcim.interface", "object_id": target.id}],
                    b_terminations=[{"object_type": "dcim.interface", "object_id": peer.id}],
                    status="connected",
                    tags=[{"slug": tag_slug}],
                )
        eth0.delete()
        logger.info(
            "Migrated eth0 IP/cable onto %s and removed eth0 from device %r",
            target_name,
            getattr(device, "name", ""),
        )
        return "migrated"

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
        *,
        status: str = "active",
        merge_client: bool = False,
        client_role_slug: str = "unifi-client",
        move_site: bool = False,
        source_site_name: str = "",
        source_site_internal_id: str = "",
        spec: Any = None,
        looked_up: bool = False,
        existing: Any | None = None,
        match: str = "",
    ) -> tuple[Any, bool, bool]:
        """Create or update an adopted UniFi device. Returns (device, created, updated).

        MAC and serial matches are global, so a device that already exists
        under another site is reused instead of duplicated. Name matches stay
        inside ``site_slug``.

        ``merge_client`` converts a tagged client-role device with this MAC
        in place (same NetBox id). That runs even when ``update_policy`` is
        ``create-only``. ``move_site`` moves the device onto ``site_slug`` and
        sets ``status``; the caller decides when create-only still allows it
        (an inventory device showing up at a real site). A legacy merge does
        not move on its own: an inventory observation passes ``move_site``
        false so the client keeps its NetBox site.
        """
        mac = (mac or "").lower()
        if not looked_up:
            existing, match = self.find_unifi_device(mac, serial, name, site_slug)

        site = self.find_site(site_slug)
        if site is None:
            raise LookupError(f"NetBox site '{site_slug}' does not exist")

        if existing is not None and not device_has_tag(existing, tag_slug):
            logger.warning(
                "UniFi device %s matches NetBox device %r, which is not tagged %r; "
                "leaving it unchanged and not creating a duplicate",
                mac,
                existing.name,
                tag_slug,
            )
            return existing, False, False

        identity = self._source_site_fields(mac, source_site_name, source_site_internal_id)
        if existing is None:
            device = self.api.dcim.devices.create(
                name=name,
                device_type=self._get_device_type(device_type_slug).id,
                role=self._get_role(role_slug).id,
                site=site.id,
                serial=serial,
                status=status,
                custom_fields=identity,
                tags=[{"slug": tag_slug}],
            )
            return device, True, False

        legacy = (
            merge_client
            and role_slug_of(existing) == client_role_slug
            and _custom_field(existing, UNIFI_MAC_FIELD).lower() == mac
        )
        current_site = device_site_slug(existing)
        cross_site = bool(current_site and site_slug and current_site != site_slug)
        if not legacy and not (move_site and cross_site) and update_policy == "create-only":
            return existing, False, False

        changes: dict[str, Any] = {}
        if legacy or update_policy != "create-only":
            if name and existing.name != name:
                changes["name"] = name
            if serial and (existing.serial or "") != serial:
                changes["serial"] = serial
            if role_slug_of(existing) != role_slug:
                changes["role"] = self._get_role(role_slug).id
            current_type = getattr(getattr(existing, "device_type", None), "slug", None)
            fake_type = getattr(existing, "device_type_slug", None)
            if (current_type or fake_type) != device_type_slug:
                changes["device_type"] = self._get_device_type(device_type_slug).id
            status_now = getattr(getattr(existing, "status", None), "value", None) or ""
            if status and status_now != status:
                changes["status"] = status
        if move_site and cross_site:
            changes["site"] = site.id
            if status:
                changes["status"] = status
        fields = dict(existing.custom_fields or {})
        fields_changed = False
        for key, value in identity.items():
            if value and str(fields.get(key) or "") != value:
                fields[key] = value
                fields_changed = True
        if fields_changed:
            changes["custom_fields"] = fields
        if changes:
            self._patch_device(existing, changes)
            if "name" in changes:
                existing.name = name
            if "serial" in changes:
                existing.serial = serial
            if "status" in changes:
                existing.status = changes["status"]
            if "site" in changes:
                existing.site = site
            if fields_changed:
                existing.custom_fields = fields
        if legacy or "device_type" in changes:
            if spec is not None:
                eth0 = self.align_device_components(existing, spec, tag_slug)
                logger.info(
                    "Aligned components on device %r after %s; eth0 %s",
                    existing.name,
                    "merge" if legacy else "device-type change",
                    eth0,
                )
        if legacy:
            logger.info(
                "Merged legacy client device id=%s %r into infrastructure %r (role %s, type %s); "
                "NetBox id, journal, and IPs kept",
                getattr(existing, "id", ""),
                name,
                name,
                role_slug,
                device_type_slug,
            )
        elif cross_site and "site" in changes:
            logger.info(
                "Moved device %r from site %r to %r (matched by %s)",
                existing.name,
                current_site,
                site_slug,
                match or "mac/serial",
            )
        return existing, False, bool(changes)

    @staticmethod
    def _source_site_fields(mac: str, source_site_name: str, source_site_internal_id: str) -> dict[str, str]:
        fields = {UNIFI_MAC_FIELD: mac}
        if source_site_name:
            fields[UNIFI_SITE_NAME_FIELD] = source_site_name
        if source_site_internal_id:
            fields[UNIFI_SITE_INTERNAL_ID_FIELD] = source_site_internal_id
        return fields

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
        self,
        site_slug: str,
        policy: str,
        name: str = "",
        unifi_site_id: str = "",
        unifi_internal_id: str = "",
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
        custom: dict[str, str] = {}
        if unifi_site_id:
            # Same value in both fields: the API site name, not the _id.
            custom[UNIFI_SITE_ID_FIELD] = unifi_site_id
            custom[UNIFI_SITE_NAME_FIELD] = unifi_site_id
        if unifi_internal_id:
            custom[UNIFI_SITE_INTERNAL_ID_FIELD] = unifi_internal_id
        if custom:
            payload["custom_fields"] = custom
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

        if device is not None:
            # A MAC that already belongs to infrastructure (including a client
            # device just merged into a switch) must not be rewritten as a client.
            current_role = role_slug_of(device)
            if current_role and current_role != role_slug:
                logger.info(
                    "Not treating %s as a client; NetBox device %r has role %s",
                    mac,
                    device.name,
                    current_role,
                )
                return device, False, False, False

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

        changes: dict[str, Any] = {}
        if device.name != name:
            changes["name"] = name
        if device.status.value != "active":
            changes["status"] = "active"
        if type_differs:
            changes["device_type"] = self._get_device_type(device_type_slug).id
            logger.info(
                "Updating %s device type %s -> %s", device.name, current_type, device_type_slug
            )
        if not changes:
            return device, False, False, False
        # PATCH the fields that actually changed. device.save() diffs against
        # the fetched record and usually does the same, but a nested field
        # that serializes differently (tags, custom fields, a choice object)
        # gets swept into that diff and overwrites operator data. The dict
        # below is the whole request body.
        self._patch_device(device, changes)
        return device, False, False, True

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
        IP (the "phone-01" error). If that device is one this sync manages,
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

    def _patch_device(self, device: Any, changes: dict[str, Any]) -> None:
        """PATCH exactly ``changes`` onto ``device``.

        Nothing else on the record is sent. Status transitions in particular
        must not rewrite name, comments, tenant, tags, or custom fields that
        were loaded alongside it.
        """
        if not changes:
            return
        req = Request(
            key=device.id,
            base=self.api.dcim.devices.url,
            token=self.api.token,
            http_session=self.api.http_session,
        )
        result = req.patch(dict(changes))
        if result and hasattr(device, "_parse_values"):
            device._parse_values(result)

    def mark_offline(self, device: Any) -> None:
        self._patch_device(device, {"status": "offline"})
