"""Command-line entry point for Versatile Load Manager."""

import argparse
import fcntl
import math
import os
import platform
import signal
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional, Sequence, Tuple
from zoneinfo import ZoneInfo

from . import __version__
from .config import (
    AppConfig, CerboConfig, ConfigError, LoadConfig, load_config,
    runtime_period_month,
)
from .battery import BalanceStatus, format_dvcc_limit
from .modbus import CerboModbus, ModbusError, SolarCurtailment
from .shelly import ShellyClient, ShellyError
from .controller import LoadController, RuntimeStore
from .policy import Action
from .tempo import ApiCouleurTempoProvider, TempoCache, TempoColour, TempoObserver
from .solar_forecast import SolarForecast, SolarForecastClient


class _ShutdownRequested(Exception):
    """Raised by a process signal so normal cleanup can run."""


class _WatchdogRestart(Exception):
    """Exit cleanly so runit recreates the process and Modbus connection."""


class InstanceAlreadyRunning(RuntimeError):
    """Raised when another controller process already owns the global lock."""


class _InstanceLock:
    """Hold one process-wide advisory lock for the lifetime of a run command."""

    def __init__(self, path="/var/run/versatile-load-manager.lock"):
        self.path = Path(path)
        self._handle = None

    def acquire(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        handle = self.path.open("a+")
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            handle.close()
            raise InstanceAlreadyRunning(
                "another Versatile Load Manager instance is already running "
                "(lock: %s)" % self.path
            )
        handle.seek(0)
        handle.truncate()
        handle.write("%d\n" % os.getpid())
        handle.flush()
        self._handle = handle
        return self

    def release(self):
        if self._handle is None:
            return
        try:
            fcntl.flock(self._handle.fileno(), fcntl.LOCK_UN)
        finally:
            self._handle.close()
            self._handle = None

    def __enter__(self):
        return self.acquire()

    def __exit__(self, _exc_type, _exc_value, _traceback):
        self.release()


def _run_log(message: str, zone: ZoneInfo, when=None, stream=None) -> None:
    """Write one offset-qualified local timestamped controller log line."""
    if when is None:
        when = datetime.now(timezone.utc)
    if when.tzinfo is None:
        local = when.replace(tzinfo=zone)
    else:
        local = when.astimezone(zone)
    print(
        "%s %s" % (local.isoformat(timespec="seconds"), message),
        file=sys.stdout if stream is None else stream,
        flush=True,
    )


def _log_interval_elapsed(previous, current, interval_s: int) -> bool:
    if previous is None or not isinstance(current, datetime):
        return True
    if (previous.tzinfo is None) != (current.tzinfo is None):
        return True
    if current.tzinfo is not None:
        previous = previous.astimezone(timezone.utc)
        current = current.astimezone(timezone.utc)
    return (current - previous).total_seconds() >= interval_s


def _runtime_text(state, month=None) -> str:
    if state.config.target_runtime_h_by_month and month is None:
        raise ValueError("month is required for a monthly runtime target")
    target = state.config.target_runtime_h_for_month(month or 1)
    required = state.config.required_runtime_h_for_month(month or 1)
    if target is None:
        return "runtime=%.2f/%.2f h" % (
            state.runtime_h, required
        )
    text = "runtime=%.2f/%.2f h required, target=%.2f h" % (
        state.runtime_h,
        required,
        target,
    )
    if state.config.target_runtime_h_by_month:
        text += " (month=%d)" % month
    return text


def _duration_text(seconds: float) -> str:
    """Format a non-negative duration for compact controller logs."""
    total = max(0, int(seconds))
    hours, remainder = divmod(total, 3600)
    minutes, seconds = divmod(remainder, 60)
    return "%02d:%02d:%02d" % (hours, minutes, seconds)


def _shutdown_signal(signum, _frame):
    try:
        name = signal.Signals(signum).name
    except ValueError:
        name = str(signum)
    raise _ShutdownRequested(name)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="versatile-load-manager")
    commands = parser.add_subparsers(dest="command", required=True)
    diagnose = commands.add_parser(
        "diagnose", help="test read-only access to Victron services"
    )
    diagnose.add_argument("--config", type=Path, help="YAML configuration file")
    diagnose.add_argument(
        "--host",
        help="Cerbo hostname/IP; defaults to localhost when --config is omitted",
    )
    diagnose.add_argument(
        "--cache-file",
        default="/data/versatile-load-manager/service_ids.json",
        help="service-ID cache used without --config",
    )
    check = commands.add_parser(
        "check", help="check configuration and devices without sending commands"
    )
    check.add_argument("--config", type=Path, required=True)
    load_test = commands.add_parser(
        "load-test", help="plan one Shelly load command without executing it"
    )
    load_test.add_argument("name", help="configured load name")
    load_test.add_argument("state", choices=("on", "off"))
    load_test.add_argument("--config", type=Path, required=True)
    load_cycle = commands.add_parser(
        "load-cycle", help="perform one guarded ON/OFF commissioning cycle"
    )
    load_cycle.add_argument("name", help="configured load name (currently SmallCumulus)")
    load_cycle.add_argument("--config", type=Path, required=True)
    load_cycle.add_argument("--confirm", required=True, help="must exactly match load name")
    load_cycle.add_argument("--seconds", type=int, default=5)
    run = commands.add_parser("run", help="run the H24 policy loop")
    run.add_argument("--config", type=Path, required=True)
    run.add_argument("--once", action="store_true", help="evaluate one cycle then exit")
    faults = commands.add_parser(
        "faults", help="list persistent load safety lockouts"
    )
    faults.add_argument("--config", type=Path, required=True)
    unlock = commands.add_parser(
        "unlock", help="safely clear one persistent load safety lockout"
    )
    unlock.add_argument("name", help="configured load name")
    unlock.add_argument("--config", type=Path, required=True)
    unlock.add_argument(
        "--service",
        default="/service/versatile-load-manager",
        help=argparse.SUPPRESS,
    )
    unlock.add_argument("--yes", action="store_true", help=argparse.SUPPRESS)
    return parser


