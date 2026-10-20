"""Solar anticipation, deadline recovery and load arbitration regressions."""

from dataclasses import replace
from datetime import datetime, timedelta
import unittest
from unittest.mock import patch

from versatile_load_manager.battery import BatteryPolicy, BatteryRuntime, BalanceStatus
from versatile_load_manager.config import BatteryConfig, ScheduleConfig, ConfigError, load_config
from versatile_load_manager.controller import LoadController
from versatile_load_manager.modbus import SolarCurtailment
from versatile_load_manager.policy import Action, LoadPolicy, LoadState, Reason, Snapshot
from tests import test_config, test_controller
from tests.test_controller import FakeCerbo, FakeShelly


class SolarBalanceTests(unittest.TestCase):
    def setUp(self):
        self.config = BatteryConfig(balance_advance_days=3)
        self.policy = BatteryPolicy(self.config, ScheduleConfig())
        self.now = datetime(2026, 10, 15, 10)
        # Due on the 18th in the evening: the entire 15th is eligible.
        self.runtime = BatteryRuntime(datetime(2026, 9, 18, 21, 23))

    def evaluate(self, now=None, soc=55, runtime=None, **kw):
        return self.policy.evaluate(
            now or self.now, soc, False, False,
            runtime or self.runtime, **kw,
        )

    def test_early_charge_below_normal_ceiling_without_forcing_grid(self):
        decision = self.evaluate(solar_opportunity=True)
        self.assertEqual(decision.status, BalanceStatus.CHARGING)
        self.assertTrue(decision.solar_priority)
        self.assertEqual(decision.target_soc, 100)
        self.assertIsNone(decision.min_soc_target)
        self.assertEqual(decision.runtime.last_completed_at, self.runtime.last_completed_at)

    def test_no_attempt_before_window_without_opportunity_or_outside_solar(self):
        for now, opportunity in (
            (self.now - timedelta(days=1), True),
            (self.now, False),
            (self.now.replace(hour=9), True),
            (self.now.replace(hour=22), True),
        ):
            with self.subTest(now=now):
                decision = self.evaluate(now, solar_opportunity=opportunity)
                self.assertFalse(decision.solar_priority)
                self.assertFalse(decision.campaign_active)

    def test_restart_retains_priority_without_a_new_forecast(self):
        started = self.evaluate(solar_opportunity=True)
        restored = self.policy.initial_runtime(self.now, started.runtime.to_payload())
        decision = self.evaluate(runtime=restored)
        self.assertTrue(decision.solar_priority)
        self.assertEqual(decision.status, BalanceStatus.CHARGING)

    def test_deadline_releases_loads_and_keeps_original_due_date(self):
        started = self.evaluate(solar_opportunity=True)
        stopped = self.evaluate(self.now.replace(hour=16), runtime=started.runtime)
        self.assertEqual(stopped.status, BalanceStatus.PAUSED_SOLAR_DEADLINE)
        self.assertFalse(stopped.solar_priority)
        self.assertFalse(stopped.campaign_active)
        self.assertEqual(stopped.due_at, started.due_at)
        self.assertEqual(stopped.target_soc, 90)
        retry = self.evaluate(self.now.replace(hour=17), runtime=stopped.runtime, solar_opportunity=True)
        self.assertFalse(retry.solar_priority)

    def test_holding_can_finish_after_deadline_then_releases_priority(self):
        started = self.evaluate(solar_opportunity=True)
        held = self.evaluate(self.now.replace(hour=15, minute=30), 100, started.runtime)
        self.assertEqual(held.status, BalanceStatus.HOLDING)
        continued = self.evaluate(self.now.replace(hour=16), 100, held.runtime)
        self.assertTrue(continued.solar_priority)
        completed = self.evaluate(self.now.replace(hour=16, minute=30), 100, continued.runtime)
        self.assertEqual(completed.status, BalanceStatus.COMPLETED)
        self.assertFalse(completed.solar_priority)
        self.assertEqual(completed.due_at, self.now.replace(hour=16, minute=30) + timedelta(days=30))

    def test_dropped_target_after_deadline_aborts_without_completion(self):
        started = self.evaluate(solar_opportunity=True)
        held = self.evaluate(self.now.replace(hour=15, minute=30), 100, started.runtime)
        aborted = self.evaluate(self.now.replace(hour=16), 99, held.runtime)
        self.assertEqual(aborted.status, BalanceStatus.PAUSED_SOLAR_DEADLINE)
        self.assertEqual(aborted.runtime.last_completed_at, self.runtime.last_completed_at)

    def test_next_day_requalifies_instead_of_blindly_resuming(self):
        started = self.evaluate(solar_opportunity=True)
        tomorrow = self.now + timedelta(days=1)
        waiting = self.evaluate(tomorrow, runtime=started.runtime)
        self.assertFalse(waiting.campaign_active)
        self.assertFalse(waiting.solar_priority)
        retry = self.evaluate(tomorrow, runtime=waiting.runtime, solar_opportunity=True)
        self.assertTrue(retry.solar_priority)

    def test_deficit_interrupts_hold_without_releasing_priority_before_deadline(self):
        started = self.evaluate(solar_opportunity=True)
        held = self.evaluate(self.now.replace(hour=12), 100, started.runtime)
        paused = self.policy.evaluate(
            self.now.replace(hour=12, minute=5), 99, False, True, held.runtime,
        )
        self.assertEqual(paused.status, BalanceStatus.PAUSED_DEFICIT)
        self.assertTrue(paused.solar_priority)
        self.assertIsNone(paused.runtime.target_reached_at)
        self.assertEqual(paused.runtime.last_completed_at, self.runtime.last_completed_at)

    def test_due_date_still_needed_for_hc_catchup(self):
        night = self.now.replace(hour=23)
        decision = self.evaluate(night, hc_catchup_authorized=True)
        self.assertFalse(decision.campaign_active)
        self.assertIsNone(decision.min_soc_target)

    def test_exclusive_priority_blocks_even_large_surplus_and_stops_owned_load(self):
        helper = test_controller.ControllerTests(); helper.setUp()
        try:
            config = helper.config()
            load = helper.small
            for on in (False, True):
                snapshot = Snapshot(
                    self.now, 95, 2000, False,
                    (LoadState(load, on, 0, output_duration_s=1000),),
                    solar_surplus_ready=True, visible_surplus_w=5000,
                    battery_priority=True, battery_holding=True,
                    battery_priority_exclusive=True,
                )
                decision = LoadPolicy(config).evaluate(snapshot)[0]
                self.assertEqual(decision.reason, Reason.BATTERY_BALANCING)
                self.assertEqual(decision.action, Action.STOP if on else Action.KEEP_OFF)
                released = LoadPolicy(config).evaluate(replace(
                    snapshot, battery_priority_exclusive=False, battery_priority=False,
                ))[0]
                self.assertNotEqual(released.reason, Reason.BATTERY_BALANCING)
        finally:
            helper.tearDown()

    def test_controller_requires_good_forecast_and_measured_solar(self):
        helper = test_controller.ControllerTests(); helper.setUp()
        try:
            base = helper.config(control=True, dry_run=False)
            config = replace(base, battery=replace(
                base.battery, control_enabled=True, balance_advance_days=3,
                balance_reference_date="2026-09-18",
            ))
            controller = LoadController(config, FakeCerbo, FakeShelly, now=lambda: self.now)
            # No forecast: native curtailment alone cannot start early.
            controller.cycle(); waiting = controller.cycle()
            self.assertFalse(waiting.battery.solar_priority)
            for forecast in (5, float("nan")):
                self.assertFalse(controller.cycle(today_pv_kwh=forecast).battery.solar_priority)
            result = controller.cycle(today_pv_kwh=25)
            self.assertTrue(result.battery.solar_priority)
            self.assertTrue(result.snapshot.battery_priority_exclusive)
            self.assertFalse(any(c.endswith(" ON") for c in result.commands))
            # Grid loss must never authorize a new early attempt.
            controller._battery_runtime = self.runtime
            FakeCerbo.grid_lost = True
            result = controller.cycle(today_pv_kwh=25)
            self.assertFalse(result.battery.solar_priority)
        finally:
            FakeCerbo.grid_lost = False
            helper.tearDown()

    def test_measured_charging_can_select_day_without_existing_curtailment(self):
        helper = test_controller.ControllerTests(); helper.setUp()
        try:
            base = helper.config(control=True, dry_run=False)
            config = replace(base, battery=replace(
                base.battery, control_enabled=True, balance_advance_days=3,
                balance_reference_date="2026-09-18",
            ))
            controller = LoadController(config, FakeCerbo, FakeShelly, now=lambda: self.now)
            charging = SolarCurtailment(False, grid_power_w=50, battery_power_w=1500)
            with patch.object(FakeCerbo, "read_solar_curtailment", return_value=charging):
                first = controller.cycle(today_pv_kwh=25)
                self.assertFalse(first.battery.solar_priority)
                second = controller.cycle(today_pv_kwh=25)
            self.assertTrue(second.battery.solar_priority)
            self.assertIsNone(second.battery.min_soc_target)
        finally:
            helper.tearDown()

    def test_config_validation_and_backward_compatible_defaults(self):
        helper = test_config.ConfigTests()
        def parse(battery):
            path = helper.write_config({"cerbo": {"host": "localhost"}, "battery": battery})
            try:
                return load_config(path).battery
            finally:
                path.unlink()
        self.assertEqual(parse({}).balance_advance_days, 0)
        self.assertEqual(parse({"balance_advance_days": 3}).balance_solar_deadline, "16:00")
        for invalid in (-1, 30, 3.5, True):
            with self.subTest(value=invalid), self.assertRaises(ConfigError):
                parse({"balance_advance_days": invalid})
        with self.assertRaises(ConfigError):
            parse({"balance_advance_days": 3, "balance_solar_deadline": "09:00"})
