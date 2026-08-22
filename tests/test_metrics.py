from __future__ import annotations

import json
import logging

from unifi_netbox_sync.metrics import log_json_summary, summary_as_dict, write_prometheus_textfile
from unifi_netbox_sync.models import SyncSummary


def test_summary_as_dict_rounds_duration_and_counts_errors():
    summary = SyncSummary(clients_seen=3, devices_created=1, errors=["a", "b"], duration_seconds=1.23456)

    data = summary_as_dict(summary)

    assert data["clients_seen"] == 3
    assert data["devices_created"] == 1
    assert data["errors"] == 2
    assert data["duration_seconds"] == 1.235


def test_write_prometheus_textfile_content(tmp_path):
    summary = SyncSummary(clients_seen=2, cables_created=1, errors=["boom"])
    path = tmp_path / "metrics.prom"

    write_prometheus_textfile(summary, str(path))

    content = path.read_text()
    assert "unifi_netbox_sync_clients_seen 2" in content
    assert "unifi_netbox_sync_cables_created 1" in content
    assert "unifi_netbox_sync_errors 1" in content
    assert "unifi_netbox_sync_last_run_success 0" in content
    assert "# HELP unifi_netbox_sync_clients_seen" in content
    assert "# TYPE unifi_netbox_sync_clients_seen gauge" in content


def test_write_prometheus_textfile_success_flag_when_no_errors(tmp_path):
    summary = SyncSummary(clients_seen=1)
    path = tmp_path / "metrics.prom"

    write_prometheus_textfile(summary, str(path))

    assert "unifi_netbox_sync_last_run_success 1" in path.read_text()


def test_write_prometheus_textfile_leaves_no_tmp_file(tmp_path):
    path = tmp_path / "metrics.prom"

    write_prometheus_textfile(SyncSummary(), str(path))

    assert path.exists()
    assert not (tmp_path / "metrics.prom.tmp").exists()


def test_log_json_summary_logs_parseable_json(caplog):
    summary = SyncSummary(clients_seen=5)

    with caplog.at_level(logging.INFO, logger="unifi_netbox_sync.metrics"):
        log_json_summary(summary)

    record = next(r for r in caplog.records if r.message.startswith("sync_summary="))
    payload = json.loads(record.message.removeprefix("sync_summary="))
    assert payload["clients_seen"] == 5
