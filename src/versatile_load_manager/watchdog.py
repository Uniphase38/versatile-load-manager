"""Communication watchdog for safety-critical Victron telemetry."""

from dataclasses import dataclass
from datetime import datetime
from enum import Enum
from typing import Optional


class WatchdogState(str, Enum):
    OK = "OK"
    DEGRADED = "DEGRADED"
    SAFE = "SAFE"
    RECOVERING = "RECOVERING"


@dataclass(frozen=True)
class WatchdogTransition:
    previous: WatchdogState
    current: WatchdogState
    consecutive_errors: int
    healthy_cycles: int
    failure_age_s: float
    restart_due: bool


class CommunicationWatchdog:
    """Track critical telemetry failures without making policy decisions.

    A fresh process starts in ``RECOVERING``. Normal starts remain blocked until
    a configurable number of complete telemetry cycles have succeeded.
    """

    def __init__(
        self,
        safe_after_errors: int = 3,
        recovery_cycles: int = 2,
        restart_after_errors: int = 10,
    ):
        self.safe_after_errors = safe_after_errors
        self.recovery_cycles = recovery_cycles
        self.restart_after_errors = restart_after_errors
        self.state = WatchdogState.RECOVERING
        self.consecutive_errors = 0
        self.healthy_cycles = 0
        self.failure_started_at: Optional[datetime] = None
        self.last_error: Optional[str] = None

    @staticmethod
    def _elapsed(now: datetime, started: Optional[datetime]) -> float:
        if started is None:
            return 0.0
        return max(0.0, (now - started).total_seconds())

    def failure(self, now: datetime, error: Exception) -> WatchdogTransition:
        previous = self.state
        if self.failure_started_at is None:
            self.failure_started_at = now
        self.consecutive_errors += 1
        self.healthy_cycles = 0
        self.last_error = str(error)
        self.state = (
            WatchdogState.SAFE
            if self.consecutive_errors >= self.safe_after_errors
            else WatchdogState.DEGRADED
        )
        return WatchdogTransition(
            previous=previous,
            current=self.state,
            consecutive_errors=self.consecutive_errors,
            healthy_cycles=0,
            failure_age_s=self._elapsed(now, self.failure_started_at),
            restart_due=self.consecutive_errors >= self.restart_after_errors,
        )

    def success(self, now: datetime) -> WatchdogTransition:
        previous = self.state
        age = self._elapsed(now, self.failure_started_at)
        self.consecutive_errors = 0
        self.healthy_cycles += 1
        if self.state != WatchdogState.OK:
            self.state = (
                WatchdogState.OK
                if self.healthy_cycles >= self.recovery_cycles
                else WatchdogState.RECOVERING
            )
        if self.state == WatchdogState.OK:
            self.failure_started_at = None
            self.last_error = None
        return WatchdogTransition(
            previous=previous,
            current=self.state,
            consecutive_errors=0,
            healthy_cycles=self.healthy_cycles,
            failure_age_s=age,
            restart_due=False,
        )

    @property
    def control_allowed(self) -> bool:
        return self.state == WatchdogState.OK
