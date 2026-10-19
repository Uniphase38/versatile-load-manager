import unittest
import tempfile
import sys
from types import ModuleType
from pathlib import Path
from unittest.mock import patch

from victron_load_manager.config import CerboConfig
from victron_load_manager.modbus import (
    AcPvLimit,
    CerboModbus,
    DcPvState,
    DbusSolarMonitor,
    DiscoveryError,
    ModbusError,
    REG_AC_POWER,
    REG_DVCC_MAX_CHARGE,
    REG_GRID_LOST,
    REG_MIN_SOC,
    REG_SOC,
    ServiceDiscovery,
    ServiceUnitIds,
    SolarCurtailment,
    _new_client,
    unit_ids_from_service_table,
)


class CurtailmentTelemetryTests(unittest.TestCase):
    def test_ac_limit_requires_a_cap_and_output_at_the_cap(self):
        limited = AcPvLimit("pv", 754.0, 755.6, 5000.0)
        below_cap = AcPvLimit("pv", 400.0, 1000.0, 5000.0)
        above_cap = AcPvLimit("pv", 3664.0, 3141.0, 5000.0)
        unrestricted = AcPvLimit("pv", 4000.0, 5000.0, 5000.0)
        self.assertTrue(limited.limit_requested)
        self.assertTrue(limited.at_limit)
        self.assertTrue(limited.limited)
        self.assertTrue(below_cap.limit_requested)
        self.assertFalse(below_cap.at_limit)
        self.assertFalse(below_cap.limited)
        # A request below measured power is active, but its physical effect is
        # not confirmed yet (or the two D-Bus samples were asynchronous).
        self.assertFalse(above_cap.at_limit)
        self.assertFalse(above_cap.limited)
        self.assertFalse(unrestricted.limit_requested)
        self.assertFalse(unrestricted.limited)

    def test_zero_output_is_not_reported_as_night_time_curtailment(self):
        value = AcPvLimit("pv", 0.0, 0.0, 5000.0)
        self.assertTrue(value.limit_requested)
        self.assertIsNone(value.at_limit)
        self.assertIsNone(value.limited)

    def test_stopped_dc_charger_is_not_described_as_tracking(self):
        self.assertIsNone(DcPvState("mppt", 0, 0.0).limited)

    def test_missing_ac_path_is_unknown(self):
        self.assertIsNone(AcPvLimit("pv", 754.0, None, 5000.0).limited)

    def test_local_dbus_discovers_dc_and_ac_services(self):
        class FakeBus:
            @staticmethod
            def list_names():
                return (
                    "com.victronenergy.hub4",
                    "com.victronenergy.system",
                    "com.victronenergy.solarcharger.ttyS5",
                    "com.victronenergy.pvinverter.pv_76_2056527",
                )

        values = {
            ("com.victronenergy.hub4", "/PvPowerLimiterActive"): 1,
            ("com.victronenergy.system", "/Ac/Grid/L1/Power"): 29.5,
            ("com.victronenergy.system", "/Dc/Battery/Power"): 1192.0,
            ("com.victronenergy.solarcharger.ttyS5", "/MppOperationMode"): 1,
            ("com.victronenergy.solarcharger.ttyS5", "/Yield/Power"): 571.14,
            ("com.victronenergy.pvinverter.pv_76_2056527", "/Ac/Power"): 754.0,
            ("com.victronenergy.pvinverter.pv_76_2056527", "/Ac/PowerLimit"): 755.58,
            ("com.victronenergy.pvinverter.pv_76_2056527", "/Ac/MaxPower"): 5000.0,
        }
        with patch.object(
            DbusSolarMonitor,
            "_value",
            side_effect=lambda _bus, service, path: values[(service, path)],
        ):
            telemetry = DbusSolarMonitor(FakeBus()).read()

        self.assertEqual(telemetry, SolarCurtailment(
            True,
            dc_chargers=telemetry.dc_chargers,
            ac_inverters=telemetry.ac_inverters,
            grid_power_w=29.5,
            battery_power_w=1192.0,
        ))
        self.assertTrue(telemetry.dc_limited)
        self.assertTrue(telemetry.ac_limit_requested)
        self.assertTrue(telemetry.ac_limited)
        self.assertAlmostEqual(telemetry.dc_chargers[0].power_w, 571.14)
        self.assertAlmostEqual(telemetry.grid_power_w, 29.5)
        self.assertAlmostEqual(telemetry.battery_power_w, 1192.0)

    def test_remote_diagnostic_does_not_read_local_dbus(self):
        cerbo = CerboModbus(
            CerboConfig(host="cerbo.local"), client=FakeClient({})
        )
        cerbo.unit_ids = ServiceUnitIds(system=100, vebus=227)
        self.assertEqual(cerbo.read_solar_curtailment(), SolarCurtailment(None))


