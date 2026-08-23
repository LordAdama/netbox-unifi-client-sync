from __future__ import annotations

import logging
import time

import pynetbox
import requests

from .config import Settings
from .metrics import log_json_summary, write_prometheus_textfile
from .models import ClientSyncResult, SitePair, SyncSummary, UnifiClient
from .naming import mac_suffixed_name, sanitize_device_name
from .netbox_client import NetboxGateway
from .unifi_client import UnifiClientAPI

logger = logging.getLogger(__name__)

# Exceptions expected from a normal network/API hiccup on a single client.
# Anything else (AttributeError, TypeError, ...) is a programming error and
# should propagate and stop the run rather than being silently swallowed
# per-client.
EXPECTED_CLIENT_ERRORS = (requests.exceptions.RequestException, pynetbox.RequestError, LookupError)


def _interface_name(client: UnifiClient) -> str:
    return "eth0" if client.is_wired else "wlan0"


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
        merged.errors.extend(s.errors or [])
        merged.client_results.extend(s.client_results or [])
    # stale_marked_offline is filled in separately by run(), after all sites'
    # clients are known — see the comment on _mark_stale below.
    return merged


class SyncEngine:
    """Syncs UniFi clients into NetBox, one or more UniFi-site -> NetBox-site
    pairs at a time (see Settings.site_map / Settings.site_pairs()).

    Two site pairs mapping to the same NetBox site are handled correctly
    within one run — stale-device marking uses the union of both pairs'
    clients for that NetBox site (see _mark_stale), and per-site device-name
    collision checks already scope by NetBox site slug. What's still not
    handled is multiple *instances* of this tool (or unrelated automation)
    writing to the same NetBox concurrently: there is no distributed/
    advisory locking, so that can race on cable creation/deletion. The
    Docker entrypoint guards against a single instance overlapping itself
    (a run that outlives its own interval) with a local flock, but that
    does not protect against separate hosts/containers syncing the same
    NetBox concurrently — don't do that.

    Also assumes the UniFi controller returns its full active-client list in
    one `/stat/sta` call per site (true for the classic controller API this
    client uses); there is no pagination handling. This is fine at typical
    homelab/SMB client counts, but hasn't been exercised against very large
    (thousands of clients) deployments.
    """

    def __init__(self, unifi: UnifiClientAPI, netbox: NetboxGateway, settings: Settings) -> None:
        self.unifi = unifi
        self.netbox = netbox
        self.settings = settings
        self._current_netbox_site_slug = ""

    def run(self) -> SyncSummary:
        start = time.monotonic()
        pairs = self.settings.site_pairs()
        multi = len(pairs) > 1

        logger.info(
            "Policies: site=%s, device_update=%s, cable_conflict=%s",
            self.settings.site_policy,
            self.settings.device_update_policy,
            self.settings.cable_conflict_policy,
        )
        if multi:
            logger.info(
                "Syncing %d site pairs: %s",
                len(pairs),
                ", ".join(f"{p.unifi_site}->{p.netbox_site_slug}" for p in pairs),
            )

        site_summaries: list[SyncSummary] = []
        # Two pairs can map to the same NetBox site (e.g. two UniFi sites
        # consolidated into one NetBox site). Stale-device marking has to see
        # the union of both pairs' clients for that site — grouping by
        # netbox_site_slug here, and doing the actual marking once per
        # distinct slug below, after every pair's clients are known.
        seen_macs_by_netbox_site: dict[str, set[str]] = {}
        failed_netbox_sites: set[str] = set()

        for pair in pairs:
            try:
                site_summary, seen_macs = self._run_site(pair)
                site_summaries.append(site_summary)
                seen_macs_by_netbox_site.setdefault(pair.netbox_site_slug, set()).update(seen_macs)
            except EXPECTED_CLIENT_ERRORS as exc:
                # A site-level failure (e.g. a `require`d NetBox site that's
                # missing) shouldn't take other, correctly-configured sites
                # down with it in the same run.
                logger.exception(
                    "Failed syncing site '%s' -> NetBox '%s'", pair.unifi_site, pair.netbox_site_slug
                )
                site_summaries.append(
                    SyncSummary(errors=[f"{pair.unifi_site}->{pair.netbox_site_slug}: {exc}"])
                )
                failed_netbox_sites.add(pair.netbox_site_slug)
        summary = _merge_summaries(site_summaries)

        if self.settings.mark_stale_offline:
            if not self.settings.dry_run:
                for netbox_site_slug, seen_macs in seen_macs_by_netbox_site.items():
                    if netbox_site_slug in failed_netbox_sites:
                        # A pair targeting this NetBox site failed before we
                        # learned its clients, so seen_macs is incomplete for
                        # this site — marking stale now could wrongly offline
                        # devices that pair would have reported as active.
                        logger.warning(
                            "Skipping stale-device marking for NetBox site '%s': a mapped "
                            "UniFi site failed to sync this run",
                            netbox_site_slug,
                        )
                        continue
                    summary.stale_marked_offline += self._mark_stale(netbox_site_slug, seen_macs)
            else:
                logger.info("[dry-run] would mark devices missing from UniFi as offline")

        summary.duration_seconds = time.monotonic() - start

        logger.info(
            "Sync complete: %d clients, %d created, %d updated, %d update-skipped (policy), "
            "%d cables created, %d cables skipped, %d marked offline, %d errors, %.1fs%s",
            summary.clients_seen,
            summary.devices_created,
            summary.devices_updated,
            summary.devices_update_skipped,
            summary.cables_created,
            summary.cables_skipped,
            summary.stale_marked_offline,
            len(summary.errors),
            summary.duration_seconds,
            f" across {len(pairs)} sites" if multi else "",
        )
        log_json_summary(summary)
        if self.settings.metrics_file:
            write_prometheus_textfile(summary, self.settings.metrics_file)
        return summary

    def _run_site(self, pair: SitePair) -> tuple[SyncSummary, set[str]]:
        """Syncs one site pair's clients. Does not do stale-device marking —
        that happens once per distinct NetBox site slug in run(), after every
        pair's clients are known (see the comment there)."""
        summary = SyncSummary()
        s = self.settings
        self._current_netbox_site_slug = pair.netbox_site_slug

        if not s.dry_run:
            _site, site_created = self.netbox.ensure_site(pair.netbox_site_slug, s.site_policy)
            summary.sites_created = 1 if site_created else 0
            logger.info(
                "NetBox site '%s': %s", pair.netbox_site_slug, "created" if site_created else "reused existing"
            )
            self.netbox.ensure_prerequisites(
                s.client_device_role_slug,
                s.client_manufacturer_slug,
                s.client_device_type_slug,
                s.sync_tag,
            )
        else:
            existing_site = self.netbox.find_site(pair.netbox_site_slug)
            if existing_site is None:
                if s.site_policy == "create":
                    summary.sites_created = 1
                    logger.info(
                        "[dry-run] would create NetBox site '%s' (SITE_POLICY=create)", pair.netbox_site_slug
                    )
                else:
                    logger.warning(
                        "[dry-run] NetBox site '%s' does not exist and SITE_POLICY=%s; "
                        "a real run would fail here",
                        pair.netbox_site_slug,
                        s.site_policy,
                    )
            logger.info("[dry-run] would ensure NetBox tag/custom-field/role/device-type exist")

        clients = self.unifi.get_clients(pair.unifi_site)
        summary.clients_seen = len(clients)
        seen_macs: set[str] = set()

        for client in clients:
            seen_macs.add(client.mac)
            result = self._sync_client(client)
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
            if result.cable_created:
                summary.cables_created += 1
            elif result.cable_skipped_reason:
                summary.cables_skipped += 1

        logger.info(
            "Site '%s' -> NetBox '%s': %d clients, %d created, %d updated, %d cables created, %d cables skipped",
            pair.unifi_site,
            pair.netbox_site_slug,
            summary.clients_seen,
            summary.devices_created,
            summary.devices_updated,
            summary.cables_created,
            summary.cables_skipped,
        )
        return summary, seen_macs

    def _resolve_device_name(self, client: UnifiClient) -> str:
        """Sanitize the UniFi-reported name and disambiguate it if some other
        device (a different MAC) already holds it in this NetBox site — NetBox
        requires device names to be unique within a site."""
        base_name = sanitize_device_name(client.display_name, client.mac)
        if self.netbox.device_name_taken_by_other(base_name, self._current_netbox_site_slug, client.mac):
            disambiguated = mac_suffixed_name(base_name, client.mac)
            logger.info(
                "Name %r for %s is already used by another device in NetBox; using %r instead",
                base_name,
                client.mac,
                disambiguated,
            )
            return disambiguated
        return base_name

    def _sync_client(self, client: UnifiClient) -> ClientSyncResult:
        result = ClientSyncResult(mac=client.mac, name=client.display_name)
        s = self.settings
        iface_name = _interface_name(client)
        try:
            device_name = self._resolve_device_name(client)
            result.name = device_name

            if s.dry_run:
                self._plan_client(client, iface_name, result)
                return result

            device, created, update_skipped = self.netbox.upsert_client_device(
                client.mac,
                device_name,
                self._current_netbox_site_slug,
                s.client_device_role_slug,
                s.client_device_type_slug,
                s.sync_tag,
                s.device_update_policy,
            )
            result.device_created = created
            result.device_updated = not created and not update_skipped
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

            if client.ip:
                result.ip_assigned = self.netbox.assign_ip(device, iface, client.ip, s.netbox_ip_status)

            if client.is_wired and client.switch_mac and client.switch_port:
                self._wire_cable(client, iface, result)
        except EXPECTED_CLIENT_ERRORS as exc:
            logger.exception("Failed syncing client %s (%s)", client.mac, client.display_name)
            result.error = str(exc)
        return result

    def _plan_client(self, client: UnifiClient, iface_name: str, result: ClientSyncResult) -> None:
        existing = self.netbox.find_client_device_by_mac(client.mac)
        result.device_created = existing is None
        if existing is not None and self.settings.device_update_policy == "create-only":
            result.device_update_skipped_reason = (
                f"DEVICE_UPDATE_POLICY={self.settings.device_update_policy!r}: existing device left as-is"
            )
        else:
            result.device_updated = existing is not None
        if client.is_wired and client.switch_mac and client.switch_port:
            switch_device = self.netbox.find_switch_device_by_mac(client.switch_mac)
            if switch_device is None:
                result.cable_skipped_reason = "switch not found in NetBox"
                return
            candidates = [t.format(port=client.switch_port) for t in self.settings.port_name_templates]
            switch_iface = self.netbox.find_interface_by_name_candidates(switch_device, candidates)
            if switch_iface is None:
                result.cable_skipped_reason = f"no switch interface matched {candidates}"
                return
            result.cable_created = True

    def _wire_cable(self, client: UnifiClient, iface: object, result: ClientSyncResult) -> None:
        switch_device = self.netbox.find_switch_device_by_mac(client.switch_mac)
        if switch_device is None:
            result.cable_skipped_reason = (
                f"switch {client.switch_mac} not found in NetBox (port {client.switch_port})"
            )
            return
        candidates = [t.format(port=client.switch_port) for t in self.settings.port_name_templates]
        switch_iface = self.netbox.find_interface_by_name_candidates(switch_device, candidates)
        if switch_iface is None:
            result.cable_skipped_reason = f"no interface on {switch_device.name} matched {candidates}"
            return
        created, reason = self.netbox.ensure_cable(iface, switch_iface, self.settings.cable_conflict_policy)
        result.cable_created = created
        result.cable_skipped_reason = reason

    def _mark_stale(self, netbox_site_slug: str, seen_macs: set[str]) -> int:
        marked = 0
        for device in self.netbox.list_synced_client_devices(self.settings.sync_tag, netbox_site_slug):
            mac = (device.custom_fields or {}).get("unifi_mac")
            if mac and mac not in seen_macs and device.status.value == "active":
                self.netbox.mark_offline(device)
                marked += 1
        return marked
