"""Pure H24 load policy: snapshots in, explainable decisions out."""

from dataclasses import dataclass
from datetime import datetime, time, timedelta, timezone
from enum import Enum
import math
from typing import Dict, Iterable, Optional, Tuple

from .config import AppConfig, LoadConfig, runtime_period_month


class Action(str, Enum):
    START = "start"
    STOP = "stop"
    KEEP_ON = "keep_on"
    KEEP_OFF = "keep_off"
    UNAVAILABLE = "unavailable"


class Reason(str, Enum):
    GRID_LOST = "grid_lost"
    TELEMETRY_INVALID = "telemetry_invalid"
    UNREACHABLE = "unreachable"
    DISABLED = "disabled"
    AC_OVERLOAD = "ac_overload"
    SOLAR_SOC_HIGH = "solar_soc_high"
    SOLAR_HYSTERESIS = "solar_hysteresis"
    SOC_LOW = "soc_low"
    OFF_PEAK_REQUIRED = "off_peak_required"
    RUNTIME_COMPLETE = "runtime_complete"
    OUTSIDE_ALLOWED_WINDOW = "outside_allowed_window"
    AC_HEADROOM_INSUFFICIENT = "ac_headroom_insufficient"
    SOLAR_SURPLUS_CONFIRMED = "solar_surplus_confirmed"
    SOLAR_SURPLUS_PENDING = "solar_surplus_pending"
    SOLAR_CHARGE_SURPLUS = "solar_charge_surplus"
    ENERGY_DEFICIT = "energy_deficit"
    PROBE_COOLDOWN = "probe_cooldown"
    PROBE_LOCKOUT = "probe_lockout"
    THERMOSTAT_SATISFIED = "thermostat_satisfied"
    ACTIVE_POWER_FAULT = "active_power_fault"
    POWER_TELEMETRY_UNAVAILABLE = "power_telemetry_unavailable"
    START_STAGGERED = "start_staggered"
    MIN_ON_TIME = "min_on_time"
    MIN_OFF_TIME = "min_off_time"
    THERMOSTAT_PENDING = "thermostat_pending"
    BATTERY_BALANCING = "battery_balancing"
    BALANCING_SURPLUS = "balancing_surplus"
    WINDOW_END_TOO_CLOSE = "window_end_too_close"
    TARGET_FINISH = "target_finish"
    TARGET_PROGRESS = "target_progress"
    FINISH_SOC_LOW = "finish_soc_low"
    FINISH_DEFICIT_EXCEEDED = "finish_deficit_exceeded"
    DAILY_COMPLETED = "daily_completed"
    POST_BALANCE_SUPPORT = "post_balance_support"
    POST_BALANCE_SOC_LOW = "post_balance_soc_low"
    POST_BALANCE_POWER_LIMIT = "post_balance_power_limit"
    POST_BALANCE_GRID_IMPORT = "post_balance_grid_import"
    POST_BALANCE_DEADLINE = "post_balance_deadline"


@dataclass(frozen=True)
class LoadState:
    config: LoadConfig
    output: Optional[bool]
    runtime_h: float = 0.0
    reachable: bool = True
    probe_blocked: bool = False
    probe_locked_out: bool = False
    power_state: Optional[str] = None
    thermostat_satisfied: bool = False
    output_duration_s: float = 0.0
    daily_complete: bool = False
    external_control: bool = False


@dataclass(frozen=True)
class Snapshot:
    now: datetime
    soc: float
    ac_power_w: float
    grid_lost: bool
    loads: Tuple[LoadState, ...]
    solar_surplus_ready: bool = False
    energy_deficit: bool = False
    visible_surplus_w: Optional[float] = None
    battery_priority: bool = False
    start_settle_remaining_s: float = 0.0
    battery_power_w: Optional[float] = None
    battery_holding: bool = False
    battery_priority_exclusive: bool = False
    balance_surplus_start_allowed: bool = False
    balance_surplus_keep_allowed: bool = False
    post_balance_until: Optional[datetime] = None
    grid_power_w: Optional[float] = None
    balance_charge_reference_w: Optional[float] = None


@dataclass(frozen=True)
class Decision:
    load_name: str
    action: Action
    reason: Reason


def _clock(value: str) -> time:
    hour, minute = (int(part) for part in value.split(":"))
    return time(hour, minute)


def _inside(now: time, start: time, end: time) -> bool:
    """Return whether now is in [start, end), including midnight wrapping."""
    if start <= end:
        return start <= now < end
    return now >= start or now < end


