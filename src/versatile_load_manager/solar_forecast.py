"""Lightweight Open-Meteo photovoltaic forecast with a persistent cache."""

from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
import json
import math
import os
from pathlib import Path
import threading
from typing import Callable, Dict, List, Optional, Tuple
from urllib.parse import urlencode
from urllib.request import Request, urlopen
from zoneinfo import ZoneInfo

from .config import SolarForecastConfig


OPEN_METEO_URL = "https://api.open-meteo.com/v1/forecast"


@dataclass(frozen=True)
class SolarForecast:
    """One observation-only PV forecast summary."""

    status: str
    today_kwh: Optional[float]
    remaining_today_kwh: Optional[float]
    tomorrow_kwh: Optional[float]
    fetched_at: Optional[datetime]
    source: str
    error: Optional[str] = None


def _open_meteo_azimuth(geographic_degrees: float) -> float:
    """Convert compass azimuth (north=0) to Open-Meteo (south=0)."""
    return (geographic_degrees - 180.0 + 180.0) % 360.0 - 180.0


class SolarForecastClient:
    """Fetch GTI values, convert them to PV energy and survive API outages."""

    def __init__(
        self,
        config: SolarForecastConfig,
        zone: ZoneInfo,
        opener: Callable = urlopen,
        background: bool = True,
    ):
        self.config = config
        self.zone = zone
        self._opener = opener
        self._background = background
        self._refreshing = False
        self._hourly: Tuple[Tuple[datetime, float], ...] = ()
        self._fetched_at: Optional[datetime] = None
        self._last_attempt_at: Optional[datetime] = None
        self._last_error: Optional[str] = None
        self._source = "none"
        self._load_cache()

    def read(self, now: Optional[datetime] = None) -> SolarForecast:
        """Return the latest forecast, refreshing it when its cadence is due."""
        current = now or datetime.now(timezone.utc)
        if current.tzinfo is None:
            current = current.replace(tzinfo=self.zone)
        current = current.astimezone(self.zone)

        if self._refresh_due(current):
            self._start_refresh(current)

        if self._fetched_at is None or not self._hourly:
            return SolarForecast(
                "unavailable", None, None, None, None, "none", self._last_error
            )

        age_s = (current - self._fetched_at.astimezone(self.zone)).total_seconds()
        if age_s > self.config.max_age_s:
            return SolarForecast(
                "stale", None, None, None, self._fetched_at, "cache",
                self._last_error,
            )

        today = current.date()
        tomorrow = today + timedelta(days=1)
        today_kwh = self._energy_for_day(today)
        tomorrow_kwh = self._energy_for_day(tomorrow)
        if today_kwh is None or tomorrow_kwh is None:
            return SolarForecast(
                "incomplete", None, None, None, self._fetched_at, self._source,
                self._last_error or "forecast does not cover today and tomorrow",
            )
        return SolarForecast(
            "valid",
            today_kwh,
            self._remaining_energy(today, current),
            tomorrow_kwh,
            self._fetched_at,
            self._source,
            self._last_error,
        )

    def _refresh_due(self, now: datetime) -> bool:
        if self._refreshing:
            return False
        if self._last_attempt_at is not None:
            since_attempt = (now - self._last_attempt_at).total_seconds()
            if self._last_error is not None and since_attempt < self.config.retry_interval_s:
                return False
        if self._fetched_at is None:
            return True
        return (now - self._fetched_at.astimezone(self.zone)).total_seconds() >= (
            self.config.refresh_interval_s
        )

    def _start_refresh(self, now: datetime) -> None:
        self._last_attempt_at = now
        if not self._background:
            self._refresh(now)
            return
        self._refreshing = True
        worker = threading.Thread(
            target=self._refresh,
            args=(now,),
            name="vlm-solar-forecast",
            daemon=True,
        )
        worker.start()

    def _refresh(self, now: datetime) -> None:
        try:
            request = Request(self._url(), headers={"User-Agent": "versatile-load-manager/0.1"})
            with self._opener(request, timeout=self.config.timeout_s) as response:
                payload = json.loads(response.read().decode("utf-8"))
            hourly = self._parse_hourly(payload)
            if not hourly:
                raise ValueError("Open-Meteo returned no hourly GTI values")
            self._hourly = hourly
            self._fetched_at = now.astimezone(timezone.utc)
            self._last_error = None
            self._source = "open-meteo"
            self._save_cache()
        except (OSError, ValueError, TypeError, KeyError, json.JSONDecodeError) as exc:
            self._last_error = str(exc)
        finally:
            self._refreshing = False

    def _url(self) -> str:
        params = {
            "latitude": self.config.latitude,
            "longitude": self.config.longitude,
            "hourly": "global_tilted_irradiance",
            "tilt": self.config.tilt_deg,
            "azimuth": _open_meteo_azimuth(self.config.azimuth_deg),
            "timezone": self.zone.key,
            "forecast_days": 3,
        }
        return "%s?%s" % (OPEN_METEO_URL, urlencode(params))

    def _parse_hourly(self, payload: Dict) -> Tuple[Tuple[datetime, float], ...]:
        hourly = payload["hourly"]
        timestamps = hourly["time"]
        values = hourly["global_tilted_irradiance"]
        if not isinstance(timestamps, list) or not isinstance(values, list):
            raise ValueError("invalid Open-Meteo hourly arrays")
        if len(timestamps) != len(values):
            raise ValueError("Open-Meteo hourly arrays have different lengths")
        parsed: List[Tuple[datetime, float]] = []
        for timestamp, value in zip(timestamps, values):
            if value is None:
                continue
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                raise ValueError("invalid Open-Meteo GTI value")
            numeric = float(value)
            if not math.isfinite(numeric) or numeric < 0:
                raise ValueError("invalid Open-Meteo GTI value")
            moment = datetime.fromisoformat(timestamp)
            if moment.tzinfo is None:
                moment = moment.replace(tzinfo=self.zone)
            else:
                moment = moment.astimezone(self.zone)
            parsed.append((moment, numeric))
        return tuple(parsed)

    def _energy(self, irradiance_values: List[float]) -> float:
        # Hourly GTI is W/m² averaged over one hour. Its sum / 1000 is kWh/m².
        return (
            sum(irradiance_values)
            / 1000.0
            * self.config.peak_power_kwp
            * self.config.performance_ratio
        )

    def _energy_for_day(self, selected: date) -> Optional[float]:
        values = [
            value for moment, value in self._hourly if moment.date() == selected
        ]
        return None if not values else self._energy(values)

    def _remaining_energy(self, selected: date, now: datetime) -> float:
        # GTI is a preceding-hour mean. Requiring timestamp > now deliberately
        # omits the partly elapsed hour and keeps this estimate conservative.
        return self._energy([
            value
            for moment, value in self._hourly
            if moment.date() == selected and moment > now
        ])

    def _load_cache(self) -> None:
        path = Path(self.config.cache_file)
        try:
            with path.open("r", encoding="utf-8") as stream:
                payload = json.load(stream)
            fetched_at = datetime.fromisoformat(payload["fetched_at"])
            if fetched_at.tzinfo is None:
                raise ValueError("cache timestamp has no timezone")
            hourly = self._parse_hourly({"hourly": payload["hourly"]})
        except (OSError, ValueError, TypeError, KeyError, json.JSONDecodeError):
            return
        self._fetched_at = fetched_at.astimezone(timezone.utc)
        self._hourly = hourly
        self._source = "cache"

    def _save_cache(self) -> None:
        if self._fetched_at is None:
            return
        path = Path(self.config.cache_file)
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_name(path.name + ".tmp")
        payload = {
            "fetched_at": self._fetched_at.isoformat(),
            "hourly": {
                "time": [moment.isoformat() for moment, _value in self._hourly],
                "global_tilted_irradiance": [
                    value for _moment, value in self._hourly
                ],
            },
        }
        with temporary.open("w", encoding="utf-8") as stream:
            json.dump(payload, stream, separators=(",", ":"), sort_keys=True)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)


