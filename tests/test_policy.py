import unittest
from dataclasses import replace
from datetime import datetime

from victron_load_manager.config import (
    AppConfig,
    BatteryConfig,
    CerboConfig,
    LoadConfig,
    ScheduleConfig,
)
from victron_load_manager.policy import (
    Action,
    LoadPolicy,
    LoadState,
    Reason,
    Snapshot,
)


class LoadPolicyTests(unittest.TestCase):
    def setUp(self):
        self.main = LoadConfig("MainCumulus", "main.local", 2200, priority=10, min_daily_runtime_h=2)
        self.small = LoadConfig("SmallCumulus", "small.local", 1200, priority=20, min_daily_runtime_h=2)
        self.pool = LoadConfig("PoolPump", "pool.local", 1300, priority=30)
        self.config = AppConfig(
            cerbo=CerboConfig("localhost"),
            battery=BatteryConfig(soc_on=70, soc_off=40),
            schedule=ScheduleConfig(
                solar_start="10:00", solar_end="18:00",
                off_peak_start="22:00", daily_reset="06:00",
            ),
            loads=(self.main, self.small, self.pool),
            max_ac_power_w=4000,
            dry_run=True,
        )

    def evaluate(
        self,
        hour,
        soc,
        ac_power,
        states,
        grid_lost=False,
        solar_surplus_ready=True,
        energy_deficit=False,
        visible_surplus_w=None,
        battery_priority=False,
        battery_holding=False,
        battery_power_w=None,
    ):
        return LoadPolicy(self.config).evaluate(Snapshot(
            now=datetime(2026, 9, 11, hour, 0),
            soc=soc,
            ac_power_w=ac_power,
            grid_lost=grid_lost,
            loads=tuple(states),
            solar_surplus_ready=solar_surplus_ready,
            energy_deficit=energy_deficit,
            visible_surplus_w=visible_surplus_w,
            battery_priority=battery_priority,
            battery_holding=battery_holding,
            battery_power_w=battery_power_w,
        ))

    @staticmethod
    def by_name(decisions):
        return {decision.load_name: decision for decision in decisions}

    def test_grid_loss_stops_every_active_load(self):
        decisions = self.by_name(self.evaluate(12, 90, 3000, [
            LoadState(self.main, True), LoadState(self.small, False)
        ], grid_lost=True))
        self.assertEqual(decisions["MainCumulus"].action, Action.STOP)
        self.assertEqual(decisions["SmallCumulus"].action, Action.KEEP_OFF)
        self.assertTrue(all(item.reason == Reason.GRID_LOST for item in decisions.values()))

    def test_invalid_telemetry_stops_active_loads(self):
        decision = self.evaluate(12, float("nan"), 1500, [
            LoadState(self.small, True)
        ])[0]
        self.assertEqual(decision.action, Action.STOP)
        self.assertEqual(decision.reason, Reason.TELEMETRY_INVALID)

    def test_solar_starts_only_highest_priority_candidate_per_cycle(self):
        decisions = self.by_name(self.evaluate(12, 80, 500, [
            LoadState(self.main, False),
            LoadState(self.small, False),
            LoadState(self.pool, False),
        ]))
        self.assertEqual(decisions["MainCumulus"].action, Action.START)
        self.assertEqual(decisions["SmallCumulus"].action, Action.KEEP_OFF)
        self.assertEqual(decisions["SmallCumulus"].reason, Reason.START_STAGGERED)
        self.assertEqual(decisions["PoolPump"].reason, Reason.START_STAGGERED)

    def test_global_settling_blocks_all_new_starts(self):
        snapshot = Snapshot(
            now=datetime(2026, 9, 11, 12, 0),
            soc=90,
            ac_power_w=500,
            grid_lost=False,
            loads=(LoadState(self.main, False), LoadState(self.small, False)),
            solar_surplus_ready=True,
            start_settle_remaining_s=12,
        )
        decisions = self.by_name(LoadPolicy(self.config).evaluate(snapshot))
        self.assertEqual(decisions["MainCumulus"].reason, Reason.START_STAGGERED)
        self.assertEqual(decisions["SmallCumulus"].reason, Reason.START_STAGGERED)

    def test_next_priority_load_can_start_after_first_is_observed_on(self):
        decisions = self.by_name(self.evaluate(12, 80, 2700, [
            LoadState(self.main, True, power_state="active"),
            LoadState(self.small, False),
            LoadState(self.pool, False),
        ]))
        self.assertEqual(decisions["MainCumulus"].action, Action.KEEP_ON)
        self.assertEqual(decisions["SmallCumulus"].action, Action.START)
        self.assertEqual(decisions["PoolPump"].reason, Reason.START_STAGGERED)

    def test_solar_start_waits_for_qualified_surplus(self):
        decision = self.evaluate(
            12,
            90,
            500,
            [LoadState(self.small, False)],
            solar_surplus_ready=False,
        )[0]
        self.assertEqual(decision.action, Action.KEEP_OFF)
        self.assertEqual(decision.reason, Reason.SOLAR_SURPLUS_PENDING)

    def test_balancing_reserves_solar_for_battery(self):
        decisions = self.by_name(self.evaluate(
            12,
            92,
            2500,
            [
                LoadState(
                    self.main,
                    True,
                    power_state="active",
                    output_duration_s=600,
                ),
                LoadState(self.small, False),
            ],
            battery_priority=True,
        ))
        self.assertEqual(decisions["MainCumulus"].action, Action.STOP)
        self.assertEqual(
            decisions["MainCumulus"].reason, Reason.BATTERY_BALANCING
        )
        self.assertEqual(decisions["SmallCumulus"].action, Action.KEEP_OFF)
        self.assertEqual(
            decisions["SmallCumulus"].reason, Reason.BATTERY_BALANCING
        )

    def test_motor_minimum_on_time_is_respected_during_balancing(self):
        decision = self.evaluate(
            12,
            92,
            2500,
            [LoadState(
                replace(self.pool, min_on_time_s=900),
                True,
                power_state="active",
                output_duration_s=60,
            )],
            battery_priority=True,
        )[0]
        self.assertEqual(decision.action, Action.KEEP_ON)
        self.assertEqual(decision.reason, Reason.MIN_ON_TIME)

    def test_balancing_charging_can_start_load_from_protected_surplus(self):
        decision = self.evaluate(
            12,
            92,
            500,
            [LoadState(self.pool, False)],
            solar_surplus_ready=False,
            visible_surplus_w=3500,
            battery_priority=True,
        )[0]
        self.assertEqual(decision.action, Action.START)
        self.assertEqual(decision.reason, Reason.BALANCING_SURPLUS)

    def test_balancing_charging_keeps_load_while_reserve_remains(self):
        decision = self.evaluate(
            12,
            92,
            1800,
            [LoadState(
                self.pool,
                True,
                power_state="active",
                output_duration_s=1200,
            )],
            visible_surplus_w=1200,
            battery_priority=True,
        )[0]
        self.assertEqual(decision.action, Action.KEEP_ON)
        self.assertEqual(decision.reason, Reason.BALANCING_SURPLUS)

    def test_balancing_charging_stops_load_below_protected_reserve(self):
        decision = self.evaluate(
            12,
            92,
            1800,
            [LoadState(
                self.pool,
                True,
                power_state="active",
                output_duration_s=1200,
            )],
            visible_surplus_w=999,
            battery_priority=True,
        )[0]
        self.assertEqual(decision.action, Action.STOP)
        self.assertEqual(decision.reason, Reason.BATTERY_BALANCING)

    def test_balancing_hold_can_release_confirmed_curtailed_solar(self):
        decision = self.evaluate(
            12,
            100,
            500,
            [LoadState(self.pool, False)],
            solar_surplus_ready=True,
            visible_surplus_w=0,
            battery_priority=True,
            battery_holding=True,
        )[0]
        self.assertEqual(decision.action, Action.START)
        self.assertEqual(decision.reason, Reason.BALANCING_SURPLUS)

    def test_visible_charge_surplus_starts_below_soc_on(self):
        decision = self.evaluate(
            12,
            67,
            2427,
            [LoadState(self.small, False)],
            solar_surplus_ready=False,
            visible_surplus_w=3986,
        )[0]
        self.assertEqual(decision.action, Action.START)
        self.assertEqual(decision.reason, Reason.SOLAR_CHARGE_SURPLUS)

    def test_visible_surplus_start_uses_soc_hysteresis_above_reserve(self):
        blocked = self.evaluate(
            12,
            40.5,
            500,
            [LoadState(self.small, False)],
            solar_surplus_ready=False,
            visible_surplus_w=4000,
        )[0]
        admitted = self.evaluate(
            12,
            41,
            500,
            [LoadState(self.small, False)],
            solar_surplus_ready=False,
            visible_surplus_w=4000,
        )[0]
        self.assertEqual(blocked.action, Action.KEEP_OFF)
        self.assertEqual(admitted.action, Action.START)
        self.assertEqual(admitted.reason, Reason.SOLAR_CHARGE_SURPLUS)

    def test_visible_surplus_preserves_battery_charge_reserve(self):
        decision = self.evaluate(
            12,
            67,
            2427,
            [LoadState(self.small, False)],
            solar_surplus_ready=False,
            visible_surplus_w=2100,
        )[0]
        self.assertEqual(decision.action, Action.KEEP_OFF)
        self.assertEqual(decision.reason, Reason.SOLAR_HYSTERESIS)

    def test_failed_probe_cooldown_blocks_another_start(self):
        decision = self.evaluate(
            12,
            73,
            2400,
            [LoadState(self.small, False, probe_blocked=True)],
            visible_surplus_w=4000,
        )[0]
        self.assertEqual(decision.action, Action.KEEP_OFF)
        self.assertEqual(decision.reason, Reason.PROBE_COOLDOWN)

    def test_satisfied_thermostat_stays_off_during_cooldown(self):
        decision = self.evaluate(
            12,
            73,
            2400,
            [LoadState(
                self.small,
                False,
                probe_blocked=True,
                thermostat_satisfied=True,
            )],
            visible_surplus_w=4000,
        )[0]
        self.assertEqual(decision.action, Action.KEEP_OFF)
        self.assertEqual(decision.reason, Reason.THERMOSTAT_SATISFIED)

    def test_failed_motor_probe_lockout_blocks_another_start(self):
        decision = self.evaluate(
            12,
            90,
            2400,
            [LoadState(self.pool, False, probe_locked_out=True)],
            visible_surplus_w=4000,
        )[0]
        self.assertEqual(decision.action, Action.KEEP_OFF)
        self.assertEqual(decision.reason, Reason.PROBE_LOCKOUT)

    def test_locked_out_motor_is_stopped_even_if_manually_switched_on(self):
        decision = self.evaluate(
            12,
            90,
            2400,
            [LoadState(
                self.pool,
                True,
                probe_locked_out=True,
                power_state="active",
            )],
        )[0]
        self.assertEqual(decision.action, Action.STOP)
        self.assertEqual(decision.reason, Reason.PROBE_LOCKOUT)

    def test_inactive_cumulus_is_stopped_as_thermostat_satisfied(self):
        protected = replace(self.small, min_on_time_s=120)
        decision = self.evaluate(
            12,
            90,
            2400,
            [LoadState(
                protected,
                True,
                power_state="inactive",
                output_duration_s=10,
            )],
        )[0]
        self.assertEqual(decision.action, Action.STOP)
        self.assertEqual(decision.reason, Reason.THERMOSTAT_SATISFIED)

    def test_cumulus_minimum_off_time_delays_off_peak_restart(self):
        protected = replace(self.small, min_off_time_s=120)
        decision = self.evaluate(
            23,
            80,
            500,
            [LoadState(protected, False, runtime_h=0.5, output_duration_s=60)],
        )[0]
        self.assertEqual(decision.action, Action.KEEP_OFF)
        self.assertEqual(decision.reason, Reason.MIN_OFF_TIME)

    def test_inactive_motor_is_stopped_as_a_fault(self):
        motor = LoadConfig(
            "PoolPump", "pool.local", 1300, probe_failure_mode="lockout"
        )
        decision = self.evaluate(
            12,
            90,
            2400,
            [LoadState(motor, True, power_state="inactive")],
        )[0]
        self.assertEqual(decision.action, Action.STOP)
        self.assertEqual(decision.reason, Reason.ACTIVE_POWER_FAULT)

    def test_missing_active_power_stops_load_fail_safe(self):
        decision = self.evaluate(
            12,
            90,
            2400,
            [LoadState(self.small, True, power_state="unavailable")],
        )[0]
        self.assertEqual(decision.action, Action.STOP)
        self.assertEqual(decision.reason, Reason.POWER_TELEMETRY_UNAVAILABLE)

    def test_persistent_energy_deficit_stops_active_solar_load(self):
        decision = self.evaluate(
            12,
            90,
            1500,
            [LoadState(self.small, True)],
            energy_deficit=True,
        )[0]
        self.assertEqual(decision.action, Action.STOP)
        self.assertEqual(decision.reason, Reason.ENERGY_DEFICIT)

    def test_minimum_on_time_bridges_a_temporary_energy_deficit(self):
        protected = replace(self.pool, min_on_time_s=900)
        held = self.evaluate(
            12,
            80,
            1500,
            [LoadState(
                protected,
                True,
                power_state="active",
                output_duration_s=120,
            )],
            energy_deficit=True,
        )[0]
        expired = self.evaluate(
            12,
            80,
            1500,
            [LoadState(
                protected,
                True,
                power_state="active",
                output_duration_s=901,
            )],
            energy_deficit=True,
        )[0]
        self.assertEqual(held.action, Action.KEEP_ON)
        self.assertEqual(held.reason, Reason.MIN_ON_TIME)
        self.assertEqual(expired.action, Action.STOP)
        self.assertEqual(expired.reason, Reason.ENERGY_DEFICIT)

    def test_minimum_off_time_delays_restart(self):
        protected = replace(self.pool, min_off_time_s=300)
        decision = self.evaluate(
            12,
            90,
            500,
            [LoadState(protected, False, output_duration_s=120)],
        )[0]
        self.assertEqual(decision.action, Action.KEEP_OFF)
        self.assertEqual(decision.reason, Reason.MIN_OFF_TIME)

    def test_hard_safety_overrides_minimum_on_time(self):
        protected = replace(self.pool, min_on_time_s=900)
        low_soc = self.evaluate(
            12,
            40,
            1500,
            [LoadState(
                protected,
                True,
                power_state="active",
                output_duration_s=60,
            )],
        )[0]
        overload = self.evaluate(
            12,
            80,
            4500,
            [LoadState(
                protected,
                True,
                power_state="active",
                output_duration_s=60,
            )],
        )[0]
        self.assertEqual(low_soc.reason, Reason.SOC_LOW)
        self.assertEqual(low_soc.action, Action.STOP)
        self.assertEqual(overload.reason, Reason.AC_OVERLOAD)
        self.assertEqual(overload.action, Action.STOP)

    def test_solar_hysteresis_keeps_existing_state_between_thresholds(self):
        decisions = self.by_name(self.evaluate(12, 55, 1500, [
            LoadState(self.main, True), LoadState(self.small, False)
        ]))
        self.assertEqual(decisions["MainCumulus"].action, Action.KEEP_ON)
        self.assertEqual(decisions["SmallCumulus"].action, Action.KEEP_OFF)

    def test_low_soc_does_not_cancel_required_off_peak_runtime(self):
        decision = self.evaluate(23, 40, 1500, [LoadState(self.small, True, runtime_h=0)])[0]
        self.assertEqual(decision.action, Action.KEEP_ON)
        self.assertEqual(decision.reason, Reason.OFF_PEAK_REQUIRED)

    def test_required_off_peak_load_starts_below_reserve_soc(self):
        decision = self.evaluate(
            23,
            39,
            500,
            [LoadState(self.small, False, runtime_h=0.5)],
            energy_deficit=True,
        )[0]
        self.assertEqual(decision.action, Action.START)
        self.assertEqual(decision.reason, Reason.OFF_PEAK_REQUIRED)

    def test_energy_deficit_does_not_stop_required_off_peak_load(self):
        decision = self.evaluate(
            23,
            39,
            1500,
            [LoadState(self.small, True, runtime_h=0.5)],
            energy_deficit=True,
        )[0]
        self.assertEqual(decision.action, Action.KEEP_ON)
        self.assertEqual(decision.reason, Reason.OFF_PEAK_REQUIRED)

    def test_low_soc_stops_active_load_in_solar_window(self):
        decision = self.evaluate(12, 40, 1500, [LoadState(self.small, True)])[0]
        self.assertEqual(decision.action, Action.STOP)
        self.assertEqual(decision.reason, Reason.SOC_LOW)

    def test_off_peak_starts_only_incomplete_required_loads(self):
        decisions = self.by_name(self.evaluate(23, 60, 500, [
            LoadState(self.main, False, runtime_h=2),
            LoadState(self.small, False, runtime_h=0.5),
            LoadState(self.pool, False, runtime_h=0),
        ]))
        self.assertEqual(decisions["MainCumulus"].reason, Reason.RUNTIME_COMPLETE)
        self.assertEqual(decisions["SmallCumulus"].action, Action.START)
        self.assertEqual(decisions["PoolPump"].action, Action.KEEP_OFF)

    def test_pool_target_is_desired_but_only_half_is_required_off_peak(self):
        pool = replace(
            self.pool,
            target_runtime_h=8,
            required_runtime_ratio=0.5,
        )
        solar_complete = LoadPolicy(self.config).evaluate(Snapshot(
            now=datetime(2026, 9, 11, 12, 0),
            soc=90,
            ac_power_w=500,
            grid_lost=False,
            loads=(LoadState(pool, False, runtime_h=8),),
            solar_surplus_ready=True,
        ))[0]
        off_peak_required = LoadPolicy(self.config).evaluate(Snapshot(
            now=datetime(2026, 9, 11, 23, 0),
            soc=40,
            ac_power_w=500,
            grid_lost=False,
            loads=(LoadState(pool, False, runtime_h=3.9),),
        ))[0]
        off_peak_complete = LoadPolicy(self.config).evaluate(Snapshot(
            now=datetime(2026, 9, 11, 23, 0),
            soc=40,
            ac_power_w=500,
            grid_lost=False,
            loads=(LoadState(pool, False, runtime_h=4),),
        ))[0]
        self.assertEqual(solar_complete.reason, Reason.RUNTIME_COMPLETE)
        self.assertEqual(off_peak_required.action, Action.START)
        self.assertEqual(off_peak_required.reason, Reason.OFF_PEAK_REQUIRED)
        self.assertEqual(off_peak_complete.action, Action.KEEP_OFF)
        self.assertEqual(off_peak_complete.reason, Reason.RUNTIME_COMPLETE)

    def test_target_finish_bridges_small_deficit_after_required_runtime(self):
        pool = replace(
            self.pool,
            target_runtime_h=6,
            required_runtime_ratio=0.5,
            target_finish_enabled=True,
        )
        decision = self.evaluate(
            17,
            85,
            1800,
            [LoadState(pool, True, runtime_h=5.5, output_duration_s=1200)],
            energy_deficit=True,
            battery_power_w=-400,
        )[0]
        self.assertEqual(decision.action, Action.KEEP_ON)
        self.assertEqual(decision.reason, Reason.TARGET_FINISH)

    def test_target_finish_stops_when_battery_support_exceeds_limit(self):
        pool = replace(
            self.pool,
            target_runtime_h=6,
            required_runtime_ratio=0.5,
            target_finish_enabled=True,
        )
        decision = self.evaluate(
            17,
            85,
            1800,
            [LoadState(pool, True, runtime_h=5.5, output_duration_s=1200)],
            energy_deficit=True,
            battery_power_w=-501,
        )[0]
        self.assertEqual(decision.action, Action.STOP)
        self.assertEqual(decision.reason, Reason.FINISH_DEFICIT_EXCEEDED)

    def test_target_finish_refuses_low_soc(self):
        pool = replace(
            self.pool,
            target_runtime_h=6,
            required_runtime_ratio=0.5,
            target_finish_enabled=True,
        )
        decision = self.evaluate(
            17,
            79,
            1800,
            [LoadState(pool, False, runtime_h=5.5, output_duration_s=600)],
            battery_power_w=100,
        )[0]
        self.assertEqual(decision.action, Action.KEEP_OFF)
        self.assertEqual(decision.reason, Reason.FINISH_SOC_LOW)

    def test_target_finish_can_start_in_grace_when_minimum_on_time_fits(self):
        pool = replace(
            self.pool,
            target_runtime_h=6,
            required_runtime_ratio=0.5,
            target_finish_enabled=True,
            min_on_time_s=600,
        )
        snapshot = Snapshot(
            now=datetime(2026, 9, 11, 18, 5),
            soc=85,
            ac_power_w=500,
            grid_lost=False,
            loads=(LoadState(pool, False, runtime_h=5.5, output_duration_s=600),),
            energy_deficit=True,
            battery_power_w=900,
        )
        decision = LoadPolicy(self.config).evaluate(snapshot)[0]
        self.assertEqual(decision.action, Action.START)
        self.assertEqual(decision.reason, Reason.TARGET_FINISH)

    def test_target_progress_uses_last_assistance_hour_when_target_is_unreachable(self):
        pool = replace(
            self.pool,
            target_runtime_h=6,
            required_runtime_ratio=0.5,
            target_finish_enabled=True,
            target_finish_max_remaining_h=1,
            target_finish_grace_s=3600,
            min_on_time_s=900,
        )
        snapshot = Snapshot(
            now=datetime(2026, 9, 11, 18, 5),
            soc=85,
            ac_power_w=500,
            grid_lost=False,
            loads=(LoadState(pool, False, runtime_h=4.51, output_duration_s=900),),
            energy_deficit=True,
            battery_power_w=900,
        )
        decision = LoadPolicy(self.config).evaluate(snapshot)[0]
        self.assertEqual(decision.action, Action.START)
        self.assertEqual(decision.reason, Reason.TARGET_PROGRESS)

    def test_target_progress_does_not_exceed_configured_assistance_duration(self):
        pool = replace(
            self.pool,
            target_runtime_h=6,
            required_runtime_ratio=0.5,
            target_finish_enabled=True,
            target_finish_max_remaining_h=1,
            target_finish_grace_s=3600,
            min_on_time_s=900,
        )
        snapshot = Snapshot(
            now=datetime(2026, 9, 11, 17, 55),
            soc=85,
            ac_power_w=500,
            grid_lost=False,
            loads=(LoadState(pool, False, runtime_h=4.51, output_duration_s=900),),
            solar_surplus_ready=False,
            energy_deficit=True,
            battery_power_w=900,
        )
        decision = LoadPolicy(self.config).evaluate(snapshot)[0]
        self.assertEqual(decision.action, Action.KEEP_OFF)
        self.assertEqual(decision.reason, Reason.SOLAR_SURPLUS_PENDING)

    def test_target_finish_grace_respects_minimum_on_time(self):
        pool = replace(
            self.pool,
            target_runtime_h=6,
            required_runtime_ratio=0.5,
            target_finish_enabled=True,
            min_on_time_s=600,
        )
        snapshot = Snapshot(
            now=datetime(2026, 9, 11, 18, 10),
            soc=85,
            ac_power_w=500,
            grid_lost=False,
            loads=(LoadState(pool, False, runtime_h=5.5, output_duration_s=600),),
            battery_power_w=900,
        )
        decision = LoadPolicy(self.config).evaluate(snapshot)[0]
        self.assertEqual(decision.action, Action.KEEP_OFF)
        self.assertEqual(decision.reason, Reason.WINDOW_END_TOO_CLOSE)

    def test_target_finish_start_reserves_load_power_against_battery_flow(self):
        pool = replace(
            self.pool,
            target_runtime_h=6,
            required_runtime_ratio=0.5,
            target_finish_enabled=True,
        )
        decision = self.evaluate(
            17,
            85,
            500,
            [LoadState(pool, False, runtime_h=5.5, output_duration_s=600)],
            solar_surplus_ready=False,
            battery_power_w=700,
        )[0]
        self.assertEqual(decision.action, Action.KEEP_OFF)
        self.assertEqual(decision.reason, Reason.FINISH_DEFICIT_EXCEEDED)

    def test_target_finish_uses_confirmed_curtailment_near_solar_end(self):
        pool = replace(
            self.pool,
            target_runtime_h=6,
            required_runtime_ratio=0.5,
            target_finish_enabled=True,
            target_finish_grace_s=3600,
            min_on_time_s=900,
        )
        snapshot = Snapshot(
            now=datetime(2026, 9, 19, 18, 21),
            soc=90,
            ac_power_w=1166,
            grid_lost=False,
            loads=(LoadState(pool, False, runtime_h=5.5, output_duration_s=1323),),
            solar_surplus_ready=True,
            energy_deficit=False,
            battery_power_w=-15,
        )
        decision = LoadPolicy(self.config).evaluate(snapshot)[0]
        self.assertEqual(decision.action, Action.START)
        self.assertEqual(decision.reason, Reason.TARGET_FINISH)

    def test_target_finish_stops_after_grace(self):
        pool = replace(
            self.pool,
            target_runtime_h=6,
            required_runtime_ratio=0.5,
            target_finish_enabled=True,
        )
        snapshot = Snapshot(
            now=datetime(2026, 9, 11, 18, 16),
            soc=85,
            ac_power_w=1800,
            grid_lost=False,
            loads=(LoadState(pool, True, runtime_h=5.5, output_duration_s=1200),),
            battery_power_w=0,
        )
        decision = LoadPolicy(self.config).evaluate(snapshot)[0]
        self.assertEqual(decision.action, Action.STOP)
        self.assertEqual(decision.reason, Reason.OUTSIDE_ALLOWED_WINDOW)

    def test_monthly_target_uses_the_vlm_day_month_for_any_runtime_load(self):
        seasonal = replace(
            self.pool,
            target_runtime_h_by_month=(2, 2, 4, 6, 7, 8, 8, 8, 6, 4, 2, 2),
            required_runtime_ratio=0.5,
        )
        before_reset = LoadPolicy(self.config).evaluate(Snapshot(
            now=datetime(2026, 3, 1, 5, 0),
            soc=40,
            ac_power_w=500,
            grid_lost=False,
            loads=(LoadState(seasonal, False, runtime_h=1.5),),
        ))[0]
        march_night = LoadPolicy(self.config).evaluate(Snapshot(
            now=datetime(2026, 3, 1, 23, 0),
            soc=40,
            ac_power_w=500,
            grid_lost=False,
            loads=(LoadState(seasonal, False, runtime_h=1.5),),
        ))[0]
        self.assertEqual(before_reset.reason, Reason.RUNTIME_COMPLETE)
        self.assertEqual(march_night.action, Action.START)
        self.assertEqual(march_night.reason, Reason.OFF_PEAK_REQUIRED)

    def test_start_is_refused_when_minimum_on_time_exceeds_window(self):
        pool = replace(self.pool, min_on_time_s=900)
        decision = LoadPolicy(self.config).evaluate(Snapshot(
            now=datetime(2026, 9, 11, 17, 50),
            soc=90,
            ac_power_w=500,
            grid_lost=False,
            loads=(LoadState(pool, False),),
            solar_surplus_ready=True,
        ))[0]
        self.assertEqual(decision.action, Action.KEEP_OFF)
        self.assertEqual(decision.reason, Reason.WINDOW_END_TOO_CLOSE)

    def test_daily_thermostat_completion_is_latched_but_window_wins_outside(self):
        thermostat = replace(self.small, completion_mode="thermostat")
        inside = self.evaluate(12, 90, 500, [
            LoadState(thermostat, False, daily_complete=True)
        ])[0]
        outside = self.evaluate(19, 90, 500, [
            LoadState(thermostat, False, daily_complete=True)
        ])[0]
        self.assertEqual(inside.reason, Reason.DAILY_COMPLETED)
        self.assertEqual(outside.reason, Reason.OUTSIDE_ALLOWED_WINDOW)

    def test_completed_thermostat_load_keeps_heating_off_peak(self):
        thermostat_load = replace(self.small, completion_mode="thermostat")
        decision = self.evaluate(23, 60, 1500, [
            LoadState(
                thermostat_load,
                True,
                runtime_h=2,
                power_state="active",
            )
        ])[0]
        self.assertEqual(decision.action, Action.KEEP_ON)
        self.assertEqual(decision.reason, Reason.THERMOSTAT_PENDING)

    def test_completed_thermostat_load_can_restart_off_peak(self):
        thermostat_load = replace(self.small, completion_mode="thermostat")
        decision = self.evaluate(23, 60, 500, [
            LoadState(thermostat_load, False, runtime_h=2)
        ])[0]
        self.assertEqual(decision.action, Action.START)
        self.assertEqual(decision.reason, Reason.THERMOSTAT_PENDING)

    def test_completed_runtime_load_stops_off_peak(self):
        runtime_load = replace(self.pool, min_daily_runtime_h=2)
        decision = self.evaluate(23, 60, 1500, [
            LoadState(
                runtime_load,
                True,
                runtime_h=2,
                power_state="active",
            )
        ])[0]
        self.assertEqual(decision.action, Action.STOP)
        self.assertEqual(decision.reason, Reason.RUNTIME_COMPLETE)

    def test_off_peak_wraps_midnight_until_daily_reset(self):
        decision = self.evaluate(2, 60, 500, [LoadState(self.small, False, runtime_h=0)])[0]
        self.assertEqual(decision.action, Action.START)
        self.assertEqual(decision.reason, Reason.OFF_PEAK_REQUIRED)

    def test_outside_windows_stops_active_load(self):
        decision = self.evaluate(19, 90, 1500, [LoadState(self.small, True)])[0]
        self.assertEqual(decision.action, Action.STOP)
        self.assertEqual(decision.reason, Reason.OUTSIDE_ALLOWED_WINDOW)

    def test_overload_sheds_lowest_priority_load_first(self):
        decisions = self.by_name(self.evaluate(12, 80, 4500, [
            LoadState(self.main, True), LoadState(self.small, True), LoadState(self.pool, True)
        ]))
        self.assertEqual(decisions["PoolPump"].action, Action.STOP)
        self.assertEqual(decisions["PoolPump"].reason, Reason.AC_OVERLOAD)
        self.assertEqual(decisions["MainCumulus"].action, Action.KEEP_ON)

    def test_unreachable_load_is_never_commanded(self):
        decision = self.evaluate(12, 90, 500, [
            LoadState(self.small, None, reachable=False)
        ])[0]
        self.assertEqual(decision.action, Action.UNAVAILABLE)
        self.assertEqual(decision.reason, Reason.UNREACHABLE)

    def test_start_margin_prevents_marginal_start_without_changing_stop_limit(self):
        self.config = AppConfig(
            cerbo=self.config.cerbo,
            battery=self.config.battery,
            schedule=self.config.schedule,
            loads=(self.small,),
            max_ac_power_w=6500,
            ac_start_margin_w=500,
        )
        marginal = self.evaluate(12, 80, 5268, [LoadState(self.small, False)])[0]
        safe = self.evaluate(12, 80, 4700, [LoadState(self.small, False)])[0]
        active = self.evaluate(12, 80, 6200, [LoadState(self.small, True)])[0]
        self.assertEqual(marginal.reason, Reason.AC_HEADROOM_INSUFFICIENT)
        self.assertEqual(safe.action, Action.START)
        self.assertEqual(active.action, Action.KEEP_ON)

    def test_startup_power_is_used_only_for_start_headroom(self):
        pump = replace(self.pool, startup_power_w=2100)
        self.config = replace(
            self.config,
            loads=(pump,),
            max_ac_power_w=6500,
            ac_start_margin_w=500,
        )

        blocked = self.evaluate(12, 90, 4000, [LoadState(pump, False)])[0]
        running = self.evaluate(12, 90, 4000, [LoadState(pump, True)])[0]

        self.assertEqual(blocked.action, Action.KEEP_OFF)
        self.assertEqual(blocked.reason, Reason.AC_HEADROOM_INSUFFICIENT)
        self.assertEqual(running.action, Action.KEEP_ON)


if __name__ == "__main__":
    unittest.main()
