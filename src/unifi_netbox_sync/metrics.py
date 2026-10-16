from __future__ import annotations

import json
import logging
import time
from collections import Counter
from pathlib import Path

from .models import SyncSummary

logger = logging.getLogger(__name__)

_HELP_TEXT = {
    "clients_seen": "Number of UniFi clients seen in the last sync run",
    "devices_created": "NetBox client devices created in the last sync run",
    "devices_updated": "NetBox client devices updated in the last sync run",
    "devices_update_skipped": "NetBox client device updates skipped by DEVICE_UPDATE_POLICY in the last sync run",
    "cables_created": "NetBox cables created in the last sync run",
    "cables_skipped": "NetBox cables skipped (conflict or no match) in the last sync run",
    "stale_marked_offline": "NetBox client devices marked offline in the last sync run",
    "sites_created": "Number of NetBox sites created by the last sync run (SITE_POLICY=create)",
    "devices_synced": "Adopted UniFi infrastructure devices seen in the last sync run",
    "infra_created": "UniFi infrastructure devices created in NetBox in the last sync run",
    "infra_merged": "Legacy client devices converted in place into infrastructure in the last sync run",
    "infra_moved": "Infrastructure devices moved to the UniFi site where they are adopted in the last sync run",
    "infra_name_conflicts": "Legacy client merges skipped because another device already held the name at the site in the last sync run",
    "infra_duplicates_avoided": "Adopted UniFi devices not created because MAC or serial already existed in the last sync run",
    "infra_type_library": "Infrastructure device types taken from devicetype-library in the last sync run",
    "infra_type_generated": "Infrastructure device types built from UniFi data in the last sync run",
    "clients_unchanged": "Clients that already matched NetBox and needed no write in the last sync run",
    "ips_unchanged": "Client IP assignments that were already correct in the last sync run",
    "cables_unchanged": "Client cables that were already correct in the last sync run",
    "cache_hits": "Run-scoped NetBox lookups served from cache in the last sync run",
    "cache_misses": "Run-scoped NetBox lookups that hit the API in the last sync run",
    "clients_fast_path": "Clients answered from the prefetched index without the write path in the last sync run",
    "prefetch_seconds": "Seconds spent bulk-loading NetBox objects before per-client work in the last sync run",
    "prefetch_hits": "Lookups served from the bulk NetBox index in the last sync run",
    "errors": "Errors encountered in the last sync run",
    "duration_seconds": "Wall-clock duration of the last sync run, in seconds",
}


def _escape(value: str) -> str:
    """Escape a Prometheus label value (backslash, quote, newline)."""
    return value.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n")


def summary_as_dict(summary: SyncSummary) -> dict:
    return {
        "clients_seen": summary.clients_seen,
        "devices_created": summary.devices_created,
        "devices_updated": summary.devices_updated,
        "devices_update_skipped": summary.devices_update_skipped,
        "cables_created": summary.cables_created,
        "cables_skipped": summary.cables_skipped,
        "stale_marked_offline": summary.stale_marked_offline,
        "sites_created": summary.sites_created,
        "devices_synced": summary.devices_synced,
        "infra_created": summary.infra_created,
        "infra_merged": summary.infra_merged,
        "infra_moved": summary.infra_moved,
        "infra_duplicates_avoided": summary.infra_duplicates_avoided,
        "infra_name_conflicts": summary.infra_name_conflicts,
        "infra_type_library": summary.infra_type_library,
        "infra_type_generated": summary.infra_type_generated,
        "clients_unchanged": summary.clients_unchanged,
        "ips_unchanged": summary.ips_unchanged,
        "cables_unchanged": summary.cables_unchanged,
        "cache_hits": summary.cache_hits,
        "cache_misses": summary.cache_misses,
        "clients_fast_path": summary.clients_fast_path,
        "prefetch_seconds": round(summary.prefetch_seconds, 3),
        "prefetch_hits": summary.prefetch_hits,
        "errors": len(summary.errors or []),
        "duration_seconds": round(summary.duration_seconds, 3),
    }


def log_json_summary(summary: SyncSummary) -> None:
    """Emit one grep-able JSON line with the run summary, regardless of LOG_FORMAT.

    This is the lightweight alternative to a Prometheus HTTP endpoint that fits
    a one-shot batch job: log shippers (Loki, CloudWatch, etc.) can parse this
    line directly instead of the tool needing to run its own metrics server.
    """
    logger.info("sync_summary=%s", json.dumps(summary_as_dict(summary)))


def write_prometheus_textfile(summary: SyncSummary, path: str) -> None:
    """Write metrics in Prometheus textfile-collector format.

    Intended for node_exporter's --collector.textfile.directory (or any
    scraper that reads the same format from disk): the idiomatic way for a
    periodic batch job to expose Prometheus metrics without running its own
    HTTP server.
    """
    data = summary_as_dict(summary)
    prefix = "unifi_netbox_sync"
    lines: list[str] = []
    for key, value in data.items():
        metric = f"{prefix}_{key}"
        lines.append(f"# HELP {metric} {_HELP_TEXT.get(key, key)}")
        lines.append(f"# TYPE {metric} gauge")
        lines.append(f"{metric} {value}")

    # Per-site series, so a slow or erroring site is identifiable rather than
    # averaged into the totals above.
    if summary.site_stats:
        for metric, help_text in (
            ("site_duration_seconds", "Duration of the last sync run for one UniFi->NetBox site pair"),
            ("site_clients_seen", "Clients seen for one UniFi->NetBox site pair in the last run"),
            ("site_errors", "Errors for one UniFi->NetBox site pair in the last run"),
        ):
            lines.append(f"# HELP {prefix}_{metric} {help_text}")
            lines.append(f"# TYPE {prefix}_{metric} gauge")
            for st in summary.site_stats:
                labels = f'unifi_site="{_escape(st.unifi_site)}",netbox_site="{_escape(st.netbox_site_slug)}"'
                value = {
                    "site_duration_seconds": round(st.duration_seconds, 3),
                    "site_clients_seen": st.clients_seen,
                    "site_errors": st.errors,
                }[metric]
                lines.append(f"{prefix}_{metric}{{{labels}}} {value}")

    if summary.cable_skips:
        counts = Counter(skip.reason for skip in summary.cable_skips)
        lines.append(f"# HELP {prefix}_cable_skips_by_reason Cables skipped in the last sync run, by reason")
        lines.append(f"# TYPE {prefix}_cable_skips_by_reason gauge")
        for reason, count in sorted(counts.items()):
            lines.append(
                f'{prefix}_cable_skips_by_reason{{reason="{_escape(reason)}"}} {count}'
            )

    lines += [
        f"# HELP {prefix}_last_run_timestamp_seconds Unix time the last sync run finished",
        f"# TYPE {prefix}_last_run_timestamp_seconds gauge",
        f"{prefix}_last_run_timestamp_seconds {int(time.time())}",
        f"# HELP {prefix}_last_run_success Whether the last sync run completed with no per-client errors",
        f"# TYPE {prefix}_last_run_success gauge",
        f"{prefix}_last_run_success {0 if data['errors'] else 1}",
    ]

    tmp_path = Path(f"{path}.tmp")
    tmp_path.write_text("\n".join(lines) + "\n")
    tmp_path.replace(path)  # atomic rename so a scraper never reads a half-written file
    logger.info("Wrote Prometheus metrics to %s", path)
