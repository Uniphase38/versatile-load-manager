"""Victron Modbus TCP access and service unit-ID discovery."""

import json
import inspect
import logging
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Callable, Iterable, List, Optional, Tuple

from .config import CerboConfig

log = logging.getLogger(__name__)

REG_SOC = 30
REG_GRID_LOST = 61
REG_AC_POWER = 817
REG_DVCC_MAX_CHARGE = 2705
REG_PV_LIMITER_ACTIVE = 2709
REG_MIN_SOC = 2901


class ModbusError(IOError):
    """Raised for connection, protocol, or register errors."""


class DiscoveryError(ModbusError):
    """Raised when Victron services cannot be identified unambiguously."""


@dataclass(frozen=True)
class ServiceUnitIds:
    system: int
    vebus: int


@dataclass(frozen=True)
class AcPvLimit:
    """Observed output and limit for one AC-coupled PV inverter."""

    service: str
    power_w: Optional[float]
    limit_w: Optional[float]
    max_power_w: Optional[float]

    @property
    def limit_requested(self) -> Optional[bool]:
        """Return whether Venus requests a cap below inverter nameplate power."""
        if None in (self.limit_w, self.max_power_w):
            return None
        assert self.limit_w is not None
        assert self.max_power_w is not None
        if self.max_power_w <= 0:
            return None
        return self.limit_w < self.max_power_w - 1.0

    @property
    def at_limit(self) -> Optional[bool]:
        """Return whether observed output has settled close to a positive cap."""
        if self.power_w is None or self.limit_w is None:
            return None
        if self.limit_w <= 0 or self.power_w <= 50:
            return None
        tolerance_w = max(100.0, self.limit_w * 0.05)
        return abs(self.power_w - self.limit_w) <= tolerance_w

    @property
    def limited(self) -> Optional[bool]:
        """Return confirmed instantaneous limiting, not merely a requested cap."""
        requested = self.limit_requested
        if requested is False:
            return False
        if requested is None:
            return None
        return self.at_limit


@dataclass(frozen=True)
class DcPvState:
    """Operating state for one DC-coupled solar charger."""

    service: str
    mpp_operation_mode: Optional[int]
    power_w: Optional[float]

    @property
    def limited(self) -> Optional[bool]:
        if self.mpp_operation_mode == 1:
            return True
        if self.mpp_operation_mode == 2:
            return False
        return None


@dataclass(frozen=True)
class SolarCurtailment:
    """Optional local D-Bus evidence of PV curtailment."""

    pv_limiter_active: Optional[bool]
    dc_chargers: Tuple[DcPvState, ...] = ()
    ac_inverters: Tuple[AcPvLimit, ...] = ()
    grid_power_w: Optional[float] = None
    battery_power_w: Optional[float] = None

    @property
    def dc_limited(self) -> Optional[bool]:
        values = [item.limited for item in self.dc_chargers if item.limited is not None]
        return any(values) if values else None

    @property
    def ac_limited(self) -> Optional[bool]:
        values = [item.limited for item in self.ac_inverters if item.limited is not None]
        return any(values) if values else None

    @property
    def ac_limit_requested(self) -> Optional[bool]:
        values = [
            item.limit_requested
            for item in self.ac_inverters
            if item.limit_requested is not None
        ]
        return any(values) if values else None


