import tempfile
import unittest
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

from versatile_load_manager.config import (
    AppConfig, BatteryConfig, CerboConfig, LoadConfig, ScheduleConfig,
)
from versatile_load_manager.battery import BatteryRuntime
from versatile_load_manager.controller import (
    LoadController, RuntimeStore, SolarFlowTracker,
)
from versatile_load_manager.policy import Action, LoadState, Reason
from versatile_load_manager.modbus import DcPvState, ModbusError, SolarCurtailment
from versatile_load_manager.tempo import TempoColour
from versatile_load_manager.shelly import ShellyCommandResult, ShellyStatus
from versatile_load_manager.watchdog import WatchdogState


class FakeCerbo:
    soc = 80.0
    ac_power = 500.0
    grid_lost = False
    dvcc = 100
    writes = []
    min_soc = 20.0
    min_soc_writes = []
    fail_min_soc_write = False

    def __init__(self, config):
        pass

    def connect(self):
        pass

    def resolve_services(self):
        pass

    def read_soc(self):
        return self.soc

    def read_ac_power(self):
        return self.ac_power

    def read_grid_lost(self):
        return self.grid_lost

    def read_solar_curtailment(self):
        return SolarCurtailment(
            True,
            dc_chargers=(DcPvState("mppt", 1, 500.0),),
            grid_power_w=0.0,
            battery_power_w=50.0,
        )

    def read_dvcc_max_charge(self):
        return self.dvcc

    def write_dvcc_max_charge(self, current_a):
        type(self).writes.append(current_a)
        type(self).dvcc = current_a

    def read_min_soc(self):
        return self.min_soc

    def write_min_soc(self, soc):
        if type(self).fail_min_soc_write:
            raise OSError("simulated Min_SOC write failure")
        type(self).min_soc_writes.append(float(soc))
        type(self).min_soc = float(soc)

    def close(self):
        pass


class FakeShelly:
    output = False
    calls = []

    def __init__(self, host, switch_id=0):
        self.host = host

    def read_status(self):
        power = 1200.0 if self.output else 0.0
        return ShellyStatus(self.host, 2, "SNSW", self.output, power)

    def set_output(self, on, dry_run=True):
        self.calls.append((self.host, on, dry_run))
        before = self.output
        if not dry_run:
            type(self).output = on
        return ShellyCommandResult(
            self.host, on, before, self.output, not dry_run and before != on, dry_run
        )


class ColdCumulusShelly(FakeShelly):
    def read_status(self):
        return ShellyStatus(self.host, 2, "SNSW", self.output, 0.0)