def _diagnostic_config(args: argparse.Namespace) -> Tuple[CerboConfig, Tuple[LoadConfig, ...]]:
    if args.config is not None:
        if args.host is not None:
            raise ConfigError("--host and --config cannot be used together")
        config = load_config(args.config)
        return config.cerbo, config.loads
    return (
        CerboConfig(
            host=args.host or "localhost",
            service_cache_file=args.cache_file,
        ),
        (),
    )


def _pymodbus_version() -> str:
    import pymodbus

    return str(pymodbus.__version__)


def _state(value: Optional[bool]) -> str:
    if value is None:
        return "unknown"
    return "limited" if value else "free"


def _active(value: Optional[bool]) -> str:
    if value is None:
        return "unknown"
    return "active" if value else "inactive"


def _confirmation(value: Optional[bool]) -> str:
    if value is None:
        return "unknown"
    return "confirmed" if value else "unconfirmed"


def _format_power_flow(value: SolarCurtailment) -> str:
    def format_one(power_w: Optional[float], positive: str, negative: str) -> str:
        if power_w is None:
            return "unknown"
        if power_w > 20:
            return "+%.0fW(%s)" % (power_w, positive)
        if power_w < -20:
            return "%.0fW(%s)" % (power_w, negative)
        return "%.0fW(neutral)" % power_w

    return "Power flow: grid=%s, battery=%s" % (
        format_one(value.grid_power_w, "import", "export"),
        format_one(value.battery_power_w, "charge", "discharge"),
    )


