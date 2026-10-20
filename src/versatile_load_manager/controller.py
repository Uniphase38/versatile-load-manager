"""H24 controller joining telemetry, policy, persistent runtime and commands."""

from dataclasses import dataclass, replace
from datetime import datetime, time as dt_time, timedelta, timezone
from collections import deque
import json
import math
import os
from pathlib import Path
import time
from typing import Callable, Dict, Optional, Tuple
from zoneinfo import ZoneInfo

from .balance_strategy import decide_hc_catchup
from .balance_loads import qualify_balance_surplus, post_balance_deadline
from .hc_balance import RequiredLoad, plan_hc_balance
from .tempo import TempoColour
from .battery import (
    BalanceStatus,
    BatteryDecision,
    BatteryPolicy,
    BatteryRuntime,
    format_dvcc_limit,
)
from .config import (
    AppConfig,
    runtime_period_month,
)
from .modbus import CerboModbus, ModbusError, SolarCurtailment
from .min_soc_control import MinSocOwnership, plan_min_soc_action
from .policy import Action, Decision, LoadPolicy, LoadState, Reason, Snapshot
from .shelly import ShellyClient, ShellyError
from .watchdog import CommunicationWatchdog, WatchdogState


@dataclass(frozen=True)
class CycleResult:
    snapshot: Snapshot
    decisions: Tuple[Decision, ...]
    commands: Tuple[str, ...]
    solar_curtailment: SolarCurtailment
    battery: BatteryDecision
    observed_dvcc_a: Optional[int]
    suspended_stops: Tuple[str, ...] = ()


@dataclass(frozen=True)
class FlowQualification:
    """Debounced interpretation of native curtailment and power flows."""

    surplus_ready: bool
    deficit_sustained: bool
    surplus_cycles: int
    deficit_cycles: int
    visible_surplus_w: Optional[float]


class SolarFlowTracker:
    """Reject isolated, asynchronous D-Bus samples before policy evaluation."""

    def __init__(self, config: AppConfig):
        self.config = config
        self._surplus_cycles = 0
        self._deficit_cycles = 0
        self._visible_surplus_samples = deque(
            maxlen=config.surplus_confirm_cycles
        )

    def reset(self) -> None:
        """Discard flow evidence invalidated by an electrical setpoint change."""
        self._surplus_cycles = 0
        self._deficit_cycles = 0
        self._visible_surplus_samples.clear()

    def update(self, telemetry: SolarCurtailment) -> FlowQualification:
        grid = telemetry.grid_power_w
        battery = telemetry.battery_power_w
        flows_known = (
            grid is not None
            and battery is not None
            and math.isfinite(grid)
            and math.isfinite(battery)
        )
        native_curtailment = (
            telemetry.dc_limited is True or telemetry.ac_limited is True
        )
        # Positive battery power is charging and negative grid power is
        # export. Combining them avoids declaring a deficit when a small grid
        # import coexists with much stronger battery charging.
        net_surplus_w = (battery - grid) if flows_known else None
        flow_tolerance_w = (
            self.config.grid_import_tolerance_w
            + self.config.battery_discharge_tolerance_w
        )
        surplus_now = bool(
            flows_known
            and native_curtailment
            and net_surplus_w >= -flow_tolerance_w
        )
        deficit_now = bool(
            flows_known
            and net_surplus_w < -flow_tolerance_w
        )

        if flows_known:
            self._visible_surplus_samples.append(net_surplus_w)
        else:
            self._visible_surplus_samples.clear()
        visible_surplus_w = (
            min(self._visible_surplus_samples)
            if len(self._visible_surplus_samples) == self.config.surplus_confirm_cycles
            else None
        )

        self._surplus_cycles = self._surplus_cycles + 1 if surplus_now else 0
        self._deficit_cycles = self._deficit_cycles + 1 if deficit_now else 0
        return FlowQualification(
            surplus_ready=(
                self._surplus_cycles >= self.config.surplus_confirm_cycles
            ),
            deficit_sustained=(
                self._deficit_cycles >= self.config.deficit_confirm_cycles
            ),
            surplus_cycles=self._surplus_cycles,
            deficit_cycles=self._deficit_cycles,
            visible_surplus_w=visible_surplus_w,
        )