class ControllerTests(unittest.TestCase):
    def setUp(self):
        FakeShelly.calls = []
        FakeShelly.output = False
        FakeCerbo.soc = 80.0
        FakeCerbo.grid_lost = False
        FakeCerbo.dvcc = 100
        FakeCerbo.writes = []
        FakeCerbo.min_soc = 20.0
        FakeCerbo.min_soc_writes = []
        FakeCerbo.fail_min_soc_write = False
        self.temp = tempfile.TemporaryDirectory()
        self.small = LoadConfig("SmallCumulus", "small.local", 1200, min_daily_runtime_h=2)
        self.main = LoadConfig("MainCumulus", "main.local", 2200, priority=1)

    def tearDown(self):
        self.temp.cleanup()

    def config(self, control=False, dry_run=True):
        return AppConfig(
            cerbo=CerboConfig("localhost"),
            battery=BatteryConfig(soc_on=70, soc_off=40),
            schedule=ScheduleConfig(solar_start="10:00", solar_end="18:00"),
            loads=(self.main, self.small),
            max_ac_power_w=4000,
            dry_run=dry_run,
            control_enabled=control,
            managed_loads=("SmallCumulus",),
            state_file=str(Path(self.temp.name) / "runtime.json"),
        )

    @staticmethod
    def _finish_watchdog_recovery(controller):
        controller.watchdog_success()
        controller.watchdog_success()

    def test_grid_loss_restores_owned_hc_min_soc_in_same_cycle(self):
        base = self.config(control=True, dry_run=False)
        config = replace(
            base,
            battery=replace(
                base.battery,
                control_enabled=True,
                balance_hc_enabled=True,
                usable_capacity_kwh=10,
                max_grid_charge_power_w=3500,
            ),
        )
        now = datetime(2026, 12, 20, 23, 0)
        controller = LoadController(config, FakeCerbo, FakeShelly, now=lambda: now)
        controller._battery_runtime = BatteryRuntime(
            now - timedelta(days=31),
            campaign_active=True,
            min_soc_owned=True,
            pre_hc_min_soc=20,
        )
        FakeCerbo.grid_lost = True
        FakeCerbo.min_soc = 100.0
        controller.cycle(tomorrow_pv_kwh=5)
        self.assertEqual(FakeCerbo.min_soc, 20.0)
        self.assertFalse(controller._battery_runtime.min_soc_owned)

    def test_hc_catchup_gate_accepts_only_overdue_feasible_night(self):
        base = self.config(control=True, dry_run=False)
        config = replace(
            base,
            battery=replace(
                base.battery,
                control_enabled=True,
                balance_hc_enabled=True,
                usable_capacity_kwh=10,
                max_grid_charge_power_w=3500,
                night_base_load_w=500,
            ),
        )
        controller = LoadController(config, FakeCerbo, FakeShelly)
        now = datetime(2026, 12, 20, 23, 0)
        state = LoadState(self.small, False, 2.0)
        allowed, reason = controller._hc_catchup_authorized(
            now=now, soc=80, due_at=now - timedelta(days=14),
            states=(state,), tomorrow_pv_kwh=20,
            tomorrow_tempo=TempoColour.RED,
        )
        self.assertTrue(allowed)
        self.assertEqual(reason, "hc_catchup_authorized")
        allowed, reason = controller._hc_catchup_authorized(
            now=now, soc=80, due_at=now - timedelta(days=13),
            states=(state,), tomorrow_pv_kwh=5,
            tomorrow_tempo=TempoColour.BLUE,
        )
        self.assertFalse(allowed)
        self.assertEqual(reason, "within_solar_defer_window")

    def test_min_soc_capture_is_persisted_before_failed_write(self):
        base = self.config(control=True, dry_run=False)
        config = replace(
            base,
            battery=replace(base.battery, control_enabled=True),
        )
        now = datetime(2026, 12, 1, 23, 0)
        controller = LoadController(
            config, FakeCerbo, FakeShelly, now=lambda: now,
            sleep=lambda _seconds: None,
        )
        controller._battery_runtime = BatteryRuntime(
            now - timedelta(days=30), campaign_active=True
        )
        cerbo = FakeCerbo(config.cerbo)
        FakeCerbo.fail_min_soc_write = True

        messages = controller._apply_min_soc_target(cerbo, now, {}, 100.0)
        self.assertTrue(any("Min_SOC set unconfirmed" in m for m in messages))

        payload = controller.store.load_battery_state()
        self.assertTrue(payload["min_soc_owned"])
        self.assertEqual(payload["pre_hc_min_soc"], 20.0)
        self.assertEqual(FakeCerbo.min_soc, 20.0)

    def test_restart_at_daily_reset_restores_hc_min_soc(self):
        base = self.config(control=True, dry_run=False)
        config = replace(
            base,
            battery=replace(
                base.battery,
                control_enabled=True,
                balance_hc_enabled=True,
                usable_capacity_kwh=10,
                max_grid_charge_power_w=3500,
            ),
            schedule=replace(
                base.schedule,
                off_peak_start="22:00",
                daily_reset="06:00",
            ),
        )
        now = datetime(2026, 12, 2, 6, 0)
        runtime = BatteryRuntime(
            now - timedelta(days=31),
            campaign_active=True,
            min_soc_owned=True,
            pre_hc_min_soc=37.5,
        )
        RuntimeStore(config.state_file, config.schedule.daily_reset).save(
            now - timedelta(minutes=1), {}, battery_state=runtime.to_payload()
        )
        FakeCerbo.min_soc = 100.0
        restarted = LoadController(
            config, FakeCerbo, FakeShelly, now=lambda: now,
            sleep=lambda _seconds: None,
        )
        restarted.cycle(tomorrow_pv_kwh=5)
        self.assertEqual(FakeCerbo.min_soc, 37.5)
        payload = restarted.store.load_battery_state()
        self.assertFalse(payload["min_soc_owned"])
        self.assertIsNone(payload["pre_hc_min_soc"])

    def test_restart_restores_exact_owned_min_soc_before_releasing(self):
        base = self.config(control=True, dry_run=False)
        config = replace(
            base,
            battery=replace(base.battery, control_enabled=True),
        )
        now = datetime(2026, 12, 2, 2, 0)
        store = RuntimeStore(config.state_file, config.schedule.daily_reset)
        runtime = BatteryRuntime(
            now - timedelta(days=30),
            campaign_active=True,
            min_soc_owned=True,
            pre_hc_min_soc=37.5,
        )
        store.save(now, {}, battery_state=runtime.to_payload())
        FakeCerbo.min_soc = 100.0

        restarted = LoadController(
            config, FakeCerbo, FakeShelly, now=lambda: now,
            sleep=lambda _seconds: None,
        )
        messages = restarted.stop_managed()

        self.assertEqual(FakeCerbo.min_soc_writes, [37.5])
        self.assertEqual(FakeCerbo.min_soc, 37.5)
        payload = restarted.store.load_battery_state()
        self.assertFalse(payload["min_soc_owned"])
        self.assertIsNone(payload["pre_hc_min_soc"])
        self.assertIn("BATTERY Min_SOC ownership released", messages)

    def test_failed_restore_keeps_min_soc_ownership_durable(self):
        base = self.config(control=True, dry_run=False)
        config = replace(
            base,
            battery=replace(base.battery, control_enabled=True),
        )
        now = datetime(2026, 12, 2, 2, 0)
        store = RuntimeStore(config.state_file, config.schedule.daily_reset)
        runtime = BatteryRuntime(
            now - timedelta(days=30),
            campaign_active=True,
            min_soc_owned=True,
            pre_hc_min_soc=37.5,
        )
        store.save(now, {}, battery_state=runtime.to_payload())
        FakeCerbo.min_soc = 100.0
        FakeCerbo.fail_min_soc_write = True

        restarted = LoadController(
            config, FakeCerbo, FakeShelly, now=lambda: now,
            sleep=lambda _seconds: None,
        )
        messages = restarted.stop_managed()

        payload = restarted.store.load_battery_state()
        self.assertTrue(payload["min_soc_owned"])
        self.assertEqual(payload["pre_hc_min_soc"], 37.5)
        self.assertTrue(any("Min_SOC restore unconfirmed" in m for m in messages))

    def test_observation_waits_then_previews_managed_small_cumulus(self):
        controller = LoadController(
            self.config(), FakeCerbo, FakeShelly,
            now=lambda: datetime(2026, 9, 11, 12, 0),
        )
        self._finish_watchdog_recovery(controller)
        first = controller.cycle()
        result = controller.cycle()
        self.assertFalse(first.snapshot.solar_surplus_ready)
        self.assertTrue(result.snapshot.solar_surplus_ready)
        self.assertEqual([item.load_name for item in result.decisions], ["SmallCumulus"])
        self.assertEqual(result.decisions[0].action, Action.START)
        self.assertEqual(FakeShelly.calls, [("small.local", True, True)])

    def test_both_locks_are_required_for_real_command(self):
        controller = LoadController(
            self.config(control=True, dry_run=False), FakeCerbo, FakeShelly,
            now=lambda: datetime(2026, 9, 11, 12, 0),
            sleep=lambda _seconds: None,
        )
        self._finish_watchdog_recovery(controller)
        controller.cycle()
        result = controller.cycle()
        self.assertEqual(FakeShelly.calls, [("small.local", True, False)])
        self.assertIn(
            "PROBE SmallCumulus CONFIRMED power=1200.0 W", result.commands
        )

    def test_battery_has_independent_write_lock(self):
        FakeCerbo.soc = 90.0
        base = self.config(control=True, dry_run=False)
        config = replace(
            base,
            battery=replace(
                base.battery,
                control_enabled=False,
                balance_reference_date="2026-08-01",
            ),
        )
        controller = LoadController(
            config,
            FakeCerbo,
            FakeShelly,
            now=lambda: datetime(2026, 9, 16, 12, 0),
            sleep=lambda _seconds: None,
        )
        result = controller.cycle()
        self.assertEqual(FakeCerbo.writes, [])
        self.assertNotEqual(
            result.observed_dvcc_a, result.battery.charge_current_a
        )

        FakeCerbo.writes = []
        enabled = replace(
            config,
            battery=replace(config.battery, control_enabled=True),
        )
        controller = LoadController(
            enabled,
            FakeCerbo,
            FakeShelly,
            now=lambda: datetime(2026, 9, 16, 12, 0),
            sleep=lambda _seconds: None,
        )
        self._finish_watchdog_recovery(controller)
        controller.cycle()
        self.assertEqual(len(FakeCerbo.writes), 1)

    def test_dvcc_write_resets_flow_and_blocks_load_start_during_settling(self):
        FakeCerbo.soc = 90.0
        FakeCerbo.dvcc = 100
        base = self.config(control=True, dry_run=False)
        config = replace(
            base,
            battery=replace(
                base.battery,
                control_enabled=True,
            ),
        )
        controller = LoadController(
            config,
            FakeCerbo,
            FakeShelly,
            now=lambda: datetime(2026, 9, 16, 12, 0),
            sleep=lambda _seconds: None,
        )
        result = controller.cycle()
        self.assertEqual(FakeCerbo.writes, [0])
        self.assertFalse(result.snapshot.solar_surplus_ready)
        self.assertEqual(result.decisions[0].reason, Reason.SOLAR_SURPLUS_PENDING)
        self.assertIn(
            "FLOW RESET after DVCC transition; starts settling for 30 s",
            result.commands,
        )
        self._finish_watchdog_recovery(controller)
        controller.cycle()
        confirmed_flow = controller.cycle()
        self.assertTrue(confirmed_flow.snapshot.solar_surplus_ready)
        self.assertEqual(
            confirmed_flow.decisions[0].reason, Reason.START_STAGGERED
        )

    def test_observed_external_dvcc_change_resets_flow_in_observe_mode(self):
        controller = LoadController(
            self.config(), FakeCerbo, FakeShelly,
            now=lambda: datetime(2026, 9, 11, 12, 0),
        )
        self._finish_watchdog_recovery(controller)
        controller.cycle()
        FakeCerbo.dvcc = 0
        result = controller.cycle()
        self.assertFalse(result.snapshot.solar_surplus_ready)
        self.assertIsNone(result.snapshot.visible_surplus_w)
        self.assertEqual(
            result.decisions[0].reason, Reason.SOLAR_SURPLUS_PENDING
        )
        controller.cycle()
        confirmed_flow = controller.cycle()
        self.assertTrue(confirmed_flow.snapshot.solar_surplus_ready)
        self.assertEqual(
            confirmed_flow.decisions[0].reason, Reason.START_STAGGERED
        )

    def test_observed_balancing_does_not_change_real_load_policy(self):
        FakeCerbo.soc = 90.0
        base = self.config(control=True, dry_run=False)
        config = replace(
            base,
            battery=replace(
                base.battery,
                control_enabled=False,
                balance_reference_date="2026-08-01",
            ),
        )
        controller = LoadController(
            config,
            FakeCerbo,
            FakeShelly,
            now=lambda: datetime(2026, 9, 16, 12, 0),
            sleep=lambda _seconds: None,
        )
        self._finish_watchdog_recovery(controller)
        controller.cycle()
        result = controller.cycle()
        self.assertTrue(result.battery.using_balance_target)
        self.assertFalse(result.snapshot.battery_priority)
        self.assertEqual(result.decisions[0].action, Action.START)

    def test_failed_power_probe_immediately_switches_load_off(self):
        ColdCumulusShelly.calls = []
        ColdCumulusShelly.output = False
        controller = LoadController(
            self.config(control=True, dry_run=False),
            FakeCerbo,
            ColdCumulusShelly,
            now=lambda: datetime(2026, 9, 11, 12, 0),
            sleep=lambda _seconds: None,
        )
        self._finish_watchdog_recovery(controller)
        controller.cycle()
        result = controller.cycle()
        self.assertEqual(
            ColdCumulusShelly.calls,
            [("small.local", True, False), ("small.local", False, False)],
        )
        self.assertIn(
            "PROBE SmallCumulus SATISFIED power=0.0 W", result.commands
        )
        self.assertEqual(result.commands[-1], "COMMAND SmallCumulus OFF")

        cooldown = controller.cycle()
        self.assertEqual(
            cooldown.decisions[0].reason.value, "thermostat_satisfied"
        )
        self.assertEqual(
            ColdCumulusShelly.calls,
            [("small.local", True, False), ("small.local", False, False)],
        )

    def test_thermostat_completion_survives_controller_restart(self):
        ColdCumulusShelly.calls = []
        ColdCumulusShelly.output = True
        config = replace(
            self.config(control=True, dry_run=False),
            loads=(
                self.main,
                replace(self.small, completion_mode="thermostat"),
            ),
        )
        controller = LoadController(
            config,
            FakeCerbo,
            ColdCumulusShelly,
            now=lambda: datetime(2026, 9, 11, 12, 0),
            sleep=lambda _seconds: None,
        )
        completed = controller.cycle()
        self.assertTrue(completed.snapshot.loads[0].daily_complete)
        self.assertEqual(
            RuntimeStore(config.state_file, config.schedule.daily_reset).load_completed(
                datetime(2026, 9, 11, 12, 0)
            ),
            {"SmallCumulus"},
        )

        ColdCumulusShelly.calls = []
        restarted = LoadController(
            config,
            FakeCerbo,
            ColdCumulusShelly,
            now=lambda: datetime(2026, 9, 11, 12, 1),
            sleep=lambda _seconds: None,
        ).cycle()
        self.assertTrue(restarted.snapshot.loads[0].daily_complete)
        self.assertEqual(
            restarted.decisions[0].reason.value, "thermostat_satisfied"
        )
        self.assertEqual(ColdCumulusShelly.calls, [])

    def test_daily_boundary_emits_an_audit_event(self):
        moments = iter((
            datetime(2026, 9, 12, 5, 59),
            datetime(2026, 9, 12, 6, 0),
        ))
        controller = LoadController(
            self.config(), FakeCerbo, FakeShelly, now=lambda: next(moments)
        )
        controller.cycle()
        reset = controller.cycle()
        self.assertIn(
            "DAILY RESET runtimes, completions and thermostat state cleared",
            reset.commands,
        )

    def test_daily_boundary_clears_stale_thermostat_before_next_window(self):
        moments = iter((
            datetime(2026, 9, 12, 5, 59),
            datetime(2026, 9, 12, 6, 0),
            datetime(2026, 9, 12, 10, 0),
        ))
        controller = LoadController(
            self.config(), FakeCerbo, FakeShelly, now=lambda: next(moments)
        )
        controller.cycle()
        controller._thermostat_satisfied.add("SmallCumulus")
        controller._probe_retry_after["SmallCumulus"] = datetime(
            2026, 9, 12, 12, 0
        )

        reset = controller.cycle()
        self.assertEqual(controller._thermostat_satisfied, set())
        self.assertEqual(controller._probe_retry_after, {})
        self.assertEqual(
            reset.decisions[0].reason.value, "outside_allowed_window"
        )

        reopened = controller.cycle()
        self.assertEqual(reopened.decisions[0].action, Action.START)
        self.assertNotEqual(
            reopened.decisions[0].reason.value, "thermostat_satisfied"
        )

    def test_lockout_mode_never_retries_failed_motor_probe(self):
        ColdCumulusShelly.calls = []
        ColdCumulusShelly.output = False
        config = self.config(control=True, dry_run=False)
        config = replace(
            config,
            loads=(
                self.main,
                replace(self.small, probe_failure_mode="lockout"),
            ),
        )
        controller = LoadController(
            config,
            FakeCerbo,
            ColdCumulusShelly,
            now=lambda: datetime(2026, 9, 11, 12, 0),
            sleep=lambda _seconds: None,
        )
        self._finish_watchdog_recovery(controller)
        controller.cycle()
        controller.cycle()
        locked = controller.cycle()
        self.assertEqual(locked.decisions[0].reason.value, "probe_lockout")
        self.assertEqual(
            ColdCumulusShelly.calls,
            [("small.local", True, False), ("small.local", False, False)],
        )

        restarted = LoadController(
            config,
            FakeCerbo,
            ColdCumulusShelly,
            now=lambda: datetime(2026, 9, 11, 12, 1),
            sleep=lambda _seconds: None,
        )
        after_restart = restarted.cycle()
        self.assertEqual(
            after_restart.decisions[0].reason.value, "probe_lockout"
        )

    def test_runtime_is_counted_from_observed_on_state(self):
        FakeShelly.output = True
        moments = iter((
            datetime(2026, 9, 11, 12, 0),
            datetime(2026, 9, 11, 12, 1),
        ))
        controller = LoadController(
            self.config(), FakeCerbo, FakeShelly, now=lambda: next(moments)
        )
        controller.cycle()
        controller.cycle()
        runtime = RuntimeStore(
            self.config().state_file, self.config().schedule.daily_reset
        ).load(datetime(2026, 9, 11, 12, 1))
        self.assertAlmostEqual(runtime["SmallCumulus"], 1 / 60)

    def test_runtime_is_not_counted_when_thermostat_is_satisfied(self):
        ColdCumulusShelly.output = True
        moments = iter((
            datetime(2026, 9, 11, 12, 0),
            datetime(2026, 9, 11, 12, 1),
        ))
        controller = LoadController(
            self.config(control=True, dry_run=False),
            FakeCerbo,
            ColdCumulusShelly,
            now=lambda: next(moments),
            sleep=lambda _seconds: None,
        )
        first = controller.cycle()
        second = controller.cycle()
        runtime = RuntimeStore(
            self.config().state_file, self.config().schedule.daily_reset
        ).load(datetime(2026, 9, 11, 12, 1))
        self.assertNotIn("SmallCumulus", runtime)
        self.assertIn(
            "MONITOR SmallCumulus SATISFIED power=0.0 W", first.commands
        )
        self.assertEqual(first.decisions[0].reason.value, "thermostat_satisfied")
        self.assertEqual(second.decisions[0].reason.value, "thermostat_satisfied")

    def test_shutdown_stops_only_a_relay_started_by_vlm_and_is_idempotent(self):
        controller = LoadController(
            self.config(control=True, dry_run=False), FakeCerbo, FakeShelly,
            now=lambda: datetime(2026, 9, 11, 12, 0),
            sleep=lambda _seconds: None,
        )
        self._finish_watchdog_recovery(controller)
        controller.cycle()
        controller.cycle()
        FakeShelly.calls = []
        messages = controller.stop_managed()
        self.assertEqual(FakeShelly.calls, [("small.local", False, False)])
        self.assertEqual(messages, ("SHUTDOWN SmallCumulus OFF",))
        self.assertEqual(controller.stop_managed(), ())
        self.assertEqual(FakeShelly.calls, [("small.local", False, False)])
        transitions = RuntimeStore(
            self.config().state_file, self.config().schedule.daily_reset
        ).load_output_transitions()
        self.assertFalse(transitions["SmallCumulus"][0])

    def test_shutdown_preserves_external_on_relay(self):
        FakeShelly.output = True
        controller = LoadController(
            self.config(control=True, dry_run=False), FakeCerbo, FakeShelly,
            now=lambda: datetime(2026, 9, 11, 19, 0),
        )
        result = controller.cycle()
        self.assertIn(
            "EXTERNAL SmallCumulus ON observed; left unchanged until daily reset",
            result.commands,
        )
        self.assertEqual(FakeShelly.calls, [])
        self.assertEqual(controller.stop_managed(), ())
        self.assertTrue(FakeShelly.output)

    def test_external_on_relay_is_adopted_at_daily_reset(self):
        FakeShelly.output = True
        moments = iter((
            datetime(2026, 9, 11, 19, 0),
            datetime(2026, 9, 12, 6, 0),
        ))
        controller = LoadController(
            self.config(control=True, dry_run=False), FakeCerbo, FakeShelly,
            now=lambda: next(moments),
        )
        controller.cycle()
        result = controller.cycle()
        self.assertIn(
            "OWNERSHIP SmallCumulus adopted at daily reset", result.commands
        )
        self.assertEqual(FakeShelly.calls, [("small.local", False, False)])

    def test_hard_safety_can_stop_external_on_relay(self):
        FakeShelly.output = True
        FakeCerbo.grid_lost = True
        try:
            controller = LoadController(
                self.config(control=True, dry_run=False), FakeCerbo, FakeShelly,
                now=lambda: datetime(2026, 9, 11, 12, 0),
            )
            result = controller.cycle()
        finally:
            FakeCerbo.grid_lost = False
        self.assertEqual(result.decisions[0].reason, Reason.GRID_LOST)
        self.assertEqual(FakeShelly.calls, [("small.local", False, False)])

    def test_owned_on_relay_survives_controller_restart(self):
        config = self.config(control=True, dry_run=False)
        controller = LoadController(
            config, FakeCerbo, FakeShelly,
            now=lambda: datetime(2026, 9, 11, 12, 0),
            sleep=lambda _seconds: None,
        )
        self._finish_watchdog_recovery(controller)
        controller.cycle()
        controller.cycle()
        self.assertIn(
            "SmallCumulus",
            RuntimeStore(config.state_file, config.schedule.daily_reset)
            .load_owned_loads(),
        )
        FakeShelly.calls = []
        restarted = LoadController(
            config, FakeCerbo, FakeShelly,
            now=lambda: datetime(2026, 9, 11, 12, 1),
        )
        self.assertEqual(
            restarted.stop_managed(), ("SHUTDOWN SmallCumulus OFF",)
        )
        self.assertEqual(FakeShelly.calls, [("small.local", False, False)])

    def test_shutdown_restores_fail_open_dvcc_when_battery_control_is_enabled(self):
        base = self.config(control=True, dry_run=False)
        config = replace(
            base,
            battery=replace(base.battery, control_enabled=True),
        )
        FakeCerbo.dvcc = 0
        controller = LoadController(
            config,
            FakeCerbo,
            FakeShelly,
            now=lambda: datetime(2026, 9, 16, 12, 0),
        )
        messages = controller.stop_managed()
        self.assertEqual(FakeCerbo.writes, [100])
        self.assertIn("BATTERY SHUTDOWN DVCC 100A", messages)

    def test_persisted_off_transition_prevents_restart_after_service_restart(self):
        protected = replace(self.small, min_off_time_s=300)
        config = replace(self.config(), loads=(self.main, protected))
        store = RuntimeStore(config.state_file, config.schedule.daily_reset)
        paris = ZoneInfo("Europe/Paris")
        changed_at = datetime(2026, 9, 11, 12, 0, tzinfo=paris)
        store.save(
            changed_at,
            {},
            output_transitions={
                "SmallCumulus": (
                    False,
                    changed_at,
                )
            },
        )
        controller = LoadController(
            config,
            FakeCerbo,
            FakeShelly,
            now=lambda: datetime(2026, 9, 11, 12, 1, tzinfo=paris),
        )
        result = controller.cycle()
        self.assertEqual(result.decisions[0].action, Action.KEEP_OFF)
        self.assertEqual(result.decisions[0].reason.value, "min_off_time")
        self.assertEqual(FakeShelly.calls, [])
        self.assertEqual(
            store.load_output_transitions()["SmallCumulus"],
            (False, changed_at),
        )

    def test_freshly_managed_off_load_has_no_invented_off_delay(self):
        protected = replace(self.small, min_off_time_s=300)
        config = replace(self.config(), loads=(self.main, protected))
        controller = LoadController(
            config,
            FakeCerbo,
            FakeShelly,
            now=lambda: datetime(2026, 9, 11, 12, 0),
        )
        controller.cycle()
        result = controller.cycle()
        self.assertEqual(result.decisions[0].action, Action.START)

    def test_utc_cerbo_clock_is_converted_before_schedule_evaluation(self):
        FakeShelly.output = True
        config = replace(
            self.config(),
            schedule=ScheduleConfig(
                timezone="Europe/Paris",
                solar_start="10:00",
                solar_end="18:00",
                off_peak_start="22:00",
                daily_reset="06:00",
            ),
        )
        controller = LoadController(
            config,
            FakeCerbo,
            FakeShelly,
            now=lambda: datetime(2026, 9, 16, 5, 0, tzinfo=timezone.utc),
        )
        result = controller.cycle()
        self.assertEqual(result.snapshot.now.hour, 7)
        self.assertEqual(result.snapshot.now.utcoffset(), timedelta(hours=2))
        self.assertEqual(result.decisions[0].action, Action.STOP)
        self.assertEqual(result.decisions[0].reason.value, "outside_allowed_window")

    def test_paris_clock_uses_winter_and_summer_offsets(self):
        config = replace(
            self.config(),
            schedule=replace(self.config().schedule, timezone="Europe/Paris"),
        )
        summer = LoadController(
            config,
            FakeCerbo,
            FakeShelly,
            now=lambda: datetime(2026, 9, 16, 5, 0, tzinfo=timezone.utc),
        )._now()
        winter = LoadController(
            config,
            FakeCerbo,
            FakeShelly,
            now=lambda: datetime(2026, 1, 16, 5, 0, tzinfo=timezone.utc),
        )._now()
        self.assertEqual((summer.hour, summer.utcoffset()), (7, timedelta(hours=2)))
        self.assertEqual((winter.hour, winter.utcoffset()), (6, timedelta(hours=1)))

    def test_elapsed_time_is_real_across_fall_dst_transition(self):
        paris = ZoneInfo("Europe/Paris")
        before = datetime(2026, 10, 25, 2, 55, tzinfo=paris, fold=0)
        after = datetime(2026, 10, 25, 2, 5, tzinfo=paris, fold=1)
        self.assertEqual(LoadController._elapsed_seconds(after, before), 600)

    def test_invalid_soc_is_a_critical_modbus_failure(self):
        FakeCerbo.soc = 3276.7
        controller = LoadController(
            self.config(), FakeCerbo, FakeShelly,
            now=lambda: datetime(2026, 10, 3, 12, 0),
        )
        with self.assertRaisesRegex(ModbusError, "invalid critical SOC"):
            controller.cycle()

    def test_watchdog_safe_stops_only_owned_load(self):
        controller = LoadController(
            self.config(control=True, dry_run=False),
            FakeCerbo,
            FakeShelly,
            now=lambda: datetime(2026, 10, 3, 12, 0),
            sleep=lambda _seconds: None,
        )
        controller._owned_on.add("SmallCumulus")
        FakeShelly.output = True

        controller.watchdog_failure(IOError("timeout"))
        controller.watchdog_failure(IOError("timeout"))
        messages = controller.watchdog_failure(IOError("timeout"))

        self.assertEqual(controller.watchdog.state, WatchdogState.SAFE)
        self.assertFalse(FakeShelly.output)
        self.assertTrue(any("WATCHDOG SAFE" in item for item in messages))

    def test_watchdog_safe_preserves_external_manual_load(self):
        controller = LoadController(
            self.config(control=True, dry_run=False),
            FakeCerbo,
            FakeShelly,
            now=lambda: datetime(2026, 10, 3, 12, 0),
        )
        FakeShelly.output = True

        controller.watchdog_failure(IOError("timeout"))
        controller.watchdog_failure(IOError("timeout"))
        controller.watchdog_failure(IOError("timeout"))

        self.assertTrue(FakeShelly.output)
        self.assertEqual(FakeShelly.calls, [])

    def test_watchdog_safe_restores_owned_min_soc_and_dvcc(self):
        base = self.config(control=True, dry_run=False)
        config = replace(
            base,
            battery=replace(base.battery, control_enabled=True),
        )
        now = datetime(2026, 10, 3, 12, 0)
        controller = LoadController(
            config, FakeCerbo, FakeShelly, now=lambda: now,
        )
        controller._battery_runtime = BatteryRuntime(
            last_completed_at=now,
            min_soc_owned=True,
            pre_hc_min_soc=37.5,
        )
        FakeCerbo.min_soc = 100.0
        FakeCerbo.dvcc = 0

        controller.watchdog_failure(IOError("timeout"))
        controller.watchdog_failure(IOError("timeout"))
        messages = controller.watchdog_failure(IOError("timeout"))

        self.assertEqual(FakeCerbo.min_soc, 37.5)
        self.assertEqual(FakeCerbo.dvcc, config.battery.max_charge_current_a)
        self.assertFalse(controller._battery_runtime.min_soc_owned)
        self.assertTrue(any("WATCHDOG SAFE BATTERY DVCC" in m for m in messages))

    def test_watchdog_evidence_survives_normal_runtime_save(self):
        controller = LoadController(
            self.config(), FakeCerbo, FakeShelly,
            now=lambda: datetime(2026, 10, 3, 12, 0),
        )
        controller.watchdog_failure(IOError("lost telemetry"))
        controller._save_state(datetime(2026, 10, 3, 12, 0), {})

        payload = controller.store.load_watchdog_state()
        self.assertTrue(payload["active"])
        self.assertEqual(payload["last_error"], "lost telemetry")

    def test_watchdog_restart_and_recovery_are_persisted(self):
        current = [datetime(2026, 10, 3, 12, 0)]
        controller = LoadController(
            self.config(), FakeCerbo, FakeShelly, now=lambda: current[0],
        )
        controller.watchdog_failure(IOError("lost telemetry"))
        current[0] += timedelta(minutes=1)
        controller.watchdog_restart_requested()
        current[0] += timedelta(minutes=1)
        first_messages = controller.watchdog_success()
        current[0] += timedelta(minutes=1)
        second_messages = controller.watchdog_success()

        self.assertEqual(first_messages, ("WATCHDOG RECOVERING healthy=1/2",))
        self.assertEqual(second_messages, ("WATCHDOG OK",))

        payload = controller.store.load_watchdog_state()
        self.assertFalse(payload["active"])
        self.assertEqual(payload["restart_count"], 1)
        self.assertEqual(payload["failure_duration_s"], 180.0)
        self.assertEqual(len(payload["history"]), 1)


