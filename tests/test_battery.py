import unittest
from dataclasses import replace
from datetime import datetime, timedelta, timezone

from victron_load_manager.battery import (
    BalanceStatus,
    BatteryPolicy,
    BatteryRuntime,
    compute_charge_current,
)
from victron_load_manager.config import BatteryConfig, ScheduleConfig


class BatteryPolicyTests(unittest.TestCase):
    def setUp(self):
        self.config = BatteryConfig(
            soc_min=40,
            soc_max=90,
            balance_target_soc=100,
            max_charge_current_a=100,
            charge_hysteresis_soc=1,
            balance_interval_days=30,
            balance_hold_s=3600,
        )
        self.policy = BatteryPolicy(
            self.config,
            ScheduleConfig(solar_start="10:00", solar_end="18:00"),
        )
        self.now = datetime(2026, 9, 16, 12, 0)
        self.due = BatteryRuntime(self.now - timedelta(days=31))

    def test_due_campaign_can_enter_hc_forced_without_prior_solar_start(self):
        policy = self.policy
        now = datetime(2026, 10, 20, 23, 0)
        runtime = BatteryRuntime(now - timedelta(days=31))
        decision = policy.evaluate(
            now, 75.0, False, False, runtime, hc_catchup_authorized=True
        )
        self.assertEqual(decision.status, BalanceStatus.HC_FORCED)
        self.assertTrue(decision.runtime.campaign_active)
        self.assertEqual(decision.min_soc_target, 100)

    def test_hc_forced_target_holds_then_completes_and_keeps_min_soc_owned(self):
        now = datetime(2026, 10, 20, 23, 0)
        runtime = BatteryRuntime(
            now - timedelta(days=31),
            campaign_active=True,
            min_soc_owned=True,
            pre_hc_min_soc=40,
        )
        holding = self.policy.evaluate(
            now, 100.0, False, False, runtime, hc_catchup_authorized=True
        )
        self.assertEqual(holding.status, BalanceStatus.HOLDING)
        self.assertEqual(holding.min_soc_target, 100)
        completed = self.policy.evaluate(
            now + timedelta(seconds=3600),
            100.0, False, False, holding.runtime,
            hc_catchup_authorized=True,
        )
        self.assertEqual(completed.status, BalanceStatus.COMPLETED)
        self.assertFalse(completed.runtime.campaign_active)
        self.assertIsNone(completed.min_soc_target)

    def test_charge_control_uses_protected_ceiling_with_one_percent_hysteresis(self):
        self.assertEqual(
            compute_charge_current(80, 90, 100, 1, None), (100, True)
        )
        self.assertEqual(
            compute_charge_current(90, 90, 100, 1, True), (0, False)
        )
        self.assertEqual(
            compute_charge_current(89.5, 90, 100, 1, False), (0, False)
        )
        self.assertEqual(
            compute_charge_current(89, 90, 100, 1, False), (100, True)
        )
        self.assertEqual(
            compute_charge_current(89.5, 90, 100, 1, True), (100, True)
        )

    def test_due_campaign_waits_for_confirmed_solar_surplus(self):
        result = self.policy.evaluate(
            self.now, 90, False, False, self.due
        )
        self.assertEqual(result.status, BalanceStatus.WAITING_SURPLUS)
        self.assertFalse(result.campaign_active)
        self.assertEqual(result.target_soc, 90)

    def test_due_campaign_waits_until_normal_soc_ceiling(self):
        result = self.policy.evaluate(
            self.now, 89.9, True, False, self.due
        )
        self.assertEqual(result.status, BalanceStatus.WAITING_SURPLUS)
        self.assertFalse(result.campaign_active)
        self.assertEqual(result.target_soc, 90)

    def test_due_campaign_starts_on_surplus_without_min_soc_action(self):
        result = self.policy.evaluate(
            self.now, 90, True, False, self.due
        )
        self.assertEqual(result.status, BalanceStatus.CHARGING)
        self.assertTrue(result.campaign_active)
        self.assertEqual(result.target_soc, 100)
        self.assertEqual(result.charge_current_a, 100)

    def test_authorised_hc_catchup_exposes_min_soc_intent(self):
        active = replace(self.due, campaign_active=True)
        result = self.policy.evaluate(
            self.now.replace(hour=23), 94, False, False, active,
            hc_catchup_authorized=True,
        )
        self.assertEqual(result.status, BalanceStatus.HC_FORCED)
        self.assertEqual(result.target_soc, 100)
        self.assertEqual(result.charge_current_a, 100)
        self.assertEqual(result.min_soc_target, 100)

    def test_hc_catchup_never_changes_min_soc_intent_without_authorisation(self):
        active = replace(self.due, campaign_active=True)
        result = self.policy.evaluate(
            self.now.replace(hour=23), 94, False, False, active
        )
        self.assertEqual(result.status, BalanceStatus.PAUSED_OUTSIDE_SOLAR)
        self.assertIsNone(result.min_soc_target)

    def test_active_campaign_pauses_outside_solar_window(self):
        active = replace(self.due, campaign_active=True)
        result = self.policy.evaluate(
            self.now.replace(hour=22), 94, True, False, active
        )
        self.assertEqual(result.status, BalanceStatus.PAUSED_OUTSIDE_SOLAR)
        self.assertTrue(result.campaign_active)
        self.assertEqual(result.target_soc, 90)
        self.assertEqual(result.charge_current_a, 0)

    def test_active_campaign_pauses_on_energy_deficit(self):
        active = replace(
            self.due,
            campaign_active=True,
            solar_authorized_date=self.now.date().isoformat(),
        )
        result = self.policy.evaluate(
            self.now, 94, False, True, active
        )
        self.assertEqual(result.status, BalanceStatus.PAUSED_DEFICIT)
        self.assertEqual(result.target_soc, 90)
        self.assertEqual(result.charge_current_a, 0)

    def test_campaign_keeps_charging_after_curtailment_is_released(self):
        started = self.policy.evaluate(self.now, 90, True, False, self.due)
        continued = self.policy.evaluate(
            self.now + timedelta(minutes=1), 91, False, False, started.runtime
        )
        self.assertEqual(continued.status, BalanceStatus.CHARGING)
        self.assertEqual(continued.target_soc, 100)

    def test_campaign_requires_fresh_surplus_on_next_day(self):
        active = replace(
            self.due,
            campaign_active=True,
            solar_authorized_date="2026-09-15",
        )
        waiting = self.policy.evaluate(self.now, 94, False, False, active)
        self.assertEqual(waiting.status, BalanceStatus.WAITING_SURPLUS)
        self.assertEqual(waiting.target_soc, 90)

        resumed = self.policy.evaluate(self.now, 94, True, False, waiting.runtime)
        self.assertEqual(resumed.status, BalanceStatus.CHARGING)
        self.assertEqual(resumed.target_soc, 100)

    def test_target_must_be_held_before_completion(self):
        active = replace(
            self.due,
            campaign_active=True,
            solar_authorized_date=self.now.date().isoformat(),
        )
        reached = self.policy.evaluate(
            self.now, 100, False, False, active
        )
        self.assertEqual(reached.status, BalanceStatus.HOLDING)
        self.assertIsNotNone(reached.runtime.target_reached_at)
        self.assertEqual(reached.charge_current_a, 100)
        self.assertEqual(reached.hold_elapsed_s, 0)
        self.assertEqual(reached.hold_remaining_s, 3600)

        halfway = self.policy.evaluate(
            self.now + timedelta(minutes=30),
            100,
            False,
            False,
            reached.runtime,
        )
        self.assertEqual(halfway.status, BalanceStatus.HOLDING)
        self.assertEqual(halfway.hold_elapsed_s, 1800)
        self.assertEqual(halfway.hold_remaining_s, 1800)

        complete = self.policy.evaluate(
            self.now + timedelta(hours=1),
            100,
            False,
            False,
            reached.runtime,
        )
        self.assertEqual(complete.status, BalanceStatus.COMPLETED)
        self.assertFalse(complete.campaign_active)
        self.assertEqual(complete.runtime.last_completed_at, self.now + timedelta(hours=1))
        self.assertEqual(complete.charge_current_a, 0)

    def test_target_hold_must_be_continuous_and_keeps_dvcc_open(self):
        active = replace(
            self.due,
            campaign_active=True,
            solar_authorized_date=self.now.date().isoformat(),
        )
        reached = self.policy.evaluate(self.now, 100, False, False, active)
        dropped = self.policy.evaluate(
            self.now + timedelta(minutes=30),
            99,
            False,
            False,
            reached.runtime,
        )
        self.assertEqual(dropped.status, BalanceStatus.CHARGING)
        self.assertIsNone(dropped.runtime.target_reached_at)
        self.assertEqual(dropped.charge_current_a, 100)

    def test_new_install_anchors_to_now_unless_reference_is_configured(self):
        fresh = self.policy.initial_runtime(self.now, {})
        self.assertEqual(fresh.last_completed_at, self.now)

        configured = BatteryPolicy(
            replace(self.config, balance_reference_date="2026-08-18"),
            ScheduleConfig(daily_reset="06:00"),
        )
        anchored = configured.initial_runtime(self.now, {})
        self.assertEqual(anchored.last_completed_at, datetime(2026, 8, 18, 6, 0))

    def test_aware_runtime_keeps_timezone(self):
        now = datetime(2026, 9, 16, 12, 0, tzinfo=timezone.utc)
        runtime = self.policy.initial_runtime(now, {
            "last_completed_at": "2026-08-01T12:00:00+00:00",
            "campaign_active": True,
        })
        self.assertTrue(runtime.campaign_active)
        self.assertEqual(runtime.last_completed_at.tzinfo, timezone.utc)


if __name__ == "__main__":
    unittest.main()
