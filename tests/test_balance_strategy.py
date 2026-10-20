import unittest
from datetime import datetime, timedelta

from versatile_load_manager.balance_strategy import (
    HcCatchupDecision,
    decide_hc_catchup,
)
from versatile_load_manager.tempo import TempoColour


class HcCatchupStrategyTests(unittest.TestCase):
    def setUp(self):
        self.now = datetime(2026, 12, 10, 23, 0)
        self.due = self.now - timedelta(days=5)

    def decide(self, **changes):
        values = dict(
            now=self.now,
            due_at=self.due,
            solar_defer_days=14,
            tomorrow_pv_kwh=18.0,
            good_solar_day_kwh=15.0,
            tomorrow_tempo=TempoColour.BLUE,
        )
        values.update(changes)
        return decide_hc_catchup(**values)

    def test_not_due_never_requests_grid_catchup(self):
        advice = self.decide(due_at=self.now + timedelta(days=1))
        self.assertEqual(advice.decision, HcCatchupDecision.NOT_DUE)
        self.assertFalse(advice.requested)

    def test_good_forecast_keeps_solar_first(self):
        advice = self.decide()
        self.assertEqual(advice.decision, HcCatchupDecision.WAIT_SOLAR)
        self.assertEqual(advice.reason, "wait_good_solar_forecast")

    def test_red_tomorrow_strengthens_solar_preference(self):
        advice = self.decide(tomorrow_tempo=TempoColour.RED)
        self.assertEqual(advice.decision, HcCatchupDecision.WAIT_SOLAR)
        self.assertEqual(advice.reason, "wait_red_day_solar")

    def test_poor_forecast_does_not_force_grid_before_deadline(self):
        advice = self.decide(tomorrow_pv_kwh=3.0)
        self.assertEqual(advice.decision, HcCatchupDecision.WAIT_SOLAR)
        self.assertEqual(advice.reason, "within_solar_defer_window")

    def test_missing_forecast_does_not_force_grid_before_deadline(self):
        advice = self.decide(tomorrow_pv_kwh=None)
        self.assertEqual(advice.decision, HcCatchupDecision.WAIT_SOLAR)

    def test_overdue_campaign_requests_catchup_even_on_red_hc(self):
        advice = self.decide(
            due_at=self.now - timedelta(days=14),
            tomorrow_tempo=TempoColour.RED,
        )
        self.assertEqual(advice.decision, HcCatchupDecision.CATCH_UP_OVERDUE)
        self.assertTrue(advice.requested)


if __name__ == "__main__":
    unittest.main()
