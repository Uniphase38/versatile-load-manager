"""Deterministic feasibility planning for off-peak balance completion."""

from dataclasses import dataclass
from datetime import datetime
from typing import Iterable, Tuple


@dataclass(frozen=True)
class RequiredLoad:
    """Remaining mandatory off-peak work for one managed load."""

    name: str
    power_w: float
    remaining_h: float


@dataclass(frozen=True)
class HcBalancePlan:
    """Result of checking whether a grid-assisted balance can finish safely."""

    feasible: bool
    reason: str
    window_h: float
    charge_window_h: float
    finish_tail_h: float
    battery_energy_kwh: float
    required_load_energy_kwh: float
    grid_energy_budget_kwh: float
    estimated_charge_h: float
    estimated_total_h: float


def plan_hc_balance(
    *,
    now: datetime,
    hc_end: datetime,
    soc: float,
    target_soc: float,
    battery_capacity_kwh: float,
    max_grid_charge_power_w: float,
    max_ac_power_w: float,
    night_base_load_w: float,
    required_loads: Iterable[RequiredLoad] = (),
    hold_s: int = 0,
    safety_margin_s: int = 1800,
    energy_margin: float = 1.15,
) -> HcBalancePlan:
    """Check time and energy headroom before authorising grid balancing.

    The planner deliberately uses configured installation characteristics rather
    than trying to infer cable, inverter or household limits. Mandatory loads
    keep their remaining rated-energy budget. The battery receives only the
    residual AC budget and is also capped by its configured grid-charge power.
    """
    loads: Tuple[RequiredLoad, ...] = tuple(required_loads)
    window_h = max(0.0, (hc_end - now).total_seconds() / 3600.0)
    finish_tail_h = (max(0, hold_s) + max(0, safety_margin_s)) / 3600.0
    charge_window_h = max(0.0, window_h - finish_tail_h)

    if not 0 <= soc <= 100 or not soc <= target_soc <= 100:
        raise ValueError("SOC values must satisfy 0 <= soc <= target_soc <= 100")
    for name, value in (
        ("battery_capacity_kwh", battery_capacity_kwh),
        ("max_grid_charge_power_w", max_grid_charge_power_w),
        ("max_ac_power_w", max_ac_power_w),
        ("energy_margin", energy_margin),
    ):
        if value <= 0:
            raise ValueError("%s must be positive" % name)
    if night_base_load_w < 0:
        raise ValueError("night_base_load_w must not be negative")
    if night_base_load_w >= max_ac_power_w:
        return HcBalancePlan(
            False, "base_load_exceeds_ac_budget", window_h, charge_window_h, finish_tail_h,
            0.0, 0.0, 0.0, 0.0, finish_tail_h,
        )

    load_energy_kwh = 0.0
    for load in loads:
        if load.power_w < 0 or load.remaining_h < 0:
            raise ValueError("required load power/runtime must not be negative")
        if load.power_w + night_base_load_w > max_ac_power_w:
            return HcBalancePlan(
                False, "required_load_exceeds_ac_budget", window_h, charge_window_h, finish_tail_h,
                0.0, load_energy_kwh, 0.0, 0.0, finish_tail_h,
            )
        if load.remaining_h > window_h:
            return HcBalancePlan(
                False, "required_load_does_not_fit_window", window_h, charge_window_h, finish_tail_h,
                0.0, load_energy_kwh, 0.0, 0.0, finish_tail_h,
            )
        load_energy_kwh += load.power_w * load.remaining_h / 1000.0

    battery_energy_kwh = (
        battery_capacity_kwh * (target_soc - soc) / 100.0 * energy_margin
    )
    ac_budget_w = max_ac_power_w - night_base_load_w
    grid_energy_budget_kwh = ac_budget_w * window_h / 1000.0

    if load_energy_kwh + battery_energy_kwh > grid_energy_budget_kwh:
        return HcBalancePlan(
            False, "insufficient_grid_energy_budget", window_h, charge_window_h, finish_tail_h,
            battery_energy_kwh, load_energy_kwh, grid_energy_budget_kwh,
            0.0, finish_tail_h,
        )

    battery_power_w = min(max_grid_charge_power_w, ac_budget_w)
    estimated_charge_h = (
        0.0 if battery_energy_kwh == 0
        else battery_energy_kwh * 1000.0 / battery_power_w
    )

    # The total-energy check above allows charging alongside mandatory loads.
    # This additional check rejects a plan that could not charge in time even
    # if no managed load were running.
    if estimated_charge_h > charge_window_h:
        return HcBalancePlan(
            False, "battery_charge_does_not_fit_before_finish_tail", window_h, charge_window_h, finish_tail_h,
            battery_energy_kwh, load_energy_kwh, grid_energy_budget_kwh,
            estimated_charge_h, estimated_charge_h + finish_tail_h,
        )

    estimated_total_h = (
        (load_energy_kwh + battery_energy_kwh) * 1000.0 / ac_budget_w
        + finish_tail_h
    )
    return HcBalancePlan(
        True, "feasible", window_h, charge_window_h, finish_tail_h, battery_energy_kwh,
        load_energy_kwh, grid_energy_budget_kwh, estimated_charge_h,
        estimated_total_h,
    )