def _seconds_until(now: datetime, end: time) -> float:
    """Return real seconds until the next local occurrence of *end*."""
    candidate = now.replace(
        hour=end.hour, minute=end.minute, second=0, microsecond=0
    )
    if candidate <= now:
        candidate += timedelta(days=1)
    if now.tzinfo is not None:
        return max(0.0, (
            candidate.astimezone(timezone.utc) - now.astimezone(timezone.utc)
        ).total_seconds())
    return max(0.0, (candidate - now).total_seconds())


def _seconds_since(now: datetime, event: time) -> float:
    """Return real seconds since today's local occurrence of *event*."""
    candidate = now.replace(
        hour=event.hour, minute=event.minute, second=0, microsecond=0
    )
    if candidate > now:
        candidate -= timedelta(days=1)
    if now.tzinfo is not None:
        return max(0.0, (
            now.astimezone(timezone.utc) - candidate.astimezone(timezone.utc)
        ).total_seconds())
    return max(0.0, (now - candidate).total_seconds())


class LoadPolicy:
    """Evaluate safety, solar hysteresis, off-peak runtime and AC headroom."""

    def __init__(self, config: AppConfig):
        self.config = config

    @staticmethod
    def _decision(state: LoadState, action: Action, reason: Reason) -> Decision:
        return Decision(state.config.name, action, reason)

    def evaluate(self, snapshot: Snapshot) -> Tuple[Decision, ...]:
        decisions: Dict[str, Decision] = {}
        projected_power = float(snapshot.ac_power_w)
        states = sorted(snapshot.loads, key=lambda item: item.config.priority)

        # Unknown relays are never guessed or commanded.
        available = []
        for state in states:
            if not state.reachable or state.output is None:
                decisions[state.config.name] = self._decision(
                    state, Action.UNAVAILABLE, Reason.UNREACHABLE
                )
            else:
                available.append(state)

        telemetry_valid = (
            math.isfinite(snapshot.soc)
            and 0 <= snapshot.soc <= 100
            and math.isfinite(snapshot.ac_power_w)
        )
        if not telemetry_valid:
            for state in available:
                action = Action.STOP if state.output else Action.KEEP_OFF
                decisions[state.config.name] = self._decision(
                    state, action, Reason.TELEMETRY_INVALID
                )
            return tuple(decisions[state.config.name] for state in states)

        # Grid loss dominates every scheduling rule.
        if snapshot.grid_lost:
            for state in available:
                action = Action.STOP if state.output else Action.KEEP_OFF
                decisions[state.config.name] = self._decision(
                    state, action, Reason.GRID_LOST
                )
            return tuple(decisions[state.config.name] for state in states)

        # Shed lowest-priority active loads until the installation is under limit.
        if projected_power > self.config.max_ac_power_w:
            for state in reversed(available):
                if state.output and projected_power > self.config.max_ac_power_w:
                    decisions[state.config.name] = self._decision(
                        state, Action.STOP, Reason.AC_OVERLOAD
                    )
                    projected_power = max(0.0, projected_power - state.config.rated_w)

        now = snapshot.now.time()
        solar = _inside(
            now,
            _clock(self.config.schedule.solar_start),
            _clock(self.config.schedule.solar_end),
        )
        off_peak = _inside(
            now,
            _clock(self.config.schedule.off_peak_start),
            _clock(self.config.schedule.daily_reset),
        )
        solar_end = _clock(self.config.schedule.solar_end)
        remaining_visible_surplus_w = snapshot.visible_surplus_w
        start_scheduled = False
        runtime_month = runtime_period_month(
            snapshot.now, self.config.schedule.daily_reset
        )
        post_context = (
            self.config.battery.post_balance_support_enabled
            and snapshot.post_balance_until is not None
            and not snapshot.battery_priority and not off_peak
        )
        post_balance = bool(
            post_context and snapshot.now < snapshot.post_balance_until
            and now >= _clock(self.config.schedule.solar_start)
        )
        battery_w = snapshot.battery_power_w
        grid_w = snapshot.grid_power_w
        post_guard = None
        if post_balance:
            if snapshot.soc <= self.config.battery.post_balance_min_soc:
                post_guard = Reason.POST_BALANCE_SOC_LOW
            elif grid_w is None or not math.isfinite(grid_w) or grid_w > self.config.grid_import_tolerance_w:
                post_guard = Reason.POST_BALANCE_GRID_IMPORT
            elif battery_w is None or not math.isfinite(battery_w):
                post_guard = Reason.POST_BALANCE_POWER_LIMIT
            elif battery_w < -self.config.battery.post_balance_max_discharge_w:
                # Shed only the lowest-priority running loads needed to bring
                # battery discharge back under the configured aggregate limit.
                for state in reversed(available):
                    if (
                        state.output and not state.external_control
                        and state.config.name not in decisions
                        and battery_w < -self.config.battery.post_balance_max_discharge_w
                    ):
                        decisions[state.config.name] = self._decision(
                            state, Action.STOP, Reason.POST_BALANCE_POWER_LIMIT,
                        )
                        projected_power = max(0.0, projected_power - state.config.rated_w)
                        battery_w += state.config.rated_w
                if battery_w < -self.config.battery.post_balance_max_discharge_w:
                    post_guard = Reason.POST_BALANCE_POWER_LIMIT

        for state in available:
            if state.config.name in decisions:
                continue
            if not state.config.enabled:
                action = Action.STOP if state.output else Action.KEEP_OFF
                decisions[state.config.name] = self._decision(
                    state, action, Reason.DISABLED
                )
                if state.output:
                    projected_power = max(0.0, projected_power - state.config.rated_w)
                continue

            target_runtime_h = state.config.target_runtime_h_for_month(
                runtime_month
            )
            required_runtime_h = state.config.required_runtime_h_for_month(
                runtime_month
            )
            runtime_required = state.runtime_h < required_runtime_h
            support_goal_h = (
                target_runtime_h if target_runtime_h is not None else required_runtime_h
            )
            support_needed = (
                state.config.completion_mode == "thermostat"
                or state.runtime_h < support_goal_h
            )
            target_remaining_h = (
                None
                if target_runtime_h is None
                else max(0.0, target_runtime_h - state.runtime_h)
            )
            finish_candidate = bool(
                state.config.target_finish_enabled
                and target_remaining_h is not None
                and state.runtime_h >= required_runtime_h
                and 0 < target_remaining_h
                <= state.config.target_finish_max_remaining_h
            )
            finish_grace = bool(
                not solar
                and state.config.target_finish_grace_s > 0
                and _seconds_since(snapshot.now, solar_end)
                <= state.config.target_finish_grace_s
            )
            finish_window = solar or finish_grace
            if finish_grace:
                finish_window_remaining_s = max(
                    0.0,
                    state.config.target_finish_grace_s
                    - _seconds_since(snapshot.now, solar_end),
                )
            elif solar:
                finish_window_remaining_s = (
                    _seconds_until(snapshot.now, solar_end)
                    + state.config.target_finish_grace_s
                )
            else:
                finish_window_remaining_s = 0.0
            progress_candidate = bool(
                state.config.target_finish_enabled
                and target_remaining_h is not None
                and state.runtime_h >= required_runtime_h
                and target_remaining_h
                > state.config.target_finish_max_remaining_h
                and finish_window
                and finish_window_remaining_s
                <= state.config.target_finish_max_remaining_h * 3600
            )
            finish_assist_candidate = finish_candidate or progress_candidate
            finish_reason = (
                Reason.TARGET_FINISH
                if finish_candidate
                else Reason.TARGET_PROGRESS
            )
            finish_soc_ok = snapshot.soc >= state.config.target_finish_min_soc
            finish_running_battery_ok = bool(
                snapshot.battery_power_w is not None
                and math.isfinite(snapshot.battery_power_w)
                and snapshot.battery_power_w
                >= -state.config.target_finish_max_battery_discharge_w
            )
            finish_start_battery_ok = bool(
                (
                    snapshot.battery_power_w is not None
                    and math.isfinite(snapshot.battery_power_w)
                    and snapshot.battery_power_w - state.config.rated_w
                    >= -state.config.target_finish_max_battery_discharge_w
                )
                or (
                    snapshot.solar_surplus_ready
                    and not snapshot.energy_deficit
                )
            )
            if state.probe_locked_out:
                decisions[state.config.name] = self._decision(
                    state,
                    Action.STOP if state.output else Action.KEEP_OFF,
                    Reason.PROBE_LOCKOUT,
                )
                if state.output:
                    projected_power = max(
                        0.0, projected_power - state.config.rated_w
                    )
                continue
            if not state.output and (
                state.daily_complete or state.thermostat_satisfied
            ):
                decisions[state.config.name] = self._decision(
                    state,
                    Action.KEEP_OFF,
                    (
                        Reason.DAILY_COMPLETED
                        if state.daily_complete and (solar or off_peak)
                        else Reason.THERMOSTAT_SATISFIED
                        if solar or off_peak
                        else Reason.OUTSIDE_ALLOWED_WINDOW
                    ),
                )
                continue
            if not state.output and state.probe_blocked:
                decisions[state.config.name] = self._decision(
                    state, Action.KEEP_OFF, Reason.PROBE_COOLDOWN
                )
                continue
            if state.output:
                if state.power_state == "inactive":
                    reason = (
                        Reason.ACTIVE_POWER_FAULT
                        if state.config.probe_failure_mode == "lockout"
                        else Reason.THERMOSTAT_SATISFIED
                    )
                    decisions[state.config.name] = self._decision(
                        state, Action.STOP, reason
                    )
                    projected_power = max(
                        0.0, projected_power - state.config.rated_w
                    )
                elif state.power_state == "unavailable":
                    decisions[state.config.name] = self._decision(
                        state, Action.STOP, Reason.POWER_TELEMETRY_UNAVAILABLE
                    )
                    projected_power = max(
                        0.0, projected_power - state.config.rated_w
                    )
                elif solar and snapshot.soc <= self.config.battery.soc_off:
                    decisions[state.config.name] = self._decision(
                        state, Action.STOP, Reason.SOC_LOW
                    )
                    projected_power = max(0.0, projected_power - state.config.rated_w)
                elif (
                    solar and snapshot.battery_priority_exclusive
                    and self.config.battery.balance_surplus_enabled
                    and not snapshot.balance_surplus_keep_allowed
                ):
                    decisions[state.config.name] = self._decision(
                        state, Action.STOP, Reason.BATTERY_BALANCING,
                    )
                    projected_power = max(0.0, projected_power - state.config.rated_w)
                elif post_balance and post_guard is not None:
                    decisions[state.config.name] = self._decision(state, Action.STOP, post_guard)
                    projected_power = max(0.0, projected_power - state.config.rated_w)
                elif post_context and not post_balance and not solar:
                    decisions[state.config.name] = self._decision(
                        state, Action.STOP, Reason.POST_BALANCE_DEADLINE,
                    )
                    projected_power = max(0.0, projected_power - state.config.rated_w)
                elif state.output_duration_s < state.config.min_on_time_s:
                    decisions[state.config.name] = self._decision(
                        state, Action.KEEP_ON, Reason.MIN_ON_TIME
                    )
                elif solar and snapshot.battery_priority_exclusive:
                    complete = target_runtime_h is not None and state.runtime_h >= target_runtime_h
                    keep = (
                        not complete and self.config.battery.balance_surplus_enabled
                        and snapshot.balance_surplus_keep_allowed
                    )
                    decisions[state.config.name] = self._decision(
                        state, Action.KEEP_ON if keep else Action.STOP,
                        Reason.RUNTIME_COMPLETE if complete
                        else Reason.BALANCING_SURPLUS if keep
                        else Reason.BATTERY_BALANCING,
                    )
                    if not keep:
                        projected_power = max(0.0, projected_power - state.config.rated_w)
                elif post_balance:
                    decisions[state.config.name] = self._decision(
                        state, Action.KEEP_ON if support_needed else Action.STOP,
                        Reason.POST_BALANCE_SUPPORT if support_needed else Reason.RUNTIME_COMPLETE,
                    )
                    if not support_needed:
                        projected_power = max(0.0, projected_power - state.config.rated_w)
                elif (
                    solar
                    and target_runtime_h is not None
                    and state.runtime_h >= target_runtime_h
                ):
                    decisions[state.config.name] = self._decision(
                        state, Action.STOP, Reason.RUNTIME_COMPLETE
                    )
                    projected_power = max(
                        0.0, projected_power - state.config.rated_w
                    )
                elif solar and snapshot.battery_priority:
                    balancing_reserve_ok = bool(
                        not snapshot.energy_deficit
                        and (
                            snapshot.battery_holding
                            or (
                                snapshot.visible_surplus_w is not None
                                and math.isfinite(snapshot.visible_surplus_w)
                                and snapshot.visible_surplus_w
                                >= self.config.battery_charge_reserve_w
                            )
                        )
                    )
                    if balancing_reserve_ok:
                        decisions[state.config.name] = self._decision(
                            state, Action.KEEP_ON, Reason.BALANCING_SURPLUS
                        )
                    else:
                        decisions[state.config.name] = self._decision(
                            state, Action.STOP, Reason.BATTERY_BALANCING
                        )
                        projected_power = max(
                            0.0, projected_power - state.config.rated_w
                        )
                elif (
                    finish_assist_candidate
                    and finish_window
                    and not finish_soc_ok
                ):
                    decisions[state.config.name] = self._decision(
                        state, Action.STOP, Reason.FINISH_SOC_LOW
                    )
                    projected_power = max(
                        0.0, projected_power - state.config.rated_w
                    )
                elif (
                    finish_assist_candidate
                    and finish_window
                    and not finish_running_battery_ok
                ):
                    decisions[state.config.name] = self._decision(
                        state, Action.STOP, Reason.FINISH_DEFICIT_EXCEEDED
                    )
                    projected_power = max(
                        0.0, projected_power - state.config.rated_w
                    )
                elif finish_assist_candidate and finish_window:
                    decisions[state.config.name] = self._decision(
                        state, Action.KEEP_ON, finish_reason
                    )
                elif off_peak and (
                    runtime_required
                    or state.config.completion_mode == "thermostat"
                ):
                    reason = (
                        Reason.OFF_PEAK_REQUIRED
                        if runtime_required
                        else Reason.THERMOSTAT_PENDING
                    )
                    decisions[state.config.name] = self._decision(
                        state, Action.KEEP_ON, reason
                    )
                elif solar and snapshot.energy_deficit:
                    decisions[state.config.name] = self._decision(
                        state, Action.STOP, Reason.ENERGY_DEFICIT
                    )
                    projected_power = max(0.0, projected_power - state.config.rated_w)
                elif solar:
                    decisions[state.config.name] = self._decision(
                        state, Action.KEEP_ON, Reason.SOLAR_HYSTERESIS
                    )
                else:
                    reason = Reason.RUNTIME_COMPLETE if off_peak else Reason.OUTSIDE_ALLOWED_WINDOW
                    decisions[state.config.name] = self._decision(state, Action.STOP, reason)
                    projected_power = max(0.0, projected_power - state.config.rated_w)
                continue

            if state.output_duration_s < state.config.min_off_time_s:
                decisions[state.config.name] = self._decision(
                    state, Action.KEEP_OFF, Reason.MIN_OFF_TIME
                )
                continue

            should_start = False
            starts_from_visible_surplus = False
            reason = Reason.OUTSIDE_ALLOWED_WINDOW
            if snapshot.battery_priority_exclusive:
                if target_runtime_h is not None and state.runtime_h >= target_runtime_h:
                    reason = Reason.RUNTIME_COMPLETE
                else:
                    should_start = (
                        self.config.battery.balance_surplus_enabled
                        and snapshot.balance_surplus_start_allowed
                    )
                    reason = Reason.BALANCING_SURPLUS if should_start else Reason.BATTERY_BALANCING
            elif post_balance:
                if not support_needed:
                    reason = Reason.RUNTIME_COMPLETE
                elif post_guard is not None:
                    reason = post_guard
                elif snapshot.soc < (
                    self.config.battery.post_balance_min_soc
                    + self.config.battery.load_start_hysteresis_soc
                ):
                    reason = Reason.POST_BALANCE_SOC_LOW
                elif battery_w - state.config.rated_w < -self.config.battery.post_balance_max_discharge_w:
                    reason = Reason.POST_BALANCE_POWER_LIMIT
                else:
                    should_start = True
                    reason = Reason.POST_BALANCE_SUPPORT
            elif post_context and not solar:
                reason = Reason.POST_BALANCE_DEADLINE
            elif snapshot.battery_priority and not solar:
                reason = Reason.BATTERY_BALANCING
            elif finish_assist_candidate and finish_window:
                if not finish_soc_ok:
                    reason = Reason.FINISH_SOC_LOW
                elif not finish_start_battery_ok:
                    reason = Reason.FINISH_DEFICIT_EXCEEDED
                else:
                    should_start = True
                    reason = finish_reason
            elif solar:
                if (
                    target_runtime_h is not None
                    and state.runtime_h >= target_runtime_h
                ):
                    reason = Reason.RUNTIME_COMPLETE
                elif snapshot.battery_priority:
                    visible_surplus_sufficient = bool(
                        remaining_visible_surplus_w is not None
                        and math.isfinite(remaining_visible_surplus_w)
                        and remaining_visible_surplus_w
                        >= state.config.rated_w
                        + self.config.battery_charge_reserve_w
                    )
                    holding_curtailment_sufficient = bool(
                        snapshot.battery_holding
                        and snapshot.solar_surplus_ready
                        and not snapshot.energy_deficit
                    )
                    if (
                        not snapshot.energy_deficit
                        and (
                            visible_surplus_sufficient
                            or holding_curtailment_sufficient
                        )
                    ):
                        should_start = True
                        starts_from_visible_surplus = (
                            visible_surplus_sufficient
                        )
                        reason = Reason.BALANCING_SURPLUS
                    else:
                        reason = Reason.BATTERY_BALANCING
                else:
                    visible_surplus_sufficient = bool(
                        remaining_visible_surplus_w is not None
                        and math.isfinite(remaining_visible_surplus_w)
                        and snapshot.soc >= (
                            self.config.battery.soc_min
                            + self.config.battery.load_start_hysteresis_soc
                        )
                        and remaining_visible_surplus_w
                        >= state.config.rated_w
                        + self.config.battery_charge_reserve_w
                    )
                    if visible_surplus_sufficient:
                        should_start = True
                        starts_from_visible_surplus = True
                        reason = Reason.SOLAR_CHARGE_SURPLUS
                    elif snapshot.soc >= self.config.battery.soc_on:
                        should_start = snapshot.solar_surplus_ready
                        reason = (
                            Reason.SOLAR_SURPLUS_CONFIRMED
                            if should_start else Reason.SOLAR_SURPLUS_PENDING
                        )
                    else:
                        reason = Reason.SOLAR_HYSTERESIS
            elif off_peak:
                if (
                    runtime_required
                    or state.config.completion_mode == "thermostat"
                ):
                    should_start = True
                    reason = (
                        Reason.OFF_PEAK_REQUIRED
                        if runtime_required
                        else Reason.THERMOSTAT_PENDING
                    )
                else:
                    reason = Reason.RUNTIME_COMPLETE

            if should_start:
                window_end = (
                    solar_end
                    if solar or reason in (
                        Reason.TARGET_FINISH,
                        Reason.TARGET_PROGRESS,
                    )
                    else _clock(self.config.schedule.daily_reset)
                )
                remaining_window_s = _seconds_until(snapshot.now, window_end)
                if reason == Reason.POST_BALANCE_SUPPORT:
                    remaining_window_s = max(
                        0.0, (snapshot.post_balance_until - snapshot.now).total_seconds(),
                    )
                if reason in (Reason.TARGET_FINISH, Reason.TARGET_PROGRESS):
                    if finish_grace:
                        remaining_window_s = max(
                            0.0,
                            state.config.target_finish_grace_s
                            - _seconds_since(snapshot.now, solar_end),
                        )
                    else:
                        remaining_window_s += state.config.target_finish_grace_s
                if (
                    state.config.min_on_time_s > 0
                    and remaining_window_s < state.config.min_on_time_s
                ):
                    decisions[state.config.name] = self._decision(
                        state, Action.KEEP_OFF, Reason.WINDOW_END_TOO_CLOSE
                    )
                    continue
                start_limit = (
                    self.config.max_ac_power_w - self.config.ac_start_margin_w
                )
                if start_scheduled or snapshot.start_settle_remaining_s > 0:
                    decisions[state.config.name] = self._decision(
                        state, Action.KEEP_OFF, Reason.START_STAGGERED
                    )
                elif (
                    projected_power + state.config.estimated_start_power_w
                    <= start_limit
                ):
                    decisions[state.config.name] = self._decision(
                        state, Action.START, reason
                    )
                    start_scheduled = True
                    projected_power += state.config.estimated_start_power_w
                    if reason == Reason.POST_BALANCE_SUPPORT:
                        battery_w -= state.config.rated_w
                    if starts_from_visible_surplus:
                        assert remaining_visible_surplus_w is not None
                        remaining_visible_surplus_w -= state.config.rated_w
                else:
                    decisions[state.config.name] = self._decision(
                        state, Action.KEEP_OFF, Reason.AC_HEADROOM_INSUFFICIENT
                    )
            else:
                decisions[state.config.name] = self._decision(
                    state, Action.KEEP_OFF, reason
                )

        return tuple(decisions[state.config.name] for state in states)
