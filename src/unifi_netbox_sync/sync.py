from __future__ import annotations

import csv
import logging
import threading
import time
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

import pynetbox
import requests

from .config import Settings
from .devicetype_library import DeviceTypeLibrary, spec_from_unifi_device
from .model_map import device_kind
from .metrics import log_json_summary, write_prometheus_textfile
from .models import CableSkip, ClientSyncResult, SitePair, SiteSyncStats, SyncSummary, UnifiClient
from .naming import mac_suffixed_name, sanitize_device_name, slugify
from .netbox_client import (
    UNIFI_SITE_ID_FIELD,
    UNIFI_SITE_INTERNAL_ID_FIELD,
    UNIFI_SITE_NAME_FIELD,
    NetboxGateway,
    SwitchHints,
    device_has_tag,
    device_site_slug,
    role_slug_of,
    site_unifi_id,
    site_unifi_internal_id,
    site_unifi_name,
)
from .oui import OuiLookup, is_locally_administered
from .portmatch import (
    candidate_names,
    coerce_port,
    is_uplink_port,
    match_port,
    sfp_ordinal,
    templates_for_model,
)
from .staleness import format_grace, offline_decision
from .unifi_client import UnifiClientAPI

logger = logging.getLogger(__name__)

# Exceptions expected from a normal network/API hiccup on a single client.
# Anything else (AttributeError, TypeError, ...) is a programming error and
# should propagate and stop the run rather than being silently swallowed
# per-client.
EXPECTED_CLIENT_ERRORS = (requests.exceptions.RequestException, pynetbox.RequestError, LookupError)


def _interface_name(client: UnifiClient) -> str:
    return "eth0" if client.is_wired else "wlan0"


def _primary_ip_matches(device: object, ip: str) -> bool:
    """True if the device's primary IPv4 is already this address.

    Assumes the single interface this tool manages per client device is the
    one holding the primary IP — true for devices it created. If someone
    hand-moves the IP to another interface, this treats it as already-correct
    and leaves it alone, the same conservative posture as DEVICE_UPDATE_POLICY.
    """
    primary = getattr(device, "primary_ip4", None)
    if not primary:
        return False
    address = getattr(primary, "address", primary)
    return str(address).split("/")[0] == ip.split("/")[0]


@dataclass
class _CableContext:
    unifi_site: str
    netbox_site: str
    shared_ports: set[tuple[str, int]]


@dataclass
class _ResolvedCable:
    switch_device: object
    switch_iface: object
    names_tried: list[str]
    netbox_names: list[str]


def _shared_switch_ports(clients: list[UnifiClient]) -> set[tuple[str, int]]:
    """Switch ports that have more than one wired client this run.

    A single physical port cannot be a dedicated cable to several devices.
    That pattern is an uplink, or an unmanaged switch (often behind an AP),
    and cabling any of the clients to it invents a connection.
    """
    counts: dict[tuple[str, int], int] = {}
    for client in clients:
        if not client.is_wired or not client.switch_mac or client.switch_port is None:
            continue
        port = coerce_port(client.switch_port)
        if port is None:
            continue
        key = (client.switch_mac.lower(), port)
        counts[key] = counts.get(key, 0) + 1
    return {key for key, count in counts.items() if count > 1}


def _merge_last_seen(dest: dict[str, float | None], src: dict[str, float | None]) -> None:
    for mac, seen in src.items():
        if mac not in dest:
            dest[mac] = seen
            continue
        current = dest[mac]
        if seen is None:
            continue
        if current is None or seen > current:
            dest[mac] = seen


def _classify_cable_reason(detail: str) -> str:
    text = detail.lower()
    if "not tagged" in text:
        return "unmanaged_cable"
    if "already cabled" in text:
        return "already_cabled"
    if "no interface" in text:
        return "no_interfaces"
    return "cable_conflict"


def _format_last_seen(epoch: float | None) -> str:
    if epoch is None:
        return "unknown"
    return datetime.fromtimestamp(epoch, timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")


_CABLE_SKIP_FIELDS = (
    "site",
    "netbox_site",
    "switch",
    "switch_mac",
    "unifi_port",
    "names_tried",
    "netbox_interfaces",
    "reason",
)


def _write_cable_skip_report(path: str, skips: list[CableSkip]) -> None:
    dest = Path(path)
    if dest.parent and str(dest.parent) not in ("", "."):
        dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_suffix(dest.suffix + ".tmp")
    with tmp.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=_CABLE_SKIP_FIELDS)
        writer.writeheader()
        for skip in skips:
            writer.writerow(
                {
                    "site": skip.unifi_site,
                    "netbox_site": skip.netbox_site,
                    "switch": skip.switch,
                    "switch_mac": skip.switch_mac,
                    "unifi_port": skip.unifi_port,
                    "names_tried": skip.names_tried,
                    "netbox_interfaces": skip.netbox_interfaces,
                    "reason": skip.reason,
                }
            )
    tmp.replace(dest)


def _already_cabled_to(interface: object, peer_interface: object) -> bool:
    """True if `interface` already has a cable whose far end is `peer_interface`."""
    if not getattr(interface, "cable", None):
        return False
    peer_id = getattr(peer_interface, "id", None)
    if peer_id is None:
        return False
    return any(getattr(p, "id", None) == peer_id for p in getattr(interface, "link_peers", None) or [])


def _merge_summaries(summaries: list[SyncSummary]) -> SyncSummary:
    merged = SyncSummary()
    for s in summaries:
        merged.clients_seen += s.clients_seen
        merged.devices_created += s.devices_created
        merged.devices_updated += s.devices_updated
        merged.devices_update_skipped += s.devices_update_skipped
        merged.cables_created += s.cables_created
        merged.cables_skipped += s.cables_skipped
        merged.sites_created += s.sites_created
        merged.devices_synced += s.devices_synced
        merged.infra_created += s.infra_created
        merged.infra_merged += s.infra_merged
        merged.infra_moved += s.infra_moved
        merged.infra_duplicates_avoided += s.infra_duplicates_avoided
        merged.infra_type_library += s.infra_type_library
        merged.infra_type_generated += s.infra_type_generated
        merged.stale_marked_offline += s.stale_marked_offline
        merged.clients_unchanged += s.clients_unchanged
        merged.ips_unchanged += s.ips_unchanged
        merged.cables_unchanged += s.cables_unchanged
        merged.errors.extend(s.errors or [])
        merged.client_results.extend(s.client_results or [])
        merged.site_stats.extend(s.site_stats or [])
        merged.cable_skips.extend(s.cable_skips or [])
    return merged


