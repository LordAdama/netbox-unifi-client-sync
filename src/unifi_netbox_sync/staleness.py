"""Decide whether a managed client device is idle or actually gone.

``/stat/sta`` is only who is associated right now. Phones and laptops drop
out of that list while asleep and come back with the same MAC, so absence
from it is not evidence the device should be marked offline. ``/stat/alluser``
carries ``last_seen``. A device inside the grace window stays active. A
device the controller still lists but without a usable timestamp also stays
active: missing data is not the same as old data.

A locally-administered (randomized) MAC uses the same rule. Those addresses
belong to phones that sleep and, separately, rotate. Sleeping is the grace
window. Once ``last_seen`` is past the window, or the controller has dropped
the MAC entirely, the address will not come back and the NetBox record can
go offline.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass


@dataclass(frozen=True)
class OfflineDecision:
    mark: bool
    reason: str  # "not_known", "no_last_seen", "within_grace", "expired"


def offline_decision(
    mac: str,
    known: Mapping[str, float | None],
    now: float,
    grace_seconds: float,
) -> OfflineDecision:
    """Whether ``mac`` should be marked offline.

    ``known`` maps a lowercased MAC to its last-seen unix time. A missing
    key means the controller has no record of the address. A value of None
    means the record exists but ``last_seen`` was missing or zero.
    """
    key = mac.lower()
    if key not in known:
        return OfflineDecision(True, "not_known")
    last = known[key]
    if last is None:
        return OfflineDecision(False, "no_last_seen")
    if now - last <= grace_seconds:
        return OfflineDecision(False, "within_grace")
    return OfflineDecision(True, "expired")


def format_grace(seconds: float) -> str:
    """Compact form of a grace period for log lines (``7d``, ``24h``)."""
    if seconds >= 86400 and seconds % 86400 == 0:
        return f"{int(seconds // 86400)}d"
    if seconds >= 3600 and seconds % 3600 == 0:
        return f"{int(seconds // 3600)}h"
    if seconds >= 60 and seconds % 60 == 0:
        return f"{int(seconds // 60)}m"
    return f"{seconds:g}s"