def _format_curtailment(value: SolarCurtailment) -> str:
    context = "unknown" if value.pv_limiter_active is None else (
        "active" if value.pv_limiter_active else "inactive"
    )
    dc = ", ".join(
        "%s:mode=%s,power=%s" % (
            item.service.rsplit(".", 1)[-1],
            "?" if item.mpp_operation_mode is None else item.mpp_operation_mode,
            "?" if item.power_w is None else "%.0fW" % item.power_w,
        ) for item in value.dc_chargers
    ) or "none"
    ac = ", ".join(
        "%s:power=%s,limit=%s,max=%s" % (
            item.service.rsplit(".", 1)[-1],
            "?" if item.power_w is None else "%.0fW" % item.power_w,
            "?" if item.limit_w is None else "%.0fW" % item.limit_w,
            "?" if item.max_power_w is None else "%.0fW" % item.max_power_w,
        ) for item in value.ac_inverters
    ) or "none"
    return (
        "PV limiting: ESS=%s, DC=%s [%s], AC_request=%s, AC_limit=%s [%s]"
        % (
            context,
            _state(value.dc_limited),
            dc,
            _active(value.ac_limit_requested),
            _confirmation(value.ac_limited),
            ac,
        )
    )


def _format_solar_forecast(value: SolarForecast) -> str:
    if value.status != "valid":
        detail = "" if not value.error else ", error=%s" % value.error
        return "PV forecast: status=%s, source=%s%s" % (
            value.status, value.source, detail
        )
    age = "unknown"
    if value.fetched_at is not None:
        age = value.fetched_at.isoformat(timespec="seconds")
    detail = "" if not value.error else ", refresh_error=%s" % value.error
    return (
        "PV forecast: today=%.1f kWh, remaining=%.1f kWh, tomorrow=%.1f kWh, "
        "source=%s, fetched=%s%s"
        % (
            value.today_kwh,
            value.remaining_today_kwh,
            value.tomorrow_kwh,
            value.source,
            age,
            detail,
        )
    )


def diagnose(args: argparse.Namespace) -> int:
    """Connect, resolve services and print a read-only health report."""
    config, loads = _diagnostic_config(args)
    cerbo = CerboModbus(config)
    try:
        cerbo.connect()
        unit_ids = cerbo.resolve_services()
        print("Versatile Load Manager diagnostic")
        print("Python: %s" % platform.python_version())
        print("pymodbus: %s" % _pymodbus_version())
        print("Cerbo: %s:%s" % (config.host, config.port))
        print("Discovery: %s" % cerbo.resolution_source)
        print("Unit IDs: system=%s, vebus=%s" % (unit_ids.system, unit_ids.vebus))
        print("SOC: %.1f %%" % cerbo.read_soc())
        print("AC power: %.1f W" % cerbo.read_ac_power())
        print("Minimum SOC: %.1f %%" % cerbo.read_min_soc())
        print("Grid lost: %s" % cerbo.read_grid_lost())
        solar = cerbo.read_solar_curtailment()
        print(_format_curtailment(solar))
        print(_format_power_flow(solar))
    finally:
        cerbo.close()

    failed_loads = 0
    if loads:
        print("Shelly loads:")
    for load in loads:
        if not load.enabled:
            print("- %s: disabled" % load.name)
            continue
        try:
            status = ShellyClient(load.host, switch_id=load.switch_id).read_status()
            power = "unavailable" if status.power_w is None else "%.1f W" % status.power_w
            output = "unavailable" if status.output is None else ("ON" if status.output else "OFF")
            validity = "OK" if status.component == "switch" else "NOT SWITCHABLE"
            print(
                "- %s: Gen%s %s, %s:%s, output=%s, power=%s [%s]"
                % (
                    load.name,
                    status.generation,
                    status.model,
                    status.component,
                    load.switch_id,
                    output,
                    power,
                    validity,
                )
            )
            if status.component != "switch":
                failed_loads += 1
        except ShellyError as exc:
            failed_loads += 1
            print("- %s: UNREACHABLE (%s)" % (load.name, exc))

    if failed_loads:
        print("Result: FAILED - %s invalid/unreachable load(s) (read-only)" % failed_loads)
        return 1
    print("Result: OK (read-only)")
    return 0