class FakeRegisters:
    def __init__(self, values):
        self.values = values

    def read(self, unit_id, address):
        try:
            return self.values[(unit_id, address)]
        except KeyError:
            raise ModbusError("missing")


class DiscoveryTests(unittest.TestCase):
    def test_reads_exact_ids_from_venus_service_table(self):
        rows = [
            {"service_name": "com.victronenergy.system", "unit_id": 100},
            {"service_name": "com.victronenergy.vebus.ttyO1", "unit_id": 227},
            {"service_name": "com.victronenergy.solarcharger.ttyO2", "unit_id": 239},
        ]
        self.assertEqual(
            unit_ids_from_service_table(rows), ServiceUnitIds(system=100, vebus=227)
        )

    def test_discovers_system_and_vebus_without_fixed_ids(self):
        values = {
            (42, REG_MIN_SOC): 400,
            (42, REG_AC_POWER): 1200,
            (173, REG_SOC): 735,
            (173, REG_GRID_LOST): 0,
        }
        found = ServiceDiscovery(FakeRegisters(values).read).discover(range(1, 200))
        self.assertEqual(found.system, 42)
        self.assertEqual(found.vebus, 173)

    def test_explicit_override_skips_detection_for_that_service(self):
        values = {(17, REG_SOC): 500, (17, REG_GRID_LOST): 0}
        found = ServiceDiscovery(FakeRegisters(values).read).discover(
            range(1, 20), system_override=100
        )
        self.assertEqual(found.system, 100)
        self.assertEqual(found.vebus, 17)

    def test_missing_service_is_reported(self):
        with self.assertRaisesRegex(DiscoveryError, "no Victron system"):
            ServiceDiscovery(FakeRegisters({}).read).discover(range(1, 5))

    def test_ambiguous_service_is_never_guessed(self):
        values = {
            (8, REG_MIN_SOC): 400,
            (8, REG_AC_POWER): 0,
            (9, REG_MIN_SOC): 400,
            (9, REG_AC_POWER): 0,
            (12, REG_SOC): 800,
            (12, REG_GRID_LOST): 0,
        }
        with self.assertRaisesRegex(DiscoveryError, "ambiguous Victron system"):
            ServiceDiscovery(FakeRegisters(values).read).discover(range(1, 20))


class FakeResult:
    def __init__(self, value=None, error=False):
        self.registers = [] if value is None else [value]
        self._error = error

    def isError(self):
        return self._error


class FakeClient:
    def __init__(self, values):
        self.values = values
        self.writes = []

    def read_holding_registers(self, address, count, slave):
        value = self.values.get((slave, address))
        return FakeResult(value=value, error=value is None)

    def write_register(self, address, value, slave):
        self.writes.append((slave, address, value))
        return FakeResult(value=value)

    def connect(self):
        return True

    def close(self):
        pass