class RuntimeStoreTests(unittest.TestCase):
    def test_daily_thermostat_completion_persists_only_for_current_period(self):
        with tempfile.TemporaryDirectory() as directory:
            store = RuntimeStore(str(Path(directory) / "state.json"), "06:00")
            before = datetime(2026, 9, 11, 23, 0)
            store.save(before, {"SmallCumulus": 1.0}, completed_loads={"SmallCumulus"})
            self.assertEqual(store.load_completed(before), {"SmallCumulus"})
            restarted = RuntimeStore(str(Path(directory) / "state.json"), "06:00")
            self.assertEqual(restarted.load_completed(before), {"SmallCumulus"})
            self.assertEqual(
                restarted.load_completed(datetime(2026, 9, 12, 6, 0)), set()
            )

    def test_daily_runtime_reset_preserves_balancing_history(self):
        with tempfile.TemporaryDirectory() as directory:
            store = RuntimeStore(str(Path(directory) / "runtime.json"), "06:00")
            battery = {
                "last_completed_at": "2026-08-18T06:00:00",
                "campaign_active": True,
                "target_reached_at": None,
            }
            store.save(
                datetime(2026, 9, 16, 5, 59),
                {"Load": 1.0},
                battery_state=battery,
            )
            store.save(datetime(2026, 9, 16, 6, 1), {})
            self.assertEqual(store.load_battery_state(), battery)

    def test_runtime_resets_at_daily_boundary(self):
        with tempfile.TemporaryDirectory() as directory:
            store = RuntimeStore(str(Path(directory) / "state.json"), "06:00")
            store.save(datetime(2026, 9, 11, 5, 0), {"SmallCumulus": 1.5})
            self.assertEqual(
                store.load(datetime(2026, 9, 11, 5, 30))["SmallCumulus"], 1.5
            )
            self.assertEqual(store.load(datetime(2026, 9, 11, 6, 0)), {})

    def test_legacy_naive_period_is_preserved_during_timezone_migration(self):
        with tempfile.TemporaryDirectory() as directory:
            store = RuntimeStore(str(Path(directory) / "state.json"), "06:00")
            store.save(datetime(2026, 9, 11, 12, 0), {"SmallCumulus": 1.5})
            local = datetime(2026, 9, 11, 12, 0, tzinfo=ZoneInfo("Europe/Paris"))
            self.assertEqual(store.load(local)["SmallCumulus"], 1.5)

    def test_aware_runtime_resets_at_six_local_not_six_utc(self):
        with tempfile.TemporaryDirectory() as directory:
            store = RuntimeStore(str(Path(directory) / "state.json"), "06:00")
            paris = ZoneInfo("Europe/Paris")
            before = datetime(2026, 9, 11, 5, 59, tzinfo=paris)
            store.save(before, {"SmallCumulus": 1.5})
            self.assertEqual(store.load(before)["SmallCumulus"], 1.5)
            self.assertEqual(
                store.load(datetime(2026, 9, 11, 6, 0, tzinfo=paris)),
                {},
            )

    def test_motor_lockout_survives_daily_reset_until_explicit_clear(self):
        with tempfile.TemporaryDirectory() as directory:
            store = RuntimeStore(str(Path(directory) / "state.json"), "06:00")
            store.save(
                datetime(2026, 9, 11, 12, 0),
                {"SmallCumulus": 1.5},
                {"PoolPump"},
            )
            self.assertEqual(store.load_lockouts(), {"PoolPump"})
            self.assertEqual(store.load(datetime(2026, 9, 12, 6, 0)), {})
            self.assertEqual(store.load_lockouts(), {"PoolPump"})
            self.assertTrue(store.clear_lockout("PoolPump"))
            self.assertEqual(store.load_lockouts(), set())
            self.assertFalse(store.clear_lockout("PoolPump"))

    def test_output_transitions_survive_daily_runtime_reset(self):
        with tempfile.TemporaryDirectory() as directory:
            store = RuntimeStore(str(Path(directory) / "state.json"), "06:00")
            changed_at = datetime(2026, 9, 11, 22, 0)
            store.save(
                changed_at,
                {"PoolPump": 1.0},
                output_transitions={"PoolPump": (False, changed_at)},
            )
            self.assertEqual(
                store.load_output_transitions()["PoolPump"],
                (False, changed_at),
            )
            self.assertEqual(store.load(datetime(2026, 9, 12, 6, 0)), {})
            self.assertEqual(
                store.load_output_transitions()["PoolPump"],
                (False, changed_at),
            )


