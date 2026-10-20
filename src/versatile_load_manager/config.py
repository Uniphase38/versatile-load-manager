"""YAML configuration loading and validation."""

from dataclasses import dataclass, replace
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import yaml


class ConfigError(ValueError):
    """Raised when the configuration is missing or inconsistent."""


@dataclass(frozen=True)
class CerboConfig:
    host: str
    port: int = 502
    timeout_s: float = 3.0
    unit_id_scan_start: int = 1
    unit_id_scan_end: int = 247
    service_cache_file: str = "/data/versatile-load-manager/service_ids.json"
    system_unit_id: Optional[int] = None
    vebus_unit_id: Optional[int] = None


@dataclass(frozen=True)
class BatteryConfig:
    soc_min: float = 40.0
    soc_max: float = 90.0
    soc_on: float = 70.0
    soc_off: float = 40.0
    max_charge_current_a: int = 100
    balance_interval_days: int = 30
    charge_hysteresis_soc: float = 1.0
    control_enabled: bool = False
    balance_target_soc: float = 100.0
    balance_hold_s: int = 3600
    balance_reference_date: Optional[str] = None
    load_start_hysteresis_soc: float = 1.0
    balance_hc_enabled: bool = False
    balance_solar_defer_days: int = 14
    balance_good_solar_day_kwh: float = 15.0
    usable_capacity_kwh: float = 0.0
    max_grid_charge_power_w: float = 0.0
    balance_hc_safety_margin_s: int = 1800
    night_base_load_w: float = 0.0
    balance_advance_days: int = 0
    balance_solar_deadline: str = "16:00"
    balance_surplus_enabled: bool = False
    balance_surplus_charge_tolerance_w: float = 150.0
    post_balance_support_enabled: bool = False
    post_balance_min_soc: float = 90.0
    post_balance_max_discharge_w: float = 3000.0
    post_balance_extension_s: int = 10800


@dataclass(frozen=True)
class ScheduleConfig:
    solar_start: str = "10:00"
    solar_end: str = "18:00"
    off_peak_start: str = "22:00"
    daily_reset: str = "06:00"
    timezone: str = "UTC"


@dataclass(frozen=True)
class TempoConfig:
    enabled: bool = False
    provider: str = "api-couleur-tempo.fr"
    cache_file: str = "/data/versatile-load-manager/tempo.json"
    refresh_interval_s: int = 3600
    retry_interval_s: int = 300
    fresh_for_s: int = 7200
    timeout_s: float = 5.0


@dataclass(frozen=True)
class SolarForecastConfig:
    enabled: bool = False
    latitude: float = 0.0
    longitude: float = 0.0
    peak_power_kwp: float = 0.0
    tilt_deg: float = 0.0
    azimuth_deg: float = 180.0
    performance_ratio: float = 0.85
    refresh_interval_s: int = 10800
    retry_interval_s: int = 900
    max_age_s: int = 43200
    timeout_s: float = 3.0
    cache_file: str = "/data/versatile-load-manager/solar_forecast.json"


@dataclass(frozen=True)
class WatchdogConfig:
    """Graduated handling of critical Cerbo/Modbus failures."""

    safe_after_errors: int = 3
    recovery_cycles: int = 2
    restart_after_errors: int = 10


def runtime_period_month(now: datetime, daily_reset: str) -> int:
    """Return the calendar month of the VLM day containing *now*."""
    hour, minute = (int(part) for part in daily_reset.split(":"))
    period_day = now.date()
    if (now.hour, now.minute) < (hour, minute):
        period_day -= timedelta(days=1)
    return period_day.month


@dataclass(frozen=True)
class LoadConfig:
    name: str
    host: str
    rated_w: int
    switch_id: int = 0
    priority: int = 100
    min_daily_runtime_h: float = 0.0
    enabled: bool = True
    min_active_power_w: Optional[float] = None
    power_confirm_timeout_s: float = 3.0
    probe_retry_delay_s: int = 1800
    probe_failure_mode: str = "cooldown"
    min_on_time_s: int = 0
    min_off_time_s: int = 0
    completion_mode: str = "runtime"
    startup_power_w: Optional[int] = None
    target_runtime_h: Optional[float] = None
    required_runtime_ratio: float = 1.0
    target_runtime_h_by_month: Tuple[float, ...] = ()
    target_finish_enabled: bool = False
    target_finish_max_remaining_h: float = 1.0
    target_finish_min_soc: float = 80.0
    target_finish_max_battery_discharge_w: int = 500
    target_finish_grace_s: int = 900

    @property
    def estimated_start_power_w(self) -> int:
        """Return the short-lived power budget used to admit a start."""
        return self.rated_w if self.startup_power_w is None else self.startup_power_w

    @property
    def required_runtime_h(self) -> float:
        """Return the fixed runtime which must be completed off peak."""
        if self.target_runtime_h_by_month:
            raise ValueError(
                "required_runtime_h_for_month() is required for a monthly target"
            )
        if self.target_runtime_h is None:
            return self.min_daily_runtime_h
        return self.target_runtime_h * self.required_runtime_ratio

    def target_runtime_h_for_month(self, month: int) -> Optional[float]:
        """Return the fixed or seasonally configured target for *month*."""
        if self.target_runtime_h_by_month:
            if not 1 <= month <= 12:
                raise ValueError("month must be between 1 and 12")
            return self.target_runtime_h_by_month[month - 1]
        return self.target_runtime_h

    def required_runtime_h_for_month(self, month: int) -> float:
        """Return the off-peak guarantee for the selected VLM month."""
        target = self.target_runtime_h_for_month(month)
        if target is None:
            return self.min_daily_runtime_h
        return target * self.required_runtime_ratio