def check(args: argparse.Namespace) -> int:
    """Explain command permissions, then run the existing read-only diagnostic."""
    config = load_config(args.config)
    print("Versatile Load Manager safety check")
    print("Configuration: OK")
    if config.dry_run:
        print("Command mode: SAFE - dry_run is enabled")
    elif not config.control_enabled and not config.battery.control_enabled:
        print("Command mode: SAFE - load and battery control are disabled")
    else:
        enabled = []
        if config.control_enabled:
            enabled.append("Shelly load control")
        if config.battery.control_enabled:
            enabled.append("DVCC battery control")
        print("Command mode: CAUTION - %s enabled" % " and ".join(enabled))
    print("No command will be sent by this check.")
    return diagnose(argparse.Namespace(config=args.config, host=None))


def load_test(args: argparse.Namespace) -> int:
    """Plan a configured load command; deliberately offers no execute switch."""
    config = load_config(args.config)
    matches = [load for load in config.loads if load.name == args.name]
    if not matches:
        raise ConfigError("unknown configured load: %s" % args.name)
    load = matches[0]
    if not load.enabled:
        raise ConfigError("load is disabled: %s" % load.name)
    requested = args.state == "on"
    result = ShellyClient(load.host, switch_id=load.switch_id).set_output(
        requested, dry_run=True
    )
    print("Shelly load command preview")
    print("Load: %s" % load.name)
    print("Host: %s" % load.host)
    print("Requested: %s" % ("ON" if requested else "OFF"))
    print("Current: %s" % ("ON" if result.previous_output else "OFF"))
    if result.previous_output == requested:
        print("Action: none (already in requested state)")
    else:
        print("Action: would switch %s" % ("ON" if requested else "OFF"))
    print("Result: DRY-RUN - no command sent")
    return 0


def _configured_load(config, name: str) -> LoadConfig:
    matches = [load for load in config.loads if load.name == name]
    if not matches:
        raise ConfigError("unknown configured load: %s" % name)
    if not matches[0].enabled:
        raise ConfigError("load is disabled: %s" % name)
    return matches[0]


def load_cycle(args: argparse.Namespace) -> int:
    """Commission SmallCumulus with guarded ON, verification and forced OFF."""
    if args.name != "SmallCumulus":
        raise ConfigError("commissioning cycle is currently restricted to SmallCumulus")
    if args.confirm != args.name:
        raise ConfigError("--confirm must exactly match %s" % args.name)
    if not 1 <= args.seconds <= 10:
        raise ConfigError("--seconds must be between 1 and 10")

    config = load_config(args.config)
    if config.dry_run:
        raise ConfigError("real commissioning requires dry_run: false")
    load = _configured_load(config, args.name)
    shelly = ShellyClient(load.host, switch_id=load.switch_id)
    initial = shelly.read_status()
    if initial.component != "switch" or initial.output is None:
        raise ShellyError("%s is not a switch" % load.name)
    if initial.output:
        raise ShellyError("%s is already ON; refusing commissioning cycle" % load.name)

    cerbo = CerboModbus(config.cerbo)
    try:
        cerbo.connect()
        cerbo.resolve_services()
        if cerbo.read_grid_lost():
            raise ModbusError("grid is lost; refusing load start")
        ac_power = cerbo.read_ac_power()
        projected = ac_power + load.estimated_start_power_w
        if projected > config.max_ac_power_w:
            raise ModbusError(
                "projected AC power %.0f W exceeds limit %s W"
                % (projected, config.max_ac_power_w)
            )
    finally:
        cerbo.close()

    print("Commissioning %s: AC %.0f W, projected %.0f W" % (
        load.name, ac_power, projected
    ), flush=True)
    armed = True
    try:
        shelly.set_output(True, dry_run=False)
        running = None
        threshold = load.rated_w * 0.5
        for attempt in range(6):
            running = shelly.read_status()
            if running.power_w is not None and running.power_w >= threshold:
                break
            if attempt < 5:
                time.sleep(0.5)
        print("ON confirmed; power=%s" % (
            "unavailable" if running.power_w is None else "%.1f W" % running.power_w
        ), flush=True)
        if running.power_w is None or running.power_w < threshold:
            raise ShellyError(
                "%s power did not reach commissioning threshold" % load.name
            )
        time.sleep(args.seconds)
    finally:
        if armed:
            try:
                stopped = shelly.set_output(False, dry_run=False)
                print("OFF confirmed; output=%s" % stopped.observed_output)
            except Exception as exc:
                print("CRITICAL: unable to confirm %s OFF: %s" % (load.name, exc), file=sys.stderr)
                raise
    print("Result: commissioning cycle OK")
    return 0


