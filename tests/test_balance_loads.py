"""Regression cases from the 16 October solar balancing cycle."""

from dataclasses import replace
from datetime import datetime, timedelta
import unittest
from unittest.mock import patch

from victron_load_manager.balance_loads import qualify_balance_surplus, post_balance_deadline
from victron_load_manager.battery import BatteryPolicy, BatteryRuntime, BalanceStatus
from victron_load_manager.config import (
    AppConfig, BatteryConfig, CerboConfig, ScheduleConfig, LoadConfig, ConfigError, load_config,
)
from victron_load_manager.controller import LoadController
from victron_load_manager.modbus import SolarCurtailment
from victron_load_manager.policy import LoadPolicy, LoadState, Snapshot, Action, Reason
from tests import test_config, test_controller


class BalanceLoadTests(unittest.TestCase):
    def setUp(self):
        self.now = datetime(2026, 10, 16, 18, 0)
        self.cfg = AppConfig(
            CerboConfig("localhost"), BatteryConfig(
                balance_surplus_enabled=True, post_balance_support_enabled=True,
            ), ScheduleConfig(),
            (LoadConfig("Main", "main", 2200, priority=10, completion_mode="thermostat"),
             LoadConfig("Pump", "pump", 1200, priority=30, target_runtime_h=4,
                        min_daily_runtime_h=2, min_on_time_s=900)),
            max_ac_power_w=6500, ac_start_margin_w=500,
        )
        self.until = self.now.replace(hour=21)

    def snapshot(self, **changes):
        return replace(Snapshot(
            self.now, 100, 1700, False,
            (LoadState(self.cfg.loads[1], True, 1.62, output_duration_s=1000),),
            battery_power_w=-1000, grid_power_w=30, post_balance_until=self.until,
        ), **changes)

    def decision(self, **changes):
        return LoadPolicy(self.cfg).evaluate(self.snapshot(**changes))[0]

    def qualify(self, **changes):
        args = dict(
            enabled=True, balancing=True, curtailment_confirmed=True,
            deficit=False, settled=True, battery_power_w=250, grid_power_w=20,
            reference_charge_w=None, charge_tolerance_w=150, grid_tolerance_w=100,
        )
        args.update(changes)
        return qualify_balance_surplus(**args)

    def test_curtailment_qualifies_trial_and_keeps_reference_during_settling(self):
        trial = self.qualify()
        self.assertTrue(trial.start_allowed)
        self.assertEqual(trial.reference_charge_w, 250)
        retained = self.qualify(settled=False, battery_power_w=200, reference_charge_w=250)
        self.assertFalse(retained.start_allowed)
        self.assertTrue(retained.keep_allowed)
        self.assertEqual(retained.reference_charge_w, 250)

    def test_trial_is_withdrawn_when_it_takes_battery_charge_or_grid_energy(self):
        for changes in (
            dict(battery_power_w=0), dict(grid_power_w=300),
            dict(deficit=True), dict(battery_power_w=None), dict(grid_power_w=float("nan")),
        ):
            with self.subTest(changes=changes):
                result = self.qualify(
                    curtailment_confirmed=False, reference_charge_w=250, **changes,
                )
                self.assertFalse(result.keep_allowed)
                self.assertFalse(result.start_allowed)

    def test_natural_bms_taper_updates_reference_only_with_curtailment(self):
        result = self.qualify(reference_charge_w=2000, battery_power_w=200)
        self.assertEqual(result.reference_charge_w, 200)
        self.assertTrue(result.keep_allowed)
        unknown = self.qualify(curtailment_confirmed=False, reference_charge_w=None)
        self.assertFalse(unknown.keep_allowed)

    def test_disabled_or_finished_balance_cannot_authorize_trials(self):
        for changes in (dict(enabled=False), dict(balancing=False)):
            result = self.qualify(reference_charge_w=200, **changes)
            self.assertFalse(result.start_allowed)
            self.assertFalse(result.keep_allowed)
            self.assertIsNone(result.reference_charge_w)

    def test_exclusive_balance_can_trial_one_load_but_still_protects_hold(self):
        snapshot = self.snapshot(
            now=self.now.replace(hour=14), post_balance_until=None,
            loads=tuple(LoadState(load, False) for load in self.cfg.loads),
            battery_priority=True, battery_priority_exclusive=True, battery_holding=True,
            balance_surplus_start_allowed=True,
        )
        decisions = LoadPolicy(self.cfg).evaluate(snapshot)
        self.assertEqual(sum(d.action == Action.START for d in decisions), 1)
        self.assertEqual(decisions[0].reason, Reason.BALANCING_SURPLUS)
        # Withdraw a failed trial even during minimum ON time.
        guarded = replace(snapshot, loads=(LoadState(self.cfg.loads[1], True, output_duration_s=10),))
        self.assertEqual(LoadPolicy(self.cfg).evaluate(guarded)[0].action, Action.STOP)
        held = replace(guarded, balance_surplus_keep_allowed=True)
        self.assertEqual(LoadPolicy(self.cfg).evaluate(held)[0].action, Action.KEEP_ON)

    def test_success_is_required_not_initial_reference_or_previous_day(self):
        self.assertIsNone(post_balance_deadline(self.now, None, self.cfg.battery, self.cfg.schedule))
        self.assertIsNone(post_balance_deadline(
            self.now, self.now - timedelta(days=1), self.cfg.battery, self.cfg.schedule,
        ))
        self.assertEqual(post_balance_deadline(
            self.now, self.now.replace(hour=16), self.cfg.battery, self.cfg.schedule,
        ), self.until)
        long = replace(self.cfg.battery, post_balance_extension_s=86400)
        self.assertEqual(post_balance_deadline(
            self.now, self.now.replace(hour=16), long, self.cfg.schedule,
        ), self.now.replace(hour=22))

    def test_completed_hold_records_success_and_survives_restart(self):
        policy = BatteryPolicy(replace(self.cfg.battery, balance_hold_s=0), self.cfg.schedule)
        now = self.now.replace(hour=15)
        runtime = BatteryRuntime(now - timedelta(days=31))
        initialized = policy.initial_runtime(now, {})
        self.assertIsNone(initialized.last_success_at)
        result = policy.evaluate(now, 100, True, False, runtime)
        self.assertEqual(result.status, BalanceStatus.COMPLETED)
        restored = policy.initial_runtime(now, result.runtime.to_payload())
        self.assertEqual(restored.last_success_at, now)

    def test_1650_small_deficit_keeps_loads_after_success(self):
        states = tuple(LoadState(load, True, .55, output_duration_s=2000) for load in self.cfg.loads)
        snapshot = self.snapshot(
            now=self.now.replace(hour=16, minute=50), loads=states,
            energy_deficit=True, battery_power_w=-280, grid_power_w=56,
        )
        decisions = LoadPolicy(self.cfg).evaluate(snapshot)
        self.assertTrue(all(d.action == Action.KEEP_ON for d in decisions))
        self.assertTrue(all(d.reason == Reason.POST_BALANCE_SUPPORT for d in decisions))

    def test_1800_pump_continues_until_target_not_just_required_minimum(self):
        self.assertEqual(self.decision().reason, Reason.POST_BALANCE_SUPPORT)
        done = LoadState(self.cfg.loads[1], True, 4.0, output_duration_s=1000)
        self.assertEqual(self.decision(loads=(done,)).reason, Reason.RUNTIME_COMPLETE)
        between = replace(done, runtime_h=2.5)
        self.assertEqual(self.decision(loads=(between,)).action, Action.KEEP_ON)

    def test_soc_grid_and_deadline_override_minimum_on_time(self):
        running = (LoadState(self.cfg.loads[1], True, 1.62, output_duration_s=10),)
        for changes, reason in (
            (dict(soc=90), Reason.POST_BALANCE_SOC_LOW),
            (dict(grid_power_w=500), Reason.POST_BALANCE_GRID_IMPORT),
            (dict(battery_power_w=None), Reason.POST_BALANCE_POWER_LIMIT),
            (dict(now=self.until), Reason.POST_BALANCE_DEADLINE),
        ):
            with self.subTest(reason=reason):
                decision = self.decision(loads=running, **changes)
                self.assertEqual(decision.action, Action.STOP)
                self.assertEqual(decision.reason, reason)

    def test_discharge_limit_sheds_lower_priority_load_first(self):
        states = tuple(LoadState(load, True, .5, output_duration_s=2000) for load in self.cfg.loads)
        decisions = LoadPolicy(self.cfg).evaluate(self.snapshot(loads=states, battery_power_w=-3400))
        self.assertEqual(decisions[0].action, Action.KEEP_ON)
        self.assertEqual(decisions[1].reason, Reason.POST_BALANCE_POWER_LIMIT)

    def test_starts_respect_soc_margin_power_ac_and_window_budget(self):
        stopped = (LoadState(self.cfg.loads[1], False, 1.62, output_duration_s=1000),)
        self.assertEqual(self.decision(loads=stopped, battery_power_w=0).action, Action.START)
        for changes, reason in (
            (dict(soc=90.5), Reason.POST_BALANCE_SOC_LOW),
            (dict(battery_power_w=-2500), Reason.POST_BALANCE_POWER_LIMIT),
            (dict(ac_power_w=5900), Reason.AC_HEADROOM_INSUFFICIENT),
            (dict(now=self.until-timedelta(minutes=5)), Reason.WINDOW_END_TOO_CLOSE),
        ):
            with self.subTest(reason=reason):
                self.assertEqual(self.decision(loads=stopped, **changes).reason, reason)

    def test_shedding_does_not_assume_external_relay_will_be_stopped(self):
        states = tuple(LoadState(load, True, .5, output_duration_s=2000) for load in self.cfg.loads)
        states = (states[0], replace(states[1], external_control=True))
        decisions = LoadPolicy(self.cfg).evaluate(self.snapshot(loads=states, battery_power_w=-3400))
        self.assertEqual(decisions[0].reason, Reason.POST_BALANCE_POWER_LIMIT)
        self.assertEqual(decisions[1].action, Action.KEEP_ON)

    def test_success_does_not_override_thermostat_grid_loss_or_hc_policy(self):
        satisfied = LoadState(self.cfg.loads[0], False, .6, thermostat_satisfied=True)
        self.assertEqual(self.decision(loads=(satisfied,)).action, Action.KEEP_OFF)
        self.assertEqual(self.decision(grid_lost=True).reason, Reason.GRID_LOST)
        # At 22:00 the normal mandatory-runtime policy resumes.
        self.assertEqual(self.decision(now=self.now.replace(hour=22)).reason, Reason.OFF_PEAK_REQUIRED)
        # Without a verified success today's old window rule remains unchanged.
        self.assertEqual(self.decision(post_balance_until=None).reason, Reason.OUTSIDE_ALLOWED_WINDOW)

    def test_disabled_sharing_preserves_existing_minimum_on_then_exclusive_stop(self):
        config = replace(self.cfg, battery=replace(self.cfg.battery, balance_surplus_enabled=False))
        snapshot = self.snapshot(
            now=self.now.replace(hour=14), post_balance_until=None,
            battery_priority=True, battery_priority_exclusive=True,
            loads=(LoadState(self.cfg.loads[1], True, output_duration_s=10),),
        )
        first = LoadPolicy(config).evaluate(snapshot)[0]
        self.assertEqual(first.reason, Reason.MIN_ON_TIME)
        settled = replace(snapshot, loads=(replace(snapshot.loads[0], output_duration_s=1000),))
        self.assertEqual(LoadPolicy(config).evaluate(settled)[0].reason, Reason.BATTERY_BALANCING)

    def test_controller_withdraws_trial_without_waiting_for_deficit_confirmation(self):
        helper = test_controller.ControllerTests(); helper.setUp()
        try:
            base = helper.config(control=True, dry_run=False)
            config = replace(base, loads=(replace(helper.small, min_on_time_s=900),), battery=replace(
                base.battery, control_enabled=True, balance_advance_days=3,
                balance_reference_date="2026-09-18", balance_surplus_enabled=True,
            ))
            moment = [datetime(2026, 10, 16, 14)]
            controller = LoadController(
                config, test_controller.FakeCerbo, test_controller.FakeShelly,
                now=lambda: moment[0], sleep=lambda _: None,
            )
            helper._finish_watchdog_recovery(controller)
            with patch.object(test_controller.FakeCerbo, "read_solar_curtailment", return_value=SolarCurtailment(
                True, grid_power_w=20, battery_power_w=500,
                dc_chargers=test_controller.FakeCerbo(None).read_solar_curtailment().dc_chargers,
            )):
                controller.cycle(today_pv_kwh=30)
                started = controller.cycle(today_pv_kwh=30)
            self.assertIn("COMMAND SmallCumulus ON", started.commands)
            moment[0] += timedelta(seconds=11)
            with patch.object(test_controller.FakeCerbo, "read_solar_curtailment", return_value=SolarCurtailment(
                False, grid_power_w=20, battery_power_w=-600,
            )):
                stopped = controller.cycle(today_pv_kwh=30)
            self.assertIn("COMMAND SmallCumulus OFF", stopped.commands)
            self.assertEqual(stopped.decisions[0].reason, Reason.BATTERY_BALANCING)
        finally:
            helper.tearDown()

    def test_config_is_opt_in_and_rejects_invalid_bounds(self):
        helper = test_config.ConfigTests()
        def parse(values):
            path = helper.write_config({"cerbo": {"host": "localhost"}, "battery": values})
            try:
                return load_config(path).battery
            finally:
                path.unlink()
        self.assertFalse(parse({}).balance_surplus_enabled)
        self.assertFalse(parse({}).post_balance_support_enabled)
        for values in (
            {"balance_surplus_enabled": "yes"}, {"post_balance_support_enabled": 1},
            {"post_balance_extension_s": True}, {"post_balance_extension_s": -1},
            {"post_balance_max_discharge_w": 0}, {"balance_surplus_charge_tolerance_w": -1},
            {"post_balance_support_enabled": True, "post_balance_min_soc": 100},
        ):
            with self.subTest(values=values), self.assertRaises(ConfigError):
                parse(values)

    def test_controller_preserves_success_for_support_after_restart(self):
        helper = test_controller.ControllerTests(); helper.setUp()
        try:
            base = helper.config(control=True, dry_run=False)
            config = replace(base, battery=replace(
                base.battery, control_enabled=True, post_balance_support_enabled=True,
            ))
            test_controller.FakeCerbo.soc = 100
            now = self.now.replace(minute=10)
            success = now.replace(hour=16, minute=16)
            controller = LoadController(
                config, test_controller.FakeCerbo, test_controller.FakeShelly,
                now=lambda: now, sleep=lambda _: None,
            )
            helper._finish_watchdog_recovery(controller)
            controller._battery_runtime = BatteryRuntime(success, last_success_at=success)
            controller.cycle()
            restarted = LoadController(
                config, test_controller.FakeCerbo, test_controller.FakeShelly,
                now=lambda: now + timedelta(minutes=1), sleep=lambda _: None,
            )
            helper._finish_watchdog_recovery(restarted)
            result = restarted.cycle()
            self.assertEqual(result.snapshot.post_balance_until, self.until)
            self.assertIn("COMMAND SmallCumulus ON", result.commands)
            self.assertIsNone(result.battery.min_soc_target)
        finally:
            helper.tearDown()
