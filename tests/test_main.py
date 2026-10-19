import io
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch
from zoneinfo import ZoneInfo

from victron_load_manager.battery import (
    BalanceStatus, BatteryDecision, BatteryRuntime,
)
from victron_load_manager.config import AppConfig, BatteryConfig, CerboConfig, LoadConfig, ScheduleConfig
from victron_load_manager.controller import RuntimeStore
from victron_load_manager.main import (
    InstanceAlreadyRunning, _InstanceLock, _ShutdownRequested,
    _duration_text, _log_interval_elapsed, _run_log, _runtime_text,
    _service_running, main,
)
from victron_load_manager.modbus import (
    AcPvLimit, DcPvState, ModbusError, ServiceUnitIds, SolarCurtailment,
)
from victron_load_manager.shelly import ShellyCommandResult, ShellyStatus


class FakeCerbo:
    resolution_source = "dbus"

    def __init__(self, config):
        self.config = config

    def connect(self):
        pass

    def close(self):
        pass

    def resolve_services(self):
        return ServiceUnitIds(system=100, vebus=227)

    def read_soc(self):
        return 88.0

    def read_ac_power(self):
        return 3391.0

    def read_min_soc(self):
        return 20.0

    def read_grid_lost(self):
        return False

    def read_solar_curtailment(self):
        return SolarCurtailment(
            True,
            (DcPvState("com.victronenergy.solarcharger.ttyS5", 1, 571.14),),
            (AcPvLimit(
                "com.victronenergy.pvinverter.pv_76_2056527",
                754.0,
                755.58,
                5000.0,
            ),),
            29.5,
            1192.0,
        )


