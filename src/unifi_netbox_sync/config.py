from __future__ import annotations

import os
from dataclasses import dataclass, field


def _bool(value: str) -> bool:
    return value.strip().lower() in ("1", "true", "yes", "on")


@dataclass
class Settings:
    unifi_host: str
    unifi_username: str = ""
    unifi_password: str = ""
    unifi_api_key: str | None = None
    unifi_site: str = "default"
    unifi_is_udm: bool = True
    unifi_verify_ssl: bool = False

    netbox_url: str = ""
    netbox_token: str = ""
    netbox_verify_ssl: bool = True
    netbox_site_slug: str = ""
    netbox_ip_status: str = "active"

    dry_run: bool = False
    log_level: str = "INFO"
    log_format: str = "text"
    sync_tag: str = "unifi-sync"
    client_device_role_slug: str = "unifi-client"
    client_manufacturer_slug: str = "generic"
    client_device_type_slug: str = "generic-network-client"
    port_name_templates: list[str] = field(
        default_factory=lambda: ["{port}", "Port {port}", "GE{port}", "Gi{port}"]
    )
    cable_conflict_policy: str = "skip"
    mark_stale_offline: bool = True
    metrics_file: str | None = None

    @classmethod
    def from_env(cls) -> "Settings":
        try:
            unifi_host = os.environ["UNIFI_HOST"]
            netbox_url = os.environ["NETBOX_URL"]
            netbox_token = os.environ["NETBOX_TOKEN"]
            netbox_site_slug = os.environ["NETBOX_SITE_SLUG"]
        except KeyError as exc:
            raise SystemExit(f"Missing required environment variable: {exc}") from exc

        unifi_api_key = os.environ.get("UNIFI_API_KEY") or None
        unifi_username = os.environ.get("UNIFI_USERNAME", "")
        unifi_password = os.environ.get("UNIFI_PASSWORD", "")
        if not unifi_api_key and not (unifi_username and unifi_password):
            raise SystemExit(
                "Provide either UNIFI_API_KEY, or both UNIFI_USERNAME and UNIFI_PASSWORD"
            )

        templates_raw = os.environ.get("PORT_NAME_TEMPLATES", "{port},Port {port},GE{port},Gi{port}")
        port_name_templates = [t.strip() for t in templates_raw.split(",") if t.strip()]

        return cls(
            unifi_host=unifi_host.rstrip("/"),
            unifi_username=unifi_username,
            unifi_password=unifi_password,
            unifi_api_key=unifi_api_key,
            unifi_site=os.environ.get("UNIFI_SITE", "default"),
            unifi_is_udm=_bool(os.environ.get("UNIFI_IS_UDM", "true")),
            unifi_verify_ssl=_bool(os.environ.get("UNIFI_VERIFY_SSL", "false")),
            netbox_url=netbox_url.rstrip("/"),
            netbox_token=netbox_token,
            netbox_verify_ssl=_bool(os.environ.get("NETBOX_VERIFY_SSL", "true")),
            netbox_site_slug=netbox_site_slug,
            netbox_ip_status=os.environ.get("NETBOX_IP_STATUS", "active"),
            dry_run=_bool(os.environ.get("DRY_RUN", "false")),
            log_level=os.environ.get("LOG_LEVEL", "INFO"),
            log_format=os.environ.get("LOG_FORMAT", "text"),
            sync_tag=os.environ.get("SYNC_TAG", "unifi-sync"),
            client_device_role_slug=os.environ.get("CLIENT_DEVICE_ROLE_SLUG", "unifi-client"),
            client_manufacturer_slug=os.environ.get("CLIENT_MANUFACTURER_SLUG", "generic"),
            client_device_type_slug=os.environ.get("CLIENT_DEVICE_TYPE_SLUG", "generic-network-client"),
            port_name_templates=port_name_templates,
            cable_conflict_policy=os.environ.get("CABLE_CONFLICT_POLICY", "skip"),
            mark_stale_offline=_bool(os.environ.get("MARK_STALE_OFFLINE", "true")),
            metrics_file=os.environ.get("METRICS_FILE") or None,
        )
