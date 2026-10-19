"""Tempo data provider and persistent cache.

This module is deliberately independent from relay and DVCC control.  Remote
data can enrich observations, but a provider failure cannot stop the H24 loop.
"""

from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from enum import Enum
import json
import os
from pathlib import Path
import tempfile
from typing import Callable, Optional
from urllib.request import Request, urlopen


class TempoError(RuntimeError):
    """Raised when Tempo data cannot be retrieved or validated."""


class TempoColour(str, Enum):
    BLUE = "blue"
    WHITE = "white"
    RED = "red"
    UNKNOWN = "unknown"


_COLOURS = {0: TempoColour.UNKNOWN, 1: TempoColour.BLUE,
            2: TempoColour.WHITE, 3: TempoColour.RED}


@dataclass(frozen=True)
class TempoDay:
    day: date
    colour: TempoColour


@dataclass(frozen=True)
class TempoSnapshot:
    today: TempoDay
    tomorrow: TempoDay
    observed_at: datetime
    source: str = "api-couleur-tempo.fr"

    def age_s(self, now: datetime) -> float:
        return max(0.0, (now.astimezone(timezone.utc) -
                         self.observed_at.astimezone(timezone.utc)).total_seconds())


@dataclass(frozen=True)
class TempoObservation:
    snapshot: Optional[TempoSnapshot]
    status: str
    refreshed: bool = False
    error: Optional[str] = None

    @property
    def signature(self):
        snapshot_signature = None
        if self.snapshot is not None:
            snapshot_signature = (
                self.snapshot.today.day,
                self.snapshot.today.colour,
                self.snapshot.tomorrow.day,
                self.snapshot.tomorrow.colour,
            )
        return self.status, snapshot_signature, self.error


def _parse_day(payload: object, expected: date) -> TempoDay:
    if not isinstance(payload, dict):
        raise TempoError("Tempo response must be a JSON object")
    try:
        day = date.fromisoformat(payload["dateJour"])
        raw_code = payload["codeJour"]
    except (KeyError, TypeError, ValueError) as exc:
        raise TempoError("Tempo response has invalid dateJour/codeJour") from exc
    if isinstance(raw_code, bool) or raw_code not in _COLOURS:
        raise TempoError("Tempo response contains an unknown codeJour")
    if day != expected:
        raise TempoError("Tempo response date %s does not match %s" % (day, expected))
    return TempoDay(day=day, colour=_COLOURS[raw_code])


class ApiCouleurTempoProvider:
    """Small synchronous client; call outside the time-critical control path."""

    def __init__(self, base_url="https://www.api-couleur-tempo.fr/api",
                 timeout_s=5.0, opener: Callable = urlopen):
        self.base_url = base_url.rstrip("/")
        self.timeout_s = timeout_s
        self._opener = opener

    def _get(self, endpoint: str) -> object:
        request = Request(self.base_url + endpoint,
                          headers={"Accept": "application/json",
                                   "User-Agent": "victron-load-manager"})
        try:
            with self._opener(request, timeout=self.timeout_s) as response:
                return json.loads(response.read().decode("utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            raise TempoError("Tempo provider request failed: %s" % exc) from exc

    def fetch(self, local_today: date, observed_at: Optional[datetime] = None):
        now = observed_at or datetime.now(timezone.utc)
        today = _parse_day(self._get("/jourTempo/today"), local_today)
        tomorrow = _parse_day(self._get("/jourTempo/tomorrow"),
                              local_today + timedelta(days=1))
        return TempoSnapshot(today=today, tomorrow=tomorrow, observed_at=now)


class TempoCache:
    """Atomic JSON cache, kept separate from runtime.json."""

    def __init__(self, path):
        self.path = Path(path)

    def save(self, snapshot: TempoSnapshot) -> None:
        payload = {
            "today": {"day": snapshot.today.day.isoformat(),
                      "colour": snapshot.today.colour.value},
            "tomorrow": {"day": snapshot.tomorrow.day.isoformat(),
                         "colour": snapshot.tomorrow.colour.value},
            "observed_at": snapshot.observed_at.isoformat(),
            "source": snapshot.source,
        }
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd, temporary = tempfile.mkstemp(prefix=self.path.name + ".",
                                         dir=str(self.path.parent), text=True)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as stream:
                json.dump(payload, stream, sort_keys=True)
                stream.write("\n")
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, self.path)
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)

    def load(self) -> Optional[TempoSnapshot]:
        try:
            with self.path.open("r", encoding="utf-8") as stream:
                payload = json.load(stream)
            return TempoSnapshot(
                today=TempoDay(date.fromisoformat(payload["today"]["day"]),
                               TempoColour(payload["today"]["colour"])),
                tomorrow=TempoDay(date.fromisoformat(payload["tomorrow"]["day"]),
                                  TempoColour(payload["tomorrow"]["colour"])),
                observed_at=datetime.fromisoformat(payload["observed_at"]),
                source=str(payload["source"]),
            )
        except FileNotFoundError:
            return None
        except (KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
            raise TempoError("invalid Tempo cache: %s" % exc) from exc


def cache_status(snapshot: Optional[TempoSnapshot], local_today: date,
                 now: datetime, fresh_for_s: int) -> str:
    if snapshot is None:
        return "missing"
    if snapshot.today.day != local_today:
        return "invalid"
    return "fresh" if snapshot.age_s(now) <= fresh_for_s else "stale"


class TempoObserver:
    """Periodically refresh Tempo data and always degrade to the local cache."""

    def __init__(self, provider, cache: TempoCache, refresh_interval_s: int,
                 retry_interval_s: int, fresh_for_s: int):
        self.provider = provider
        self.cache = cache
        self.refresh_interval_s = refresh_interval_s
        self.retry_interval_s = retry_interval_s
        self.fresh_for_s = fresh_for_s
        self.snapshot = None
        self.next_refresh_at = None
        self.cache_error = None
        try:
            self.snapshot = cache.load()
        except TempoError as exc:
            self.cache_error = str(exc)

    def poll(self, now: datetime) -> TempoObservation:
        """Refresh when due; provider/cache failures never escape this method."""
        refreshed = False
        error = self.cache_error
        due = self.next_refresh_at is None or now >= self.next_refresh_at
        if due:
            try:
                snapshot = self.provider.fetch(now.date(), now)
                self.cache.save(snapshot)
                self.snapshot = snapshot
                self.cache_error = None
                error = None
                refreshed = True
                delay_s = self.refresh_interval_s
            except (TempoError, OSError) as exc:
                error = str(exc)
                delay_s = self.retry_interval_s
            self.next_refresh_at = now + timedelta(seconds=delay_s)
        status = cache_status(self.snapshot, now.date(), now, self.fresh_for_s)
        return TempoObservation(self.snapshot, status, refreshed, error)