class RuntimeStore:
    """Persist per-load run time, resetting at the configured daily boundary."""

    def __init__(self, path: str, reset_clock: str):
        self.path = Path(path)
        self.reset_clock = reset_clock

    def _period(self, now: datetime) -> str:
        hour, minute = (int(part) for part in self.reset_clock.split(":"))
        boundary = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
        if now < boundary:
            boundary -= timedelta(days=1)
        return boundary.isoformat()

    def load(self, now: datetime) -> Dict[str, float]:
        payload = self._read_payload()
        stored_period = payload.get("period")
        current_period = self._period(now)
        if not self._same_period(stored_period, current_period):
            return {}
        runtimes = payload.get("runtime_h", {})
        if not isinstance(runtimes, dict):
            return {}
        return {
            str(name): float(value)
            for name, value in runtimes.items()
            if isinstance(value, (int, float)) and math.isfinite(value) and value >= 0
        }

    def period(self, now: datetime) -> str:
        """Return the persistence period containing *now*."""
        return self._period(now)

    def load_completed(self, now: datetime) -> set:
        """Load daily thermostat completions for the current period only."""
        payload = self._read_payload()
        if not self._same_period(payload.get("period"), self._period(now)):
            return set()
        values = payload.get("completed_loads", [])
        if not isinstance(values, list):
            return set()
        return {
            value for value in values
            if isinstance(value, str) and value.strip()
        }

    @staticmethod
    def _same_period(stored: object, current: str) -> bool:
        """Accept one legacy timezone-less period and migrate it on save."""
        if stored == current:
            return True
        if not isinstance(stored, str):
            return False
        try:
            stored_at = datetime.fromisoformat(stored)
            current_at = datetime.fromisoformat(current)
        except ValueError:
            return False
        return stored_at.replace(tzinfo=None) == current_at.replace(tzinfo=None)

    def _read_payload(self) -> dict:
        try:
            payload = json.loads(self.path.read_text(encoding="utf-8"))
        except (FileNotFoundError, OSError, ValueError, TypeError):
            return {}
        return payload if isinstance(payload, dict) else {}

    def load_lockouts(self) -> set:
        """Load durable safety lockouts independently of the daily period."""
        values = self._read_payload().get("probe_lockouts", [])
        if not isinstance(values, list):
            return set()
        return {
            value for value in values
            if isinstance(value, str) and value.strip()
        }

    def load_battery_state(self) -> dict:
        """Load balancing history independently of the daily load period."""
        value = self._read_payload().get("battery", {})
        return value if isinstance(value, dict) else {}

    def load_watchdog_state(self) -> dict:
        """Load persistent communication incident evidence."""
        value = self._read_payload().get("watchdog", {})
        return value if isinstance(value, dict) else {}

    def save_watchdog_state(self, watchdog_state: dict) -> None:
        """Atomically persist watchdog evidence without disturbing runtime."""
        payload = self._read_payload()
        payload["watchdog"] = watchdog_state
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_name(self.path.name + ".tmp")
        temporary.write_text(
            json.dumps(payload, sort_keys=True) + "\n", encoding="utf-8"
        )
        os.replace(str(temporary), str(self.path))

    def load_owned_loads(self) -> set:
        """Load relays which VLM last commanded ON."""
        values = self._read_payload().get("owned_loads", [])
        if not isinstance(values, list):
            return set()
        return {
            value for value in values
            if isinstance(value, str) and value.strip()
        }

    def load_output_transitions(self) -> Dict[str, Tuple[bool, datetime]]:
        """Load the last observed ON/OFF transition for anti-cycling."""
        values = self._read_payload().get("output_transitions", {})
        if not isinstance(values, dict):
            return {}
        transitions = {}
        for name, item in values.items():
            if not isinstance(name, str) or not isinstance(item, dict):
                continue
            output = item.get("output")
            changed_at = item.get("changed_at")
            if not isinstance(output, bool) or not isinstance(changed_at, str):
                continue
            try:
                timestamp = datetime.fromisoformat(changed_at)
            except ValueError:
                continue
            transitions[name] = (output, timestamp)
        return transitions

    def save(
        self,
        now: datetime,
        runtimes: Dict[str, float],
        probe_lockouts=(),
        output_transitions=None,
        battery_state=None,
        completed_loads=None,
        owned_loads=None,
    ) -> None:
        previous_payload = self._read_payload()
        if output_transitions is None:
            transitions_payload = previous_payload.get(
                "output_transitions", {}
            )
            if not isinstance(transitions_payload, dict):
                transitions_payload = {}
        else:
            transitions_payload = {
                name: {
                    "output": output,
                    "changed_at": changed_at.isoformat(),
                }
                for name, (output, changed_at) in output_transitions.items()
            }
        if battery_state is None:
            battery_payload = previous_payload.get("battery", {})
            if not isinstance(battery_payload, dict):
                battery_payload = {}
        else:
            battery_payload = battery_state
        if completed_loads is None:
            completed_payload = previous_payload.get("completed_loads", [])
            if not isinstance(completed_payload, list):
                completed_payload = []
        else:
            completed_payload = sorted(set(completed_loads))
        if owned_loads is None:
            owned_payload = previous_payload.get("owned_loads", [])
            if not isinstance(owned_payload, list):
                owned_payload = []
        else:
            owned_payload = sorted(set(owned_loads))
        watchdog_payload = previous_payload.get("watchdog", {})
        if not isinstance(watchdog_payload, dict):
            watchdog_payload = {}
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_name(self.path.name + ".tmp")
        temporary.write_text(json.dumps({
            "period": self._period(now),
            "runtime_h": runtimes,
            "probe_lockouts": sorted(set(probe_lockouts)),
            "output_transitions": transitions_payload,
            "battery": battery_payload,
            "completed_loads": completed_payload,
            "owned_loads": owned_payload,
            "watchdog": watchdog_payload,
        }, sort_keys=True) + "\n", encoding="utf-8")
        os.replace(str(temporary), str(self.path))

    def clear_lockout(self, name: str) -> bool:
        """Remove one durable lockout while preserving runtime information."""
        payload = self._read_payload()
        values = payload.get("probe_lockouts", [])
        lockouts = {
            value for value in values
            if isinstance(value, str) and value.strip()
        } if isinstance(values, list) else set()
        removed = name in lockouts
        if not removed:
            return False
        lockouts.remove(name)
        payload["probe_lockouts"] = sorted(lockouts)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_name(self.path.name + ".tmp")
        temporary.write_text(
            json.dumps(payload, sort_keys=True) + "\n", encoding="utf-8"
        )
        os.replace(str(temporary), str(self.path))
        return True


