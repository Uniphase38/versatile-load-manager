"""Bounded load allowances during and after a successful balance."""

from dataclasses import dataclass
from datetime import datetime, timedelta
import math
from typing import Optional


@dataclass(frozen=True)
class BalanceSurplusAllowance:
    start_allowed: bool
    keep_allowed: bool
    reference_charge_w: Optional[float]


def qualify_balance_surplus(
    *, enabled, balancing, curtailment_confirmed, deficit, settled,
    battery_power_w, grid_power_w, reference_charge_w,
    charge_tolerance_w, grid_tolerance_w,
) -> BalanceSurplusAllowance:
    """Authorize a monitored trial, then protect the observed battery charge.

    Curtailment proves unused production, not its amount. A load start remains
    a trial. Keep the pre-start charge reference during settling, then renew
    it only with confirmed curtailment (the BMS may naturally taper). On a
    restart no reference is guessed: curtailment must qualify again.
    """
    if not enabled or not balancing:
        return BalanceSurplusAllowance(False, False, None)
    known = all(
        value is not None and math.isfinite(value)
        for value in (battery_power_w, grid_power_w)
    )
    if not known:
        return BalanceSurplusAllowance(False, False, reference_charge_w)
    if (
        settled and curtailment_confirmed and not deficit
        and battery_power_w >= 0 and grid_power_w <= grid_tolerance_w
    ):
        reference_charge_w = battery_power_w
    keep = bool(
        reference_charge_w is not None and not deficit
        and battery_power_w >= max(0.0, reference_charge_w) - charge_tolerance_w
        and grid_power_w <= grid_tolerance_w
    )
    return BalanceSurplusAllowance(
        bool(settled and curtailment_confirmed and keep), keep, reference_charge_w,
    )


def post_balance_deadline(now, last_success_at, battery, schedule):
    """Return today's bounded deadline only after a recorded real success."""
    if not battery.post_balance_support_enabled or last_success_at is None:
        return None
    if (now.tzinfo is None) != (last_success_at.tzinfo is None):
        return None
    if now.tzinfo is not None:
        last_success_at = last_success_at.astimezone(now.tzinfo)
    if last_success_at.date() != now.date() or last_success_at > now:
        return None
    def at(clock):
        hour, minute = map(int, clock.split(":"))
        return now.replace(hour=hour, minute=minute, second=0, microsecond=0)
    return min(
        at(schedule.solar_end) + timedelta(seconds=battery.post_balance_extension_s),
        at(schedule.off_peak_start),
    )