class SyncEngine:
    """Syncs UniFi clients into NetBox for one or more UniFi-site -> NetBox-site
    pairs (see Settings.site_map / Settings.site_pairs()).

    Pairs are grouped by their *NetBox* site slug. Each group is one unit of
    work: its pairs run sequentially, and stale-device marking runs once at the
    end of the group over the union of every pair's clients. Groups touch
    disjoint NetBox sites by construction, so they can run concurrently —
    MAX_WORKERS controls how many at once, defaulting to 1 (the original
    strictly-sequential behavior). Grouping is what makes both the
    stale-marking union and the no-concurrent-writers-per-site guarantee
    structural rather than something callers must remember.

    What this does NOT protect against is multiple *instances* of this tool (or
    unrelated automation) writing the same NetBox concurrently: there is no
    distributed lock, so that can still race on cable creation/deletion. The
    Docker entrypoint's LOCK_FILE guards a single instance against overlapping
    itself; separate hosts/containers syncing the same NetBox are on you.

    One client MAC appearing in two UniFi sites mapped to *different* NetBox
    sites is also unhandled: the by-MAC device lookup is global, so those two
    groups would fight over the device's site assignment. Don't map a roaming
    client's sites that way.

    Also assumes the UniFi controller returns its full active-client list in
    one `/stat/sta` call per site, and client history in one `/stat/alluser`
    call (true for the classic controller API this client uses); there is no
    pagination handling. Offline marking compares `last_seen` from that
    history to `STALE_OFFLINE_GRACE` and does nothing when the history read
    fails.
    """

    def __init__(self, unifi: UnifiClientAPI, netbox: NetboxGateway, settings: Settings) -> None:
        self.unifi = unifi
        self.netbox = netbox
        self.settings = settings
        self._oui = OuiLookup(settings.oui_file)
        self._library = DeviceTypeLibrary(settings.devicetype_library_path)
        # UniFi switch MAC -> what UniFi knows about that switch. Populated
        # per site from /stat/device, and used to find the switch in NetBox by
        # name or serial when its interfaces carry no MAC address.
        self._switch_hints: dict[str, SwitchHints] = {}
        self._hints_lock = threading.Lock()
        # One warning per switch, not one per client behind it.
        self._warned_switches: set[str] = set()
        # Port-name warnings are also once per switch. Every skipped cable is
        # still recorded on the run summary and, when configured, the CSV.
        self._warned_ports: set[str] = set()
        # UniFi site id -> human label, for matching NetBox sites by name.
        self._site_labels: dict[str, str] = {}
        # API site name -> controller _id.
        self._site_internal_ids: dict[str, str] = {}
        # NetBox slugs this run treats as inventory/staging sites.
        self._inventory_netbox_slugs: set[str] = set()
        # Serializes find-then-create across site workers so two sites cannot
        # each create the same chassis.
        self._infra_lock = threading.Lock()
        # Conflicts found while linking sites. Merged into the run summary so
        # a collapsed mapping cannot look like a successful sync.
        self._link_errors: list[str] = []

    # -- orchestration ---------------------------------------------------

    def _load_controller_sites(self) -> None:
        """Best-effort site list, so explicit SITE_MAP entries gain an _id."""
        try:
            sites = self.unifi.get_sites()
        except Exception as exc:
            logger.warning(
                "Could not list UniFi sites (%s); site matching will use the API site name only",
                exc,
            )
            return
        for site in sites:
            self._site_labels.setdefault(site.name, site.label)
            if site.internal_id:
                self._site_internal_ids[site.name] = site.internal_id

    def _discover_site_pairs(self) -> list[SitePair]:
        """Ask the controller which sites exist and derive a NetBox slug each.

        The slug comes from the site's description ("Head Office" ->
        "head-office") rather than its API id, which is an opaque string like
        "7xk2p9qr" and meaningless in NetBox. An explicit SITE_MAP entry always
        wins for the site it names.
        """
        sites = self.unifi.get_sites()
        if not sites:
            raise LookupError(
                "SITE_MAP=* is set but the controller reported no sites; check that this "
                "account can see them"
            )

        pairs: list[SitePair] = []
        slug_owners: dict[str, str] = {}
        for site in sites:
            override = self.settings.site_map.get(site.name)
            slug = override or slugify(site.label) or slugify(site.name)
            if not slug:
                logger.warning("Skipping UniFi site %r: no usable NetBox slug from %r",
                               site.name, site.label)
                continue
            if slug in slug_owners:
                logger.warning(
                    "UniFi sites %r and %r both map to NetBox site '%s'. That is a conflict: "
                    "both will be skipped and unifi_site_id will not be recorded. Pin one with "
                    "an explicit SITE_MAP entry so each NetBox site has a single UniFi site.",
                    slug_owners[slug],
                    site.name,
                    slug,
                )
            slug_owners[slug] = site.name
            self._site_labels[site.name] = site.label
            if site.internal_id:
                self._site_internal_ids[site.name] = site.internal_id
            pairs.append(
                SitePair(
                    unifi_site=site.name,
                    netbox_site_slug=slug,
                    unifi_internal_id=site.internal_id,
                )
            )

        logger.info(
            "Discovered %d site(s) from the controller: %s",
            len(pairs),
            ", ".join(f"{p.unifi_site}->{p.netbox_site_slug}" for p in pairs),
        )
        if self.settings.site_policy != "create":
            # Discovering sites you haven't hand-created in NetBox is the norm,
            # so say this up front rather than letting every site fail in turn.
            logger.info(
                "SITE_POLICY=%s: any discovered site missing from NetBox will be reported as an "
                "error rather than created. Set SITE_POLICY=create to have them created.",
                self.settings.site_policy,
            )
        return pairs

    def run(self) -> SyncSummary:
        start = time.monotonic()
        if self.settings.sync_all_sites:
            pairs = self._discover_site_pairs()
        else:
            # SITE_MAP still decides the slug. The controller site list, when
            # readable, only adds the immutable _id so a later rename of the
            # UniFi description still matches.
            self._load_controller_sites()
            pairs = self.settings.site_pairs()
            for pair in pairs:
                if not pair.unifi_internal_id:
                    pair.unifi_internal_id = self._site_internal_ids.get(pair.unifi_site, "")

        if not self.settings.dry_run:
            # The id custom field has to exist before linking, which runs
            # before per-site prerequisites. A failure here degrades to name
            # matching for this run; it does not abort the others.
            try:
                self.netbox.ensure_site_identity_field()
            except Exception as exc:
                logger.warning(
                    "Could not ensure the %s custom field (%s); site linking will use names only",
                    UNIFI_SITE_ID_FIELD,
                    exc,
                )

        pairs = self._link_existing_sites(pairs)
        tokens = set(self.settings.inventory_site_ids)
        self._inventory_netbox_slugs = {
            pair.netbox_site_slug
            for pair in pairs
            if pair.unifi_site in tokens or (pair.unifi_internal_id and pair.unifi_internal_id in tokens)
        }

        groups: dict[str, list[SitePair]] = {}
        for pair in pairs:
            groups.setdefault(pair.netbox_site_slug, []).append(pair)

        workers = max(1, min(self.settings.max_workers, len(groups)))
        logger.info(
            "Policies: site=%s, device_update=%s, cable_conflict=%s, merge_client_infra=%s, "
            "inventory_sites=%s | %d pair(s) in %d NetBox site group(s), %d worker(s)",
            self.settings.site_policy,
            self.settings.device_update_policy,
            self.settings.cable_conflict_policy,
            self.settings.merge_client_infra,
            ",".join(self.settings.inventory_site_ids) or "(none)",
            len(pairs),
            len(groups),
            workers,
        )

        if workers == 1:
            group_summaries = [self._run_group(slug, gp) for slug, gp in groups.items()]
        else:
            with ThreadPoolExecutor(max_workers=workers, thread_name_prefix="site") as pool:
                group_summaries = list(
                    pool.map(lambda item: self._run_group(item[0], item[1]), list(groups.items()))
                )

        summary = _merge_summaries(group_summaries)
        if self._link_errors:
            summary.errors.extend(self._link_errors)
        summary.duration_seconds = time.monotonic() - start
        summary.cache_hits = getattr(self.netbox, "hits", 0)
        summary.cache_misses = getattr(self.netbox, "misses", 0)
        if hasattr(self.netbox, "log_stats"):
            self.netbox.log_stats()

        logger.info(
            "Sync complete: %d clients (%d unchanged), %d created, %d updated, %d update-skipped "
            "(policy), %d cables created, %d cables skipped, %d marked offline, %d errors, %.1fs "
            "across %d site pair(s)",
            summary.clients_seen,
            summary.clients_unchanged,
            summary.devices_created,
            summary.devices_updated,
            summary.devices_update_skipped,
            summary.cables_created,
            summary.cables_skipped,
            summary.stale_marked_offline,
            len(summary.errors),
            summary.duration_seconds,
            len(pairs),
        )
        self._log_cable_skips(summary)
        log_json_summary(summary)
        if self.settings.metrics_file:
            write_prometheus_textfile(summary, self.settings.metrics_file)
        return summary

    def _log_cable_skips(self, summary: SyncSummary) -> None:
        skips = summary.cable_skips or []
        if skips:
            counts = Counter(skip.reason for skip in skips)
            logger.warning(
                "Cable skips by reason: %s",
                ", ".join(f"{reason}={count}" for reason, count in sorted(counts.items())),
            )
        path = self.settings.cable_skip_report
        if not path:
            return
        _write_cable_skip_report(path, skips)
        logger.info("Wrote cable skip report to %s (%d row(s))", path, len(skips))

    def _link_existing_sites(self, pairs: list[SitePair]) -> list[SitePair]:
        """Bind each pair to the NetBox site that already represents it.

        A derived slug is a guess at what the site is called in NetBox, and a
        guess that misses creates a duplicate rather than adding to the site
        you already have. Punctuation is the usual culprit: "Acme's Depot"
        slugifies to `acme-s-depot`, which does not equal an existing
        `acmes-depot`.

        A derived slug that misses is left as-is. Two UniFi sites that resolve
        to the same NetBox site are a conflict: neither is synced and the
        site's ``unifi_site_id`` is left untouched, so one site cannot be
        claimed by every UniFi site that happened to sort first.

        Matched sites are only ever linked to, never renamed.
        """
        checked: list[tuple[SitePair, object | None]] = []
        for pair in pairs:
            label = self._site_labels.get(pair.unifi_site, "")
            try:
                site = self.netbox.resolve_existing_site(
                    pair.netbox_site_slug,
                    label,
                    self.settings.site_match,
                    pair.unifi_site,
                    unifi_internal_id=pair.unifi_internal_id,
                )
            except Exception as exc:
                logger.warning(
                    "Could not check NetBox for an existing site matching '%s' (%s); "
                    "continuing with that slug",
                    pair.netbox_site_slug,
                    exc,
                )
                checked.append((pair, None))
                continue
            checked.append((pair, site))

        by_slug: dict[str, list[SitePair]] = {}
        for pair, site in checked:
            slug = site.slug if site is not None else pair.netbox_site_slug
            by_slug.setdefault(slug, []).append(pair)
        conflicts = {slug for slug, group in by_slug.items() if len(group) > 1}
        for slug in sorted(conflicts):
            ids = ", ".join(pair.unifi_site for pair in by_slug[slug])
            message = (
                f"UniFi sites {ids} all resolve to NetBox site '{slug}'; skipping them and "
                f"not recording {UNIFI_SITE_ID_FIELD}. Each NetBox site can belong to only "
                "one UniFi site."
            )
            logger.error(message)
            self._link_errors.append(message)

        resolved: list[SitePair] = []
        for pair, site in checked:
            slug = site.slug if site is not None else pair.netbox_site_slug
            if slug in conflicts:
                continue
            if site is None:
                resolved.append(pair)
                continue
            if site.slug != pair.netbox_site_slug:
                logger.info(
                    "Linking UniFi site '%s' to existing NetBox site '%s' (%r) instead of "
                    "creating '%s'. Its name and slug are left unchanged.",
                    pair.unifi_site,
                    site.slug,
                    getattr(site, "name", ""),
                    pair.netbox_site_slug,
                )
            self._remember_site_link(site, pair.unifi_site, pair.unifi_internal_id)
            resolved.append(
                SitePair(
                    unifi_site=pair.unifi_site,
                    netbox_site_slug=site.slug,
                    unifi_internal_id=pair.unifi_internal_id,
                )
            )
        return resolved

    def _remember_site_link(self, site: object, unifi_site_id: str, unifi_internal_id: str = "") -> None:
        """Stamp UniFi identifiers onto a matched site, without renaming it.

        ``unifi_site_id`` keeps the API site name. An id that is already set
        to a different site is never replaced. When it already matches, empty
        ``unifi_site_name`` / ``unifi_site_internal_id`` fields are still filled.
        """
        bound = site_unifi_id(site)
        if bound and bound != unifi_site_id:
            message = (
                f"Refusing to overwrite {UNIFI_SITE_ID_FIELD} on NetBox site "
                f"{getattr(site, 'slug', '')!r}: already {bound!r}, not {unifi_site_id!r}"
            )
            logger.error(message)
            self._link_errors.append(message)
            return
        bound_internal = site_unifi_internal_id(site)
        if bound_internal and unifi_internal_id and bound_internal != unifi_internal_id:
            message = (
                f"Refusing to overwrite {UNIFI_SITE_INTERNAL_ID_FIELD} on NetBox site "
                f"{getattr(site, 'slug', '')!r}: already {bound_internal!r}, not {unifi_internal_id!r}"
            )
            logger.error(message)
            self._link_errors.append(message)
            return
        needs_name = bool(unifi_site_id) and not site_unifi_name(site)
        needs_id = bool(unifi_site_id) and not bound
        needs_internal = bool(unifi_internal_id) and not bound_internal
        if not (needs_name or needs_id or needs_internal):
            return
        if self.settings.dry_run:
            logger.info(
                "[dry-run] would record %s=%r on NetBox site %r (%r) without changing its name or slug",
                UNIFI_SITE_ID_FIELD,
                unifi_site_id,
                getattr(site, "slug", ""),
                getattr(site, "name", ""),
            )
            if needs_name or needs_internal:
                logger.info(
                    "[dry-run] would record %s=%r and %s=%r on NetBox site %r",
                    UNIFI_SITE_NAME_FIELD,
                    unifi_site_id,
                    UNIFI_SITE_INTERNAL_ID_FIELD,
                    unifi_internal_id or "(unknown)",
                    getattr(site, "slug", ""),
                )
            return
        try:
            self.netbox.remember_site_link(site, unifi_site_id, unifi_internal_id)
        except Exception as exc:
            logger.warning(
                "Could not record %s on NetBox site %r (%s); it will be matched by name until this succeeds",
                UNIFI_SITE_ID_FIELD,
                getattr(site, "slug", ""),
                exc,
            )

    def _run_group(self, netbox_site_slug: str, pairs: list[SitePair]) -> SyncSummary:
        """Sync every UniFi site mapped to one NetBox site, then stale-mark once."""
        summaries: list[SyncSummary] = []
        seen_macs: set[str] = set()
        known_last_seen: dict[str, float | None] = {}
        history_ok = True
        any_pair_failed = False

        for pair in pairs:
            pair_start = time.monotonic()
            try:
                pair_summary, pair_macs, pair_known, pair_history_ok = self._run_site(pair)
                pair_summary.site_stats = [
                    SiteSyncStats(
                        unifi_site=pair.unifi_site,
                        netbox_site_slug=pair.netbox_site_slug,
                        clients_seen=pair_summary.clients_seen,
                        devices_created=pair_summary.devices_created,
                        devices_updated=pair_summary.devices_updated,
                        cables_created=pair_summary.cables_created,
                        errors=len(pair_summary.errors or []),
                        duration_seconds=time.monotonic() - pair_start,
                    )
                ]
                summaries.append(pair_summary)
                seen_macs.update(pair_macs)
                _merge_last_seen(known_last_seen, pair_known)
                history_ok = history_ok and pair_history_ok
            except Exception as exc:
                # Isolate the site. A missing site, a timed-out HTTP call, or
                # a programming error in one site must not abort the others or
                # skip the run summary — that is what left a hung worker with
                # no further log line. Stale-marking still waits until every
                # pair in the group succeeded, because a partial client list
                # is not evidence that a device is gone.
                logger.exception(
                    "Failed syncing site '%s' -> NetBox '%s'", pair.unifi_site, pair.netbox_site_slug
                )
                summaries.append(
                    SyncSummary(
                        errors=[f"{pair.unifi_site}->{pair.netbox_site_slug}: {exc}"],
                        site_stats=[
                            SiteSyncStats(
                                unifi_site=pair.unifi_site,
                                netbox_site_slug=pair.netbox_site_slug,
                                errors=1,
                                duration_seconds=time.monotonic() - pair_start,
                            )
                        ],
                    )
                )
                any_pair_failed = True

        group = _merge_summaries(summaries)

        if self.settings.mark_stale_offline:
            if any_pair_failed or not history_ok:
                # seen_macs or last_seen is incomplete for this NetBox site, so
                # marking now could offline a client that is only idle, or one
                # a failed pair would have reported. Not knowing is not the
                # same as the device being gone.
                logger.warning(
                    "Skipping stale-device marking for NetBox site '%s': %s",
                    netbox_site_slug,
                    "a mapped UniFi site failed to sync this run"
                    if any_pair_failed
                    else "UniFi client history (last_seen) was not available",
                )
            else:
                group.stale_marked_offline = self._mark_stale(
                    netbox_site_slug, seen_macs, known_last_seen
                )

        return group

    def _run_site(
        self, pair: SitePair
    ) -> tuple[SyncSummary, set[str], dict[str, float | None], bool]:
        """Syncs one pair's clients. Stale marking is the group's job, not this."""
        summary = SyncSummary()
        s = self.settings
        slug = pair.netbox_site_slug

        label = self._site_labels.get(pair.unifi_site, "")
        if not s.dry_run:
            # The site-id custom field was ensured at the start of the run, so
            # a site created here can store it. Prerequisites (roles, the
            # generic device type) wait until the site is known to exist, so a
            # missing site does not create those objects on the way to failing.
            _site, created = self.netbox.ensure_site(
                slug, s.site_policy, label, pair.unifi_site, pair.unifi_internal_id
            )
            summary.sites_created = 1 if created else 0
            logger.info("NetBox site '%s': %s", slug, "created" if created else "reused existing")
            self.netbox.ensure_prerequisites(
                s.client_device_role_slug,
                s.client_manufacturer_slug,
                s.client_device_type_slug,
                s.sync_tag,
            )
        else:
            if self.netbox.find_site(slug) is None:
                if s.site_policy == "create":
                    summary.sites_created = 1
                    logger.info("[dry-run] would create NetBox site '%s' (SITE_POLICY=create)", slug)
                else:
                    logger.warning(
                        "[dry-run] NetBox site '%s' does not exist and SITE_POLICY=%s; "
                        "a real run would fail here",
                        slug,
                        s.site_policy,
                    )
            logger.info("[dry-run] would ensure NetBox tag/custom-field/role/device-type exist")

        self._load_switch_hints(pair.unifi_site)

        if s.sync_unifi_devices:
            # Before clients, so a switch created now is immediately available
            # to terminate this same run's cables.
            infra = self._sync_unifi_devices(pair)
            summary.devices_synced = infra.devices_synced
            summary.infra_created = infra.infra_created
            summary.infra_merged = infra.infra_merged
            summary.infra_moved = infra.infra_moved
            summary.infra_duplicates_avoided = infra.infra_duplicates_avoided
            summary.infra_type_library = infra.infra_type_library
            summary.infra_type_generated = infra.infra_type_generated

        clients = self.unifi.get_clients(pair.unifi_site)
        summary.clients_seen = len(clients)
        seen_macs: set[str] = set()
        known_last_seen, history_ok = self._load_known_clients(pair.unifi_site)
        if history_ok and not known_last_seen and clients:
            # An empty history next to a non-empty active list means the
            # history endpoint returned nothing useful. Treating every client
            # that is merely idle as gone is the failure this grace exists
            # to stop.
            logger.warning(
                "UniFi site '%s' has %d active client(s) but no last_seen history; "
                "skipping offline marking so idle clients are not marked offline",
                pair.unifi_site,
                len(clients),
            )
            history_ok = False
        shared_ports = _shared_switch_ports(clients)
        cable_ctx = _CableContext(
            unifi_site=pair.unifi_site,
            netbox_site=slug,
            shared_ports=shared_ports,
        )
        logger.info(
            "Syncing %d client(s) for UniFi site '%s' -> NetBox '%s'",
            len(clients),
            pair.unifi_site,
            slug,
        )

        for index, client in enumerate(clients, start=1):
            seen_macs.add(client.mac)
            if index % 100 == 0:
                logger.info(
                    "Site '%s': processed %d/%d clients", pair.unifi_site, index, len(clients)
                )
            result = self._sync_client(client, slug, cable_ctx)
            summary.client_results.append(result)
            if result.error:
                summary.errors.append(f"{pair.unifi_site}/{client.mac}: {result.error}")
                continue
            if result.device_created:
                summary.devices_created += 1
            elif result.device_updated:
                summary.devices_updated += 1
            elif result.device_update_skipped_reason:
                summary.devices_update_skipped += 1
            if result.unchanged:
                summary.clients_unchanged += 1
            if result.ip_unchanged:
                summary.ips_unchanged += 1
            if result.cable_unchanged:
                summary.cables_unchanged += 1
            if result.cable_created:
                summary.cables_created += 1
            elif result.cable_skipped_reason:
                summary.cables_skipped += 1
                if result.cable_skip is not None:
                    summary.cable_skips.append(result.cable_skip)

        logger.info(
            "Site '%s' -> NetBox '%s': %d clients, %d created, %d updated, %d unchanged, "
            "%d cables created, %d cables skipped",
            pair.unifi_site,
            slug,
            summary.clients_seen,
            summary.devices_created,
            summary.devices_updated,
            summary.clients_unchanged,
            summary.cables_created,
            summary.cables_skipped,
        )
        return summary, seen_macs, known_last_seen, history_ok

    def _load_switch_hints(self, unifi_site: str) -> None:
        """Record each UniFi device's name/serial, keyed by MAC.

        Without this the only way to find a switch in NetBox is by an
        interface `mac_address`, which most deployments never populate — so
        every cable gets skipped even though the switch is plainly there.
        """
        try:
            devices = self.unifi.get_devices(unifi_site)
        except EXPECTED_CLIENT_ERRORS as exc:
            # Non-fatal: we simply lose the name/serial fallbacks.
            logger.warning(
                "Could not list UniFi devices for site '%s' (%s); switch matching will rely on "
                "interface MAC addresses alone",
                unifi_site,
                exc,
            )
            return
        with self._hints_lock:
            for device in devices:
                if device.mac:
                    self._switch_hints[device.mac] = SwitchHints(
                        name=device.name,
                        serial=device.serial,
                        model=device.model,
                        ports=tuple(device.ports),
                    )
        logger.info("Loaded %d UniFi device(s) for site '%s' to match switches by name/serial",
                    len(devices), unifi_site)

    def _role_for(self, device: object) -> tuple[str, str]:
        """Return (kind, role slug) for an adopted UniFi device."""
        s = self.settings
        kind = device_kind(device)
        role = {
            "switch": s.unifi_switch_role_slug,
            "ap": s.unifi_ap_role_slug,
            "gateway": s.unifi_gateway_role_slug,
            "power": s.unifi_power_role_slug,
        }.get(kind, s.unifi_device_role_slug)
        return kind, role

    def _type_spec(self, device: object):
        spec = self._library.lookup(getattr(device, "model", "") or "")
        if spec is not None:
            return spec, "library"
        return spec_from_unifi_device(device), "generated"

    def _is_inventory_pair(self, pair: SitePair) -> bool:
        tokens = set(self.settings.inventory_site_ids)
        if not tokens:
            return False
        return pair.unifi_site in tokens or (
            bool(pair.unifi_internal_id) and pair.unifi_internal_id in tokens
        )

    def _device_from_inventory(self, existing: object) -> bool:
        fields = getattr(existing, "custom_fields", None) or {}
        tokens = set(self.settings.inventory_site_ids)
        source_name = str(fields.get(UNIFI_SITE_NAME_FIELD) or "")
        source_internal = str(fields.get(UNIFI_SITE_INTERNAL_ID_FIELD) or "")
        if source_name and source_name in tokens:
            return True
        if source_internal and source_internal in tokens:
            return True
        site = device_site_slug(existing)
        return bool(site) and site in self._inventory_netbox_slugs

    def _sync_unifi_devices(self, pair: SitePair) -> SyncSummary:
        """Create, merge, or move adopted UniFi infrastructure."""
        summary = SyncSummary()
        s = self.settings
        try:
            devices = self.unifi.get_devices(pair.unifi_site)
        except EXPECTED_CLIENT_ERRORS as exc:
            logger.warning("Could not list UniFi devices for '%s': %s", pair.unifi_site, exc)
            return summary

        adopted = [d for d in devices if d.adopted and d.mac]
        summary.devices_synced = len(adopted)
        if not adopted:
            return summary

        inventory = self._is_inventory_pair(pair)
        created_by_kind: Counter[str] = Counter()
        prefix = "[dry-run] " if s.dry_run else ""

        for device in adopted:
            kind, role_slug = self._role_for(device)
            spec, source = self._type_spec(device)
            status = s.inventory_device_status if inventory else "active"
            with self._infra_lock:
                try:
                    self._sync_one_unifi_device(
                        pair,
                        device,
                        kind,
                        role_slug,
                        spec,
                        source,
                        status,
                        inventory,
                        summary,
                        created_by_kind,
                        prefix,
                    )
                except EXPECTED_CLIENT_ERRORS as exc:
                    logger.exception("Failed syncing UniFi device %s (%s)", device.mac, device.name)
                    raise LookupError(f"UniFi device {device.name} ({device.mac}): {exc}") from exc

        kind_text = ", ".join(f"{name}={created_by_kind[name]}" for name in ("switch", "ap", "gateway", "power"))
        logger.info(
            "%sSite '%s': %d adopted UniFi device(s); creates by kind: %s; "
            "merged=%d; moved=%d; cross-site matches avoided=%d; "
            "type sources library=%d generated=%d",
            prefix,
            pair.unifi_site,
            len(adopted),
            kind_text,
            summary.infra_merged,
            summary.infra_moved,
            summary.infra_duplicates_avoided,
            summary.infra_type_library,
            summary.infra_type_generated,
        )
        if s.dry_run:
            # Dry-run logs the plan and writes nothing. Counters stay at zero
            # so a preview is not mistaken for a completed write. The lines
            # above are the record of what would have happened.
            return SyncSummary(devices_synced=len(adopted))
        return summary

    def _sync_one_unifi_device(
        self,
        pair: SitePair,
        device: object,
        kind: str,
        role_slug: str,
        spec: object,
        source: str,
        status: str,
        inventory: bool,
        summary: SyncSummary,
        created_by_kind: Counter,
        prefix: str,
    ) -> None:
        s = self.settings
        mac = device.mac
        serial = device.serial or ""
        existing, match = self.netbox.find_unifi_device(mac, serial, device.name, pair.netbox_site_slug)
        is_legacy_client = (
            existing is not None
            and role_slug_of(existing) == s.client_device_role_slug
            and device_has_tag(existing, s.sync_tag)
            and str((getattr(existing, "custom_fields", None) or {}).get("unifi_mac") or "").lower() == mac.lower()
        )
        # Merging is independent of DEVICE_UPDATE_POLICY. When it is off, the
        # client device stays a client and no second device is created.
        legacy = is_legacy_client and s.merge_client_infra
        current_site = device_site_slug(existing) if existing is not None else ""
        cross_site = bool(existing is not None and current_site and current_site != pair.netbox_site_slug)
        tagged = existing is None or device_has_tag(existing, s.sync_tag)
        interface_names = [name for name, _ in spec.interfaces]
        outlet_count = len(getattr(spec, "power_outlets", None) or [])

        if existing is not None and not tagged:
            summary.infra_duplicates_avoided += 1
            logger.info(
                "%scross-site match avoided: UniFi %s %r (%s) matches untagged NetBox device "
                "id=%s %r at site %r by %s; not creating a duplicate",
                prefix,
                kind,
                device.name,
                device.model,
                getattr(existing, "id", ""),
                getattr(existing, "name", ""),
                current_site or "?",
                match,
            )
            return

        # Inventory must not relocate a device that already lives elsewhere,
        # and must not create a second one. A tagged legacy client is the
        # exception: MERGE_CLIENT_INFRA converts it in place (same NetBox id
        # and site) instead of skipping the match.
        if inventory and existing is not None and cross_site and not legacy:
            summary.infra_duplicates_avoided += 1
            logger.info(
                "%sinventory site %r: %s %r (%s) matches NetBox device id=%s %r at site %r by %s; "
                "not creating a duplicate and not moving it",
                prefix,
                pair.unifi_site,
                kind,
                device.name,
                device.model,
                getattr(existing, "id", ""),
                getattr(existing, "name", ""),
                current_site,
                match,
            )
            return

        from_inventory = existing is not None and self._device_from_inventory(existing)
        move_site = bool(
            cross_site
            and not inventory
            and (s.device_update_policy != "create-only" or from_inventory or legacy)
        )

        if existing is not None and cross_site and not move_site and not legacy and not inventory:
            summary.infra_duplicates_avoided += 1
            logger.info(
                "%scross-site match avoided: UniFi %s %r (%s) matches NetBox device id=%s %r "
                "at site %r by %s; leaving it there (DEVICE_UPDATE_POLICY=%s)",
                prefix,
                kind,
                device.name,
                device.model,
                getattr(existing, "id", ""),
                getattr(existing, "name", ""),
                current_site,
                match,
                s.device_update_policy,
            )
            return

        if is_legacy_client and not s.merge_client_infra:
            summary.infra_duplicates_avoided += 1
            logger.info(
                "%slegacy client device id=%s %r matches adopted %s %r by MAC; "
                "MERGE_CLIENT_INFRA=false so it is left unchanged and not duplicated",
                prefix,
                getattr(existing, "id", ""),
                getattr(existing, "name", ""),
                kind,
                device.name,
            )
            return

        eth0_plan = ""
        if legacy:
            eth0_plan = self.netbox.legacy_eth0_plan(existing, interface_names)

        if s.dry_run:
            # Count on the local summary so the site line shows the plan.
            # The caller discards these counts for a dry-run.
            if source == "library":
                summary.infra_type_library += 1
            else:
                summary.infra_type_generated += 1
            if legacy:
                summary.infra_merged += 1
                # move_site is false for an inventory observation, so a
                # cross-site legacy client keeps the site it already has.
                kept_site = (
                    current_site
                    if cross_site and not move_site and current_site
                    else pair.netbox_site_slug
                )
                logger.info(
                    "[dry-run] would merge legacy client device id=%s name=%r (MAC %s) into %s %r "
                    "role=%r type=%r source=%s status=%s site=%r; eth0 %s",
                    getattr(existing, "id", ""),
                    getattr(existing, "name", ""),
                    mac,
                    kind,
                    device.name,
                    role_slug,
                    spec.slug,
                    source,
                    status,
                    kept_site,
                    eth0_plan,
                )
            elif move_site:
                summary.infra_moved += 1
                logger.info(
                    "[dry-run] would move NetBox device id=%s %r from site %r to %r "
                    "(matched by %s) and set status=%s role=%r type=%r source=%s",
                    getattr(existing, "id", ""),
                    getattr(existing, "name", ""),
                    current_site,
                    pair.netbox_site_slug,
                    match,
                    status,
                    role_slug,
                    spec.slug,
                    source,
                )
            elif existing is None:
                created_by_kind[kind] += 1
                summary.infra_created += 1
                logger.info(
                    "[dry-run] would create %s %r (%s) role=%r status=%s type=%r source=%s "
                    "interfaces=%d power_outlets=%d",
                    kind,
                    device.name,
                    device.model,
                    role_slug,
                    status,
                    spec.slug,
                    source,
                    len(interface_names),
                    outlet_count,
                )
            else:
                logger.info(
                    "[dry-run] would keep %s %r (%s) role=%r status=%s type=%r source=%s "
                    "(already present, matched by %s)",
                    kind,
                    device.name,
                    device.model,
                    role_slug,
                    status,
                    spec.slug,
                    source,
                    match,
                )
            return

        self.netbox.ensure_device_role(role_slug)
        type_slug = self.netbox.ensure_device_type_from_spec(spec)
        _dev, created, updated = self.netbox.upsert_infrastructure_device(
            mac,
            device.name,
            pair.netbox_site_slug,
            role_slug,
            type_slug,
            s.sync_tag,
            serial,
            s.device_update_policy,
            status=status,
            merge_client=legacy,
            client_role_slug=s.client_device_role_slug,
            move_site=move_site,
            source_site_name=pair.unifi_site,
            source_site_internal_id=pair.unifi_internal_id,
            spec=spec,
            looked_up=True,
            existing=existing,
            match=match,
        )
        if source == "library":
            summary.infra_type_library += 1
        else:
            summary.infra_type_generated += 1
        if created:
            summary.infra_created += 1
            created_by_kind[kind] += 1
            logger.info(
                "Created %s %r (%s) role=%r status=%s type=%r source=%s "
                "interfaces=%d power_outlets=%d",
                kind,
                device.name,
                device.model,
                role_slug,
                status,
                type_slug,
                source,
                len(interface_names),
                outlet_count,
            )
            self._forget_switch(mac)
        elif legacy:
            summary.infra_merged += 1
            if move_site:
                summary.infra_moved += 1
            logger.info(
                "Merged legacy client device id=%s into %s %r role=%r type=%r source=%s; eth0 %s",
                getattr(_dev, "id", ""),
                kind,
                device.name,
                role_slug,
                type_slug,
                source,
                eth0_plan,
            )
            self._forget_switch(mac)
        elif move_site and updated:
            summary.infra_moved += 1
            self._forget_switch(mac)

    def _forget_switch(self, mac: str) -> None:
        """Invalidate a cached negative switch lookup after creating it."""
        forget = getattr(self.netbox, "forget_switch", None)
        if forget is not None:
            forget(mac)

    def _hints_for(self, mac: str) -> SwitchHints | None:
        with self._hints_lock:
            return self._switch_hints.get(mac)

    def _log_switch_not_found(self, mac: str) -> None:
        """Explain an unmatched switch once, with the fixes that actually work.

        Warned rather than debugged because the symptom otherwise is silent:
        the run simply reports skipped cables with no clue why.
        """
        with self._hints_lock:
            if mac in self._warned_switches:
                return
            self._warned_switches.add(mac)
            hints = self._switch_hints.get(mac)
        logger.warning(
            "No NetBox device found for UniFi switch %s%s, so its cables are skipped. Tried: "
            "interface mac_address, device custom field unifi_mac, device serial, device name. "
            "No infrastructure device will be created. Match an existing NetBox device "
            "(same name as UniFi, its serial, or a unifi_mac custom field) or inventory the "
            "switch yourself.",
            mac,
            f" (named {hints.name!r} in UniFi)" if hints and hints.name else "",
        )

    def _log_port_mismatch(
        self,
        switch_device: object,
        port: object,
        candidates: list[str],
        names: list[str],
        *,
        ambiguous: list[str] | None = None,
    ) -> None:
        """Say what the switch's ports are actually called, once per switch.

        Every skipped cable is recorded on the run summary (and the CSV when
        configured). Repeating this warning per client buried the one fact an
        operator needs: the names that switch actually uses.
        """
        device_name = getattr(switch_device, "name", "?")
        key = str(getattr(switch_device, "id", None) or device_name)
        with self._hints_lock:
            if key in self._warned_ports:
                return
            self._warned_ports.add(key)
        if not names:
            logger.warning(
                "NetBox device %r has no interfaces, so cables for UniFi port %s are skipped. "
                "No device or interface will be created to fill the gap; add the interfaces "
                "on that device in NetBox.",
                device_name,
                port,
            )
            return
        if ambiguous:
            logger.warning(
                "UniFi port %s on %r matched more than one NetBox interface (%s). "
                "Looked for %s; that device's interfaces are named %s%s. "
                "Pin the scheme with PORT_MODEL_TEMPLATES. No cable will be created.",
                port,
                device_name,
                ambiguous,
                candidates,
                names[:12],
                f" (+{len(names) - 12} more)" if len(names) > 12 else "",
            )
            return
        logger.warning(
            "UniFi port %s on %r matched no NetBox interface. Looked for %s; that device's "
            "interfaces are named %s%s. Set PORT_NAME_TEMPLATES or PORT_MODEL_TEMPLATES to match. "
            "No interface will be created. Every skipped cable is listed in the run summary.",
            port,
            device_name,
            candidates,
            names[:12],
            f" (+{len(names) - 12} more)" if len(names) > 12 else "",
        )

    def _client_device_type(self, client: UnifiClient) -> str:
        """Device type slug for a client, carrying its real manufacturer when known."""
        s = self.settings
        if not s.use_oui_manufacturer:
            return s.client_device_type_slug

        vendor = client.oui
        if not vendor and not is_locally_administered(client.mac):
            # Randomized/locally-administered MACs have no owning vendor, so
            # only fall back to the offline table for real burned-in addresses.
            vendor = self._oui.lookup(client.mac)
        if not vendor:
            return s.client_device_type_slug

        try:
            return self.netbox.ensure_client_device_type(vendor)
        except ValueError:
            logger.warning("Unusable manufacturer name %r for %s; using the generic type",
                           vendor, client.mac)
            return s.client_device_type_slug

    # -- per-client ------------------------------------------------------

    def _resolve_device_name(
        self, client: UnifiClient, netbox_site_slug: str, existing: object | None
    ) -> str:
        """Sanitize the UniFi-reported name, disambiguating only when needed.

        If the device we already found is itself using this name, the name is
        by definition not taken by anyone else, so the collision query is
        skipped — one saved NetBox call per client on steady-state runs, which
        are the overwhelmingly common case.
        """
        base_name = sanitize_device_name(client.display_name, client.mac)
        if existing is not None and getattr(existing, "name", None) == base_name:
            return base_name
        if self.netbox.device_name_taken_by_other(base_name, netbox_site_slug, client.mac):
            disambiguated = mac_suffixed_name(base_name, client.mac)
            logger.info(
                "Name %r for %s is already used by another device in NetBox; using %r instead",
                base_name,
                client.mac,
                disambiguated,
            )
            return disambiguated
        return base_name

    def _sync_client(
        self, client: UnifiClient, netbox_site_slug: str, cable_ctx: _CableContext
    ) -> ClientSyncResult:
        result = ClientSyncResult(mac=client.mac, name=client.display_name)
        s = self.settings
        iface_name = _interface_name(client)
        try:
            existing = self.netbox.find_client_device_by_mac(client.mac)
            if existing is not None and not device_has_tag(existing, s.sync_tag):
                logger.warning(
                    "Leaving NetBox device %r (%s) untouched: it is not tagged %r. "
                    "This sync only updates objects it created.",
                    getattr(existing, "name", "?"),
                    client.mac,
                    s.sync_tag,
                )
                result.name = getattr(existing, "name", result.name)
                result.device_update_skipped_reason = (
                    f"device is not tagged {s.sync_tag}; manual NetBox object left untouched"
                )
                result.unchanged = True
                return result

            device_name = self._resolve_device_name(client, netbox_site_slug, existing)
            result.name = device_name

            if s.dry_run:
                self._plan_client(client, existing, result, netbox_site_slug, cable_ctx)
                return result

            device, created, update_skipped, updated = self.netbox.upsert_client_device(
                client.mac,
                device_name,
                netbox_site_slug,
                s.client_device_role_slug,
                self._client_device_type(client),
                s.sync_tag,
                s.device_update_policy,
                existing,
                True,
            )
            result.device_created = created
            # "updated" means fields were actually written, not merely that an
            # existing device was seen — otherwise every steady-state run would
            # report the whole estate as updated.
            result.device_updated = updated
            if update_skipped:
                result.device_update_skipped_reason = (
                    f"DEVICE_UPDATE_POLICY={s.device_update_policy!r}: existing device left as-is"
                )
                logger.info(
                    "Not updating existing device for %s (DEVICE_UPDATE_POLICY=%s)",
                    client.mac,
                    s.device_update_policy,
                )

            iface = self.netbox.ensure_interface(device, iface_name, client.is_wired)

            ip_wrote = False
            if client.ip and self._may_assign_ip(existing):
                if _primary_ip_matches(device, client.ip):
                    result.ip_unchanged = True
                else:
                    ip_wrote = self.netbox.assign_ip(
                        device, iface, client.ip, s.netbox_ip_status, s.sync_tag
                    )
                    result.ip_assigned = ip_wrote
            elif client.ip and existing is not None and not self._may_assign_ip(existing):
                logger.info(
                    "Not assigning IP %s to existing device %r (%s): it is not tagged %r "
                    "(DEVICE_UPDATE_POLICY=%s)",
                    client.ip,
                    getattr(existing, "name", result.name),
                    client.mac,
                    s.sync_tag,
                    s.device_update_policy,
                )

            cable_wrote = False
            if client.is_wired and client.switch_mac and client.switch_port:
                cable_wrote = self._wire_cable(client, iface, result, cable_ctx)

            result.unchanged = not (created or updated or ip_wrote or cable_wrote)
        except EXPECTED_CLIENT_ERRORS as exc:
            logger.exception("Failed syncing client %s (%s)", client.mac, client.display_name)
            result.error = str(exc)
        return result

    def _may_assign_ip(self, existing: object | None) -> bool:
        """Whether this client's address may be written onto ``existing``.

        A device that does not exist yet is created with the sync tag, so its
        first address is part of creation. An existing device without that tag
        is someone else's object: ``create-only`` (and ``sync``) must not move
        its IP. Tagged devices still receive address updates under
        ``create-only``, because that policy freezes name and status, not the
        address a client is currently using.
        """
        if existing is None:
            return True
        # create-only freezes name and status on tagged devices but still
        # tracks the address. A device without the sync tag is not ours under
        # either policy, so its IP stays where an operator put it.
        return device_has_tag(existing, self.settings.sync_tag)

    def _plan_client(
        self,
        client: UnifiClient,
        existing: object | None,
        result: ClientSyncResult,
        netbox_site_slug: str,
        cable_ctx: _CableContext,
    ) -> None:
        """Log every write this client would cause. Performs no NetBox writes."""
        iface_name = _interface_name(client)
        s = self.settings
        if existing is None:
            result.device_created = True
            logger.info(
                "[dry-run] would create device %r (mac %s) in site %r",
                result.name,
                client.mac,
                netbox_site_slug,
            )
        else:
            name_differs = getattr(existing, "name", None) != result.name
            status = getattr(getattr(existing, "status", None), "value", None)
            status_differs = status not in (None, "active")
            if s.device_update_policy == "create-only":
                if name_differs or status_differs:
                    result.device_update_skipped_reason = (
                        f"DEVICE_UPDATE_POLICY={s.device_update_policy!r}: existing device left as-is"
                    )
                    logger.info(
                        "[dry-run] would not update device %r (mac %s); DEVICE_UPDATE_POLICY=create-only",
                        getattr(existing, "name", result.name),
                        client.mac,
                    )
            elif name_differs or status_differs:
                result.device_updated = True
                logger.info(
                    "[dry-run] would update device %r (mac %s) name %r -> %r status %r -> active",
                    getattr(existing, "name", result.name),
                    client.mac,
                    getattr(existing, "name", None),
                    result.name,
                    status,
                )
        if client.ip and self._may_assign_ip(existing):
            if existing is not None and _primary_ip_matches(existing, client.ip):
                result.ip_unchanged = True
            else:
                logger.info(
                    "[dry-run] would assign IP %s to %s %s",
                    client.ip,
                    result.name,
                    iface_name,
                )
        elif client.ip and existing is not None and not self._may_assign_ip(existing):
            logger.info(
                "[dry-run] would not assign IP %s to existing device %r (%s): it is not tagged %r "
                "(DEVICE_UPDATE_POLICY=%s)",
                client.ip,
                getattr(existing, "name", result.name),
                client.mac,
                s.sync_tag,
                s.device_update_policy,
            )
        if client.is_wired and client.switch_mac and client.switch_port:
            resolved = self._resolve_cable(client, result, cable_ctx)
            if resolved is None:
                return
            existing_iface = None
            if existing is not None:
                interfaces = getattr(existing, "interfaces", None) or {}
                existing_iface = interfaces.get(iface_name) if isinstance(interfaces, dict) else None
            if existing_iface is not None and _already_cabled_to(existing_iface, resolved.switch_iface):
                result.cable_unchanged = True
                logger.info(
                    "[dry-run] cable %s:%s -- %s:%s already in place",
                    result.name,
                    iface_name,
                    resolved.switch_device.name,
                    resolved.switch_iface.name,
                )
                return
            if getattr(resolved.switch_iface, "cable", None) and s.cable_conflict_policy != "replace":
                detail = (
                    f"interface {resolved.switch_device.name}:{resolved.switch_iface.name} is already "
                    "cabled to something else"
                )
                self._record_cable_skip(
                    result,
                    cable_ctx,
                    reason="already_cabled",
                    detail=detail,
                    switch_mac=client.switch_mac,
                    switch_name=resolved.switch_device.name,
                    port=client.switch_port,
                    names_tried=resolved.names_tried,
                    netbox_names=resolved.netbox_names,
                )
                logger.info(
                    "[dry-run] would skip cable %s:%s -- %s:%s; target is already cabled "
                    "(CABLE_CONFLICT_POLICY=%s)",
                    result.name,
                    iface_name,
                    resolved.switch_device.name,
                    resolved.switch_iface.name,
                    s.cable_conflict_policy,
                )
                return
            if getattr(resolved.switch_iface, "cable", None) and s.cable_conflict_policy == "replace":
                logger.info(
                    "[dry-run] would delete and replace the cable on %s:%s for %s:%s "
                    "(only if that cable is tagged %r)",
                    resolved.switch_device.name,
                    resolved.switch_iface.name,
                    result.name,
                    iface_name,
                    s.sync_tag,
                )
            else:
                logger.info(
                    "[dry-run] would create cable %s:%s -- %s:%s",
                    result.name,
                    iface_name,
                    resolved.switch_device.name,
                    resolved.switch_iface.name,
                )
            result.cable_created = True

    def _wire_cable(
        self,
        client: UnifiClient,
        iface: object,
        result: ClientSyncResult,
        cable_ctx: _CableContext,
    ) -> bool:
        """Returns True if a cable was actually written."""
        resolved = self._resolve_cable(client, result, cable_ctx)
        if resolved is None:
            return False
        # The interface came straight from ensure_interface, so its cable state
        # is current for this run — if it already points at the right port
        # there is nothing to write and nothing to re-read.
        if _already_cabled_to(iface, resolved.switch_iface):
            result.cable_unchanged = True
            return False
        created, reason = self.netbox.ensure_cable(
            iface,
            resolved.switch_iface,
            self.settings.cable_conflict_policy,
            self.settings.sync_tag,
        )
        result.cable_created = created
        if reason:
            self._record_cable_skip(
                result,
                cable_ctx,
                reason=_classify_cable_reason(reason),
                detail=reason,
                switch_mac=client.switch_mac or "",
                switch_name=getattr(resolved.switch_device, "name", ""),
                port=client.switch_port,
                names_tried=resolved.names_tried,
                netbox_names=resolved.netbox_names,
            )
        return created

    def _mark_stale(
        self,
        netbox_site_slug: str,
        seen_macs: set[str],
        known_last_seen: dict[str, float | None],
    ) -> int:
        """Mark managed client devices whose UniFi last_seen is past the grace.

        Limited to devices that carry the sync tag and the client role, so a
        manually tagged switch, an infrastructure device this tool created,
        or an untagged object is never taken offline. A client still in the
        active list is never marked, and neither is one whose last_seen falls
        inside the grace — that is an idle phone or laptop, not a removal.
        """
        marked = 0
        tag = self.settings.sync_tag
        role = self.settings.client_device_role_slug
        now = time.time()
        grace = self.settings.stale_offline_grace_seconds
        grace_label = format_grace(grace)
        for device in self.netbox.list_synced_client_devices(tag, netbox_site_slug, role):
            if not device_has_tag(device, tag):
                continue
            mac = (device.custom_fields or {}).get("unifi_mac")
            status = getattr(getattr(device, "status", None), "value", None)
            if not mac or mac in seen_macs or status != "active":
                continue
            decision = offline_decision(mac, known_last_seen, now, grace)
            randomized = is_locally_administered(mac)
            if not decision.mark:
                if decision.reason == "within_grace":
                    logger.info(
                        "%s device %r (%s) active; UniFi last_seen %s is within the %s grace%s",
                        "[dry-run] would leave" if self.settings.dry_run else "Leaving",
                        device.name,
                        mac,
                        _format_last_seen(known_last_seen.get(mac.lower())),
                        grace_label,
                        " (randomized MAC)" if randomized else "",
                    )
                else:
                    logger.info(
                        "Leaving device %r (%s) active; UniFi lists it but has no usable last_seen%s",
                        device.name,
                        mac,
                        " (randomized MAC)" if randomized else "",
                    )
                continue
            if decision.reason == "not_known":
                why = "the controller has no record of it"
            else:
                why = (
                    f"UniFi last_seen {_format_last_seen(known_last_seen.get(mac.lower()))} "
                    f"is outside the {grace_label} grace"
                )
            rotation = (
                " Randomized MAC; phones rotate these and the old address does not return."
                if randomized
                else ""
            )
            if self.settings.dry_run:
                logger.info(
                    "[dry-run] would mark device %r (%s) offline; %s.%s",
                    device.name,
                    mac,
                    why,
                    rotation,
                )
            else:
                self.netbox.mark_offline(device)
                logger.info(
                    "Marked device %r (%s) offline; %s.%s", device.name, mac, why, rotation
                )
            marked += 1
        return marked

    def _load_known_clients(self, unifi_site: str) -> tuple[dict[str, float | None], bool]:
        """MAC -> last_seen, and whether that map is safe to offline against.

        A failed read is not an empty network. Callers skip offline marking
        rather than treat every managed device as gone.
        """
        getter = getattr(self.unifi, "get_known_clients", None)
        if getter is None:
            logger.warning(
                "UniFi client cannot report last_seen for site '%s'; skipping offline marking",
                unifi_site,
            )
            return {}, False
        try:
            records = getter(unifi_site)
        except EXPECTED_CLIENT_ERRORS as exc:
            logger.warning(
                "Could not read UniFi client history for site '%s' (%s); skipping offline marking",
                unifi_site,
                exc,
            )
            return {}, False
        found: dict[str, float | None] = {}
        for record in records:
            mac = (getattr(record, "mac", "") or "").lower()
            if not mac:
                continue
            last = getattr(record, "last_seen", None)
            if mac not in found:
                found[mac] = last
                continue
            current = found[mac]
            if last is None:
                continue
            if current is None or last > current:
                found[mac] = last
        return found, True

    def _resolve_cable(
        self, client: UnifiClient, result: ClientSyncResult, cable_ctx: _CableContext
    ) -> _ResolvedCable | None:
        """The switch interface to cable, or None when the cable must be skipped."""
        switch_mac = (client.switch_mac or "").lower()
        hints = self._hints_for(switch_mac)
        port_no = coerce_port(client.switch_port)
        ports = list(hints.ports) if hints is not None else []
        sfp_index = sfp_ordinal(ports, port_no) if port_no is not None else None
        model = hints.model if hints is not None else None
        model_templates = templates_for_model(self.settings.port_model_templates, model)
        extra: list[str] = []
        if port_no is not None:
            for port in ports:
                if port.index == port_no and port.name:
                    extra.append(port.name)
        candidates = (
            candidate_names(
                model_templates,
                self.settings.port_name_templates,
                port_no,
                sfp_index,
                extra,
            )
            if port_no is not None
            else []
        )
        switch_name = hints.name if hints is not None and hints.name else switch_mac

        if port_no is not None and (switch_mac, port_no) in cable_ctx.shared_ports:
            switch_device = self.netbox.find_switch_device_by_mac(switch_mac, hints)
            netbox_names = self._interface_names(switch_device) if switch_device is not None else []
            if switch_device is not None:
                switch_name = getattr(switch_device, "name", switch_name)
            self._record_cable_skip(
                result,
                cable_ctx,
                reason="shared_port",
                detail=(
                    f"port {client.switch_port} on {switch_name} is shared by multiple wired "
                    "clients; not cabling an uplink or downstream switch"
                ),
                switch_mac=switch_mac,
                switch_name=switch_name,
                port=client.switch_port,
                names_tried=candidates,
                netbox_names=netbox_names,
            )
            return None

        if port_no is not None and is_uplink_port(ports, port_no):
            switch_device = self.netbox.find_switch_device_by_mac(switch_mac, hints)
            netbox_names = self._interface_names(switch_device) if switch_device is not None else []
            if switch_device is not None:
                switch_name = getattr(switch_device, "name", switch_name)
            self._record_cable_skip(
                result,
                cable_ctx,
                reason="uplink_port",
                detail=(
                    f"port {client.switch_port} on {switch_name} is a UniFi uplink; "
                    "not cabling a client to it"
                ),
                switch_mac=switch_mac,
                switch_name=switch_name,
                port=client.switch_port,
                names_tried=candidates,
                netbox_names=netbox_names,
            )
            return None

        switch_device = self.netbox.find_switch_device_by_mac(switch_mac, hints)
        if switch_device is None:
            self._log_switch_not_found(switch_mac)
            self._record_cable_skip(
                result,
                cable_ctx,
                reason="switch_not_found",
                detail=f"switch {switch_mac} not found in NetBox (port {client.switch_port})",
                switch_mac=switch_mac,
                switch_name=switch_name,
                port=client.switch_port,
                names_tried=candidates,
                netbox_names=[],
            )
            return None

        switch_name = getattr(switch_device, "name", switch_name)
        interfaces = self._interfaces(switch_device)
        netbox_names = [iface.name for iface in interfaces]
        if not netbox_names:
            self._log_port_mismatch(switch_device, client.switch_port, candidates, [])
            self._record_cable_skip(
                result,
                cable_ctx,
                reason="no_interfaces",
                detail=(
                    f"NetBox device {switch_name!r} has no interfaces, so UniFi port "
                    f"{client.switch_port} was not cabled"
                ),
                switch_mac=switch_mac,
                switch_name=switch_name,
                port=client.switch_port,
                names_tried=candidates,
                netbox_names=[],
            )
            return None

        outcome = match_port(
            netbox_names,
            candidates,
            port_no,
            sfp_index,
            allow_structural=not model_templates,
        )
        if outcome.via == "ambiguous" or outcome.name is None:
            self._log_port_mismatch(
                switch_device,
                client.switch_port,
                list(outcome.names_tried),
                netbox_names,
                ambiguous=list(outcome.ambiguous_names) if outcome.via == "ambiguous" else None,
            )
            if outcome.via == "ambiguous":
                detail = (
                    f"port {client.switch_port} on {switch_name} matched multiple interfaces "
                    f"{list(outcome.ambiguous_names)}"
                )
                reason = "port_ambiguous"
            else:
                detail = (
                    f"no interface on {switch_name} matched {list(outcome.names_tried)}"
                )
                reason = "port_not_found"
            self._record_cable_skip(
                result,
                cable_ctx,
                reason=reason,
                detail=detail,
                switch_mac=switch_mac,
                switch_name=switch_name,
                port=client.switch_port,
                names_tried=list(outcome.names_tried),
                netbox_names=netbox_names,
            )
            return None

        switch_iface = next(iface for iface in interfaces if iface.name == outcome.name)
        logger.info(
            "Matched switch port %s on %s using %s -> interface %s",
            client.switch_port,
            switch_name,
            outcome.via,
            switch_iface.name,
        )
        return _ResolvedCable(
            switch_device=switch_device,
            switch_iface=switch_iface,
            names_tried=list(outcome.names_tried),
            netbox_names=netbox_names,
        )

    def _interfaces(self, switch_device: object) -> list[object]:
        lister = getattr(self.netbox, "list_device_interfaces", None)
        if lister is None:
            return []
        # A failed list is a client error, not "this switch has no ports".
        return list(lister(switch_device))

    def _interface_names(self, switch_device: object) -> list[str]:
        return [iface.name for iface in self._interfaces(switch_device)]

    def _record_cable_skip(
        self,
        result: ClientSyncResult,
        cable_ctx: _CableContext,
        *,
        reason: str,
        detail: str,
        switch_mac: str,
        switch_name: str,
        port: object,
        names_tried: list[str],
        netbox_names: list[str],
    ) -> None:
        result.cable_skipped_reason = detail
        result.cable_skip = CableSkip(
            unifi_site=cable_ctx.unifi_site,
            netbox_site=cable_ctx.netbox_site,
            switch=switch_name or switch_mac,
            switch_mac=switch_mac,
            unifi_port="" if port is None else str(port),
            names_tried=" | ".join(names_tried),
            netbox_interfaces=" | ".join(netbox_names),
            reason=reason,
        )
        prefix = "[dry-run] " if self.settings.dry_run else ""
        logger.info(
            "%scable skip site=%s switch=%s port=%s reason=%s tried=[%s] interfaces=[%s]",
            prefix,
            cable_ctx.unifi_site,
            switch_name or switch_mac,
            port,
            reason,
            ", ".join(names_tried),
            ", ".join(netbox_names),
        )