class SolarFlowTrackerTests(unittest.TestCase):
    def setUp(self):
        self.config = AppConfig(
            cerbo=CerboConfig("localhost"),
            battery=BatteryConfig(),
            schedule=ScheduleConfig(),
            loads=(),
            surplus_confirm_cycles=2,
            deficit_confirm_cycles=2,
            grid_import_tolerance_w=100,
            battery_discharge_tolerance_w=200,
        )
        self.tracker = SolarFlowTracker(self.config)

    @staticmethod
    def telemetry(grid, battery, limited=True):
        return SolarCurtailment(
            True,
            dc_chargers=(DcPvState("mppt", 1 if limited else 2, 500.0),),
            grid_power_w=grid,
            battery_power_w=battery,
        )

    def test_surplus_requires_two_consecutive_safe_limited_samples(self):
        first = self.tracker.update(self.telemetry(-50.0, 50.0))
        second = self.tracker.update(self.telemetry(10.0, 45.0))
        self.assertFalse(first.surplus_ready)
        self.assertTrue(second.surplus_ready)

    def test_reset_requires_fresh_confirmation_samples(self):
        self.tracker.update(self.telemetry(-50.0, 50.0))
        self.assertTrue(
            self.tracker.update(self.telemetry(10.0, 45.0)).surplus_ready
        )
        self.tracker.reset()
        first_after_reset = self.tracker.update(self.telemetry(-50.0, 50.0))
        self.assertFalse(first_after_reset.surplus_ready)
        self.assertIsNone(first_after_reset.visible_surplus_w)

    def test_iron_transient_does_not_immediately_confirm_deficit(self):
        first = self.tracker.update(self.telemetry(172.0, -1828.0, limited=False))
        recovered = self.tracker.update(self.telemetry(70.0, 30.0))
        self.assertFalse(first.deficit_sustained)
        self.assertFalse(recovered.deficit_sustained)

    def test_persistent_import_or_discharge_confirms_deficit(self):
        first = self.tracker.update(self.telemetry(150.0, -300.0, limited=False))
        second = self.tracker.update(self.telemetry(120.0, -250.0, limited=False))
        self.assertFalse(first.deficit_sustained)
        self.assertTrue(second.deficit_sustained)

    def test_battery_charge_offsets_small_grid_import(self):
        first = self.tracker.update(self.telemetry(137.0, 2256.0))
        second = self.tracker.update(self.telemetry(204.0, 2461.0))
        self.assertFalse(first.deficit_sustained)
        self.assertFalse(second.deficit_sustained)
        self.assertTrue(second.surplus_ready)

    def test_visible_surplus_is_confirmed_without_pv_curtailment(self):
        first = self.tracker.update(self.telemetry(52.0, 4038.0, limited=False))
        second = self.tracker.update(self.telemetry(65.0, 4038.0, limited=False))
        self.assertIsNone(first.visible_surplus_w)
        self.assertEqual(second.visible_surplus_w, 3973.0)
        self.assertFalse(second.surplus_ready)


if __name__ == "__main__":
    unittest.main()