class PymodbusCompatibilityTests(unittest.TestCase):
    @staticmethod
    def _client_class(unit_keyword):
        class Client:
            def __init__(self, host, port, timeout):
                self.arguments = (host, port, timeout)

        if unit_keyword == "unit":
            # This matches pymodbus 2.5, where unit is consumed through kwargs.
            Client.read_holding_registers = lambda self, address, count, **kwargs: None
        elif unit_keyword == "slave":
            Client.read_holding_registers = lambda self, address, count, slave: None
        else:
            Client.read_holding_registers = lambda self, address, count, device_id: None

        return Client

    def test_pymodbus_v2_uses_sync_import_and_unit_keyword(self):
        client_class = self._client_class("unit")
        pymodbus = ModuleType("pymodbus")
        pymodbus.__version__ = "2.5.3"
        client_package = ModuleType("pymodbus.client")
        sync_module = ModuleType("pymodbus.client.sync")
        sync_module.ModbusTcpClient = client_class

        with patch.dict(sys.modules, {
            "pymodbus": pymodbus,
            "pymodbus.client": client_package,
            "pymodbus.client.sync": sync_module,
        }):
            client, keyword = _new_client(CerboConfig(host="cerbo.local"))

        self.assertIsInstance(client, client_class)
        self.assertEqual(keyword, "unit")

    def test_pymodbus_v3_uses_client_import_and_slave_keyword(self):
        client_class = self._client_class("slave")
        pymodbus = ModuleType("pymodbus")
        pymodbus.__version__ = "3.11.2"
        client_module = ModuleType("pymodbus.client")
        client_module.ModbusTcpClient = client_class

        with patch.dict(sys.modules, {
            "pymodbus": pymodbus,
            "pymodbus.client": client_module,
        }):
            client, keyword = _new_client(CerboConfig(host="cerbo.local"))

        self.assertIsInstance(client, client_class)
        self.assertEqual(keyword, "slave")

    def test_recent_pymodbus_v3_uses_device_id_keyword(self):
        client_class = self._client_class("device_id")
        pymodbus = ModuleType("pymodbus")
        pymodbus.__version__ = "3.15.0"
        client_module = ModuleType("pymodbus.client")
        client_module.ModbusTcpClient = client_class

        with patch.dict(sys.modules, {
            "pymodbus": pymodbus,
            "pymodbus.client": client_module,
        }):
            client, keyword = _new_client(CerboConfig(host="cerbo.local"))

        self.assertIsInstance(client, client_class)
        self.assertEqual(keyword, "device_id")


class FakeDbusTable:
    def read(self):
        return [
            {"service_name": "com.victronenergy.system", "unit_id": 100},
            {"service_name": "com.victronenergy.vebus.ttyO1", "unit_id": 227},
        ]


class HybridDiscoveryTests(unittest.TestCase):
    VALUES = {
        (100, REG_MIN_SOC): 400,
        (100, REG_AC_POWER): 500,
        (227, REG_SOC): 750,
        (227, REG_GRID_LOST): 0,
    }

    def test_localhost_prefers_exact_dbus_table(self):
        with tempfile.TemporaryDirectory() as directory:
            config = CerboConfig(
                host="localhost",
                service_cache_file=str(Path(directory) / "ids.json"),
            )
            cerbo = CerboModbus(
                config,
                client=FakeClient(self.VALUES),
                dbus_table=FakeDbusTable(),
            )
            self.assertEqual(
                cerbo.resolve_services(), ServiceUnitIds(system=100, vebus=227)
            )
            self.assertTrue(Path(config.service_cache_file).exists())

    def test_remote_host_reuses_only_a_valid_cache(self):
        with tempfile.TemporaryDirectory() as directory:
            cache = Path(directory) / "ids.json"
            cache.write_text('{"system": 100, "vebus": 227}', encoding="utf-8")
            config = CerboConfig(
                host="cerbo.local",
                unit_id_scan_start=1,
                unit_id_scan_end=2,
                service_cache_file=str(cache),
            )
            cerbo = CerboModbus(config, client=FakeClient(self.VALUES))
            self.assertEqual(
                cerbo.resolve_services(), ServiceUnitIds(system=100, vebus=227)
            )

    def test_min_soc_write_scales_percent_and_validates_range(self):
        client = FakeClient(dict(self.VALUES))
        cerbo = CerboModbus(
            CerboConfig(host="localhost"),
            client=client,
            dbus_table=FakeDbusTable(),
        )
        cerbo.resolve_services()
        cerbo.write_min_soc(37.5)
        self.assertEqual(client.writes[-1], (100, REG_MIN_SOC, 375))
        for invalid in (-0.1, 100.1, True, "40"):
            with self.assertRaises(ValueError):
                cerbo.write_min_soc(invalid)

    def test_dvcc_limit_can_be_read_and_written_on_discovered_system(self):
        values = dict(self.VALUES)
        values[(100, REG_DVCC_MAX_CHARGE)] = 42
        client = FakeClient(values)
        cerbo = CerboModbus(
            CerboConfig(host="localhost"),
            client=client,
            dbus_table=FakeDbusTable(),
        )
        cerbo.resolve_services()
        self.assertEqual(cerbo.read_dvcc_max_charge(), 42)
        cerbo.write_dvcc_max_charge(37)
        self.assertEqual(client.writes, [(100, REG_DVCC_MAX_CHARGE, 37)])
        cerbo.write_dvcc_max_charge(-1)
        self.assertEqual(
            client.writes[-1], (100, REG_DVCC_MAX_CHARGE, 0xFFFF)
        )


if __name__ == "__main__":
    unittest.main()