@dataclass(frozen=True)
class AppConfig:
    cerbo: CerboConfig
    battery: BatteryConfig
    schedule: ScheduleConfig
    loads: Tuple[LoadConfig, ...]
    tempo: TempoConfig = TempoConfig()
    solar_forecast: SolarForecastConfig = SolarForecastConfig()
    watchdog: WatchdogConfig = WatchdogConfig()
    loop_interval_s: int = 60
    max_ac_power_w: int = 4000
    ac_start_margin_w: int = 0
    surplus_confirm_cycles: int = 2
    deficit_confirm_cycles: int = 2
    grid_import_tolerance_w: int = 100
    battery_discharge_tolerance_w: int = 200
    battery_charge_reserve_w: int = 1000
    dry_run: bool = False
    control_enabled: bool = False
    managed_loads: Tuple[str, ...] = ()
    state_file: str = "/data/versatile-load-manager/runtime.json"
    lock_file: str = "/var/run/versatile-load-manager.lock"
    start_settle_s: int = 30
    status_log_interval_s: int = 60


def _mapping(value: Any, key: str) -> Dict[str, Any]:
    if not isinstance(value, dict):
        raise ConfigError("'%s' must be a mapping" % key)
    return value


def _clock(value: Any, key: str) -> str:
    if not isinstance(value, str):
        raise ConfigError("'%s' must use HH:MM format" % key)
    try:
        hour_text, minute_text = value.split(":")
        hour, minute = int(hour_text), int(minute_text)
    except (ValueError, TypeError):
        raise ConfigError("'%s' must use HH:MM format" % key)
    if not (0 <= hour <= 23 and 0 <= minute <= 59):
        raise ConfigError("'%s' is not a valid time" % key)
    return "%02d:%02d" % (hour, minute)


def _timezone(value: Any, key: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ConfigError("'%s' must be an IANA timezone name" % key)
    name = value.strip()
    try:
        ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError):
        raise ConfigError("'%s' is not an available IANA timezone" % key)
    return name


def _optional_unit_id(value: Any, key: str) -> Optional[int]:
    if value is None or value == "auto":
        return None
    if isinstance(value, bool) or not isinstance(value, int) or not 0 <= value <= 247:
        raise ConfigError("'%s' must be 'auto' or an integer from 0 to 247" % key)
    return value


_MISSING = object()


def _number(data: Dict[str, Any], key: str, default: Any = _MISSING) -> Any:
    value = data.get(key, default)
    if value is _MISSING:
        raise ConfigError("missing required setting '%s'" % key)
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ConfigError("'%s' must be a number" % key)
    return value