class DbusSolarMonitor:
    """Read curtailment evidence directly from local Venus OS D-Bus."""

    INTERFACE = "com.victronenergy.BusItem"

    def __init__(self, bus=None):
        self._bus = bus

    def _system_bus(self):
        if self._bus is not None:
            return self._bus
        try:
            import dbus
        except ImportError as exc:
            raise DiscoveryError("Python D-Bus bindings are unavailable") from exc
        self._bus = dbus.SystemBus()
        return self._bus

    @staticmethod
    def _value(bus, service: str, path: str):
        import dbus

        item = bus.get_object(service, path)
        return dbus.Interface(item, DbusSolarMonitor.INTERFACE).GetValue()

    @classmethod
    def _optional_value(cls, bus, service: str, path: str):
        try:
            value = cls._value(bus, service, path)
        except Exception:  # D-Bus exposes optional paths differently by device.
            return None
        if isinstance(value, (list, tuple)) and not value:
            return None
        return value

    def read(self) -> SolarCurtailment:
        bus = self._system_bus()
        names = tuple(str(name) for name in bus.list_names())
        limiter = None
        limiter_services = sorted(
            name for name in names
            if name == "com.victronenergy.hub4" or name.startswith("com.victronenergy.hub4.")
        )
        limiter_services.extend(sorted(
            name for name in names
            if name == "com.victronenergy.system" or name.startswith("com.victronenergy.system.")
        ))
        for service in limiter_services:
            raw = self._optional_value(bus, service, "/PvPowerLimiterActive")
            if raw is not None:
                limiter = bool(int(raw))
                break

        grid_power = None
        battery_power = None
        system_services = sorted(
            name for name in names
            if name == "com.victronenergy.system"
            or name.startswith("com.victronenergy.system.")
        )
        if system_services:
            system_service = system_services[0]
            phases = [
                self._optional_value(
                    bus, system_service, "/Ac/Grid/L%d/Power" % phase
                )
                for phase in (1, 2, 3)
            ]
            available_phases = [float(value) for value in phases if value is not None]
            if available_phases:
                grid_power = sum(available_phases)
            raw_battery_power = self._optional_value(
                bus, system_service, "/Dc/Battery/Power"
            )
            if raw_battery_power is not None:
                battery_power = float(raw_battery_power)

        dc_chargers = []
        for service in sorted(
            name for name in names if name.startswith("com.victronenergy.solarcharger.")
        ):
            mode = self._optional_value(bus, service, "/MppOperationMode")
            power = self._optional_value(bus, service, "/Yield/Power")
            dc_chargers.append(DcPvState(
                service=service,
                mpp_operation_mode=None if mode is None else int(mode),
                power_w=None if power is None else float(power),
            ))

        ac_inverters = []
        for service in sorted(
            name for name in names if name.startswith("com.victronenergy.pvinverter.")
        ):
            power = self._optional_value(bus, service, "/Ac/Power")
            limit = self._optional_value(bus, service, "/Ac/PowerLimit")
            maximum = self._optional_value(bus, service, "/Ac/MaxPower")
            ac_inverters.append(AcPvLimit(
                service=service,
                power_w=None if power is None else float(power),
                limit_w=None if limit is None else float(limit),
                max_power_w=None if maximum is None else float(maximum),
            ))
        return SolarCurtailment(
            limiter,
            tuple(dc_chargers),
            tuple(ac_inverters),
            grid_power,
            battery_power,
        )


class DbusServiceTable:
    """Read the exact service-to-unit-ID table exposed by Venus OS."""

    SERVICE = "com.victronenergy.modbustcp"
    INTERFACE = "com.victronenergy.BusItem"

    @staticmethod
    def _value(bus, path: str):
        import dbus

        item = bus.get_object(DbusServiceTable.SERVICE, path)
        return dbus.Interface(item, DbusServiceTable.INTERFACE).GetValue()

    def read(self) -> List[dict]:
        """Return the same rows displayed by Remote Console."""
        try:
            import dbus
        except ImportError as exc:
            raise DiscoveryError("Python D-Bus bindings are unavailable") from exc

        bus = dbus.SystemBus()
        count = int(self._value(bus, "/Services/Count"))
        rows = []
        for index in range(count):
            base = "/Services/%d" % index
            rows.append({
                "service_name": str(self._value(bus, base + "/ServiceName")),
                "unit_id": int(self._value(bus, base + "/UnitId")),
            })
        return rows


def unit_ids_from_service_table(rows: Iterable[dict]) -> ServiceUnitIds:
    """Extract the required services from Venus OS service-table rows."""
    system = []
    vebus = []
    for row in rows:
        name = str(row.get("service_name", ""))
        try:
            unit_id = int(row["unit_id"])
        except (KeyError, TypeError, ValueError):
            continue
        if name == "com.victronenergy.system" or name.startswith(
            "com.victronenergy.system."
        ):
            system.append(unit_id)
        if name == "com.victronenergy.vebus" or name.startswith(
            "com.victronenergy.vebus."
        ):
            vebus.append(unit_id)
    return ServiceUnitIds(
        system=ServiceDiscovery._one("system", system),
        vebus=ServiceDiscovery._one("vebus", vebus),
    )


def _is_local_host(host: str) -> bool:
    return host.lower() in ("localhost", "127.0.0.1", "::1")


def _signed(raw: int) -> int:
    return raw - 65536 if raw > 32767 else raw


def _new_client(config: CerboConfig):
    """Import pymodbus only when real Modbus access is requested."""
    import pymodbus

    pymodbus_v2 = int(pymodbus.__version__.split(".")[0]) < 3
    if pymodbus_v2:
        from pymodbus.client.sync import ModbusTcpClient
    else:
        from pymodbus.client import ModbusTcpClient

    client = ModbusTcpClient(config.host, port=config.port, timeout=config.timeout_s)
    # Pymodbus 2.5 receives ``unit`` through **kwargs, so introspection cannot
    # see it. Version 3 has successively exposed ``slave`` then ``device_id``.
    if pymodbus_v2:
        return client, "unit"
    parameters = inspect.signature(client.read_holding_registers).parameters
    for unit_keyword in ("device_id", "slave", "unit"):
        if unit_keyword in parameters:
            return client, unit_keyword
    raise ModbusError(
        "unsupported pymodbus read_holding_registers signature (version %s)"
        % pymodbus.__version__
    )