class DiagnosticCommandTests(unittest.TestCase):
    @patch("victron_load_manager.main.CerboModbus", FakeCerbo)
    @patch("victron_load_manager.main._pymodbus_version", return_value="2.5.3")
    def test_diagnose_defaults_to_localhost_and_is_read_only(self, _version):
        output = io.StringIO()
        with patch("sys.stdout", output):
            result = main(["diagnose"])

        self.assertEqual(result, 0)
        self.assertIn("Cerbo: localhost:502", output.getvalue())
        self.assertIn("Discovery: dbus", output.getvalue())
        self.assertIn("Unit IDs: system=100, vebus=227", output.getvalue())
        self.assertIn("PV limiting: ESS=active, DC=limited", output.getvalue())
        self.assertIn("AC_request=active, AC_limit=confirmed", output.getvalue())
        self.assertIn(
            "Power flow: grid=+30W(import), battery=+1192W(charge)",
            output.getvalue(),
        )
        self.assertIn("Result: OK (read-only)", output.getvalue())

    def test_config_and_host_are_mutually_exclusive(self):
        errors = io.StringIO()
        with patch("sys.stderr", errors):
            result = main(["diagnose", "--config", "config.yaml", "--host", "cerbo"])

        self.assertEqual(result, 1)
        self.assertIn("--host and --config cannot be used together", errors.getvalue())

    @patch("victron_load_manager.main.CerboModbus", FakeCerbo)
    @patch("victron_load_manager.main._pymodbus_version", return_value="2.5.3")
    @patch("victron_load_manager.main.ShellyClient")
    @patch("victron_load_manager.main.load_config")
    def test_config_diagnoses_enabled_shelly_loads(
        self, load_config_mock, shelly_client, _version
    ):
        load_config_mock.return_value = AppConfig(
            cerbo=CerboConfig(host="localhost"),
            battery=BatteryConfig(),
            schedule=ScheduleConfig(),
            loads=(LoadConfig("Cumulus", "cumulus.local", 2200),),
        )
        shelly_client.return_value.read_status.return_value = ShellyStatus(
            "cumulus.local", 3, "S3PL-00112EU", True, 2198.0
        )
        output = io.StringIO()
        with patch("sys.stdout", output):
            result = main(["diagnose", "--config", "config.yaml"])

        self.assertEqual(result, 0)
        self.assertIn("Cumulus: Gen3", output.getvalue())
        self.assertIn("output=ON", output.getvalue())
        self.assertIn("power=2198.0 W [OK]", output.getvalue())

    @patch("victron_load_manager.main.CerboModbus", FakeCerbo)
    @patch("victron_load_manager.main._pymodbus_version", return_value="2.5.3")
    @patch("victron_load_manager.main.ShellyClient")
    @patch("victron_load_manager.main.load_config")
    def test_meter_only_device_fails_load_validation(
        self, load_config_mock, shelly_client, _version
    ):
        load_config_mock.return_value = AppConfig(
            cerbo=CerboConfig(host="localhost"),
            battery=BatteryConfig(),
            schedule=ScheduleConfig(),
            loads=(LoadConfig("Meter", "meter.local", 1000),),
        )
        shelly_client.return_value.read_status.return_value = ShellyStatus(
            "meter.local", 3, "S3PM-001PCEU16", None, 42.0, component="pm1"
        )
        output = io.StringIO()
        with patch("sys.stdout", output):
            result = main(["diagnose", "--config", "config.yaml"])

        self.assertEqual(result, 1)
        self.assertIn("NOT SWITCHABLE", output.getvalue())

    @patch("victron_load_manager.main.CerboModbus", FakeCerbo)
    @patch("victron_load_manager.main._pymodbus_version", return_value="2.5.3")
    @patch("victron_load_manager.main.load_config")
    def test_check_reports_safe_mode_and_remains_read_only(
        self, load_config_mock, _version
    ):
        load_config_mock.return_value = AppConfig(
            cerbo=CerboConfig(host="localhost"),
            battery=BatteryConfig(control_enabled=False),
            schedule=ScheduleConfig(),
            loads=(),
            dry_run=True,
            control_enabled=False,
        )
        output = io.StringIO()
        with patch("sys.stdout", output):
            result = main(["check", "--config", "config.yaml"])

        self.assertEqual(result, 0)
        self.assertIn("Configuration: OK", output.getvalue())
        self.assertIn("Command mode: SAFE - dry_run is enabled", output.getvalue())
        self.assertIn("No command will be sent by this check", output.getvalue())
        self.assertIn("Result: OK (read-only)", output.getvalue())


