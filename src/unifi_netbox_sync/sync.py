from __future__ import annotations

import logging

from .config import Settings
from .models import ClientSyncResult, SyncSummary, UnifiClient
from .netbox_client import NetboxGateway
from .unifi_client import UnifiClientAPI

logger = logging.getLogger(__name__)


def _interface_name(client: UnifiClient) -> str:
    return "eth0" if client.is_wired else "wlan0"


class SyncEngine:
    def __init__(self, unifi: UnifiClientAPI, netbox: NetboxGateway, settings: Settings) -> None:
        self.unifi = unifi
        self.netbox = netbox
        self.settings = settings

    def run(self) -> SyncSummary:
        summary = SyncSummary()
        s = self.settings

        if not s.dry_run:
            self.netbox.ensure_prerequisites(
                s.client_device_role_slug,
                s.client_manufacturer_slug,
                s.client_device_type_slug,
                s.sync_tag,
            )
        else:
            logger.info("[dry-run] would ensure NetBox tag/custom-field/role/device-type exist")

        clients = self.unifi.get_clients(s.unifi_site)
        summary.clients_seen = len(clients)
        seen_macs: set[str] = set()

        for client in clients:
            seen_macs.add(client.mac)
            result = self._sync_client(client)
            summary.client_results.append(result)
            if result.error:
                summary.errors.append(f"{client.mac}: {result.error}")
                continue
            if result.device_created:
                summary.devices_created += 1
            elif result.device_updated:
                summary.devices_updated += 1
            if result.cable_created:
                summary.cables_created += 1
            elif result.cable_skipped_reason:
                summary.cables_skipped += 1

        if s.mark_stale_offline and not s.dry_run:
            summary.stale_marked_offline = self._mark_stale(seen_macs)
        elif s.mark_stale_offline:
            logger.info("[dry-run] would mark devices missing from UniFi as offline")

        logger.info(
            "Sync complete: %d clients, %d created, %d updated, %d cables created, "
            "%d cables skipped, %d marked offline, %d errors",
            summary.clients_seen,
            summary.devices_created,
            summary.devices_updated,
            summary.cables_created,
            summary.cables_skipped,
            summary.stale_marked_offline,
            len(summary.errors),
        )
        return summary

    def _sync_client(self, client: UnifiClient) -> ClientSyncResult:
        result = ClientSyncResult(mac=client.mac, name=client.display_name)
        s = self.settings
        iface_name = _interface_name(client)
        try:
            if s.dry_run:
                self._plan_client(client, iface_name, result)
                return result

            device, created = self.netbox.upsert_client_device(
                client.mac,
                client.display_name,
                s.netbox_site_slug,
                s.client_device_role_slug,
                s.client_device_type_slug,
                s.sync_tag,
            )
            result.device_created = created
            result.device_updated = not created

            iface = self.netbox.ensure_interface(device, iface_name, client.is_wired)

            if client.ip:
                result.ip_assigned = self.netbox.assign_ip(device, iface, client.ip)

            if client.is_wired and client.switch_mac and client.switch_port:
                self._wire_cable(client, iface, result)
        except Exception as exc:  # noqa: BLE001 - surfaced via result.error and summary
            logger.exception("Failed syncing client %s (%s)", client.mac, client.display_name)
            result.error = str(exc)
        return result

    def _plan_client(self, client: UnifiClient, iface_name: str, result: ClientSyncResult) -> None:
        existing = self.netbox.find_client_device_by_mac(client.mac)
        result.device_created = existing is None
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

    def _mark_stale(self, seen_macs: set[str]) -> int:
        marked = 0
        for device in self.netbox.list_synced_client_devices(self.settings.sync_tag):
            mac = (device.custom_fields or {}).get("unifi_mac")
            if mac and mac not in seen_macs and device.status.value == "active":
                self.netbox.mark_offline(device)
                marked += 1
        return marked
