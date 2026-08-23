from __future__ import annotations

import logging
import threading
import time
from concurrent.futures import ThreadPoolExecutor

import pynetbox
import requests

from .config import Settings
from .devicetype_library import DeviceTypeLibrary, spec_from_unifi_device
from .metrics import log_json_summary, write_prometheus_textfile
from .models import ClientSyncResult, SitePair, SiteSyncStats, SyncSummary, UnifiClient
from .naming import mac_suffixed_name, sanitize_device_name, slugify
from .netbox_client import NetboxGateway, SwitchHints
from .oui import OuiLookup, is_locally_administered
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
        merged.stale_marked_offline += s.stale_marked_offline
        merged.clients_unchanged += s.clients_unchanged
        merged.ips_unchanged += s.ips_unchanged
        merged.cables_unchanged += s.cables_unchanged
        merged.errors.extend(s.errors or [])
        merged.client_results.extend(s.client_results or [])
        merged.site_stats.extend(s.site_stats or [])
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
    one `/stat/sta` call per site (true for the classic controller API this
    client uses); there is no pagination handling.
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

    # -- orchestration ---------------------------------------------------

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
                # Two sites collapsing to one NetBox site is handled safely
                # (they become one group, stale-marking unions their clients),
                # but it's almost certainly not what was intended.
                logger.warning(
                    "UniFi sites %r and %r both map to NetBox site '%s'; their clients will share "
                    "it. Pin one with an explicit SITE_MAP entry to separate them.",
                    slug_owners[slug],
                    site.name,
                    slug,
                )
            slug_owners[slug] = site.name
            pairs.append(SitePair(unifi_site=site.name, netbox_site_slug=slug))

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
        pairs = self._discover_site_pairs() if self.settings.sync_all_sites else self.settings.site_pairs()

        groups: dict[str, list[SitePair]] = {}
        for pair in pairs:
            groups.setdefault(pair.netbox_site_slug, []).append(pair)

        workers = max(1, min(self.settings.max_workers, len(groups)))
        logger.info(
            "Policies: site=%s, device_update=%s, cable_conflict=%s | %d pair(s) in %d NetBox site "
            "group(s), %d worker(s)",
            self.settings.site_policy,
            self.settings.device_update_policy,
            self.settings.cable_conflict_policy,
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
        log_json_summary(summary)
        if self.settings.metrics_file:
            write_prometheus_textfile(summary, self.settings.metrics_file)
        return summary

    def _run_group(self, netbox_site_slug: str, pairs: list[SitePair]) -> SyncSummary:
        """Sync every UniFi site mapped to one NetBox site, then stale-mark once."""
        summaries: list[SyncSummary] = []
        seen_macs: set[str] = set()
        any_pair_failed = False

        for pair in pairs:
            pair_start = time.monotonic()
            try:
                pair_summary, pair_macs = self._run_site(pair)
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
            except EXPECTED_CLIENT_ERRORS as exc:
                # A site-level failure (e.g. a `require`d NetBox site that's
                # missing) shouldn't take other pairs or other groups with it.
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
            if self.settings.dry_run:
                logger.info("[dry-run] would mark devices missing from UniFi as offline")
            elif any_pair_failed:
                # seen_macs is incomplete for this NetBox site, so marking now
                # could offline devices a failed pair would have reported as
                # active. Not knowing a client is not the same as it being gone.
                logger.warning(
                    "Skipping stale-device marking for NetBox site '%s': a mapped UniFi site "
                    "failed to sync this run",
                    netbox_site_slug,
                )
            else:
                group.stale_marked_offline = self._mark_stale(netbox_site_slug, seen_macs)

        return group

    def _run_site(self, pair: SitePair) -> tuple[SyncSummary, set[str]]:
        """Syncs one pair's clients. Stale marking is the group's job, not this."""
        summary = SyncSummary()
        s = self.settings
        slug = pair.netbox_site_slug

        if not s.dry_run:
            _site, created = self.netbox.ensure_site(slug, s.site_policy)
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
            summary.devices_synced, summary.infra_created = self._sync_unifi_devices(pair)

        clients = self.unifi.get_clients(pair.unifi_site)
        summary.clients_seen = len(clients)
        seen_macs: set[str] = set()

        for client in clients:
            seen_macs.add(client.mac)
            result = self._sync_client(client, slug)
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
        return summary, seen_macs

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
                        name=device.name, serial=device.serial, model=device.model
                    )
        logger.info("Loaded %d UniFi device(s) for site '%s' to match switches by name/serial",
                    len(devices), unifi_site)

    # Per-type NetBox roles, so you can filter access points apart from
    # switches in NetBox. Unrecognised types use unifi_device_role_slug.
    _ROLE_BY_TYPE = {
        "usw": "switch",
        "uap": "wireless-ap",
        "ugw": "router",
        "udm": "router",
        "uxg": "router",
    }

    def _sync_unifi_devices(self, pair: SitePair) -> tuple[int, int]:
        """Create/update adopted UniFi infrastructure. Returns (seen, created)."""
        s = self.settings
        try:
            devices = self.unifi.get_devices(pair.unifi_site)
        except EXPECTED_CLIENT_ERRORS as exc:
            logger.warning("Could not list UniFi devices for '%s': %s", pair.unifi_site, exc)
            return 0, 0

        adopted = [d for d in devices if d.adopted and d.mac]
        if not adopted:
            return 0, 0

        created_count = 0
        for device in adopted:
            role_slug = self._ROLE_BY_TYPE.get(device.device_type, s.unifi_device_role_slug)
            spec = self._library.lookup(device.model) or spec_from_unifi_device(device)
            if s.dry_run:
                logger.info(
                    "[dry-run] would ensure UniFi %s %r (%s) as NetBox device type %r, role %r, "
                    "with %d interface(s)",
                    device.device_type or "device",
                    device.name,
                    device.model,
                    spec.slug,
                    role_slug,
                    len(spec.interfaces),
                )
                continue

            try:
                self.netbox.ensure_device_role(role_slug)
                type_slug = self.netbox.ensure_device_type_from_spec(spec)
                _dev, created, _updated = self.netbox.upsert_infrastructure_device(
                    device.mac,
                    device.name,
                    pair.netbox_site_slug,
                    role_slug,
                    type_slug,
                    s.sync_tag,
                    device.serial or "",
                    s.device_update_policy,
                )
                created_count += int(created)
                if created:
                    logger.info(
                        "Created NetBox device %r (%s %s) with %d port(s) from the controller",
                        device.name,
                        device.model,
                        device.device_type,
                        len(spec.interfaces),
                    )
                    # A switch that didn't exist a moment ago is now cabling
                    # material; drop any cached "not found" for it.
                    self._forget_switch(device.mac)
            except EXPECTED_CLIENT_ERRORS as exc:
                logger.exception("Failed syncing UniFi device %s (%s)", device.mac, device.name)
                raise LookupError(f"UniFi device {device.name} ({device.mac}): {exc}") from exc

        logger.info(
            "Site '%s': %d adopted UniFi device(s), %d created in NetBox",
            pair.unifi_site,
            len(adopted),
            created_count,
        )
        return len(adopted), created_count

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
            "Fix by giving the NetBox device the same name UniFi uses, setting its serial, or "
            "adding a unifi_mac custom field.",
            mac,
            f" (named {hints.name!r} in UniFi)" if hints and hints.name else "",
        )

    def _log_port_mismatch(self, switch_device: object, port: object, candidates: list[str]) -> None:
        """Say what the switch's ports are actually called.

        A port-name mismatch is otherwise near-impossible to diagnose from the
        outside: the run just reports skipped cables. Printing the real names
        next to what we looked for turns it into an obvious PORT_NAME_TEMPLATES fix.
        """
        lister = getattr(self.netbox, "list_device_interfaces", None)
        if lister is None:
            return
        try:
            names = sorted(iface.name for iface in lister(switch_device))
        except EXPECTED_CLIENT_ERRORS:
            return
        logger.warning(
            "UniFi port %s on %r matched no NetBox interface. Looked for %s; that device's "
            "interfaces are named %s%s. Set PORT_NAME_TEMPLATES to match.",
            port,
            getattr(switch_device, "name", "?"),
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

    def _sync_client(self, client: UnifiClient, netbox_site_slug: str) -> ClientSyncResult:
        result = ClientSyncResult(mac=client.mac, name=client.display_name)
        s = self.settings
        iface_name = _interface_name(client)
        try:
            existing = self.netbox.find_client_device_by_mac(client.mac)
            device_name = self._resolve_device_name(client, netbox_site_slug, existing)
            result.name = device_name

            if s.dry_run:
                self._plan_client(client, existing, result)
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
            if client.ip:
                if _primary_ip_matches(device, client.ip):
                    result.ip_unchanged = True
                else:
                    ip_wrote = self.netbox.assign_ip(device, iface, client.ip, s.netbox_ip_status)
                    result.ip_assigned = ip_wrote

            cable_wrote = False
            if client.is_wired and client.switch_mac and client.switch_port:
                cable_wrote = self._wire_cable(client, iface, result)

            result.unchanged = not (created or updated or ip_wrote or cable_wrote)
        except EXPECTED_CLIENT_ERRORS as exc:
            logger.exception("Failed syncing client %s (%s)", client.mac, client.display_name)
            result.error = str(exc)
        return result

    def _plan_client(self, client: UnifiClient, existing: object | None, result: ClientSyncResult) -> None:
        result.device_created = existing is None
        if existing is not None and self.settings.device_update_policy == "create-only":
            result.device_update_skipped_reason = (
                f"DEVICE_UPDATE_POLICY={self.settings.device_update_policy!r}: existing device left as-is"
            )
        else:
            result.device_updated = existing is not None
        if client.is_wired and client.switch_mac and client.switch_port:
            switch_device = self.netbox.find_switch_device_by_mac(
                client.switch_mac, self._hints_for(client.switch_mac)
            )
            if switch_device is None:
                result.cable_skipped_reason = (
                    f"switch {client.switch_mac} not found in NetBox (port {client.switch_port})"
                )
                # Same diagnostics as a real run: a preview reporting "N cables
                # skipped" without saying why is the one thing an operator
                # cannot act on.
                self._log_switch_not_found(client.switch_mac)
                return
            candidates = [t.format(port=client.switch_port) for t in self.settings.port_name_templates]
            switch_iface = self.netbox.find_interface_by_name_candidates(switch_device, candidates)
            if switch_iface is None:
                result.cable_skipped_reason = (
                    f"no interface on {switch_device.name} matched {candidates}"
                )
                self._log_port_mismatch(switch_device, client.switch_port, candidates)
                return
            result.cable_created = True

    def _wire_cable(self, client: UnifiClient, iface: object, result: ClientSyncResult) -> bool:
        """Returns True if a cable was actually written."""
        switch_device = self.netbox.find_switch_device_by_mac(
            client.switch_mac, self._hints_for(client.switch_mac)
        )
        if switch_device is None:
            result.cable_skipped_reason = (
                f"switch {client.switch_mac} not found in NetBox (port {client.switch_port})"
            )
            self._log_switch_not_found(client.switch_mac)
            return False
        candidates = [t.format(port=client.switch_port) for t in self.settings.port_name_templates]
        switch_iface = self.netbox.find_interface_by_name_candidates(switch_device, candidates)
        if switch_iface is None:
            result.cable_skipped_reason = (
                f"no interface on {switch_device.name} matched {candidates}"
            )
            self._log_port_mismatch(switch_device, client.switch_port, candidates)
            return False
        # The interface came straight from ensure_interface, so its cable state
        # is current for this run — if it already points at the right port
        # there is nothing to write and nothing to re-read.
        if _already_cabled_to(iface, switch_iface):
            result.cable_unchanged = True
            return False
        created, reason = self.netbox.ensure_cable(iface, switch_iface, self.settings.cable_conflict_policy)
        result.cable_created = created
        result.cable_skipped_reason = reason
        return created

    def _mark_stale(self, netbox_site_slug: str, seen_macs: set[str]) -> int:
        marked = 0
        for device in self.netbox.list_synced_client_devices(self.settings.sync_tag, netbox_site_slug):
            mac = (device.custom_fields or {}).get("unifi_mac")
            if mac and mac not in seen_macs and device.status.value == "active":
                self.netbox.mark_offline(device)
                marked += 1
        return marked