class LockoutCommandTests(unittest.TestCase):
    @patch("victron_load_manager.main.subprocess.run")
    def test_service_state_accepts_svstat_path_prefix(self, run_command):
        run_command.return_value = unittest.mock.Mock(
            returncode=0,
            stdout="/service/victron-load-manager: up (pid 42) 10 seconds\n",
            stderr="",
        )
        self.assertTrue(_service_running("/service/victron-load-manager"))

    @patch("victron_load_manager.main.load_config")
    def test_faults_lists_motor_lockout(self, load_config_mock):
        with tempfile.TemporaryDirectory() as directory:
            state_file = str(Path(directory) / "runtime.json")
            config = AppConfig(
                cerbo=CerboConfig(host="localhost"),
                battery=BatteryConfig(),
                schedule=ScheduleConfig(),
                loads=(LoadConfig(
                    "PoolPump",
                    "pool.local",
                    1300,
                    probe_failure_mode="lockout",
                ),),
                state_file=state_file,
            )
            load_config_mock.return_value = config
            store = RuntimeStore(state_file, config.schedule.daily_reset)
            now = datetime(2026, 9, 15, 12, 0)
            store.save(now, {"PoolPump": 0.5}, {"PoolPump"})

            output = io.StringIO()
            with patch("sys.stdout", output):
                result = main(["faults", "--config", "config.yaml"])

            self.assertEqual(result, 0)
            self.assertIn("Safety lockouts:", output.getvalue())
            self.assertIn(
                "PoolPump: no active power detected after start",
                output.getvalue(),
            )
            self.assertIn("/data/vlm unlock <load>", output.getvalue())

    @patch("victron_load_manager.main.load_config")
    def test_faults_reports_empty_state(self, load_config_mock):
        with tempfile.TemporaryDirectory() as directory:
            load_config_mock.return_value = AppConfig(
                cerbo=CerboConfig(host="localhost"),
                battery=BatteryConfig(),
                schedule=ScheduleConfig(),
                loads=(),
                state_file=str(Path(directory) / "runtime.json"),
            )
            output = io.StringIO()
            with patch("sys.stdout", output):
                result = main(["faults", "--config", "config.yaml"])
        self.assertEqual(result, 0)
        self.assertEqual(output.getvalue(), "No safety lockouts.\n")

    @patch("victron_load_manager.main.subprocess.run")
    @patch("victron_load_manager.main._wait_service_down")
    @patch("victron_load_manager.main._service_running", return_value=True)
    @patch("victron_load_manager.main.load_config")
    def test_unlock_pauses_service_and_preserves_runtime(
        self, load_config_mock, _running, wait_down, run_command
    ):
        with tempfile.TemporaryDirectory() as directory:
            state_file = str(Path(directory) / "runtime.json")
            config = AppConfig(
                cerbo=CerboConfig(host="localhost"),
                battery=BatteryConfig(),
                schedule=ScheduleConfig(),
                loads=(LoadConfig(
                    "PoolPump",
                    "pool.local",
                    1300,
                    probe_failure_mode="lockout",
                ),),
                state_file=state_file,
            )
            load_config_mock.return_value = config
            store = RuntimeStore(state_file, config.schedule.daily_reset)
            now = datetime(2026, 9, 15, 12, 0)
            store.save(now, {"PoolPump": 0.5}, {"PoolPump"})

            output = io.StringIO()
            with patch("sys.stdout", output), patch("builtins.input", return_value="oui"):
                result = main([
                    "unlock", "PoolPump", "--config", "config.yaml",
                    "--service", "/service/victron-load-manager",
                ])

            self.assertEqual(result, 0)
            self.assertIn("Safety lockout cleared: PoolPump", output.getvalue())
            self.assertIn("Service restarted.", output.getvalue())
            self.assertEqual(store.load_lockouts(), set())
            self.assertEqual(store.load(now)["PoolPump"], 0.5)
            self.assertEqual(run_command.call_args_list, [
                unittest.mock.call(
                    ["svc", "-d", "/service/victron-load-manager"], check=True
                ),
                unittest.mock.call(
                    ["svc", "-u", "/service/victron-load-manager"], check=True
                ),
            ])
            wait_down.assert_called_once_with("/service/victron-load-manager")

    @patch("victron_load_manager.main._service_running")
    @patch("victron_load_manager.main.load_config")
    def test_unlock_cancelled_before_service_is_touched(
        self, load_config_mock, service_running
    ):
        with tempfile.TemporaryDirectory() as directory:
            state_file = str(Path(directory) / "runtime.json")
            config = AppConfig(
                cerbo=CerboConfig(host="localhost"),
                battery=BatteryConfig(),
                schedule=ScheduleConfig(),
                loads=(LoadConfig(
                    "PoolPump", "pool.local", 1300, probe_failure_mode="lockout"
                ),),
                state_file=state_file,
            )
            load_config_mock.return_value = config
            store = RuntimeStore(state_file, config.schedule.daily_reset)
            store.save(datetime(2026, 9, 15, 12, 0), {}, {"PoolPump"})
            output = io.StringIO()
            with patch("sys.stdout", output), patch("builtins.input", return_value="non"):
                result = main(["unlock", "PoolPump", "--config", "config.yaml"])

            self.assertEqual(result, 0)
            self.assertIn("Unlock cancelled.", output.getvalue())
            self.assertEqual(store.load_lockouts(), {"PoolPump"})
            service_running.assert_not_called()


