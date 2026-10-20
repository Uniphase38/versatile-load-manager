import unittest
from datetime import datetime

from versatile_load_manager.hc_balance import RequiredLoad, plan_hc_balance


class HcBalancePlannerTests(unittest.TestCase):
    def setUp(self):
        self.now = datetime(2026, 12, 1, 1, 0)
        self.end = datetime(2026, 12, 1, 6, 0)

    def plan(self, **changes):
        values = dict(
            now=self.now,
            hc_end=self.end,
            soc=95,
            target_soc=100,
            battery_capacity_kwh=15,
            max_grid_charge_power_w=4000,
            max_ac_power_w=6500,
            night_base_load_w=500,
            required_loads=(),
            hold_s=3600,
            safety_margin_s=1800,
            energy_margin=1.15,
        )
        values.update(changes)
        return plan_hc_balance(**values)

    def test_near_full_battery_fits_with_hold_and_margin(self):
        result = self.plan()
        self.assertTrue(result.feasible)
        self.assertEqual(result.reason, "feasible")
        self.assertAlmostEqual(result.battery_energy_kwh, 0.8625)
        self.assertAlmostEqual(result.charge_window_h, 3.5)
        self.assertAlmostEqual(result.finish_tail_h, 1.5)

    def test_required_load_energy_is_reserved(self):
        result = self.plan(required_loads=(
            RequiredLoad("Cumulus", 2200, 2.0),
            RequiredLoad("PoolPump", 1300, 1.0),
        ))
        self.assertTrue(result.feasible)
        self.assertAlmostEqual(result.required_load_energy_kwh, 5.7)

    def test_large_soc_gap_is_rejected_when_grid_budget_is_insufficient(self):
        result = self.plan(
            soc=20,
            required_loads=(RequiredLoad("LargeLoad", 5000, 4.0),),
        )
        self.assertFalse(result.feasible)
        self.assertEqual(result.reason, "insufficient_grid_energy_budget")

    def test_load_that_cannot_finish_in_hc_window_is_rejected(self):
        result = self.plan(
            required_loads=(RequiredLoad("Pump", 1200, 5.1),),
        )
        self.assertFalse(result.feasible)
        self.assertEqual(result.reason, "required_load_does_not_fit_window")

    def test_required_load_may_continue_during_low_current_finish_tail(self):
        result = self.plan(
            required_loads=(RequiredLoad("Pump", 1200, 4.0),),
            soc=99.5,
        )
        self.assertTrue(result.feasible)
        self.assertEqual(result.reason, "feasible")

    def test_hold_reserves_time_but_not_full_power_energy(self):
        short = self.plan(soc=99, hold_s=0)
        long = self.plan(soc=99, hold_s=3600)
        self.assertAlmostEqual(short.battery_energy_kwh, long.battery_energy_kwh)
        self.assertGreater(long.finish_tail_h, short.finish_tail_h)

    def test_base_load_can_make_plan_impossible(self):
        result = self.plan(night_base_load_w=6500)
        self.assertFalse(result.feasible)
        self.assertEqual(result.reason, "base_load_exceeds_ac_budget")

    def test_battery_charge_power_limit_is_checked(self):
        result = self.plan(
            soc=80,
            max_grid_charge_power_w=500,
            required_loads=(),
        )
        self.assertFalse(result.feasible)
        self.assertEqual(
            result.reason, "battery_charge_does_not_fit_before_finish_tail"
        )



if __name__ == "__main__":
    unittest.main()