def load_config(path: Path) -> AppConfig:
    """Load *path* and return an immutable, validated configuration."""
    try:
        with Path(path).open("r", encoding="utf-8") as stream:
            raw = yaml.safe_load(stream)
    except FileNotFoundError:
        raise ConfigError("configuration file not found: %s" % path)
    except yaml.YAMLError as exc:
        raise ConfigError("invalid YAML in %s: %s" % (path, exc))

    root = _mapping(raw, "configuration")
    cerbo_raw = _mapping(root.get("cerbo"), "cerbo")
    host = cerbo_raw.get("host")
    if not isinstance(host, str) or not host.strip():
        raise ConfigError("'cerbo.host' must be a non-empty hostname or IP address")
    port = _number(cerbo_raw, "port", 502)
    if not isinstance(port, int) or not 1 <= port <= 65535:
        raise ConfigError("'cerbo.port' must be an integer from 1 to 65535")
    cerbo = CerboConfig(
        host=host.strip(),
        port=port,
        timeout_s=float(_number(cerbo_raw, "timeout_s", 3)),
        unit_id_scan_start=int(_number(cerbo_raw, "unit_id_scan_start", 1)),
        unit_id_scan_end=int(_number(cerbo_raw, "unit_id_scan_end", 247)),
        service_cache_file=str(
            cerbo_raw.get(
                "service_cache_file",
                "/data/versatile-load-manager/service_ids.json",
            )
        ),
        system_unit_id=_optional_unit_id(
            cerbo_raw.get("system_unit_id", "auto"), "cerbo.system_unit_id"
        ),
        vebus_unit_id=_optional_unit_id(
            cerbo_raw.get("vebus_unit_id", "auto"), "cerbo.vebus_unit_id"
        ),
    )
    if cerbo.timeout_s <= 0:
        raise ConfigError("'cerbo.timeout_s' must be positive")
    if not 0 <= cerbo.unit_id_scan_start <= cerbo.unit_id_scan_end <= 247:
        raise ConfigError("Cerbo unit ID scan limits must satisfy 0 <= start <= end <= 247")

    battery_raw = _mapping(root.get("battery", {}), "battery")
    advance_days = _number(battery_raw, "balance_advance_days", 0)
    if isinstance(advance_days, bool) or not isinstance(advance_days, int):
        raise ConfigError("battery.balance_advance_days must be an integer")
    extension_s = _number(battery_raw, "post_balance_extension_s", 10800)
    if isinstance(extension_s, bool) or not isinstance(extension_s, int):
        raise ConfigError("battery.post_balance_extension_s must be an integer")
    balance_hold_s = _number(battery_raw, "balance_hold_s", 3600)
    if isinstance(balance_hold_s, bool) or not isinstance(balance_hold_s, int):
        raise ConfigError("'battery.balance_hold_s' must be an integer")
    battery = BatteryConfig(
        soc_min=float(_number(battery_raw, "soc_min", 40)),
        soc_max=float(_number(battery_raw, "soc_max", 90)),
        soc_on=float(_number(battery_raw, "soc_on", 70)),
        soc_off=float(_number(battery_raw, "soc_off", 40)),
        max_charge_current_a=int(_number(battery_raw, "max_charge_current_a", 100)),
        balance_interval_days=int(_number(battery_raw, "balance_interval_days", 30)),
        charge_hysteresis_soc=float(
            _number(battery_raw, "charge_hysteresis_soc", 1)
        ),
        control_enabled=battery_raw.get("control_enabled", False),
        balance_target_soc=float(
            _number(battery_raw, "balance_target_soc", 100)
        ),
        balance_hold_s=balance_hold_s,
        balance_reference_date=battery_raw.get("balance_reference_date"),
        load_start_hysteresis_soc=float(
            _number(battery_raw, "load_start_hysteresis_soc", 1)
        ),
        balance_hc_enabled=battery_raw.get("balance_hc_enabled", False),
        balance_solar_defer_days=int(
            _number(battery_raw, "balance_solar_defer_days", 14)
        ),
        balance_good_solar_day_kwh=float(
            _number(battery_raw, "balance_good_solar_day_kwh", 15)
        ),
        usable_capacity_kwh=float(
            _number(battery_raw, "usable_capacity_kwh", 0)
        ),
        max_grid_charge_power_w=float(
            _number(battery_raw, "max_grid_charge_power_w", 0)
        ),
        balance_hc_safety_margin_s=int(
            _number(battery_raw, "balance_hc_safety_margin_s", 1800)
        ),
        night_base_load_w=float(
            _number(battery_raw, "night_base_load_w", 0)
        ),
        balance_advance_days=advance_days,
        balance_solar_deadline=_clock(
            battery_raw.get("balance_solar_deadline", "16:00"),
            "battery.balance_solar_deadline",
        ),
        balance_surplus_enabled=battery_raw.get("balance_surplus_enabled", False),
        balance_surplus_charge_tolerance_w=float(_number(
            battery_raw, "balance_surplus_charge_tolerance_w", 150,
        )),
        post_balance_support_enabled=battery_raw.get("post_balance_support_enabled", False),
        post_balance_min_soc=float(_number(battery_raw, "post_balance_min_soc", 90)),
        post_balance_max_discharge_w=float(_number(
            battery_raw, "post_balance_max_discharge_w", 3000,
        )),
        post_balance_extension_s=extension_s,
    )
    if not 0 <= battery.soc_min < battery.soc_max <= 100:
        raise ConfigError("battery SOC limits must satisfy 0 <= soc_min < soc_max <= 100")
    if not battery.soc_off <= battery.soc_on:
        raise ConfigError("battery.soc_off must be lower than or equal to battery.soc_on")
    if battery.max_charge_current_a <= 0 or battery.balance_interval_days <= 0:
        raise ConfigError("charge current and balancing interval must be positive")
    if not 0 < battery.charge_hysteresis_soc <= 10:
        raise ConfigError(
            "battery.charge_hysteresis_soc must be greater than 0 and at most 10"
        )
    if not 0 <= battery.load_start_hysteresis_soc <= 10:
        raise ConfigError(
            "battery.load_start_hysteresis_soc must be between 0 and 10"
        )
    if battery.soc_min + battery.load_start_hysteresis_soc > battery.soc_on:
        raise ConfigError(
            "battery.soc_min + load_start_hysteresis_soc must not exceed soc_on"
        )
    if not isinstance(battery.control_enabled, bool):
        raise ConfigError("'battery.control_enabled' must be true or false")
    for key in ("balance_surplus_enabled", "post_balance_support_enabled"):
        if not isinstance(getattr(battery, key), bool):
            raise ConfigError("battery.%s must be true or false" % key)
    if not 0 <= battery.balance_surplus_charge_tolerance_w <= 1000:
        raise ConfigError("battery.balance_surplus_charge_tolerance_w must be between 0 and 1000")
    if not 0 <= battery.post_balance_min_soc < 100 or (
        battery.post_balance_support_enabled
        and not battery.soc_min <= battery.post_balance_min_soc < battery.balance_target_soc
    ):
        raise ConfigError("battery.post_balance_min_soc must be >= soc_min and below balance_target_soc")
    if battery.post_balance_max_discharge_w <= 0:
        raise ConfigError("battery.post_balance_max_discharge_w must be positive")
    if not 0 <= battery.post_balance_extension_s <= 86400:
        raise ConfigError("battery.post_balance_extension_s must be between 0 and 86400")
    if not isinstance(battery.balance_hc_enabled, bool):
        raise ConfigError("'battery.balance_hc_enabled' must be true or false")
    if battery.balance_solar_defer_days < 0:
        raise ConfigError("battery.balance_solar_defer_days must not be negative")
    if not 0 <= battery.balance_advance_days < battery.balance_interval_days:
        raise ConfigError("battery.balance_advance_days must be >= 0 and less than balance_interval_days")
    if battery.balance_good_solar_day_kwh <= 0:
        raise ConfigError("battery.balance_good_solar_day_kwh must be positive")
    if battery.balance_hc_safety_margin_s < 0:
        raise ConfigError("battery.balance_hc_safety_margin_s must not be negative")
    if battery.night_base_load_w < 0:
        raise ConfigError("battery.night_base_load_w must not be negative")
    if battery.balance_hc_enabled and (
        battery.usable_capacity_kwh <= 0
        or battery.max_grid_charge_power_w <= 0
    ):
        raise ConfigError(
            "HC balancing requires positive battery.usable_capacity_kwh "
            "and battery.max_grid_charge_power_w"
        )
    if not battery.soc_max < battery.balance_target_soc <= 100:
        raise ConfigError(
            "battery balance target must satisfy soc_max < balance_target_soc <= 100"
        )
    if not 0 <= battery.balance_hold_s <= 86400:
        raise ConfigError("battery.balance_hold_s must be between 0 and 86400")
    if battery.balance_reference_date is not None:
        if not isinstance(battery.balance_reference_date, str):
            raise ConfigError(
                "battery.balance_reference_date must use YYYY-MM-DD format"
            )
        try:
            normalized_reference = date.fromisoformat(
                battery.balance_reference_date
            ).isoformat()
        except ValueError:
            raise ConfigError(
                "battery.balance_reference_date must use YYYY-MM-DD format"
            )
        battery = replace(
            battery,
            balance_reference_date=normalized_reference,
        )

    schedule_raw = _mapping(root.get("schedule", {}), "schedule")
    schedule = ScheduleConfig(
        timezone=_timezone(
            schedule_raw.get("timezone", "UTC"), "schedule.timezone"
        ),
        solar_start=_clock(schedule_raw.get("solar_start", "10:00"), "schedule.solar_start"),
        solar_end=_clock(schedule_raw.get("solar_end", "18:00"), "schedule.solar_end"),
        off_peak_start=_clock(
            schedule_raw.get("off_peak_start", "22:00"), "schedule.off_peak_start"
        ),
        daily_reset=_clock(schedule_raw.get("daily_reset", "06:00"), "schedule.daily_reset"),
    )

    if battery.balance_advance_days:
        start, end = schedule.solar_start, schedule.solar_end
        deadline = battery.balance_solar_deadline
        if not start < deadline <= end:
            raise ConfigError(
                "solar anticipation requires solar_start < "
                "battery.balance_solar_deadline <= solar_end in a daytime window"
            )

    if battery.post_balance_support_enabled and not (
        schedule.solar_start < schedule.solar_end <= schedule.off_peak_start
    ):
        raise ConfigError("post-balance support requires a daytime solar window ending before HC")

    tempo_raw = _mapping(root.get("tempo", {}), "tempo")
    tempo_enabled = tempo_raw.get("enabled", False)
    if not isinstance(tempo_enabled, bool):
        raise ConfigError("'tempo.enabled' must be true or false")
    tempo_provider = tempo_raw.get("provider", "api-couleur-tempo.fr")
    if tempo_provider != "api-couleur-tempo.fr":
        raise ConfigError(
            "'tempo.provider' must be 'api-couleur-tempo.fr'"
        )
    tempo_cache_file = tempo_raw.get(
        "cache_file", "/data/versatile-load-manager/tempo.json"
    )
    if not isinstance(tempo_cache_file, str) or not tempo_cache_file.strip():
        raise ConfigError("'tempo.cache_file' must be a non-empty path")
    tempo_values = {
        "refresh_interval_s": _number(tempo_raw, "refresh_interval_s", 3600),
        "retry_interval_s": _number(tempo_raw, "retry_interval_s", 300),
        "fresh_for_s": _number(tempo_raw, "fresh_for_s", 7200),
    }
    for key, value in tempo_values.items():
        if (
            isinstance(value, bool)
            or not isinstance(value, int)
            or not 60 <= value <= 172800
        ):
            raise ConfigError(
                "'tempo.%s' must be an integer from 60 to 172800" % key
            )
    tempo_timeout_s = _number(tempo_raw, "timeout_s", 5)
    if not 0 < tempo_timeout_s <= 30:
        raise ConfigError("'tempo.timeout_s' must be greater than 0 and at most 30")
    tempo = TempoConfig(
        enabled=tempo_enabled,
        provider=tempo_provider,
        cache_file=tempo_cache_file.strip(),
        refresh_interval_s=tempo_values["refresh_interval_s"],
        retry_interval_s=tempo_values["retry_interval_s"],
        fresh_for_s=tempo_values["fresh_for_s"],
        timeout_s=float(tempo_timeout_s),
    )
    forecast_raw = _mapping(root.get("solar_forecast", {}), "solar_forecast")
    forecast_enabled = forecast_raw.get("enabled", False)
    if not isinstance(forecast_enabled, bool):
        raise ConfigError("'solar_forecast.enabled' must be true or false")
    forecast_cache_file = forecast_raw.get(
        "cache_file", "/data/versatile-load-manager/solar_forecast.json"
    )
    if not isinstance(forecast_cache_file, str) or not forecast_cache_file.strip():
        raise ConfigError("'solar_forecast.cache_file' must be a non-empty path")
    if forecast_enabled:
        missing_forecast = [
            key for key in (
                "latitude", "longitude", "peak_power_kwp", "tilt_deg",
                "azimuth_deg",
            ) if key not in forecast_raw
        ]
        if missing_forecast:
            raise ConfigError(
                "solar_forecast is enabled but missing: %s"
                % ", ".join(missing_forecast)
            )
    forecast_intervals = {}
    for key, default in (
        ("refresh_interval_s", 10800),
        ("retry_interval_s", 900),
        ("max_age_s", 43200),
    ):
        value = _number(forecast_raw, key, default)
        if isinstance(value, bool) or not isinstance(value, int):
            raise ConfigError("'solar_forecast.%s' must be an integer" % key)
        forecast_intervals[key] = value
    solar_forecast = SolarForecastConfig(
        enabled=forecast_enabled,
        latitude=float(_number(forecast_raw, "latitude", 0)),
        longitude=float(_number(forecast_raw, "longitude", 0)),
        peak_power_kwp=float(_number(forecast_raw, "peak_power_kwp", 0)),
        tilt_deg=float(_number(forecast_raw, "tilt_deg", 0)),
        azimuth_deg=float(_number(forecast_raw, "azimuth_deg", 180)),
        performance_ratio=float(
            _number(forecast_raw, "performance_ratio", 0.85)
        ),
        refresh_interval_s=forecast_intervals["refresh_interval_s"],
        retry_interval_s=forecast_intervals["retry_interval_s"],
        max_age_s=forecast_intervals["max_age_s"],
        timeout_s=float(_number(forecast_raw, "timeout_s", 3)),
        cache_file=forecast_cache_file.strip(),
    )
    if not -90 <= solar_forecast.latitude <= 90:
        raise ConfigError("solar_forecast.latitude must be between -90 and 90")
    if not -180 <= solar_forecast.longitude <= 180:
        raise ConfigError("solar_forecast.longitude must be between -180 and 180")
    if forecast_enabled and solar_forecast.peak_power_kwp <= 0:
        raise ConfigError("solar_forecast.peak_power_kwp must be positive when enabled")
    if not 0 <= solar_forecast.tilt_deg <= 90:
        raise ConfigError("solar_forecast.tilt_deg must be between 0 and 90")
    if not 0 <= solar_forecast.azimuth_deg < 360:
        raise ConfigError("solar_forecast.azimuth_deg must be between 0 and 360")
    if not 0 < solar_forecast.performance_ratio <= 1.2:
        raise ConfigError("solar_forecast.performance_ratio must be in (0, 1.2]")
    for key, value, maximum in (
        ("refresh_interval_s", solar_forecast.refresh_interval_s, 86400),
        ("retry_interval_s", solar_forecast.retry_interval_s, 86400),
        ("max_age_s", solar_forecast.max_age_s, 604800),
    ):
        if not 1 <= value <= maximum:
            raise ConfigError(
                "solar_forecast.%s must be between 1 and %d" % (key, maximum)
            )
    if not 0 < solar_forecast.timeout_s <= 60:
        raise ConfigError("solar_forecast.timeout_s must be in (0, 60]")

    loads_raw = root.get("loads", [])
    if not isinstance(loads_raw, list):
        raise ConfigError("'loads' must be a list")
    loads: List[LoadConfig] = []
    names = set()
    for index, item in enumerate(loads_raw):
        item = _mapping(item, "loads[%d]" % index)
        name, load_host = item.get("name"), item.get("host")
        if not isinstance(name, str) or not name.strip():
            raise ConfigError("loads[%d].name must be a non-empty string" % index)
        if name in names:
            raise ConfigError("duplicate load name: %s" % name)
        if not isinstance(load_host, str) or not load_host.strip():
            raise ConfigError("loads[%d].host must be a non-empty hostname or IP" % index)
        rated_w = _number(item, "rated_w")
        if rated_w <= 0:
            raise ConfigError("loads[%d].rated_w must be positive" % index)
        startup_power_w = item.get("startup_power_w")
        if startup_power_w is not None:
            if (
                isinstance(startup_power_w, bool)
                or not isinstance(startup_power_w, (int, float))
                or startup_power_w < rated_w
            ):
                raise ConfigError(
                    "loads[%d].startup_power_w must be a number greater than "
                    "or equal to rated_w" % index
                )
        switch_id = _number(item, "switch_id", 0)
        if not isinstance(switch_id, int) or switch_id < 0:
            raise ConfigError("loads[%d].switch_id must be a non-negative integer" % index)
        runtime = _number(item, "min_daily_runtime_h", 0)
        if runtime < 0 or runtime > 24:
            raise ConfigError("loads[%d].min_daily_runtime_h must be between 0 and 24" % index)
        target_runtime_h = item.get("target_runtime_h")
        if target_runtime_h is not None:
            if (
                isinstance(target_runtime_h, bool)
                or not isinstance(target_runtime_h, (int, float))
                or not 0 < target_runtime_h <= 24
            ):
                raise ConfigError(
                    "loads[%d].target_runtime_h must be greater than 0 and at most 24"
                    % index
                )
        monthly_target_raw = item.get("target_runtime_h_by_month")
        monthly_target = ()
        if monthly_target_raw is not None:
            if not isinstance(monthly_target_raw, dict):
                raise ConfigError(
                    "loads[%d].target_runtime_h_by_month must be a mapping"
                    % index
                )
            expected_months = set(range(1, 13))
            actual_months = set(monthly_target_raw)
            if actual_months != expected_months or any(
                isinstance(month, bool) or not isinstance(month, int)
                for month in actual_months
            ):
                raise ConfigError(
                    "loads[%d].target_runtime_h_by_month must contain integer "
                    "months 1 through 12" % index
                )
            for month in range(1, 13):
                value = monthly_target_raw[month]
                if (
                    isinstance(value, bool)
                    or not isinstance(value, (int, float))
                    or not 0 < value <= 24
                ):
                    raise ConfigError(
                        "loads[%d].target_runtime_h_by_month[%d] must be "
                        "greater than 0 and at most 24" % (index, month)
                    )
            monthly_target = tuple(
                float(monthly_target_raw[month]) for month in range(1, 13)
            )
        if target_runtime_h is not None and monthly_target:
            raise ConfigError(
                "loads[%d] cannot combine target_runtime_h and "
                "target_runtime_h_by_month" % index
            )
        required_runtime_ratio = _number(item, "required_runtime_ratio", 1)
        if not 0 <= required_runtime_ratio <= 1:
            raise ConfigError(
                "loads[%d].required_runtime_ratio must be between 0 and 1"
                % index
            )
        if (
            target_runtime_h is None
            and not monthly_target
            and "required_runtime_ratio" in item
        ):
            raise ConfigError(
                "loads[%d].required_runtime_ratio requires target_runtime_h "
                "or target_runtime_h_by_month"
                % index
            )
        min_active_power_w = item.get("min_active_power_w")
        if min_active_power_w is not None:
            if (
                isinstance(min_active_power_w, bool)
                or not isinstance(min_active_power_w, (int, float))
                or min_active_power_w <= 0
                or min_active_power_w > rated_w
            ):
                raise ConfigError(
                    "loads[%d].min_active_power_w must be positive and not "
                    "exceed rated_w" % index
                )
        power_confirm_timeout_s = _number(item, "power_confirm_timeout_s", 3)
        if not 0 < power_confirm_timeout_s <= 60:
            raise ConfigError(
                "loads[%d].power_confirm_timeout_s must be between 0 and 60"
                % index
            )
        probe_retry_delay_s = _number(item, "probe_retry_delay_s", 1800)
        if (
            not isinstance(probe_retry_delay_s, int)
            or isinstance(probe_retry_delay_s, bool)
            or not 1 <= probe_retry_delay_s <= 86400
        ):
            raise ConfigError(
                "loads[%d].probe_retry_delay_s must be an integer from 1 to 86400"
                % index
            )
        probe_failure_mode = item.get("probe_failure_mode", "cooldown")
        if probe_failure_mode not in ("cooldown", "lockout"):
            raise ConfigError(
                "loads[%d].probe_failure_mode must be 'cooldown' or 'lockout'"
                % index
            )
        completion_mode = item.get("completion_mode", "runtime")
        if completion_mode not in ("runtime", "thermostat"):
            raise ConfigError(
                "loads[%d].completion_mode must be 'runtime' or 'thermostat'"
                % index
            )
        min_on_time_s = _number(item, "min_on_time_s", 0)
        min_off_time_s = _number(item, "min_off_time_s", 0)
        for key, value in (
            ("min_on_time_s", min_on_time_s),
            ("min_off_time_s", min_off_time_s),
        ):
            if (
                isinstance(value, bool)
                or not isinstance(value, int)
                or not 0 <= value <= 86400
            ):
                raise ConfigError(
                    "loads[%d].%s must be an integer from 0 to 86400"
                    % (index, key)
                )
        target_finish_enabled = item.get("target_finish_enabled", False)
        if not isinstance(target_finish_enabled, bool):
            raise ConfigError(
                "loads[%d].target_finish_enabled must be a boolean" % index
            )
        target_finish_max_remaining_h = _number(
            item, "target_finish_max_remaining_h", 1
        )
        target_finish_min_soc = _number(item, "target_finish_min_soc", 80)
        target_finish_max_battery_discharge_w = _number(
            item, "target_finish_max_battery_discharge_w", 500
        )
        target_finish_grace_s = _number(item, "target_finish_grace_s", 900)
        if not 0 < target_finish_max_remaining_h <= 24:
            raise ConfigError(
                "loads[%d].target_finish_max_remaining_h must be between 0 and 24"
                % index
            )
        if not 0 <= target_finish_min_soc <= 100:
            raise ConfigError(
                "loads[%d].target_finish_min_soc must be between 0 and 100"
                % index
            )
        if (
            isinstance(target_finish_max_battery_discharge_w, bool)
            or not isinstance(target_finish_max_battery_discharge_w, int)
            or target_finish_max_battery_discharge_w < 0
        ):
            raise ConfigError(
                "loads[%d].target_finish_max_battery_discharge_w must be a "
                "non-negative integer" % index
            )
        if (
            isinstance(target_finish_grace_s, bool)
            or not isinstance(target_finish_grace_s, int)
            or not 0 <= target_finish_grace_s <= 86400
        ):
            raise ConfigError(
                "loads[%d].target_finish_grace_s must be an integer from 0 to 86400"
                % index
            )
        if target_finish_enabled and target_runtime_h is None and not monthly_target:
            raise ConfigError(
                "loads[%d].target_finish_enabled requires target_runtime_h or "
                "target_runtime_h_by_month" % index
            )
        if target_finish_enabled and completion_mode != "runtime":
            raise ConfigError(
                "loads[%d].target_finish_enabled requires completion_mode 'runtime'"
                % index
            )
        loads.append(LoadConfig(
            name=name.strip(),
            host=load_host.strip(),
            rated_w=int(rated_w),
            switch_id=switch_id,
            priority=int(_number(item, "priority", 100)),
            min_daily_runtime_h=float(runtime),
            enabled=bool(item.get("enabled", True)),
            min_active_power_w=(
                None if min_active_power_w is None else float(min_active_power_w)
            ),
            power_confirm_timeout_s=float(power_confirm_timeout_s),
            probe_retry_delay_s=probe_retry_delay_s,
            probe_failure_mode=probe_failure_mode,
            min_on_time_s=min_on_time_s,
            min_off_time_s=min_off_time_s,
            completion_mode=completion_mode,
            startup_power_w=(
                None if startup_power_w is None else int(startup_power_w)
            ),
            target_runtime_h=(
                None if target_runtime_h is None else float(target_runtime_h)
            ),
            required_runtime_ratio=float(required_runtime_ratio),
            target_runtime_h_by_month=monthly_target,
            target_finish_enabled=target_finish_enabled,
            target_finish_max_remaining_h=float(target_finish_max_remaining_h),
            target_finish_min_soc=float(target_finish_min_soc),
            target_finish_max_battery_discharge_w=(
                target_finish_max_battery_discharge_w
            ),
            target_finish_grace_s=target_finish_grace_s,
        ))
        names.add(name)

    watchdog_raw = _mapping(root.get("watchdog", {}), "watchdog")
    watchdog = WatchdogConfig(
        safe_after_errors=_number(watchdog_raw, "safe_after_errors", 3),
        recovery_cycles=_number(watchdog_raw, "recovery_cycles", 2),
        restart_after_errors=_number(
            watchdog_raw, "restart_after_errors", 10
        ),
    )
    for key, value in (
        ("safe_after_errors", watchdog.safe_after_errors),
        ("recovery_cycles", watchdog.recovery_cycles),
        ("restart_after_errors", watchdog.restart_after_errors),
    ):
        if isinstance(value, bool) or not isinstance(value, int) or value < 1:
            raise ConfigError(
                "'watchdog.%s' must be a positive integer" % key
            )
    if watchdog.restart_after_errors < watchdog.safe_after_errors:
        raise ConfigError(
            "'watchdog.restart_after_errors' must be greater than or equal to "
            "'watchdog.safe_after_errors'"
        )

    loop_interval_s = _number(root, "loop_interval_s", 60)
    max_ac_power_w = _number(root, "max_ac_power_w", 4000)
    ac_start_margin_w = _number(root, "ac_start_margin_w", 0)
    surplus_confirm_cycles = _number(root, "surplus_confirm_cycles", 2)
    deficit_confirm_cycles = _number(root, "deficit_confirm_cycles", 2)
    grid_import_tolerance_w = _number(root, "grid_import_tolerance_w", 100)
    battery_discharge_tolerance_w = _number(
        root, "battery_discharge_tolerance_w", 200
    )
    battery_charge_reserve_w = _number(root, "battery_charge_reserve_w", 1000)
    start_settle_s = _number(root, "start_settle_s", 30)
    status_log_interval_s = _number(root, "status_log_interval_s", 60)
    if loop_interval_s <= 0 or max_ac_power_w <= 0:
        raise ConfigError("loop interval and maximum AC power must be positive")
    if not 0 <= ac_start_margin_w < max_ac_power_w:
        raise ConfigError(
            "AC start margin must satisfy 0 <= ac_start_margin_w < max_ac_power_w"
        )
    if (
        not isinstance(surplus_confirm_cycles, int)
        or surplus_confirm_cycles <= 0
        or not isinstance(deficit_confirm_cycles, int)
        or deficit_confirm_cycles <= 0
    ):
        raise ConfigError("flow confirmation cycles must be positive integers")
    if (
        grid_import_tolerance_w < 0
        or battery_discharge_tolerance_w < 0
        or battery_charge_reserve_w < 0
    ):
        raise ConfigError("power-flow tolerances must be non-negative")
    for key, value, maximum in (
        ("start_settle_s", start_settle_s, 3600),
        ("status_log_interval_s", status_log_interval_s, 86400),
    ):
        if (
            isinstance(value, bool)
            or not isinstance(value, int)
            or not 1 <= value <= maximum
        ):
            raise ConfigError(
                "'%s' must be an integer from 1 to %d" % (key, maximum)
            )

    control_enabled = root.get("control_enabled", False)
    if not isinstance(control_enabled, bool):
        raise ConfigError("'control_enabled' must be true or false")
    managed_raw = root.get("managed_loads", [])
    if not isinstance(managed_raw, list) or not all(
        isinstance(name, str) and name.strip() for name in managed_raw
    ):
        raise ConfigError("'managed_loads' must be a list of load names")
    managed_loads = tuple(name.strip() for name in managed_raw)
    if len(set(managed_loads)) != len(managed_loads):
        raise ConfigError("'managed_loads' must not contain duplicates")
    unknown_managed = sorted(set(managed_loads) - {load.name for load in loads})
    if unknown_managed:
        raise ConfigError("unknown managed load(s): %s" % ", ".join(unknown_managed))
    state_file = root.get("state_file", "/data/versatile-load-manager/runtime.json")
    if not isinstance(state_file, str) or not state_file.strip():
        raise ConfigError("'state_file' must be a non-empty path")
    lock_file = root.get("lock_file", "/var/run/versatile-load-manager.lock")
    if not isinstance(lock_file, str) or not lock_file.strip():
        raise ConfigError("'lock_file' must be a non-empty path")

    return AppConfig(
        cerbo=cerbo,
        battery=battery,
        schedule=schedule,
        loads=tuple(loads),
        tempo=tempo,
        solar_forecast=solar_forecast,
        watchdog=watchdog,
        loop_interval_s=int(loop_interval_s),
        max_ac_power_w=int(max_ac_power_w),
        ac_start_margin_w=int(ac_start_margin_w),
        surplus_confirm_cycles=surplus_confirm_cycles,
        deficit_confirm_cycles=deficit_confirm_cycles,
        grid_import_tolerance_w=int(grid_import_tolerance_w),
        battery_discharge_tolerance_w=int(battery_discharge_tolerance_w),
        battery_charge_reserve_w=int(battery_charge_reserve_w),
        dry_run=bool(root.get("dry_run", False)),
        control_enabled=control_enabled,
        managed_loads=managed_loads,
        state_file=state_file.strip(),
        lock_file=lock_file.strip(),
        start_settle_s=start_settle_s,
        status_log_interval_s=status_log_interval_s,
    )

