"""Battery charge ceiling and solar-only balancing policy."""

from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone
from enum import Enum
import math
from typing import Optional, Tuple

from .config import BatteryConfig, ScheduleConfig


DVCC_UNLIMITED = -1
DVCC_CHARGE_DISABLED = 0


def format_dvcc_limit(value: int) -> str:
    """Render Victron's -1 sentinel without pretending it is an amp value."""
    return "unlimited" if value == DVCC_UNLIMITED else "%sA" % value


class BalanceStatus(str, Enum):
    """Human-readable state of the balancing campaign."""

    INITIALIZED = "initialized"
    NOT_DUE = "not_due"
    WAITING_SURPLUS = "waiting_surplus"
    PAUSED_OUTSIDE_SOLAR = "paused_outside_solar"
    PAUSED_DEFICIT = "paused_energy_deficit"
    HC_FORCED = "hc_forced"
    CHARGING = "charging"
    HOLDING = "holding_target"
    COMPLETED = "completed"
    PAUSED_SOLAR_DEADLINE = "paused_solar_deadline"


@dataclass(frozen=True)
class BatteryRuntime:
    """Durable state; load daily resets must never erase it."""

    last_completed_at: datetime
    campaign_active: bool = False
    target_reached_at: Optional[datetime] = None
    solar_authorized_date: Optional[str] = None
    charge_enabled: Optional[bool] = None
    min_soc_owned: bool = False
    pre_hc_min_soc: Optional[float] = None
    solar_priority_date: Optional[str] = None
    last_success_at: Optional[datetime] = None

    def to_payload(self) -> dict:
        return {
            "last_completed_at": self.last_completed_at.isoformat(),
            "campaign_active": self.campaign_active,
            "target_reached_at": (
                None
                if self.target_reached_at is None
                else self.target_reached_at.isoformat()
            ),
            "solar_authorized_date": self.solar_authorized_date,
            "charge_enabled": self.charge_enabled,
            "min_soc_owned": self.min_soc_owned,
            "pre_hc_min_soc": self.pre_hc_min_soc,
            "solar_priority_date": self.solar_priority_date,
            "last_success_at": (
                self.last_success_at.isoformat() if self.last_success_at else None
            ),
        }


@dataclass(frozen=True)
class BatteryDecision:
    status: BalanceStatus
    runtime: BatteryRuntime
    due_at: datetime
    target_soc: float
    charge_current_a: int
    hold_elapsed_s: Optional[float] = None
    hold_remaining_s: Optional[float] = None
    min_soc_target: Optional[float] = None
    solar_priority: bool = False

    @property
    def campaign_active(self) -> bool:
        return self.runtime.campaign_active

    @property
    def using_balance_target(self) -> bool:
        return self.status in (
            BalanceStatus.CHARGING,
            BalanceStatus.HOLDING,
            BalanceStatus.HC_FORCED,
        )


def compute_charge_current(
    soc: float,
    target_soc: float,
    maximum_a: int,
    hysteresis_soc: float,
    was_enabled: Optional[bool],
) -> Tuple[int, bool]:
    """Toggle Victron's user charge limit with persistent SOC hysteresis."""
    if not math.isfinite(soc):
        raise ValueError("SOC must be finite")
    if soc >= target_soc:
        enabled = False
    elif soc <= target_soc - hysteresis_soc:
        enabled = True
    elif was_enabled is None:
        enabled = True
    else:
        enabled = was_enabled
    return (
        maximum_a if enabled else DVCC_CHARGE_DISABLED,
        enabled,
    )


