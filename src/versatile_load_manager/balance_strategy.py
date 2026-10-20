"""Policy deciding whether an overdue balance deserves grid catch-up."""

from dataclasses import dataclass
from datetime import datetime
from enum import Enum
from typing import Optional

from .tempo import TempoColour


class HcCatchupDecision(str, Enum):
    NOT_DUE = "not_due"
    WAIT_SOLAR = "wait_solar"
    CATCH_UP = "catch_up"
    CATCH_UP_OVERDUE = "catch_up_overdue"


@dataclass(frozen=True)
class HcCatchupAdvice:
    decision: HcCatchupDecision
    reason: str
    overdue_days: float

    @property
    def requested(self) -> bool:
        return self.decision in (
            HcCatchupDecision.CATCH_UP,
            HcCatchupDecision.CATCH_UP_OVERDUE,
        )


def decide_hc_catchup(
    *,
    now: datetime,
    due_at: datetime,
    solar_defer_days: int,
    tomorrow_pv_kwh: Optional[float],
    good_solar_day_kwh: float,
    tomorrow_tempo: TempoColour = TempoColour.UNKNOWN,
) -> HcCatchupAdvice:
    """Decide whether to stop waiting for a solar top-balance opportunity.

    Before the configured defer deadline, a credible solar day tomorrow wins.
    Once overdue, catch-up is requested regardless of Tempo colour: HC colour
    differences are intentionally secondary to completing the maintenance
    cycle. Tempo RED only strengthens the preference to use tomorrow's solar
    while the campaign is still inside its defer allowance.
    """
    if solar_defer_days < 0:
        raise ValueError("solar_defer_days must not be negative")
    if good_solar_day_kwh <= 0:
        raise ValueError("good_solar_day_kwh must be positive")

    overdue_s = max(0.0, (now - due_at).total_seconds())
    overdue_days = overdue_s / 86400.0
    if now < due_at:
        return HcCatchupAdvice(HcCatchupDecision.NOT_DUE, "balance_not_due", 0.0)

    deadline_days = float(solar_defer_days)
    forecast_good = (
        tomorrow_pv_kwh is not None
        and tomorrow_pv_kwh >= good_solar_day_kwh
    )

    if overdue_days >= deadline_days:
        return HcCatchupAdvice(
            HcCatchupDecision.CATCH_UP_OVERDUE,
            "solar_defer_deadline_reached",
            overdue_days,
        )

    if forecast_good:
        reason = (
            "wait_red_day_solar"
            if tomorrow_tempo == TempoColour.RED
            else "wait_good_solar_forecast"
        )
        return HcCatchupAdvice(
            HcCatchupDecision.WAIT_SOLAR, reason, overdue_days
        )

    # A missing/poor forecast is not by itself a reason to buy grid energy
    # before the defer allowance expires. This keeps solar-first deterministic.
    return HcCatchupAdvice(
        HcCatchupDecision.WAIT_SOLAR,
        "within_solar_defer_window",
        overdue_days,
    )