class ServiceDiscovery:
    """Identify Victron services from service-specific register signatures."""

    def __init__(self, read_register: Callable[[int, int], int]):
        self._read_register = read_register

    def _is_system(self, unit_id: int) -> bool:
        try:
            min_soc = self._read_register(unit_id, REG_MIN_SOC)
            ac_power = _signed(self._read_register(unit_id, REG_AC_POWER))
        except ModbusError:
            return False
        return 0 <= min_soc <= 1000 and -100000 <= ac_power <= 100000

    def _is_vebus(self, unit_id: int) -> bool:
        try:
            soc = self._read_register(unit_id, REG_SOC)
            grid_alarm = self._read_register(unit_id, REG_GRID_LOST)
        except ModbusError:
            return False
        return 0 <= soc <= 1000 and grid_alarm in (0, 1, 2)

    @staticmethod
    def _one(service: str, candidates: List[int]) -> int:
        if not candidates:
            raise DiscoveryError("no Victron %s service found" % service)
        if len(candidates) > 1:
            raise DiscoveryError(
                "ambiguous Victron %s service; matching unit IDs: %s"
                % (service, ", ".join(str(item) for item in candidates))
            )
        return candidates[0]

    def discover(
        self,
        unit_ids: Iterable[int],
        system_override: Optional[int] = None,
        vebus_override: Optional[int] = None,
    ) -> ServiceUnitIds:
        system_candidates: List[int] = []
        vebus_candidates: List[int] = []
        for unit_id in unit_ids:
            if system_override is None and self._is_system(unit_id):
                system_candidates.append(unit_id)
            if vebus_override is None and self._is_vebus(unit_id):
                vebus_candidates.append(unit_id)

        system = system_override
        if system is None:
            system = self._one("system", system_candidates)
        vebus = vebus_override
        if vebus is None:
            vebus = self._one("vebus", vebus_candidates)
        return ServiceUnitIds(system=system, vebus=vebus)


