from __future__ import annotations

import argparse
import logging
import os
import socket
import sys
from pathlib import Path

from . import __version__
from .caching import CachingNetboxGateway
from .config import Settings
from .health import write_success_marker
from .logging_utils import configure_logging
from .netbox_client import PynetboxGateway
from .sync import SyncEngine
from .unifi_client import UnifiClientAPI

logger = logging.getLogger(__name__)


def _read_build_file(name: str) -> str:
    """Image label companion files written by the Dockerfile, when present."""
    candidates = [Path("/app") / name, Path(__file__).resolve().parents[2] / name]
    for path in candidates:
        try:
            text = path.read_text(encoding="utf-8").strip()
        except OSError:
            continue
        if text and text != "unknown":
            return text
    return "unknown"


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Sync UniFi Controller client devices and cable connections into NetBox"
    )
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument(
        "--dry-run",
        action="store_true",
        help="Plan the sync and log intended changes without writing to NetBox (the default)",
    )
    mode.add_argument(
        "--apply",
        action="store_true",
        help="Write changes to NetBox. Overrides DRY_RUN=true.",
    )
    parser.add_argument("--log-level", default=None, help="Override LOG_LEVEL env var")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    settings = Settings.from_env()
    if args.dry_run:
        settings.dry_run = True
    if args.apply:
        settings.dry_run = False
    if args.log_level:
        settings.log_level = args.log_level

    configure_logging(settings.log_level, settings.log_format)

    # Backstop for any HTTP library call that forgot its own timeout. A hung
    # socket is what left the long-running container silent for weeks.
    socket.setdefaulttimeout(max(settings.unifi_timeout, settings.netbox_timeout))

    revision = _read_build_file("REVISION")
    logger.info(
        "unifi-netbox-sync %s revision %s dry_run=%s site_policy=%s device_update_policy=%s "
        "cable_conflict_policy=%s",
        __version__,
        revision,
        settings.dry_run,
        settings.site_policy,
        settings.device_update_policy,
        settings.cable_conflict_policy,
    )
    if settings.dry_run:
        logger.warning(
            "DRY_RUN is enabled: every create/update/delete is logged and nothing is written. "
            "Pass --apply or set DRY_RUN=false to write to NetBox."
        )
    else:
        logger.warning("DRY_RUN is disabled: this run will write to NetBox.")

    with UnifiClientAPI(
        host=settings.unifi_host,
        username=settings.unifi_username or None,
        password=settings.unifi_password or None,
        api_key=settings.unifi_api_key,
        is_udm=settings.unifi_is_udm,
        verify_ssl=settings.unifi_verify_ssl,
        timeout=settings.unifi_timeout,
    ) as unifi:
        netbox = CachingNetboxGateway(
            PynetboxGateway(
                url=settings.netbox_url,
                token=settings.netbox_token,
                verify_ssl=settings.netbox_verify_ssl,
                timeout=settings.netbox_timeout,
                page_size=settings.netbox_page_size,
                max_inflight=settings.netbox_max_inflight,
                rate_limit_retries=settings.netbox_rate_limit_retries,
            )
        )
        engine = SyncEngine(unifi=unifi, netbox=netbox, settings=settings)
        summary = engine.run()

    heartbeat = os.environ.get("HEARTBEAT_FILE")
    if not summary.errors:
        if heartbeat:
            write_success_marker(heartbeat)
            logger.info("Wrote success marker %s", heartbeat)
    elif heartbeat:
        logger.error(
            "Not writing success marker %s: %d error(s) this run",
            heartbeat,
            len(summary.errors),
        )

    return 1 if summary.errors else 0


if __name__ == "__main__":
    sys.exit(main())