class BatteryPolicy:
    """Plan DVCC without ever forcing ESS minimum SOC upward."""

    def __init__(self, battery: BatteryConfig, schedule: ScheduleConfig):
        self.battery = battery
        self.schedule = schedule

    @staticmethod
    def _clock(value: str) -> time:
        hour, minute = (int(part) for part in value.split(":"))
        return time(hour, minute)

    def _inside_solar_window(self, now: datetime) -> bool:
        current = now.time().replace(tzinfo=None)
        start = self._clock(self.schedule.solar_start)
        end = self._clock(self.schedule.solar_end)
        if start <= end:
            return start <= current < end
        return current >= start or current < end

    def initial_runtime(self, now: datetime, payload: object) -> BatteryRuntime:
        """Restore state, or anchor a new installation without an instant cycle."""
        if isinstance(payload, dict):
            completed = self._timestamp(payload.get("last_completed_at"), now)
            reached = self._timestamp(payload.get("target_reached_at"), now)
            active = payload.get("campaign_active") is True
            charge_enabled = payload.get("charge_enabled")
            if not isinstance(charge_enabled, bool):
                charge_enabled = None
            authorized = payload.get("solar_authorized_date")
            if not isinstance(authorized, str):
                authorized = None
            if completed is not None:
                return BatteryRuntime(
                    completed,
                    active,
                    reached if active else None,
                    authorized if active else None,
                    charge_enabled,
                    payload.get("min_soc_owned") is True,
                    (
                        float(payload["pre_hc_min_soc"])
                        if isinstance(payload.get("pre_hc_min_soc"), (int, float))
                        and not isinstance(payload.get("pre_hc_min_soc"), bool)
                        else None
                    ),
                    payload.get("solar_priority_date")
                    if isinstance(payload.get("solar_priority_date"), str) else None,
                    self._timestamp(payload.get("last_success_at"), now),
                )

        reference = self.battery.balance_reference_date
        if reference is None:
            return BatteryRuntime(now)
        reference_date = date.fromisoformat(reference)
        reset = self._clock(self.schedule.daily_reset)
        anchored = datetime.combine(reference_date, reset)
        if now.tzinfo is not None:
            anchored = anchored.replace(tzinfo=now.tzinfo)
        return BatteryRuntime(anchored)

    @staticmethod
    def _timestamp(value: object, now: datetime) -> Optional[datetime]:
        if not isinstance(value, str):
            return None
        try:
            parsed = datetime.fromisoformat(value)
        except ValueError:
            return None
        if (parsed.tzinfo is None) != (now.tzinfo is None):
            return None
        return parsed

    @staticmethod
    def _elapsed(now: datetime, then: datetime) -> float:
        if now.tzinfo is not None:
            return max(0.0, (
                now.astimezone(timezone.utc) - then.astimezone(timezone.utc)
            ).total_seconds())
        return max(0.0, (now - then).total_seconds())

    def evaluate(
        self,
        now: datetime,
        soc: float,
        surplus_ready: bool,
        energy_deficit: bool,
        runtime: BatteryRuntime,
        hc_catchup_authorized: bool = False,
        solar_opportunity: bool = False,
    ) -> BatteryDecision:
        due_at = runtime.last_completed_at + timedelta(
            days=self.battery.balance_interval_days
        )
        due = now >= due_at
        active = runtime.campaign_active
        reached_at = runtime.target_reached_at
        authorized_date = runtime.solar_authorized_date
        last_completed_at = runtime.last_completed_at
        last_success_at = runtime.last_success_at
        today = now.date().isoformat()
        priority_date = runtime.solar_priority_date
        inside_solar = self._inside_solar_window(now)
        before_deadline = now.time().replace(tzinfo=None) < self._clock(
            self.battery.balance_solar_deadline
        )
        advance_enabled = self.battery.balance_advance_days > 0
        eligible = now.date() >= (
            due_at.date() - timedelta(days=self.battery.balance_advance_days)
        )
        # An unfinished early attempt must qualify again on each new day.
        # Restarts during the same day retain the selected solar priority.
        if priority_date is not None and priority_date != today and not due:
            active = False
            reached_at = None
            authorized_date = None
        deadline_expired = bool(
            advance_enabled and inside_solar and not before_deadline
            and priority_date == today
            and (reached_at is None or soc < self.battery.balance_target_soc)
        )
        if deadline_expired:
            active = False
            authorized_date = None
            reached_at = None
        if (
            advance_enabled and eligible and inside_solar and before_deadline
            and solar_opportunity and not energy_deficit
        ):
            active = True
            priority_date = today
            authorized_date = today
        status = BalanceStatus.NOT_DUE
        hold_elapsed_s = None
        hold_remaining_s = None

        if deadline_expired:
            status = BalanceStatus.PAUSED_SOLAR_DEADLINE
        elif not active and due:
            status = BalanceStatus.WAITING_SURPLUS
            if not self._inside_solar_window(now) and hc_catchup_authorized:
                active = True
                status = BalanceStatus.HC_FORCED
            elif (
                self._inside_solar_window(now)
                and (not advance_enabled or before_deadline)
                and soc >= self.battery.soc_max
                and surplus_ready
                and not energy_deficit
            ):
                active = True
                authorized_date = today
                status = BalanceStatus.CHARGING

        if active:
            if not self._inside_solar_window(now):
                authorized_date = None
                if hc_catchup_authorized and soc >= self.battery.balance_target_soc:
                    if reached_at is None:
                        reached_at = now
                    held_s = self._elapsed(now, reached_at)
                    if held_s >= self.battery.balance_hold_s:
                        last_completed_at = now
                        active = False
                        reached_at = None
                        status = BalanceStatus.COMPLETED
                        due_at = now + timedelta(
                            days=self.battery.balance_interval_days
                        )
                    else:
                        status = BalanceStatus.HOLDING
                        hold_elapsed_s = held_s
                        hold_remaining_s = max(
                            0.0, self.battery.balance_hold_s - held_s
                        )
                elif hc_catchup_authorized:
                    reached_at = None
                    status = BalanceStatus.HC_FORCED
                else:
                    reached_at = None
                    status = BalanceStatus.PAUSED_OUTSIDE_SOLAR
            elif energy_deficit:
                status = BalanceStatus.PAUSED_DEFICIT
                reached_at = None
                authorized_date = None
            elif authorized_date != today:
                if (
                    surplus_ready and soc >= self.battery.soc_max
                    and (not advance_enabled or before_deadline)
                ):
                    authorized_date = today
                    status = BalanceStatus.CHARGING
                else:
                    status = BalanceStatus.WAITING_SURPLUS
            elif soc >= self.battery.balance_target_soc:
                if reached_at is None:
                    reached_at = now
                held_s = self._elapsed(now, reached_at)
                if held_s >= self.battery.balance_hold_s:
                    last_completed_at = now
                    active = False
                    reached_at = None
                    authorized_date = None
                    status = BalanceStatus.COMPLETED
                    due_at = now + timedelta(
                        days=self.battery.balance_interval_days
                    )
                else:
                    status = BalanceStatus.HOLDING
                    hold_elapsed_s = held_s
                    hold_remaining_s = max(
                        0.0, self.battery.balance_hold_s - held_s
                    )
            else:
                reached_at = None
                status = BalanceStatus.CHARGING

        balancing_now = status in (
            BalanceStatus.CHARGING,
            BalanceStatus.HOLDING,
            BalanceStatus.HC_FORCED,
        )
        if status == BalanceStatus.COMPLETED:
            priority_date = None
            last_success_at = now
        solar_priority = bool(
            active and priority_date == today and inside_solar
            and (before_deadline or status == BalanceStatus.HOLDING)
        )
        target = (
            self.battery.balance_target_soc
            if balancing_now
            else self.battery.soc_max
        )
        if balancing_now:
            # Keep the external ceiling fully open during the entire top-balance
            # hold. The managed battery BMS must remain free to alternate between
            # zero and its requested current while equalising its cells.
            charge_current_a = self.battery.max_charge_current_a
            charge_enabled = True
        else:
            charge_current_a, charge_enabled = compute_charge_current(
                soc,
                target,
                self.battery.max_charge_current_a,
                self.battery.charge_hysteresis_soc,
                runtime.charge_enabled,
            )
        updated = BatteryRuntime(
            last_completed_at,
            active,
            reached_at,
            authorized_date,
            charge_enabled,
            runtime.min_soc_owned,
            runtime.pre_hc_min_soc,
            priority_date,
            last_success_at,
        )
        return BatteryDecision(
            status,
            updated,
            due_at,
            target,
            charge_current_a,
            hold_elapsed_s,
            hold_remaining_s,
            self.battery.balance_target_soc
            if (
                status == BalanceStatus.HC_FORCED
                or (
                    status == BalanceStatus.HOLDING
                    and not self._inside_solar_window(now)
                    and hc_catchup_authorized
                )
            )
            else None,
            solar_priority,
        )