def faults(args: argparse.Namespace) -> int:
    """List durable safety lockouts without touching the service."""
    config = load_config(args.config)
    store = RuntimeStore(config.state_file, config.schedule.daily_reset)
    lockouts = store.load_lockouts()
    if not lockouts:
        print("No safety lockouts.")
        return 0
    configured = {load.name: load for load in config.loads}
    print("Safety lockouts:")
    for name in sorted(lockouts):
        load = configured.get(name)
        if load is None:
            reason = "load is no longer present in the configuration"
        else:
            reason = "no active power detected after start"
        print("- %s: %s" % (name, reason))
    print("Clear one with: /data/vlm unlock <load>")
    return 0


def _service_running(service: str) -> bool:
    """Return the supervised service state reported by svstat."""
    result = subprocess.run(
        ["svstat", service], capture_output=True, text=True, check=False
    )
    if result.returncode != 0:
        detail = (result.stderr or result.stdout).strip()
        raise ConfigError(
            "unable to read service state%s"
            % (": %s" % detail if detail else "")
        )
    status = result.stdout.strip()
    return status.startswith("up") or ": up " in status or status.endswith(": up")


def _wait_service_down(service: str, timeout_s: float = 5.0) -> None:
    deadline = time.monotonic() + timeout_s
    while _service_running(service):
        if time.monotonic() >= deadline:
            raise ConfigError("service did not stop within %.0f seconds" % timeout_s)
        time.sleep(0.1)


def unlock(args: argparse.Namespace) -> int:
    """Confirm, pause the controller, clear a motor lockout and restore it."""
    config = load_config(args.config)
    load = _configured_load(config, args.name)
    if load.probe_failure_mode != "lockout":
        raise ConfigError("%s is not configured for safety lockout" % load.name)
    store = RuntimeStore(config.state_file, config.schedule.daily_reset)
    if load.name not in store.load_lockouts():
        print("No safety lockout present: %s" % load.name)
        return 0

    if not args.yes:
        try:
            answer = input(
                "Unlock %s? The load may be started again by the controller [y/N]: "
                % load.name
            ).strip().lower()
        except EOFError:
            answer = ""
        if answer not in ("y", "yes", "o", "oui"):
            print("Unlock cancelled.")
            return 0

    was_running = _service_running(args.service)
    try:
        if was_running:
            subprocess.run(["svc", "-d", args.service], check=True)
            _wait_service_down(args.service)
        if store.clear_lockout(load.name):
            print("Safety lockout cleared: %s" % load.name)
        else:
            print("No safety lockout present: %s" % load.name)
    finally:
        if was_running:
            subprocess.run(["svc", "-u", args.service], check=True)
            print("Service restarted.")
    return 0