class LoadTestCommandTests(unittest.TestCase):
    @patch("victron_load_manager.main.ShellyClient")
    @patch("victron_load_manager.main.load_config")
    def test_load_test_is_hard_wired_to_dry_run(self, load_config_mock, shelly_client):
        load_config_mock.return_value = AppConfig(
            cerbo=CerboConfig(host="localhost"),
            battery=BatteryConfig(),
            schedule=ScheduleConfig(),
            loads=(LoadConfig("SmallCumulus", "cumulus.local", 1600),),
        )
        shelly_client.return_value.set_output.return_value = ShellyCommandResult(
            "cumulus.local", True, False, False, False, True
        )
        output = io.StringIO()
        with patch("sys.stdout", output):
            result = main([
                "load-test", "SmallCumulus", "on", "--config", "config.yaml"
            ])

        self.assertEqual(result, 0)
        shelly_client.return_value.set_output.assert_called_once_with(True, dry_run=True)
        self.assertIn("would switch ON", output.getvalue())
        self.assertIn("DRY-RUN - no command sent", output.getvalue())


class LoadCycleCommandTests(unittest.TestCase):
    @staticmethod
    def _config(dry_run=False):
        return AppConfig(
            cerbo=CerboConfig(host="localhost"),
            battery=BatteryConfig(),
            schedule=ScheduleConfig(),
            loads=(LoadConfig("SmallCumulus", "cumulus.local", 1600),),
            max_ac_power_w=6000,
            dry_run=dry_run,
        )

    @patch("victron_load_manager.main.time.sleep")
    @patch("victron_load_manager.main.CerboModbus", FakeCerbo)
    @patch("victron_load_manager.main.ShellyClient")
    @patch("victron_load_manager.main.load_config")
    def test_guarded_cycle_turns_on_and_always_back_off(
        self, load_config_mock, shelly_client, sleep
    ):
        load_config_mock.return_value = self._config()
        shelly_client.return_value.read_status.side_effect = [
            ShellyStatus("cumulus.local", 2, "SNSW", False, 0.0),
            ShellyStatus("cumulus.local", 2, "SNSW", True, 1600.0),
        ]
        shelly_client.return_value.set_output.side_effect = [
            ShellyCommandResult("cumulus.local", True, False, True, True, False),
            ShellyCommandResult("cumulus.local", False, True, False, True, False),
        ]
        output = io.StringIO()
        with patch("sys.stdout", output):
            result = main([
                "load-cycle", "SmallCumulus", "--config", "config.yaml",
                "--confirm", "SmallCumulus", "--seconds", "1",
            ])

        self.assertEqual(result, 0)
        self.assertEqual(
            shelly_client.return_value.set_output.call_args_list,
            [unittest.mock.call(True, dry_run=False), unittest.mock.call(False, dry_run=False)],
        )
        sleep.assert_called_once_with(1)
        self.assertIn("commissioning cycle OK", output.getvalue())

    @patch("victron_load_manager.main.CerboModbus", FakeCerbo)
    @patch("victron_load_manager.main.ShellyClient")
    @patch("victron_load_manager.main.load_config")
    def test_guarded_cycle_reserves_startup_power(
        self, load_config_mock, shelly_client
    ):
        load_config_mock.return_value = AppConfig(
            cerbo=CerboConfig(host="localhost"),
            battery=BatteryConfig(),
            schedule=ScheduleConfig(),
            loads=(LoadConfig(
                "SmallCumulus",
                "cumulus.local",
                1600,
                startup_power_w=2700,
            ),),
            max_ac_power_w=6000,
        )
        shelly_client.return_value.read_status.return_value = ShellyStatus(
            "cumulus.local", 2, "SNSW", False, 0.0
        )
        errors = io.StringIO()
        with patch("sys.stderr", errors):
            result = main([
                "load-cycle", "SmallCumulus", "--config", "config.yaml",
                "--confirm", "SmallCumulus",
            ])

        self.assertEqual(result, 1)
        self.assertIn("projected AC power 6091 W exceeds limit 6000 W", errors.getvalue())
        shelly_client.return_value.set_output.assert_not_called()

    @patch("victron_load_manager.main.time.sleep")
    @patch("victron_load_manager.main.CerboModbus", FakeCerbo)
    @patch("victron_load_manager.main.ShellyClient")
    @patch("victron_load_manager.main.load_config")
    def test_low_power_still_forces_off(
        self, load_config_mock, shelly_client, sleep
    ):
        load_config_mock.return_value = self._config()
        shelly_client.return_value.read_status.side_effect = [
            ShellyStatus("cumulus.local", 2, "SNSW", False, 0.0),
            *[
                ShellyStatus("cumulus.local", 2, "SNSW", True, 0.0)
                for _ in range(6)
            ],
        ]
        shelly_client.return_value.set_output.side_effect = [
            ShellyCommandResult("cumulus.local", True, False, True, True, False),
            ShellyCommandResult("cumulus.local", False, True, False, True, False),
        ]
        with patch("sys.stderr", io.StringIO()):
            result = main([
                "load-cycle", "SmallCumulus", "--config", "config.yaml",
                "--confirm", "SmallCumulus",
            ])

        self.assertEqual(result, 1)
        self.assertEqual(shelly_client.return_value.set_output.call_count, 2)
        self.assertEqual(sleep.call_count, 5)

    @patch("victron_load_manager.main.load_config")
    def test_cycle_refuses_dry_run_configuration(self, load_config_mock):
        load_config_mock.return_value = self._config(dry_run=True)
        errors = io.StringIO()
        with patch("sys.stderr", errors):
            result = main([
                "load-cycle", "SmallCumulus", "--config", "config.yaml",
                "--confirm", "SmallCumulus",
            ])
        self.assertEqual(result, 1)
        self.assertIn("dry_run: false", errors.getvalue())

    @patch("victron_load_manager.main.load_config")
    def test_cycle_rejects_other_load_even_if_confirmed(self, load_config_mock):
        load_config_mock.return_value = self._config()
        errors = io.StringIO()
        with patch("sys.stderr", errors):
            result = main([
                "load-cycle", "MainCumulus", "--config", "config.yaml",
                "--confirm", "MainCumulus",
            ])
        self.assertEqual(result, 1)
        self.assertIn("restricted to SmallCumulus", errors.getvalue())


