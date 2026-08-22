from __future__ import annotations

import argparse
import sys

from .config import Settings
from .logging_utils import configure_logging
from .netbox_client import PynetboxGateway
from .sync import SyncEngine
from .unifi_client import UnifiClientAPI


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Sync UniFi Controller client devices and cable connections into NetBox"
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Plan the sync and log intended changes without writing to NetBox",
    )
    parser.add_argument("--log-level", default=None, help="Override LOG_LEVEL env var")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    settings = Settings.from_env()
    if args.dry_run:
        settings.dry_run = True
    if args.log_level:
        settings.log_level = args.log_level

    configure_logging(settings.log_level, settings.log_format)

    with UnifiClientAPI(
        host=settings.unifi_host,
        username=settings.unifi_username or None,
        password=settings.unifi_password or None,
        api_key=settings.unifi_api_key,
        is_udm=settings.unifi_is_udm,
        verify_ssl=settings.unifi_verify_ssl,
    ) as unifi:
        netbox = PynetboxGateway(
            url=settings.netbox_url,
            token=settings.netbox_token,
            verify_ssl=settings.netbox_verify_ssl,
        )
        engine = SyncEngine(unifi=unifi, netbox=netbox, settings=settings)
        summary = engine.run()

    return 1 if summary.errors else 0


if __name__ == "__main__":
    sys.exit(main())