class CerboModbus:
    """Small Modbus facade whose service unit IDs are resolved at startup."""

    def __init__(
        self,
        config: CerboConfig,
        client=None,
        unit_keyword: str = "slave",
        dbus_table=None,
    ):
        self.config = config
        if client is None:
            self.client, self._unit_keyword = _new_client(config)
        else:
            self.client = client
            self._unit_keyword = unit_keyword
        self._dbus_table = dbus_table or DbusServiceTable()
        self.unit_ids: Optional[ServiceUnitIds] = None
        self.resolution_source: Optional[str] = None

    def connect(self) -> None:
        if not self.client.connect():
            raise ModbusError("unable to connect to Cerbo %s:%s" % (
                self.config.host, self.config.port
            ))

    def close(self) -> None:
        self.client.close()

    def _read_raw(self, unit_id: int, address: int) -> int:
        result = self.client.read_holding_registers(
            address=address, count=1, **{self._unit_keyword: unit_id}
        )
        if result.isError() or not getattr(result, "registers", None):
            raise ModbusError("register %s unavailable on unit %s" % (address, unit_id))
        return result.registers[0]

    def _write_raw(self, unit_id: int, address: int, value: int) -> None:
        result = self.client.write_register(
            address=address, value=value, **{self._unit_keyword: unit_id}
        )
        if result.isError():
            raise ModbusError(
                "unable to write register %s on unit %s" % (address, unit_id)
            )

    def _valid_ids(self, unit_ids: ServiceUnitIds) -> bool:
        discovery = ServiceDiscovery(self._read_raw)
        return discovery._is_system(unit_ids.system) and discovery._is_vebus(
            unit_ids.vebus
        )

    def _load_cached_ids(self) -> Optional[ServiceUnitIds]:
        try:
            with Path(self.config.service_cache_file).open("r", encoding="utf-8") as stream:
                data = json.load(stream)
            cached = ServiceUnitIds(system=int(data["system"]), vebus=int(data["vebus"]))
            if self._valid_ids(cached):
                return cached
            log.warning("Ignoring stale Victron service cache")
        except (OSError, ValueError, KeyError, TypeError, json.JSONDecodeError):
            return None
        return None

    def _save_cached_ids(self, unit_ids: ServiceUnitIds) -> None:
        path = Path(self.config.service_cache_file)
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            temporary = path.with_suffix(path.suffix + ".tmp")
            with temporary.open("w", encoding="utf-8") as stream:
                json.dump(asdict(unit_ids), stream)
            temporary.replace(path)
        except OSError as exc:
            log.warning("Unable to save Victron service cache: %s", exc)

    def _apply_overrides(self, detected: ServiceUnitIds) -> ServiceUnitIds:
        return ServiceUnitIds(
            system=self.config.system_unit_id
            if self.config.system_unit_id is not None
            else detected.system,
            vebus=self.config.vebus_unit_id
            if self.config.vebus_unit_id is not None
            else detected.vebus,
        )

    def resolve_services(self) -> ServiceUnitIds:
        """Resolve via local D-Bus, verified cache, then Modbus probing."""
        if self.config.system_unit_id is not None and self.config.vebus_unit_id is not None:
            configured = ServiceUnitIds(
                system=self.config.system_unit_id,
                vebus=self.config.vebus_unit_id,
            )
            if not self._valid_ids(configured):
                raise DiscoveryError("configured Victron service unit IDs failed validation")
            self.unit_ids = configured
            self.resolution_source = "configuration"
            return configured

        if _is_local_host(self.config.host):
            try:
                detected = self._apply_overrides(
                    unit_ids_from_service_table(self._dbus_table.read())
                )
                if self._valid_ids(detected):
                    self.unit_ids = detected
                    self.resolution_source = "dbus"
                    self._save_cached_ids(detected)
                    return detected
                log.warning("Venus OS D-Bus service table failed Modbus validation")
            except DiscoveryError as exc:
                log.warning("D-Bus service discovery unavailable: %s", exc)

        cached = self._load_cached_ids()
        if cached is not None:
            self.unit_ids = self._apply_overrides(cached)
            if self._valid_ids(self.unit_ids):
                self.resolution_source = "cache"
                return self.unit_ids

        discovery = ServiceDiscovery(self._read_raw)
        self.unit_ids = discovery.discover(
            range(self.config.unit_id_scan_start, self.config.unit_id_scan_end + 1),
            system_override=self.config.system_unit_id,
            vebus_override=self.config.vebus_unit_id,
        )
        self.resolution_source = "modbus-scan"
        self._save_cached_ids(self.unit_ids)
        return self.unit_ids

    def _require_ids(self) -> ServiceUnitIds:
        if self.unit_ids is None:
            raise ModbusError("Victron services have not been resolved")
        return self.unit_ids

    def read_soc(self) -> float:
        return _signed(self._read_raw(self._require_ids().vebus, REG_SOC)) / 10.0

    def read_ac_power(self) -> float:
        return float(_signed(self._read_raw(self._require_ids().system, REG_AC_POWER)))

    def read_min_soc(self) -> float:
        return _signed(self._read_raw(self._require_ids().system, REG_MIN_SOC)) / 10.0

    def write_min_soc(self, soc: float) -> None:
        """Write ESS minimum SOC as Victron's 0.1 percent register value."""
        if isinstance(soc, bool) or not isinstance(soc, (int, float)):
            raise ValueError("ESS minimum SOC must be numeric")
        if not 0 <= float(soc) <= 100:
            raise ValueError("ESS minimum SOC must be between 0 and 100")
        self._write_raw(
            self._require_ids().system,
            REG_MIN_SOC,
            int(round(float(soc) * 10.0)),
        )

    def read_dvcc_max_charge(self) -> int:
        return _signed(
            self._read_raw(self._require_ids().system, REG_DVCC_MAX_CHARGE)
        )

    def write_dvcc_max_charge(self, current_a: int) -> None:
        if isinstance(current_a, bool) or not isinstance(current_a, int):
            raise ValueError("DVCC charge current must be an integer")
        if not -1 <= current_a <= 32767:
            raise ValueError("DVCC charge current is outside int16 range")
        self._write_raw(
            self._require_ids().system,
            REG_DVCC_MAX_CHARGE,
            current_a & 0xFFFF,
        )

    def read_grid_lost(self) -> bool:
        return self._read_raw(self._require_ids().vebus, REG_GRID_LOST) == 2

    def read_solar_curtailment(self) -> SolarCurtailment:
        """Return local D-Bus PV limiting evidence, or unknown off-device."""
        if not _is_local_host(self.config.host):
            try:
                return SolarCurtailment(bool(
                    self._read_raw(self._require_ids().system, REG_PV_LIMITER_ACTIVE)
                ))
            except ModbusError:
                return SolarCurtailment(None)
        try:
            telemetry = DbusSolarMonitor().read()
            if telemetry.pv_limiter_active is None:
                telemetry = SolarCurtailment(
                    bool(self._read_raw(
                        self._require_ids().system, REG_PV_LIMITER_ACTIVE
                    )),
                    telemetry.dc_chargers,
                    telemetry.ac_inverters,
                    telemetry.grid_power_w,
                    telemetry.battery_power_w,
                )
            return telemetry
        except Exception as exc:
            log.warning("PV curtailment telemetry unavailable: %s", exc)
            return SolarCurtailment(None)