class RunCommandTests(unittest.TestCase):
    def test_duration_text_is_stable_for_hold_progress(self):
        self.assertEqual(_duration_text(2535), "00:42:15")
        self.assertEqual(_duration_text(1065), "00:17:45")

    def test_instance_lock_refuses_a_second_process_and_is_reusable(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "vlm.lock"
            with _InstanceLock(path):
                self.assertEqual(path.read_text().strip(), str(__import__("os").getpid()))
                with self.assertRaises(InstanceAlreadyRunning):
                    _InstanceLock(path).acquire()

            with _InstanceLock(path):
                pass

    @patch("victron_load_manager.main._run_locked", return_value=0)
    @patch("victron_load_manager.main._InstanceLock")
    @patch("victron_load_manager.main.load_config")
    def test_run_uses_configured_lock_file(
        self, load_config_mock, lock_class, _run_locked
    ):
        load_config_mock.return_value = AppConfig(
            cerbo=CerboConfig(host="localhost"),
            battery=BatteryConfig(),
            schedule=ScheduleConfig(),
            loads=(),
            lock_file="/tmp/vlm-pc.lock",
        )
        self.assertEqual(main(["run", "--config", "config.yaml", "--once"]), 0)
        lock_class.assert_called_once_with("/tmp/vlm-pc.lock")

    @patch("victron_load_manager.main._InstanceLock.acquire")
    @patch("victron_load_manager.main.load_config")
    def test_second_instance_is_refused_before_controller_construction(
        self, load_config_mock, acquire
    ):
        load_config_mock.return_value = AppConfig(
            cerbo=CerboConfig(host="localhost"),
            battery=BatteryConfig(),
            schedule=ScheduleConfig(),
            loads=(),
        )
        acquire.side_effect = InstanceAlreadyRunning("already running")
        errors = io.StringIO()
        with patch("sys.stderr", errors), patch(
            "victron_load_manager.main.LoadController"
        ) as controller_class:
            result = main(["run", "--config", "config.yaml", "--once"])

        self.assertEqual(result, 1)
        self.assertIn("Startup refused: already running", errors.getvalue())
        controller_class.assert_not_called()

    def test_status_heartbeat_uses_configured_interval(self):
        previous = datetime(2026, 9, 16, 10, 0, tzinfo=timezone.utc)
        self.assertFalse(_log_interval_elapsed(
            previous,
            datetime(2026, 9, 16, 10, 0, 59, tzinfo=timezone.utc),
            60,
        ))
        self.assertTrue(_log_interval_elapsed(
            previous,
            datetime(2026, 9, 16, 10, 1, tzinfo=timezone.utc),
            60,
        ))

    def test_runtime_text_shows_required_and_desired_pool_durations(self):
        load = LoadConfig(
            "PoolPump", "pool.local", 1300,
            target_runtime_h=8, required_runtime_ratio=0.5,
        )
        state = unittest.mock.Mock(config=load, runtime_h=2.25)
        self.assertEqual(
            _runtime_text(state),
            "runtime=2.25/4.00 h required, target=8.00 h",
        )

    def test_runtime_text_resolves_monthly_target(self):
        load = LoadConfig(
            "SeasonalLoad", "load.local", 500,
            required_runtime_ratio=0.5,
            target_runtime_h_by_month=(2, 2, 4, 6, 7, 8, 8, 8, 6, 4, 2, 2),
        )
        state = unittest.mock.Mock(config=load, runtime_h=2.25)
        self.assertEqual(
            _runtime_text(state, 9),
            "runtime=2.25/3.00 h required, target=6.00 h (month=9)",
        )

    def test_run_log_uses_configured_local_time_and_offset(self):
        output = io.StringIO()
        _run_log(
            "test line",
            ZoneInfo("Europe/Paris"),
            datetime(2026, 9, 16, 5, 0, tzinfo=timezone.utc),
            output,
        )
        self.assertEqual(
            output.getvalue(),
            "2026-09-16T07:00:00+02:00 test line\n",
        )

    @patch("victron_load_manager.main.LoadController")
    @patch("victron_load_manager.main.load_config")
    def test_once_always_runs_shutdown_guard(self, load_config_mock, controller_class):
        load_config_mock.return_value = AppConfig(
            cerbo=CerboConfig(host="localhost"),
            battery=BatteryConfig(),
            schedule=ScheduleConfig(),
            loads=(LoadConfig("SmallCumulus", "small.local", 1200),),
            managed_loads=("SmallCumulus",),
            lock_file=str(Path(tempfile.gettempdir()) / "vlm-test-main.lock"),
        )
        controller_class.return_value.cycle.return_value = unittest.mock.Mock(
            snapshot=unittest.mock.Mock(
                soc=80.0,
                ac_power_w=500.0,
                grid_lost=False,
                loads=(),
                solar_surplus_ready=True,
                energy_deficit=False,
            ),
            decisions=(),
            commands=(),
            solar_curtailment=FakeCerbo(None).read_solar_curtailment(),
            battery=BatteryDecision(
                BalanceStatus.NOT_DUE,
                BatteryRuntime(datetime(2026, 9, 1, 6, 0)),
                datetime(2026, 10, 1, 6, 0),
                90,
                28,
            ),
            observed_dvcc_a=100,
        )
        controller_class.return_value.stop_managed.return_value = (
            "DRY-RUN SmallCumulus OFF",
        )
        output = io.StringIO()
        with patch("sys.stdout", output):
            result = main(["run", "--config", "config.yaml", "--once"])
        self.assertEqual(result, 0)
        controller_class.return_value.stop_managed.assert_called_once_with()
        self.assertIn("DRY-RUN SmallCumulus OFF", output.getvalue())
        self.assertIn("Version: 0.1.0, source=", output.getvalue())
        self.assertIn("WATCHDOG RECOVERING healthy=0/2", output.getvalue())
        self.assertIn("SOC=80.0%", output.getvalue())
        self.assertIn("start_limit=4000 W", output.getvalue())
        self.assertIn("headroom=3500 W", output.getvalue())
        self.assertIn("surplus_ready=True, deficit=False", output.getvalue())
        self.assertIn("PV limiting: ESS=active, DC=limited", output.getvalue())
        self.assertIn("Power flow: grid=+30W(import)", output.getvalue())
        self.assertIn(
            "Battery: OBSERVE, balance=not_due, target=90.0%, "
            "DVCC=100A->28A",
            output.getvalue(),
        )

    @patch("victron_load_manager.main.LoadController")
    @patch("victron_load_manager.main.load_config")
    def test_termination_signal_runs_shutdown_guard(self, load_config_mock, controller_class):
        load_config_mock.return_value = AppConfig(
            cerbo=CerboConfig(host="localhost"),
            battery=BatteryConfig(),
            schedule=ScheduleConfig(),
            loads=(LoadConfig("SmallCumulus", "small.local", 1200),),
            managed_loads=("SmallCumulus",),
            lock_file=str(Path(tempfile.gettempdir()) / "vlm-test-main.lock"),
        )
        controller_class.return_value.cycle.side_effect = _ShutdownRequested("SIGTERM")
        controller_class.return_value.stop_managed.return_value = (
            "DRY-RUN SmallCumulus OFF",
        )
        output = io.StringIO()
        with patch("sys.stdout", output):
            result = main(["run", "--config", "config.yaml"])
        self.assertEqual(result, 0)
        controller_class.return_value.stop_managed.assert_called_once_with()
        self.assertIn("Shutdown requested: SIGTERM", output.getvalue())

    @patch("victron_load_manager.main.time.sleep")
    @patch("victron_load_manager.main.LoadController")
    @patch("victron_load_manager.main.load_config")
    def test_ten_modbus_failures_request_runit_restart_without_cleanup(
        self, load_config_mock, controller_class, sleep
    ):
        load_config_mock.return_value = AppConfig(
            cerbo=CerboConfig(host="localhost"),
            battery=BatteryConfig(),
            schedule=ScheduleConfig(),
            loads=(),
            lock_file=str(Path(tempfile.gettempdir()) / "vlm-test-main.lock"),
        )
        controller = controller_class.return_value
        controller.store.load_watchdog_state.return_value = {}
        controller.cycle.side_effect = ModbusError("Cerbo unavailable")
        controller.watchdog.consecutive_errors = 0

        def record_failure(_error):
            controller.watchdog.consecutive_errors += 1
            return (
                "WATCHDOG failure %d"
                % controller.watchdog.consecutive_errors,
            )

        controller.watchdog_failure.side_effect = record_failure
        with patch("sys.stderr", io.StringIO()):
            result = main(["run", "--config", "config.yaml"])

        self.assertEqual(result, 75)
        self.assertEqual(controller.watchdog_failure.call_count, 10)
        controller.watchdog_restart_requested.assert_called_once_with()
        controller.stop_managed.assert_not_called()
        self.assertEqual(sleep.call_count, 9)


if __name__ == "__main__":
    unittest.main()