class LoadController:
    """Run one safe policy cycle; only explicitly managed loads may be switched."""

    def __init__(
        self,
        config: AppConfig,
        cerbo_factory=CerboModbus,
        shelly_factory=ShellyClient,
        now: Optional[Callable[[], datetime]] = None,
        sleep: Callable[[float], None] = time.sleep,
    ):
        self.config = config
        self.policy = LoadPolicy(config)
        self.battery_policy = BatteryPolicy(config.battery, config.schedule)
        self.cerbo_factory = cerbo_factory
        self.shelly_factory = shelly_factory
        self.timezone = ZoneInfo(config.schedule.timezone)
        self.now = now or (lambda: datetime.now(timezone.utc))
        self.sleep = sleep
        self.store = RuntimeStore(config.state_file, config.schedule.daily_reset)
        self.flow_tracker = SolarFlowTracker(config)
        self._last_sample: Optional[datetime] = None
        self._probe_retry_after: Dict[str, datetime] = {}
        self._probe_lockouts = self.store.load_lockouts()
        self._output_transitions = self.store.load_output_transitions()
        self._thermostat_satisfied = set()
        self._battery_runtime: Optional[BatteryRuntime] = None
        self._last_confirmed_start: Optional[datetime] = None
        self._last_dvcc_transition: Optional[datetime] = None
        self._last_observed_dvcc: Optional[int] = None
        self._active_period: Optional[str] = None
        self._daily_completed = set()
        self._owned_on = self.store.load_owned_loads()
        self._reported_external = set()
        self._balance_charge_reference_w = None
        self.watchdog = CommunicationWatchdog(
            safe_after_errors=config.watchdog.safe_after_errors,
            recovery_cycles=config.watchdog.recovery_cycles,
            restart_after_errors=config.watchdog.restart_after_errors,
        )

    def _now(self) -> datetime:
        """Return schedule-local time while keeping naive injected test clocks."""
        value = self.now()
        if value.tzinfo is None:
            return value
        return value.astimezone(self.timezone)

    @staticmethod
    def _elapsed_seconds(later: datetime, earlier: datetime) -> Optional[float]:
        """Measure real elapsed time safely across DST and legacy timestamps."""
        if (later.tzinfo is None) != (earlier.tzinfo is None):
            return None
        if later.tzinfo is not None:
            later = later.astimezone(timezone.utc)
            earlier = earlier.astimezone(timezone.utc)
        return max(0.0, (later - earlier).total_seconds())

    @staticmethod
    def _active_power_threshold(load) -> float:
        return (
            load.min_active_power_w
            if load.min_active_power_w is not None
            else load.rated_w * 0.5
        )

    def _power_state(self, load, power_w: Optional[float]) -> str:
        if power_w is None or not math.isfinite(power_w):
            return "unavailable"
        if power_w >= self._active_power_threshold(load):
            return "active"
        return "inactive"

    @staticmethod
    def _validate_critical_telemetry(
        soc: float,
        ac_power_w: float,
        grid_lost: bool,
        dvcc_a: int,
    ) -> None:
        """Reject protocol-success responses which are unsafe for control."""
        if (
            isinstance(soc, bool)
            or not isinstance(soc, (int, float))
            or not math.isfinite(soc)
            or not 0 <= soc <= 100
        ):
            raise ModbusError("invalid critical SOC telemetry: %r" % (soc,))
        if (
            isinstance(ac_power_w, bool)
            or not isinstance(ac_power_w, (int, float))
            or not math.isfinite(ac_power_w)
        ):
            raise ModbusError(
                "invalid critical AC power telemetry: %r" % (ac_power_w,)
            )
        if not isinstance(grid_lost, bool):
            raise ModbusError(
                "invalid critical grid state telemetry: %r" % (grid_lost,)
            )
        if isinstance(dvcc_a, bool) or not isinstance(dvcc_a, int):
            raise ModbusError(
                "invalid critical DVCC telemetry: %r" % (dvcc_a,)
            )

    def _save_state(self, now: datetime, runtimes: Dict[str, float]) -> None:
        self.store.save(
            now,
            runtimes,
            self._probe_lockouts,
            self._output_transitions,
            None if self._battery_runtime is None else self._battery_runtime.to_payload(),
            self._daily_completed,
            self._owned_on,
        )

    def _apply_min_soc_target(
        self,
        cerbo: CerboModbus,
        now: datetime,
        runtimes: Dict[str, float],
        desired_soc: Optional[float],
    ) -> Tuple[str, ...]:
        """Apply a temporary Min_SOC override without losing its original value."""
        if self._battery_runtime is None:
            return ()
        ownership = MinSocOwnership(
            self._battery_runtime.min_soc_owned,
            self._battery_runtime.pre_hc_min_soc,
        )
        if desired_soc is None and not ownership.owned:
            return ()
        if not self.config.battery.control_enabled:
            return ()
        if self.config.dry_run:
            return (
                "BATTERY DRY-RUN Min_SOC %s"
                % ("restore" if desired_soc is None else "%.1f%%" % desired_soc),
            )

        observed = cerbo.read_min_soc()
        messages = []
        for _step in range(3):
            action = plan_min_soc_action(
                desired_soc=desired_soc,
                observed_soc=observed,
                ownership=ownership,
            )
            if action.action == "capture":
                ownership = action.ownership
                self._battery_runtime = replace(
                    self._battery_runtime,
                    min_soc_owned=True,
                    pre_hc_min_soc=ownership.original_soc,
                )
                # Transaction boundary: the original value must be durable
                # before the first write which can raise Min_SOC.
                self._save_state(now, runtimes)
                messages.append(
                    "BATTERY Min_SOC ownership captured %.1f%%"
                    % ownership.original_soc
                )
                continue
            if action.action in ("set", "restore"):
                try:
                    cerbo.write_min_soc(action.value)
                    observed = cerbo.read_min_soc()
                except (OSError, ValueError) as exc:
                    messages.append(
                        "CRITICAL battery Min_SOC %s unconfirmed: %s"
                        % (action.action, exc)
                    )
                    return tuple(messages)
                if abs(observed - action.value) > 0.05:
                    messages.append(
                        "CRITICAL battery Min_SOC %s unconfirmed: wanted %.1f%%, observed %.1f%%"
                        % (action.action, action.value, observed)
                    )
                    return tuple(messages)
                messages.append(
                    "BATTERY COMMAND Min_SOC %.1f%% (%s)"
                    % (action.value, action.action)
                )
                continue
            if action.action == "release":
                ownership = action.ownership
                self._battery_runtime = replace(
                    self._battery_runtime,
                    min_soc_owned=False,
                    pre_hc_min_soc=None,
                )
                self._save_state(now, runtimes)
                messages.append("BATTERY Min_SOC ownership released")
                return tuple(messages)
            if action.action == "fault":
                messages.append(
                    "CRITICAL battery Min_SOC ownership has no original value"
                )
                return tuple(messages)
            return tuple(messages)
        return tuple(messages)

    def _start_settle_remaining(self, now: datetime) -> float:
        remaining = 0.0
        for transition in (
            self._last_confirmed_start,
            self._last_dvcc_transition,
        ):
            if transition is None:
                continue
            elapsed = self._elapsed_seconds(now, transition)
            if elapsed is None:
                candidate = float(self.config.start_settle_s)
            else:
                candidate = max(0.0, self.config.start_settle_s - elapsed)
            remaining = max(remaining, candidate)
        return remaining

    def _output_duration(
        self, load, output: Optional[bool], now: datetime
    ) -> float:
        """Track observed transitions and return seconds in the current state."""
        if output is None:
            return 0.0
        previous = self._output_transitions.get(load.name)
        if previous is None:
            # A newly managed load already observed OFF was not switched by us;
            # do not invent a five-minute wait on first installation. An
            # already-ON motor is treated conservatively as freshly started.
            changed_at = (
                now - timedelta(seconds=load.min_off_time_s)
                if output is False
                else now
            )
            self._output_transitions[load.name] = (output, changed_at)
            return max(0.0, (now - changed_at).total_seconds())
        if previous[0] != output:
            self._output_transitions[load.name] = (output, now)
            return 0.0
        elapsed = self._elapsed_seconds(now, previous[1])
        if elapsed is None:
            # Pre-timezone state files contain ambiguous naive Cerbo time.
            # Restart both ON and OFF protections from zero rather than
            # accidentally bypassing a motor safety delay.
            self._output_transitions[load.name] = (output, now)
            return 0.0
        return elapsed

    def _record_output(self, name: str, output: bool, now: datetime) -> None:
        previous = self._output_transitions.get(name)
        if previous is None or previous[0] != output:
            self._output_transitions[name] = (output, now)

    def _confirm_active_power(
        self, client: ShellyClient, load
    ) -> Tuple[bool, Optional[float]]:
        """Wait briefly for a commanded load to draw its expected power."""
        threshold = self._active_power_threshold(load)
        poll_s = 0.5
        attempts = max(1, int(math.ceil(load.power_confirm_timeout_s / poll_s)))
        last_power = None
        for attempt in range(attempts):
            self.sleep(min(poll_s, load.power_confirm_timeout_s))
            try:
                status = client.read_status()
            except (ShellyError, OSError):
                continue
            last_power = status.power_w
            if (
                status.component == "switch"
                and status.output is True
                and last_power is not None
                and math.isfinite(last_power)
                and last_power >= threshold
            ):
                return True, last_power
        return False, last_power

    @staticmethod
    def _power_text(power_w: Optional[float]) -> str:
        return "unavailable" if power_w is None else "%.1f W" % power_w

    def _hc_end(self, now: datetime) -> datetime:
        hour, minute = (int(part) for part in self.config.schedule.daily_reset.split(":"))
        end = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
        if end <= now:
            end += timedelta(days=1)
        return end

    def _hc_catchup_authorized(
        self,
        *,
        now: datetime,
        soc: float,
        due_at: datetime,
        states: Tuple[LoadState, ...],
        tomorrow_pv_kwh: Optional[float],
        tomorrow_tempo: TempoColour,
    ) -> Tuple[bool, str]:
        cfg = self.config.battery
        if not cfg.balance_hc_enabled:
            return False, "hc_balance_disabled"
        current = now.time()
        start_h, start_m = (int(part) for part in self.config.schedule.off_peak_start.split(":"))
        end_h, end_m = (int(part) for part in self.config.schedule.daily_reset.split(":"))
        start, end = dt_time(start_h, start_m), dt_time(end_h, end_m)
        in_hc = current >= start or current < end if start > end else start <= current < end
        if not in_hc:
            return False, "outside_off_peak"
        if (
            self._battery_runtime is not None
            and self._battery_runtime.campaign_active
            and self._battery_runtime.min_soc_owned
        ):
            # Once grid catch-up has actually taken ownership of Min_SOC, do
            # not abandon the top-balance because a projection drifts late.
            return True, "hc_catchup_committed"

        advice = decide_hc_catchup(
            now=now,
            due_at=due_at,
            solar_defer_days=cfg.balance_solar_defer_days,
            tomorrow_pv_kwh=tomorrow_pv_kwh,
            good_solar_day_kwh=cfg.balance_good_solar_day_kwh,
            tomorrow_tempo=tomorrow_tempo,
        )
        if not advice.requested:
            return False, advice.reason

        required = []
        month = runtime_period_month(now, self.config.schedule.daily_reset)
        for state in states:
            remaining_h = max(
                0.0,
                state.config.required_runtime_h_for_month(month) - state.runtime_h,
            )
            if remaining_h <= 0:
                continue
            if not state.reachable:
                return False, "required_load_unreachable:%s" % state.config.name
            required.append(RequiredLoad(
                state.config.name, state.config.rated_w, remaining_h
            ))
        plan = plan_hc_balance(
            now=now,
            hc_end=self._hc_end(now),
            soc=soc,
            target_soc=cfg.balance_target_soc,
            battery_capacity_kwh=cfg.usable_capacity_kwh,
            max_grid_charge_power_w=cfg.max_grid_charge_power_w,
            max_ac_power_w=self.config.max_ac_power_w,
            night_base_load_w=cfg.night_base_load_w,
            required_loads=required,
            hold_s=cfg.balance_hold_s,
            safety_margin_s=cfg.balance_hc_safety_margin_s,
        )
        return plan.feasible, (
            "hc_catchup_authorized" if plan.feasible else plan.reason
        )

    def cycle(
        self,
        *,
        tomorrow_pv_kwh: Optional[float] = None,
        tomorrow_tempo: TempoColour = TempoColour.UNKNOWN,
        today_pv_kwh: Optional[float] = None,
    ) -> CycleResult:
        now = self._now()
        control_allowed_at_start = self.watchdog.control_allowed
        current_period = self.store.period(now)
        reset_events = []
        daily_reset = bool(
            self._active_period is not None
            and current_period != self._active_period
        )
        if daily_reset:
            # Thermostat/probe conclusions are valid only for the daily period
            # in which they were observed.  Without this reset, the preceding
            # day's satisfied thermostat remains hidden outside the solar
            # window, then wrongly blocks the load as soon as the window opens.
            self._thermostat_satisfied.clear()
            self._probe_retry_after.clear()
            reset_events.append(
                "DAILY RESET runtimes, completions and thermostat state cleared"
            )
        self._active_period = current_period
        runtimes = self.store.load(now)
        self._daily_completed = self.store.load_completed(now)
        states = []
        clients = {}
        monitor_events = []
        managed = set(self.config.managed_loads)
        for load in self.config.loads:
            if load.name not in managed:
                continue
            client = self.shelly_factory(load.host, switch_id=load.switch_id)
            clients[load.name] = client
            try:
                status = client.read_status()
                reachable = status.component == "switch" and status.output is not None
                output = status.output if reachable else None
                power_w = status.power_w if reachable else None
            except (ShellyError, OSError):
                reachable, output, power_w = False, None, None
            power_state = None
            if output is True:
                power_state = self._power_state(load, power_w)
                if power_state != "active":
                    confirmed, confirmed_power_w = self._confirm_active_power(
                        client, load
                    )
                    power_w = confirmed_power_w
                    power_state = (
                        "active" if confirmed
                        else self._power_state(load, confirmed_power_w)
                    )
                    if not confirmed:
                        if load.probe_failure_mode == "lockout":
                            self._probe_lockouts.add(load.name)
                            self._thermostat_satisfied.discard(load.name)
                            self._save_state(now, runtimes)
                            outcome = "FAILED"
                        else:
                            if power_state == "inactive":
                                self._thermostat_satisfied.add(load.name)
                                if load.completion_mode == "thermostat":
                                    self._daily_completed.add(load.name)
                                    self._probe_retry_after.pop(load.name, None)
                                else:
                                    self._probe_retry_after[load.name] = (
                                        now + timedelta(
                                            seconds=load.probe_retry_delay_s
                                        )
                                    )
                                outcome = "SATISFIED"
                            else:
                                self._thermostat_satisfied.discard(load.name)
                                self._probe_retry_after[load.name] = (
                                    now + timedelta(seconds=load.probe_retry_delay_s)
                                )
                                outcome = "FAILED"
                        monitor_events.append(
                            "MONITOR %s %s power=%s" % (
                                load.name, outcome, self._power_text(power_w)
                            )
                        )
            retry_after = self._probe_retry_after.get(load.name)
            if retry_after is not None and now >= retry_after:
                self._probe_retry_after.pop(load.name, None)
                self._thermostat_satisfied.discard(load.name)
                retry_after = None
            probe_blocked = bool(
                output is False and retry_after is not None and now < retry_after
            )
            probe_locked_out = load.name in self._probe_lockouts
            thermostat_satisfied = bool(
                load.name in self._thermostat_satisfied
            )
            output_duration_s = self._output_duration(load, output, now)
            if output is False:
                self._owned_on.discard(load.name)
                self._reported_external.discard(load.name)
            elif output is True and daily_reset:
                self._owned_on.add(load.name)
                self._reported_external.discard(load.name)
                monitor_events.append(
                    "OWNERSHIP %s adopted at daily reset" % load.name
                )
            elif output is True and load.name not in self._owned_on:
                if load.name not in self._reported_external:
                    monitor_events.append(
                        "EXTERNAL %s ON observed; left unchanged until daily reset"
                        % load.name
                    )
                    self._reported_external.add(load.name)
            states.append(LoadState(
                load,
                output,
                runtimes.get(load.name, 0.0),
                reachable,
                probe_blocked,
                probe_locked_out,
                power_state,
                thermostat_satisfied,
                output_duration_s,
                load.name in self._daily_completed,
                output is True and load.name not in self._owned_on,
            ))

        if self._last_sample is not None:
            elapsed_s = self._elapsed_seconds(now, self._last_sample)
            elapsed_h = max(0.0, min(
                0.0 if elapsed_s is None else elapsed_s,
                self.config.loop_interval_s * 2,
            )) / 3600.0
            for state in states:
                if state.output is True and state.power_state == "active":
                    runtimes[state.config.name] = runtimes.get(state.config.name, 0.0) + elapsed_h
        self._last_sample = now

        cerbo = self.cerbo_factory(self.config.cerbo)
        observed_dvcc_a = None
        try:
            cerbo.connect()
            cerbo.resolve_services()
            solar_curtailment = cerbo.read_solar_curtailment()
            flow = self.flow_tracker.update(solar_curtailment)
            soc = cerbo.read_soc()
            grid_lost = cerbo.read_grid_lost()
            observed_dvcc_a = cerbo.read_dvcc_max_charge()
            ac_power_w = cerbo.read_ac_power()
            self._validate_critical_telemetry(
                soc, ac_power_w, grid_lost, observed_dvcc_a
            )
            if self._battery_runtime is None:
                self._battery_runtime = self.battery_policy.initial_runtime(
                    now, self.store.load_battery_state()
                )
            preview = self.battery_policy.evaluate(
                now,
                soc,
                flow.surplus_ready,
                flow.deficit_sustained,
                self._battery_runtime,
                solar_opportunity=bool(
                    not grid_lost
                    and today_pv_kwh is not None
                    and math.isfinite(today_pv_kwh)
                    and today_pv_kwh >= self.config.battery.balance_good_solar_day_kwh
                    and (
                        flow.surplus_ready
                        or (
                            flow.visible_surplus_w is not None
                            and flow.visible_surplus_w >= max(
                                1.0,
                                self.config.grid_import_tolerance_w
                                + self.config.battery_discharge_tolerance_w,
                            )
                        )
                    )
                ),
            )
            hc_authorized, hc_reason = (
                (False, "grid_lost")
                if grid_lost
                else self._hc_catchup_authorized(
                now=now,
                soc=soc,
                due_at=preview.due_at,
                states=tuple(states),
                tomorrow_pv_kwh=tomorrow_pv_kwh,
                tomorrow_tempo=tomorrow_tempo,
                )
            )
            battery = (
                self.battery_policy.evaluate(
                    now,
                    soc,
                    flow.surplus_ready,
                    flow.deficit_sustained,
                    self._battery_runtime,
                    hc_catchup_authorized=True,
                )
                if hc_authorized
                else preview
            )
            if self.config.battery.balance_hc_enabled:
                monitor_events.append("BALANCE HC planner: %s" % hc_reason)
            self._battery_runtime = battery.runtime
            allowance = qualify_balance_surplus(
                enabled=self.config.battery.balance_surplus_enabled,
                balancing=(
                    battery.using_balance_target and battery.min_soc_target is None
                ),
                curtailment_confirmed=flow.surplus_ready,
                deficit=flow.deficit_sustained,
                settled=self._start_settle_remaining(now) <= 0,
                battery_power_w=solar_curtailment.battery_power_w,
                grid_power_w=solar_curtailment.grid_power_w,
                reference_charge_w=self._balance_charge_reference_w,
                charge_tolerance_w=self.config.battery.balance_surplus_charge_tolerance_w,
                grid_tolerance_w=self.config.grid_import_tolerance_w,
            )
            self._balance_charge_reference_w = allowance.reference_charge_w
            writes_dvcc = bool(
                self.config.battery.control_enabled
                and not self.config.dry_run
                and observed_dvcc_a != battery.charge_current_a
            )
            dvcc_changed_externally = bool(
                self._last_observed_dvcc is not None
                and observed_dvcc_a != self._last_observed_dvcc
            )
            dvcc_transition = writes_dvcc or dvcc_changed_externally
            if dvcc_transition:
                self.flow_tracker.reset()
                self._last_dvcc_transition = now
                monitor_events.append(
                    "FLOW RESET after DVCC transition; starts settling for %s s"
                    % self.config.start_settle_s
                )
            self._last_observed_dvcc = (
                battery.charge_current_a if writes_dvcc else observed_dvcc_a
            )
            snapshot = Snapshot(
                now=now,
                soc=soc,
                ac_power_w=ac_power_w,
                grid_lost=grid_lost,
                loads=tuple(
                    LoadState(
                        s.config,
                        s.output,
                        runtimes.get(s.config.name, 0.0),
                        s.reachable,
                        s.probe_blocked,
                        s.probe_locked_out,
                        s.power_state,
                        s.thermostat_satisfied,
                        s.output_duration_s,
                        s.config.name in self._daily_completed,
                        s.external_control,
                    )
                    for s in states
                ),
                solar_surplus_ready=(
                    flow.surplus_ready and not dvcc_transition
                ),
                energy_deficit=flow.deficit_sustained,
                visible_surplus_w=(
                    None if dvcc_transition else flow.visible_surplus_w
                ),
                battery_priority=(
                    self.config.battery.control_enabled
                    and (battery.using_balance_target or battery.solar_priority)
                ),
                battery_priority_exclusive=(
                    self.config.battery.control_enabled
                    and (
                        battery.solar_priority
                        or (
                            self.config.battery.balance_surplus_enabled
                            and battery.using_balance_target
                            and battery.min_soc_target is None
                        )
                    )
                ),
                battery_holding=(
                    self.config.battery.control_enabled
                    and battery.status == BalanceStatus.HOLDING
                ),
                start_settle_remaining_s=self._start_settle_remaining(now),
                battery_power_w=solar_curtailment.battery_power_w,
                grid_power_w=solar_curtailment.grid_power_w,
                balance_surplus_start_allowed=allowance.start_allowed,
                balance_surplus_keep_allowed=allowance.keep_allowed,
                balance_charge_reference_w=allowance.reference_charge_w,
                post_balance_until=(
                    post_balance_deadline(
                        now, battery.runtime.last_success_at,
                        self.config.battery, self.config.schedule,
                    ) if self.config.battery.control_enabled else None
                ),
            )
            if writes_dvcc:
                cerbo.write_dvcc_max_charge(battery.charge_current_a)
            monitor_events.extend(
                self._apply_min_soc_target(
                    cerbo, now, runtimes, battery.min_soc_target
                )
            )
        finally:
            cerbo.close()

        decisions = self.policy.evaluate(snapshot)
        commands = list(reset_events) + list(monitor_events)
        suspended_stops = []
        if observed_dvcc_a != battery.charge_current_a:
            if self.config.battery.control_enabled and not self.config.dry_run:
                commands.append(
                    "BATTERY COMMAND DVCC %s"
                    % format_dvcc_limit(battery.charge_current_a)
                )
        for decision in decisions:
            if decision.load_name not in managed:
                continue
            requested = {
                Action.START: True,
                Action.STOP: False,
            }.get(decision.action)
            if requested is None:
                continue
            if requested is True and not control_allowed_at_start:
                commands.append(
                    "WATCHDOG %s blocks START %s"
                    % (self.watchdog.state.value, decision.load_name)
                )
                continue
            dry_run = self.config.dry_run or not self.config.control_enabled
            forced_stop = bool(
                requested is False
                and decision.reason in {
                    Reason.GRID_LOST,
                    Reason.TELEMETRY_INVALID,
                    Reason.AC_OVERLOAD,
                    Reason.ACTIVE_POWER_FAULT,
                    Reason.POWER_TELEMETRY_UNAVAILABLE,
                    Reason.PROBE_LOCKOUT,
                }
            )
            if (
                requested is False
                and not dry_run
                and decision.load_name not in self._owned_on
                and not forced_stop
            ):
                suspended_stops.append(decision.load_name)
                continue
            result = clients[decision.load_name].set_output(requested, dry_run=dry_run)
            mode = "DRY-RUN" if dry_run else "COMMAND"
            commands.append("%s %s %s" % (
                mode, decision.load_name, "ON" if requested else "OFF"
            ))
            if not dry_run:
                self._record_output(decision.load_name, requested, now)
                if requested:
                    self._owned_on.add(decision.load_name)
                else:
                    self._owned_on.discard(decision.load_name)
                self._save_state(now, runtimes)
            if requested and not dry_run:
                load = next(
                    state.config for state in states
                    if state.config.name == decision.load_name
                )
                confirmed, power_w = self._confirm_active_power(
                    clients[decision.load_name], load
                )
                if confirmed:
                    self._probe_retry_after.pop(decision.load_name, None)
                    self._probe_lockouts.discard(decision.load_name)
                    self._thermostat_satisfied.discard(decision.load_name)
                    self._last_confirmed_start = now
                    commands.append("PROBE %s CONFIRMED power=%s" % (
                        decision.load_name, self._power_text(power_w)
                    ))
                else:
                    if load.probe_failure_mode == "lockout":
                        self._probe_lockouts.add(decision.load_name)
                        self._thermostat_satisfied.discard(decision.load_name)
                        self._save_state(now, runtimes)
                        outcome = "FAILED"
                    else:
                        if self._power_state(load, power_w) == "inactive":
                            self._thermostat_satisfied.add(decision.load_name)
                            if load.completion_mode == "thermostat":
                                self._daily_completed.add(decision.load_name)
                                self._probe_retry_after.pop(
                                    decision.load_name, None
                                )
                            else:
                                self._probe_retry_after[decision.load_name] = (
                                    now + timedelta(
                                        seconds=load.probe_retry_delay_s
                                    )
                                )
                            outcome = "SATISFIED"
                        else:
                            self._thermostat_satisfied.discard(decision.load_name)
                            self._probe_retry_after[decision.load_name] = (
                                now + timedelta(seconds=load.probe_retry_delay_s)
                            )
                            outcome = "FAILED"
                    commands.append("PROBE %s %s power=%s" % (
                        decision.load_name, outcome, self._power_text(power_w)
                    ))
                    clients[decision.load_name].set_output(False, dry_run=False)
                    self._record_output(decision.load_name, False, now)
                    self._owned_on.discard(decision.load_name)
                    commands.append("COMMAND %s OFF" % decision.load_name)

        self._save_state(now, runtimes)
        return CycleResult(
            snapshot,
            decisions,
            tuple(commands),
            solar_curtailment,
            battery,
            observed_dvcc_a,
            tuple(suspended_stops),
        )

    def watchdog_failure(self, error: Exception) -> Tuple[str, ...]:
        """Record a critical Cerbo failure and apply graduated safety."""
        now = self._now()
        transition = self.watchdog.failure(now, error)
        payload = self.store.load_watchdog_state()
        started_at = payload.get("started_at")
        if not started_at or transition.previous == WatchdogState.OK:
            started_at = now.isoformat()
        evidence = {
            "active": True,
            "started_at": started_at,
            "last_error_at": now.isoformat(),
            "last_success_at": payload.get("last_success_at"),
            "consecutive_errors": transition.consecutive_errors,
            "highest_state": (
                WatchdogState.SAFE.value
                if transition.current == WatchdogState.SAFE
                or payload.get("highest_state") == WatchdogState.SAFE.value
                else transition.current.value
            ),
            "last_error": str(error),
            "restart_count": int(payload.get("restart_count", 0)),
            "recovery_action": (
                "vlm_restart" if transition.restart_due else None
            ),
            "history": payload.get("history", []),
        }
        self.store.save_watchdog_state(evidence)
        messages = [
            "WATCHDOG %s errors=%d/%d age=%.0fs: %s"
            % (
                transition.current.value,
                transition.consecutive_errors,
                self.config.watchdog.restart_after_errors,
                transition.failure_age_s,
                error,
            )
        ]
        if transition.current == WatchdogState.SAFE:
            messages.extend(self.stop_owned_loads(reason="WATCHDOG SAFE"))
            runtimes = self.store.load(now)
            messages.extend(
                self._restore_battery_controls(
                    now, runtimes, reason="WATCHDOG SAFE"
                )
            )
            self._save_state(now, runtimes)
        if transition.restart_due:
            messages.append("WATCHDOG restart requested")
        return tuple(messages)

    def watchdog_success(self) -> Tuple[str, ...]:
        """Confirm one complete critical telemetry cycle."""
        now = self._now()
        transition = self.watchdog.success(now)
        payload = self.store.load_watchdog_state()
        was_active = bool(payload.get("active"))
        payload.update({
            "last_success_at": now.isoformat(),
            "consecutive_errors": 0,
            "state": transition.current.value,
        })
        if transition.current == WatchdogState.OK:
            if was_active:
                started_at = payload.get("started_at")
                try:
                    started = (
                        datetime.fromisoformat(started_at)
                        if started_at else None
                    )
                except (TypeError, ValueError):
                    started = None
                payload["failure_duration_s"] = (
                    None if started is None
                    else max(0.0, self._elapsed_seconds(now, started) or 0.0)
                )
                incident = {
                    key: value for key, value in payload.items()
                    if key != "history"
                }
                history = payload.get("history", [])
                if not isinstance(history, list):
                    history = []
                history.append(incident)
                payload["history"] = history[-10:]
            payload["active"] = False
            payload["recovery_action"] = None
        self.store.save_watchdog_state(payload)
        if transition.current == WatchdogState.RECOVERING:
            return (
                "WATCHDOG RECOVERING healthy=%d/%d"
                % (transition.healthy_cycles, self.config.watchdog.recovery_cycles),
            )
        if transition.previous != transition.current:
            return ("WATCHDOG %s" % transition.current.value,)
        return ()

    def watchdog_restart_requested(self) -> None:
        """Persist restart evidence before returning control to runit."""
        payload = self.store.load_watchdog_state()
        payload["restart_count"] = int(payload.get("restart_count", 0)) + 1
        payload["recovery_action"] = "vlm_restart"
        started_at = payload.get("started_at")
        try:
            started = datetime.fromisoformat(started_at) if started_at else None
        except (TypeError, ValueError):
            started = None
        payload["failure_duration_s"] = (
            None if started is None
            else max(0.0, self._elapsed_seconds(self._now(), started) or 0.0)
        )
        self.store.save_watchdog_state(payload)

    def stop_owned_loads(self, reason: str = "SAFE") -> Tuple[str, ...]:
        """Stop relays VLM owns while preserving external manual control."""
        messages = []
        dry_run = self.config.dry_run or not self.config.control_enabled
        now = self._now()
        runtimes = self.store.load(now)
        managed = set(self.config.managed_loads)
        for load in self.config.loads:
            if load.name not in managed or load.name not in self._owned_on:
                continue
            try:
                self.shelly_factory(
                    load.host, switch_id=load.switch_id
                ).set_output(False, dry_run=dry_run)
                if not dry_run:
                    self._record_output(load.name, False, now)
                    self._owned_on.discard(load.name)
                messages.append("%s %s %s OFF" % (
                    reason,
                    "DRY-RUN" if dry_run else "COMMAND",
                    load.name,
                ))
            except (ShellyError, OSError) as exc:
                messages.append(
                    "CRITICAL %s %s OFF unconfirmed: %s"
                    % (reason, load.name, exc)
                )
        self._save_state(now, runtimes)
        return tuple(messages)

    def _restore_battery_controls(
        self,
        now: datetime,
        runtimes: Dict[str, float],
        *,
        reason: str,
    ) -> Tuple[str, ...]:
        """Best-effort restoration of VLM-owned battery setpoints."""
        if not self.config.battery.control_enabled:
            return ()
        prefix = (
            "BATTERY SHUTDOWN" if reason == "SHUTDOWN"
            else "%s BATTERY" % reason
        )
        if self.config.dry_run:
            return (
                "%s DRY-RUN restore Min_SOC and DVCC %sA"
                % (prefix, self.config.battery.max_charge_current_a),
            )
        messages = []
        cerbo = self.cerbo_factory(self.config.cerbo)
        try:
            cerbo.connect()
            cerbo.resolve_services()
            if self._battery_runtime is None:
                self._battery_runtime = self.battery_policy.initial_runtime(
                    now, self.store.load_battery_state()
                )
            messages.extend(
                self._apply_min_soc_target(cerbo, now, runtimes, None)
            )
            cerbo.write_dvcc_max_charge(
                self.config.battery.max_charge_current_a
            )
            messages.append(
                "%s DVCC %sA"
                % (prefix, self.config.battery.max_charge_current_a)
            )
        except (ModbusError, OSError, ValueError) as exc:
            messages.append(
                "CRITICAL %s battery restore unconfirmed: %s"
                % (reason, exc)
            )
        finally:
            cerbo.close()
        return tuple(messages)

    def stop_managed(self, restore_battery: bool = True) -> Tuple[str, ...]:
        """Request OFF only for managed relays still owned ON by this controller."""
        messages = []
        dry_run = self.config.dry_run or not self.config.control_enabled
        now = self._now()
        runtimes = self.store.load(now)
        managed = set(self.config.managed_loads)
        for load in self.config.loads:
            if load.name not in managed or load.name not in self._owned_on:
                continue
            try:
                self.shelly_factory(
                    load.host, switch_id=load.switch_id
                ).set_output(False, dry_run=dry_run)
                if not dry_run:
                    self._record_output(load.name, False, now)
                    self._owned_on.discard(load.name)
                messages.append("%s %s OFF" % (
                    "DRY-RUN" if dry_run else "SHUTDOWN", load.name
                ))
            except (ShellyError, OSError) as exc:
                messages.append("CRITICAL %s OFF unconfirmed: %s" % (load.name, exc))
        if restore_battery:
            messages.extend(
                self._restore_battery_controls(
                    now, runtimes, reason="SHUTDOWN"
                )
            )
        self._save_state(now, runtimes)
        return tuple(messages)