def _run_locked(args: argparse.Namespace, config: AppConfig) -> int:
    """Run after the caller has acquired the global process lock."""
    controller = LoadController(config)
    zone = ZoneInfo(config.schedule.timezone)
    previous_watchdog = controller.store.load_watchdog_state()
    if previous_watchdog.get("active"):
        _run_log(
            "WATCHDOG previous incident: state=%s errors=%s restarts=%s "
            "last_error=%s"
            % (
                previous_watchdog.get("highest_state", "unknown"),
                previous_watchdog.get("consecutive_errors", 0),
                previous_watchdog.get("restart_count", 0),
                previous_watchdog.get("last_error", "unknown"),
            ),
            zone,
            stream=sys.stderr,
        )
    forecast_client = (
        SolarForecastClient(config.solar_forecast, zone)
        if config.solar_forecast.enabled else None
    )
    mode = "CONTROL" if config.control_enabled and not config.dry_run else "OBSERVE"
    _run_log("Versatile Load Manager H24: %s" % mode, zone)
    _run_log("Version: %s, source=%s" % (__version__, Path(__file__).resolve()), zone)
    _run_log("Schedule timezone: %s" % config.schedule.timezone, zone)
    _run_log("Managed loads: %s" % (
        ", ".join(config.managed_loads) if config.managed_loads else "none"
    ), zone)
    _run_log(
        "WATCHDOG RECOVERING healthy=0/%d"
        % config.watchdog.recovery_cycles,
        zone,
    )
    tempo_observer = None
    if config.tempo.enabled:
        tempo_observer = TempoObserver(
            ApiCouleurTempoProvider(timeout_s=config.tempo.timeout_s),
            TempoCache(config.tempo.cache_file),
            config.tempo.refresh_interval_s,
            config.tempo.retry_interval_s,
            config.tempo.fresh_for_s,
        )
        _run_log(
            "Tempo: OBSERVE, provider=%s, cache=%s"
            % (config.tempo.provider, config.tempo.cache_file),
            zone,
        )
    handled_signals = tuple(
        item for item in (
            getattr(signal, "SIGINT", None),
            getattr(signal, "SIGTERM", None),
            getattr(signal, "SIGHUP", None),
        ) if item is not None
    )
    previous_handlers = {
        item: signal.signal(item, _shutdown_signal) for item in handled_signals
    }
    last_status_at = None
    last_battery_signature = None
    last_load_signatures = {}
    last_tempo_signature = None
    watchdog_restart = False
    try:
        while True:
            planning_now = datetime.now(zone)
            forecast = (
                forecast_client.read(planning_now)
                if forecast_client is not None else None
            )
            tempo = (
                tempo_observer.poll(planning_now)
                if tempo_observer is not None else None
            )
            tomorrow_pv_kwh = (
                forecast.tomorrow_kwh
                if forecast is not None and forecast.status == "valid"
                else None
            )
            tomorrow_tempo = (
                tempo.snapshot.tomorrow.colour
                if tempo is not None and tempo.snapshot is not None
                else TempoColour.UNKNOWN
            )
            try:
                result = controller.cycle(
                    tomorrow_pv_kwh=tomorrow_pv_kwh,
                    tomorrow_tempo=tomorrow_tempo,
                    today_pv_kwh=(
                        forecast.today_kwh
                        if forecast is not None and forecast.status == "valid"
                        else None
                    ),
                )
            except (ModbusError, OSError) as exc:
                for message in controller.watchdog_failure(exc):
                    _run_log(message, zone, stream=sys.stderr)
                if (
                    controller.watchdog.consecutive_errors
                    >= config.watchdog.restart_after_errors
                ):
                    controller.watchdog_restart_requested()
                    _run_log(
                        "WATCHDOG exiting for runit restart",
                        zone,
                        stream=sys.stderr,
                    )
                    raise _WatchdogRestart()
                if args.once:
                    raise
                time.sleep(config.loop_interval_s)
                continue
            for message in controller.watchdog_success():
                _run_log(message, zone)
            snapshot = result.snapshot
            cycle_time = getattr(snapshot, "now", None)
            headroom = config.max_ac_power_w - snapshot.ac_power_w
            start_limit = config.max_ac_power_w - config.ac_start_margin_w
            visible_surplus_w = snapshot.visible_surplus_w
            visible_surplus_text = (
                "%.0f W" % visible_surplus_w
                if isinstance(visible_surplus_w, (int, float))
                and not isinstance(visible_surplus_w, bool)
                and math.isfinite(visible_surplus_w)
                else "unknown"
            )
            full_status = _log_interval_elapsed(
                last_status_at, cycle_time, config.status_log_interval_s
            )
            if full_status:
                last_status_at = cycle_time
                _run_log(
                    "Telemetry: SOC=%.1f%%, AC=%.0f W, limit=%s W, "
                    "start_limit=%s W, headroom=%.0f W, grid_lost=%s, "
                    "surplus_ready=%s, deficit=%s, visible_surplus=%s"
                    % (
                        snapshot.soc,
                        snapshot.ac_power_w,
                        config.max_ac_power_w,
                        start_limit,
                        headroom,
                        snapshot.grid_lost,
                        snapshot.solar_surplus_ready,
                        snapshot.energy_deficit,
                        visible_surplus_text,
                    ), zone, cycle_time
                )
                _run_log(
                    _format_curtailment(result.solar_curtailment),
                    zone,
                    cycle_time,
                )
                _run_log(
                    _format_power_flow(result.solar_curtailment),
                    zone,
                    cycle_time,
                )
                if forecast_client is not None:
                    _run_log(
                        _format_solar_forecast(forecast),
                        zone,
                        cycle_time,
                    )
            if tempo_observer is not None:
                if full_status or tempo.signature != last_tempo_signature:
                    if tempo.snapshot is None:
                        message = "Tempo: OBSERVE, status=%s, data=unavailable" % tempo.status
                    else:
                        message = (
                            "Tempo: OBSERVE, status=%s, today=%s, tomorrow=%s, "
                            "observed_at=%s"
                            % (
                                tempo.status,
                                tempo.snapshot.today.colour.value,
                                tempo.snapshot.tomorrow.colour.value,
                                tempo.snapshot.observed_at.isoformat(),
                            )
                        )
                    if tempo.error:
                        message += ", refresh_error=%s" % tempo.error
                    _run_log(message, zone, cycle_time)
                last_tempo_signature = tempo.signature
            battery_mode = (
                "CONTROL"
                if config.battery.control_enabled and not config.dry_run
                else "OBSERVE"
            )
            battery_signature = (
                battery_mode,
                result.battery.status.value,
                result.battery.target_soc,
                result.observed_dvcc_a,
                result.battery.charge_current_a,
                result.battery.due_at,
                result.battery.solar_priority,
                snapshot.balance_surplus_start_allowed
                if config.battery.balance_surplus_enabled else False,
                snapshot.balance_surplus_keep_allowed
                if config.battery.balance_surplus_enabled else False,
                snapshot.post_balance_until
                if config.battery.post_balance_support_enabled else None,
            )
            if full_status or battery_signature != last_battery_signature:
                hold_text = ""
                allowance_text = ""
                if config.battery.balance_surplus_enabled:
                    allowance_text += ", surplus_start=%s, surplus_keep=%s" % (
                        snapshot.balance_surplus_start_allowed,
                        snapshot.balance_surplus_keep_allowed,
                    )
                    allowance_text += ", surplus_charge_ref=%s" % (
                        "unknown" if snapshot.balance_charge_reference_w is None
                        else "%.0fW" % snapshot.balance_charge_reference_w
                    )
                if config.battery.post_balance_support_enabled:
                    allowance_text += ", post_balance_until=%s" % snapshot.post_balance_until
                if (
                    result.battery.status == BalanceStatus.HOLDING
                    and result.battery.hold_elapsed_s is not None
                    and result.battery.hold_remaining_s is not None
                ):
                    hold_text = ", held=%s, remaining=%s" % (
                        _duration_text(result.battery.hold_elapsed_s),
                        _duration_text(result.battery.hold_remaining_s),
                    )
                _run_log(
                    "Battery: %s, balance=%s, target=%.1f%%, "
                    "DVCC=%s->%s, next=%s%s, solar_priority=%s%s"
                    % (
                        battery_mode,
                        result.battery.status.value,
                        result.battery.target_soc,
                        (
                            "unknown"
                            if result.observed_dvcc_a is None
                            else format_dvcc_limit(result.observed_dvcc_a)
                        ),
                        format_dvcc_limit(result.battery.charge_current_a),
                        result.battery.due_at.isoformat(),
                        hold_text,
                        result.battery.solar_priority,
                        allowance_text,
                    ),
                    zone,
                    cycle_time,
                )
            last_battery_signature = battery_signature
            states = {state.config.name: state for state in snapshot.loads}
            runtime_month = (
                runtime_period_month(cycle_time, config.schedule.daily_reset)
                if isinstance(cycle_time, datetime)
                else None
            )
            for decision in result.decisions:
                state = states[decision.load_name]
                stop_suspended = decision.load_name in result.suspended_stops
                action_text = (
                    "stop_suspended" if stop_suspended else decision.action.value
                )
                suspension_text = (
                    ", external_control=protected_until_daily_reset"
                    if stop_suspended else ""
                )
                projected = snapshot.ac_power_w
                if decision.action == Action.START:
                    projected += state.config.estimated_start_power_w
                signature = (
                    decision.action,
                    decision.reason,
                    state.output,
                    state.reachable,
                    state.daily_complete,
                    stop_suspended,
                )
                if (
                    full_status
                    or signature != last_load_signatures.get(decision.load_name)
                ):
                    _run_log(
                        "%s: %s (%s), output=%s, state_for=%.0f s, "
                        "%s, projected=%.0f W%s"
                        % (
                            decision.load_name,
                            action_text,
                            decision.reason.value,
                            (
                                "ON" if state.output else
                                "OFF" if state.output is False else "UNKNOWN"
                            ),
                            state.output_duration_s,
                            _runtime_text(state, runtime_month),
                            projected,
                            suspension_text,
                        ), zone, cycle_time
                    )
                last_load_signatures[decision.load_name] = signature
            for command in result.commands:
                _run_log(command, zone, cycle_time)
            if args.once:
                return 0
            time.sleep(config.loop_interval_s)
    except _WatchdogRestart:
        watchdog_restart = True
        return 75
    except (_ShutdownRequested, KeyboardInterrupt) as exc:
        reason = str(exc) or "keyboard interrupt"
        _run_log("Shutdown requested: %s" % reason, zone)
        return 0
    finally:
        try:
            if watchdog_restart:
                messages = ()
            else:
                messages = controller.stop_managed()
        finally:
            for item, previous in previous_handlers.items():
                signal.signal(item, previous)
        for message in messages:
            stream = sys.stderr if message.startswith("CRITICAL") else sys.stdout
            _run_log(message, zone, stream=stream)


def run(args: argparse.Namespace) -> int:
    """Run the controller continuously, or once for commissioning."""
    config = load_config(args.config)
    with _InstanceLock(config.lock_file):
        return _run_locked(args, config)


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = _parser()
    args = parser.parse_args(argv)
    try:
        if args.command == "diagnose":
            return diagnose(args)
        if args.command == "check":
            return check(args)
        if args.command == "load-test":
            return load_test(args)
        if args.command == "load-cycle":
            return load_cycle(args)
        if args.command == "run":
            return run(args)
        if args.command == "faults":
            return faults(args)
        if args.command == "unlock":
            return unlock(args)
    except InstanceAlreadyRunning as exc:
        print("Startup refused: %s" % exc, file=sys.stderr)
        return 1
    except (ConfigError, ImportError, ModbusError, OSError, subprocess.SubprocessError) as exc:
        print("Diagnostic failed: %s" % exc, file=sys.stderr)
        return 1
    return 2


if __name__ == "__main__":
    sys.exit(main())
