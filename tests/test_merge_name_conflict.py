"""Legacy client merges that would collide with an existing device name."""

from __future__ import annotations

import logging


from unifi_netbox_sync.models import UnifiPort, UnifiSwitchDevice

from .fakes import FakeDevice, FakeNetboxGateway
from .test_device_sync import SWITCH_MAC, _run, _switch

OTHER_MAC = "aa:bb:cc:dd:ee:02"


def _seed_name_holder(netbox: FakeNetboxGateway, name: str = "core-switch", site: str = "main") -> FakeDevice:
    """An untagged device someone created by hand with the UniFi name."""
    holder = FakeDevice(name=name, site_slug=site)
    holder.role_slug = "switch"
    netbox.switches_without_mac.append(holder)
    return holder


def test_name_conflict_is_a_duplicate_not_an_error(caplog):
    netbox = FakeNetboxGateway()
    legacy = netbox.seed_legacy_client(SWITCH_MAC, site_slug="main")
    holder = _seed_name_holder(netbox)
    old_name = legacy.name

    with caplog.at_level(logging.WARNING):
        summary, _ = _run([_switch()], netbox=netbox)

    assert summary.errors == []
    assert summary.infra_name_conflicts == 1
    assert summary.infra_duplicates_avoided == 1
    assert summary.infra_merged == 0
    assert summary.infra_created == 0
    # Nothing renamed, converted, or deleted.
    assert legacy.name == old_name
    assert legacy.role_slug == "unifi-client"
    assert netbox.clients_by_mac[SWITCH_MAC] is legacy
    assert holder in netbox.switches_without_mac and holder.name == "core-switch"
    assert f"id={holder.id}" in caplog.text


def test_dry_run_reports_the_conflict(caplog):
    netbox = FakeNetboxGateway()
    legacy = netbox.seed_legacy_client(SWITCH_MAC, site_slug="main")
    _seed_name_holder(netbox)

    with caplog.at_level(logging.INFO):
        summary, _ = _run([_switch()], netbox=netbox, dry_run=True)

    assert summary.errors == []
    assert summary.infra_name_conflicts == 1
    assert "[dry-run] legacy client device" in caplog.text
    assert "would merge" not in caplog.text
    assert legacy.role_slug == "unifi-client"


def test_conflict_at_another_site_does_not_block_the_merge():
    netbox = FakeNetboxGateway()
    legacy = netbox.seed_legacy_client(SWITCH_MAC, site_slug="main")
    _seed_name_holder(netbox, site="site-b")

    summary, _ = _run([_switch()], netbox=netbox)

    assert summary.infra_name_conflicts == 0
    assert summary.infra_merged == 1
    assert legacy.name == "core-switch"


def test_one_failing_device_does_not_skip_the_rest_of_the_site():
    netbox = FakeNetboxGateway()
    real_upsert = netbox.upsert_infrastructure_device

    def flaky(mac, *args, **kwargs):
        if mac == SWITCH_MAC:
            raise LookupError("simulated NetBox rejection")
        return real_upsert(mac, *args, **kwargs)

    netbox.upsert_infrastructure_device = flaky
    second = UnifiSwitchDevice(
        mac=OTHER_MAC,
        name="switch-02",
        model="USW24POE",
        device_type="usw",
        serial="F09FC2AAAA02",
        ports=[UnifiPort(index=i, media="GE", max_speed=1000) for i in range(1, 5)],
    )

    summary, _ = _run([_switch(), second], netbox=netbox)

    assert len(summary.errors) == 1
    assert SWITCH_MAC in summary.errors[0]
    assert summary.infra_created == 1
    assert OTHER_MAC in netbox.infra_by_mac
