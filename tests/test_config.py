import tempfile
import unittest
from datetime import datetime
from pathlib import Path

import yaml

from victron_load_manager.config import (
    ConfigError, load_config, runtime_period_month,
)


class ConfigTests(unittest.TestCase):
    def write_config(self, data):
        temp = tempfile.NamedTemporaryFile("w", suffix=".yaml", delete=False)
        with temp:
            yaml.safe_dump(data, temp)
        return Path(temp.name)

    def test_minimal_config_uses_defaults_and_auto_discovery(self):
        path = self.write_config({"cerbo": {"host": "localhost"}})
        config = load_config(path)
        self.assertEqual(config.cerbo.host, "localhost")
        self.assertIsNone(config.cerbo.system_unit_id)
        self.assertIsNone(config.cerbo.vebus_unit_id)
        self.assertEqual(config.battery.soc_max, 90)
        self.assertEqual(config.battery.max_charge_current_a, 100)
        self.assertFalse(config.battery.control_enabled)
        self.assertEqual(config.battery.charge_hysteresis_soc, 1)
        self.assertEqual(config.battery.load_start_hysteresis_soc, 1)
        self.assertEqual(config.battery.balance_target_soc, 100)
        self.assertEqual(config.battery.balance_hold_s, 3600)
        self.assertIsNone(config.battery.balance_reference_date)
        self.assertEqual(config.surplus_confirm_cycles, 2)
        self.assertEqual(config.deficit_confirm_cycles, 2)
        self.assertEqual(config.grid_import_tolerance_w, 100)
        self.assertEqual(config.battery_discharge_tolerance_w, 200)
        self.assertEqual(config.battery_charge_reserve_w, 1000)
        self.assertEqual(config.start_settle_s, 30)
        self.assertEqual(config.status_log_interval_s, 60)
        self.assertEqual(config.watchdog.safe_after_errors, 3)
        self.assertEqual(config.watchdog.recovery_cycles, 2)
        self.assertEqual(config.watchdog.restart_after_errors, 10)
        self.assertEqual(config.lock_file, "/var/run/victron-load-manager.lock")
        self.assertFalse(config.tempo.enabled)
        self.assertEqual(config.tempo.provider, "api-couleur-tempo.fr")
        self.assertEqual(config.tempo.refresh_interval_s, 3600)
        self.assertEqual(config.schedule.timezone, "UTC")
        self.assertFalse(config.solar_forecast.enabled)
        self.assertEqual(config.loads, ())

    def test_watchdog_is_parsed_and_validated(self):
        path = self.write_config({
            "cerbo": {"host": "localhost"},
            "watchdog": {
                "safe_after_errors": 4,
                "recovery_cycles": 3,
                "restart_after_errors": 12,
            },
        })
        watchdog = load_config(path).watchdog
        self.assertEqual(watchdog.safe_after_errors, 4)
        self.assertEqual(watchdog.recovery_cycles, 3)
        self.assertEqual(watchdog.restart_after_errors, 12)

        for values in (
            {"safe_after_errors": 0},
            {"recovery_cycles": 0},
            {"restart_after_errors": 0},
            {"restart_after_errors": 10.5},
            {"safe_after_errors": 4, "restart_after_errors": 3},
        ):
            with self.subTest(values=values):
                path = self.write_config({
                    "cerbo": {"host": "localhost"},
                    "watchdog": values,
                })
                with self.assertRaisesRegex(ConfigError, "watchdog"):
                    load_config(path)

    def test_hc_balance_is_opt_in_and_requires_installation_limits(self):
        path = self.write_config({"cerbo": {"host": "localhost"}})
        battery = load_config(path).battery
        self.assertFalse(battery.balance_hc_enabled)
        self.assertEqual(battery.balance_solar_defer_days, 14)

        path = self.write_config({
            "cerbo": {"host": "localhost"},
            "battery": {"balance_hc_enabled": True},
        })
        with self.assertRaisesRegex(ConfigError, "HC balancing requires"):
            load_config(path)

        path = self.write_config({
            "cerbo": {"host": "localhost"},
            "battery": {
                "balance_hc_enabled": True,
                "usable_capacity_kwh": 10.0,
                "max_grid_charge_power_w": 4000,
                "night_base_load_w": 500,
                "balance_good_solar_day_kwh": 15,
            },
        })
        battery = load_config(path).battery
        self.assertTrue(battery.balance_hc_enabled)
        self.assertEqual(battery.usable_capacity_kwh, 10.0)
        self.assertEqual(battery.max_grid_charge_power_w, 4000)
        self.assertEqual(battery.night_base_load_w, 500)

    def test_solar_forecast_is_parsed(self):
        path = self.write_config({
            "cerbo": {"host": "localhost"},
            "solar_forecast": {
                "enabled": True,
                "latitude": 45.0,
                "longitude": 4.0,
                "peak_power_kwp": 8,
                "tilt_deg": 24,
                "azimuth_deg": 217,
                "performance_ratio": 0.85,
            },
        })
        forecast = load_config(path).solar_forecast
        self.assertTrue(forecast.enabled)
        self.assertEqual(forecast.peak_power_kwp, 8)
        self.assertEqual(forecast.azimuth_deg, 217)

    def test_enabled_solar_forecast_requires_peak_power(self):
        path = self.write_config({
            "cerbo": {"host": "localhost"},
            "solar_forecast": {"enabled": True},
        })
        with self.assertRaisesRegex(ConfigError, "missing"):
            load_config(path)

    def test_solar_forecast_intervals_require_integers(self):
        path = self.write_config({
            "cerbo": {"host": "localhost"},
            "solar_forecast": {"refresh_interval_s": 2.5},
        })
        with self.assertRaisesRegex(ConfigError, "refresh_interval_s"):
            load_config(path)

    def test_runtime_target_ratio_and_log_timing_are_parsed(self):
        path = self.write_config({
            "cerbo": {"host": "localhost"},
            "start_settle_s": 45,
            "status_log_interval_s": 120,
            "loads": [{
                "name": "PoolPump",
                "host": "pool.local",
                "rated_w": 1300,
                "target_runtime_h": 8,
                "required_runtime_ratio": 0.5,
            }],
        })
        config = load_config(path)
        self.assertEqual(config.start_settle_s, 45)
        self.assertEqual(config.status_log_interval_s, 120)
        self.assertEqual(config.loads[0].target_runtime_h, 8)
        self.assertEqual(config.loads[0].required_runtime_h, 4)

    def test_target_finish_settings_are_parsed(self):
        path = self.write_config({
            "cerbo": {"host": "localhost"},
            "loads": [{
                "name": "PoolPump",
                "host": "pool.local",
                "rated_w": 1300,
                "target_runtime_h": 6,
                "required_runtime_ratio": 0.5,
                "target_finish_enabled": True,
                "target_finish_max_remaining_h": 0.75,
                "target_finish_min_soc": 82,
                "target_finish_max_battery_discharge_w": 450,
                "target_finish_grace_s": 600,
            }],
        })
        load = load_config(path).loads[0]
        self.assertTrue(load.target_finish_enabled)
        self.assertEqual(load.target_finish_max_remaining_h, 0.75)
        self.assertEqual(load.target_finish_min_soc, 82)
        self.assertEqual(load.target_finish_max_battery_discharge_w, 450)
        self.assertEqual(load.target_finish_grace_s, 600)

    def test_tempo_observation_settings_are_parsed(self):
        path = self.write_config({
            "cerbo": {"host": "localhost"},
            "tempo": {
                "enabled": True,
                "cache_file": "/tmp/tempo.json",
                "refresh_interval_s": 1800,
                "retry_interval_s": 120,
                "fresh_for_s": 3600,
                "timeout_s": 3,
            },
        })
        tempo = load_config(path).tempo
        self.assertTrue(tempo.enabled)
        self.assertEqual(tempo.cache_file, "/tmp/tempo.json")
        self.assertEqual(tempo.refresh_interval_s, 1800)
        self.assertEqual(tempo.retry_interval_s, 120)
        self.assertEqual(tempo.fresh_for_s, 3600)
        self.assertEqual(tempo.timeout_s, 3)

    def test_lock_file_is_configurable_and_validated(self):
        path = self.write_config({
            "cerbo": {"host": "localhost"},
            "lock_file": "/tmp/vlm-pc.lock",
        })
        self.assertEqual(load_config(path).lock_file, "/tmp/vlm-pc.lock")

        path = self.write_config({
            "cerbo": {"host": "localhost"}, "lock_file": "",
        })
        with self.assertRaisesRegex(ConfigError, "lock_file"):
            load_config(path)

    def test_tempo_settings_are_validated(self):
        for settings, message in (
            ({"enabled": "yes"}, "tempo.enabled"),
            ({"provider": "unknown"}, "tempo.provider"),
            ({"refresh_interval_s": 30}, "tempo.refresh_interval_s"),
            ({"timeout_s": 0}, "tempo.timeout_s"),
        ):
            path = self.write_config({
                "cerbo": {"host": "localhost"}, "tempo": settings,
            })
            with self.assertRaisesRegex(ConfigError, message):
                load_config(path)

    def test_target_finish_requires_a_runtime_target(self):
        path = self.write_config({
            "cerbo": {"host": "localhost"},
            "loads": [{
                "name": "PoolPump",
                "host": "pool.local",
                "rated_w": 1300,
                "target_finish_enabled": True,
            }],
        })
        with self.assertRaisesRegex(ConfigError, "requires target_runtime_h"):
            load_config(path)

    def test_monthly_runtime_target_is_generic_and_complete(self):
        targets = {month: float(month) for month in range(1, 13)}
        path = self.write_config({
            "cerbo": {"host": "localhost"},
            "loads": [{
                "name": "SeasonalLoad",
                "host": "load.local",
                "rated_w": 500,
                "target_runtime_h_by_month": targets,
                "required_runtime_ratio": 0.5,
            }],
        })
        load = load_config(path).loads[0]
        self.assertEqual(load.target_runtime_h_for_month(9), 9)
        self.assertEqual(load.required_runtime_h_for_month(9), 4.5)
        with self.assertRaisesRegex(ValueError, "for_month"):
            _ = load.required_runtime_h

    def test_monthly_target_requires_all_integer_months(self):
        path = self.write_config({
            "cerbo": {"host": "localhost"},
            "loads": [{
                "name": "SeasonalLoad",
                "host": "load.local",
                "rated_w": 500,
                "target_runtime_h_by_month": {
                    month: 2 for month in range(1, 12)
                },
            }],
        })
        with self.assertRaisesRegex(ConfigError, "months 1 through 12"):
            load_config(path)

    def test_fixed_and_monthly_targets_are_mutually_exclusive(self):
        path = self.write_config({
            "cerbo": {"host": "localhost"},
            "loads": [{
                "name": "SeasonalLoad",
                "host": "load.local",
                "rated_w": 500,
                "target_runtime_h": 4,
                "target_runtime_h_by_month": {
                    month: 2 for month in range(1, 13)
                },
            }],
        })
        with self.assertRaisesRegex(ConfigError, "cannot combine"):
            load_config(path)

    def test_runtime_month_changes_at_daily_reset_not_midnight(self):
        self.assertEqual(
            runtime_period_month(datetime(2026, 3, 1, 5, 59), "06:00"), 2
        )
        self.assertEqual(
            runtime_period_month(datetime(2026, 3, 1, 6, 0), "06:00"), 3
        )

    def test_runtime_target_ratio_requires_a_target(self):
        path = self.write_config({
            "cerbo": {"host": "localhost"},
            "loads": [{
                "name": "PoolPump",
                "host": "pool.local",
                "rated_w": 1300,
                "required_runtime_ratio": 0.5,
            }],
        })
        with self.assertRaisesRegex(ConfigError, "requires target_runtime_h"):
            load_config(path)

    def test_start_settle_and_log_interval_require_positive_integers(self):
        for key, value in (("start_settle_s", 0), ("status_log_interval_s", 2.5)):
            path = self.write_config({
                "cerbo": {"host": "localhost"},
                key: value,
            })
            with self.assertRaisesRegex(ConfigError, key):
                load_config(path)

    def test_schedule_timezone_is_parsed_and_validated(self):
        path = self.write_config({
            "cerbo": {"host": "localhost"},
            "schedule": {"timezone": "Europe/Paris"},
        })
        self.assertEqual(load_config(path).schedule.timezone, "Europe/Paris")

        path = self.write_config({
            "cerbo": {"host": "localhost"},
            "schedule": {"timezone": "Mars/Olympus"},
        })
        with self.assertRaisesRegex(ConfigError, "schedule.timezone"):
            load_config(path)

    def test_startup_power_defaults_to_rated_power(self):
        path = self.write_config({
            "cerbo": {"host": "localhost"},
            "loads": [{
                "name": "Pump",
                "host": "pump.local",
                "rated_w": 1300,
            }],
        })
        load = load_config(path).loads[0]
        self.assertIsNone(load.startup_power_w)
        self.assertEqual(load.estimated_start_power_w, 1300)

    def test_load_completion_defaults_to_runtime(self):
        path = self.write_config({
            "cerbo": {"host": "localhost"},
            "loads": [{
                "name": "Pump",
                "host": "pump.local",
                "rated_w": 1200,
            }],
        })
        config = load_config(path)
        self.assertEqual(config.loads[0].completion_mode, "runtime")

    def test_complete_load_is_parsed(self):
        path = self.write_config({
            "cerbo": {"host": "einstein", "system_unit_id": 100},
            "loads": [{
                "name": "Cumulus",
                "host": "cumulus.local",
                "rated_w": 2200,
                "priority": 10,
                "min_daily_runtime_h": 2.5,
                "min_active_power_w": 800,
                "power_confirm_timeout_s": 4,
                "probe_retry_delay_s": 900,
                "probe_failure_mode": "lockout",
                "min_on_time_s": 900,
                "min_off_time_s": 300,
                "completion_mode": "thermostat",
                "startup_power_w": 2400,
            }],
        })
        config = load_config(path)
        self.assertEqual(config.cerbo.system_unit_id, 100)
        self.assertEqual(config.loads[0].min_daily_runtime_h, 2.5)
        self.assertEqual(config.loads[0].min_active_power_w, 800)
        self.assertEqual(config.loads[0].power_confirm_timeout_s, 4)
        self.assertEqual(config.loads[0].probe_retry_delay_s, 900)
        self.assertEqual(config.loads[0].probe_failure_mode, "lockout")
        self.assertEqual(config.loads[0].min_on_time_s, 900)
        self.assertEqual(config.loads[0].min_off_time_s, 300)
        self.assertEqual(config.loads[0].completion_mode, "thermostat")
        self.assertEqual(config.loads[0].startup_power_w, 2400)
        self.assertEqual(config.loads[0].estimated_start_power_w, 2400)

    def test_load_power_confirmation_settings_are_validated(self):
        path = self.write_config({
            "cerbo": {"host": "localhost"},
            "loads": [{
                "name": "Cumulus",
                "host": "cumulus.local",
                "rated_w": 1200,
                "min_active_power_w": 1300,
            }],
        })
        with self.assertRaisesRegex(ConfigError, "min_active_power_w"):
            load_config(path)

        path = self.write_config({
            "cerbo": {"host": "localhost"},
            "loads": [{
                "name": "Cumulus",
                "host": "cumulus.local",
                "rated_w": 1200,
                "probe_retry_delay_s": 0,
            }],
        })
        with self.assertRaisesRegex(ConfigError, "probe_retry_delay_s"):
            load_config(path)

        path = self.write_config({
            "cerbo": {"host": "localhost"},
            "loads": [{
                "name": "Pump",
                "host": "pump.local",
                "rated_w": 1200,
                "probe_failure_mode": "retry_forever",
            }],
        })
        with self.assertRaisesRegex(ConfigError, "probe_failure_mode"):
            load_config(path)

    def test_minimum_switch_times_are_validated(self):
        for key, value in (("min_on_time_s", -1), ("min_off_time_s", 1.5)):
            path = self.write_config({
                "cerbo": {"host": "localhost"},
                "loads": [{
                    "name": "Pump",
                    "host": "pump.local",
                    "rated_w": 1200,
                    key: value,
                }],
            })
            with self.assertRaisesRegex(ConfigError, key):
                load_config(path)

    def test_example_water_heaters_have_anti_cycling_delays(self):
        path = Path(__file__).parents[1] / "config" / "config.example.yaml"
        loads = {load.name: load for load in load_config(path).loads}
        for name in ("MainCumulus", "SmallCumulus"):
            self.assertEqual(loads[name].min_on_time_s, 120)
            self.assertEqual(loads[name].min_off_time_s, 120)

    def test_example_pool_finish_grace_reaches_nineteen_hundred(self):
        path = Path(__file__).parents[1] / "config" / "config.example.yaml"
        loads = {load.name: load for load in load_config(path).loads}
        self.assertTrue(loads["PoolPump"].target_finish_enabled)
        self.assertEqual(loads["PoolPump"].target_finish_grace_s, 3600)

    def test_completion_mode_is_validated(self):
        path = self.write_config({
            "cerbo": {"host": "localhost"},
            "loads": [{
                "name": "Cumulus",
                "host": "cumulus.local",
                "rated_w": 1200,
                "completion_mode": "temperature_guess",
            }],
        })
        with self.assertRaisesRegex(ConfigError, "completion_mode"):
            load_config(path)

    def test_startup_power_cannot_be_lower_than_rated_power(self):
        path = self.write_config({
            "cerbo": {"host": "localhost"},
            "loads": [{
                "name": "Pump",
                "host": "pump.local",
                "rated_w": 1300,
                "startup_power_w": 1200,
            }],
        })
        with self.assertRaisesRegex(ConfigError, "startup_power_w"):
            load_config(path)

    def test_duplicate_load_names_are_rejected(self):
        load = {"name": "Same", "host": "one.local", "rated_w": 1000}
        path = self.write_config({"cerbo": {"host": "localhost"}, "loads": [load, load]})
        with self.assertRaisesRegex(ConfigError, "duplicate load name"):
            load_config(path)

    def test_inconsistent_soc_limits_are_rejected(self):
        path = self.write_config({
            "cerbo": {"host": "localhost"},
            "battery": {"soc_min": 95, "soc_max": 90},
        })
        with self.assertRaisesRegex(ConfigError, "SOC limits"):
            load_config(path)

        path = self.write_config({
            "cerbo": {"host": "localhost"},
            "battery": {
                "soc_min": 40,
                "soc_on": 40.5,
                "load_start_hysteresis_soc": 1,
            },
        })
        with self.assertRaisesRegex(ConfigError, "load_start_hysteresis_soc"):
            load_config(path)

    def test_battery_balancing_settings_are_parsed_and_validated(self):
        path = self.write_config({
            "cerbo": {"host": "localhost"},
            "battery": {
                "control_enabled": True,
                "charge_hysteresis_soc": 1.5,
                "balance_target_soc": 100,
                "balance_hold_s": 1800,
                "balance_reference_date": "2026-08-18",
            },
        })
        battery = load_config(path).battery
        self.assertTrue(battery.control_enabled)
        self.assertEqual(battery.charge_hysteresis_soc, 1.5)
        self.assertEqual(battery.balance_target_soc, 100)
        self.assertEqual(battery.balance_hold_s, 1800)
        self.assertEqual(battery.balance_reference_date, "2026-08-18")

        for key, value in (
            ("balance_target_soc", 90),
            ("balance_hold_s", -1),
            ("balance_reference_date", "18/08/2026"),
            ("control_enabled", "yes"),
            ("charge_hysteresis_soc", 0),
            ("balance_hold_s", 1.5),
        ):
            path = self.write_config({
                "cerbo": {"host": "localhost"},
                "battery": {key: value},
            })
            with self.assertRaisesRegex(ConfigError, "battery|balance target"):
                load_config(path)

    def test_bad_clock_is_rejected(self):
        path = self.write_config({
            "cerbo": {"host": "localhost"},
            "schedule": {"daily_reset": "29:00"},
        })
        with self.assertRaisesRegex(ConfigError, "valid time"):
            load_config(path)

    def test_ac_start_margin_must_be_below_ac_limit(self):
        path = self.write_config({
            "cerbo": {"host": "localhost"},
            "max_ac_power_w": 6500,
            "ac_start_margin_w": 6500,
        })
        with self.assertRaisesRegex(ConfigError, "AC start margin"):
            load_config(path)

    def test_flow_confirmation_cycles_must_be_positive_integers(self):
        path = self.write_config({
            "cerbo": {"host": "localhost"},
            "surplus_confirm_cycles": 0,
        })
        with self.assertRaisesRegex(ConfigError, "confirmation cycles"):
            load_config(path)

    def test_power_flow_tolerances_must_be_non_negative(self):
        path = self.write_config({
            "cerbo": {"host": "localhost"},
            "grid_import_tolerance_w": -1,
        })
        with self.assertRaisesRegex(ConfigError, "power-flow tolerances"):
            load_config(path)

        path = self.write_config({
            "cerbo": {"host": "localhost"},
            "battery_charge_reserve_w": -1,
        })
        with self.assertRaisesRegex(ConfigError, "power-flow tolerances"):
            load_config(path)


    def test_minimal_example_is_valid_and_read_only(self):
        path = Path(__file__).parents[1] / "config" / "config.minimal.yaml"
        config = load_config(path)
        self.assertTrue(config.dry_run)
        self.assertFalse(config.control_enabled)
        self.assertFalse(config.battery.control_enabled)
        self.assertEqual(config.managed_loads, ())
        self.assertEqual(len(config.loads), 1)

if __name__ == "__main__":
    unittest.main()


