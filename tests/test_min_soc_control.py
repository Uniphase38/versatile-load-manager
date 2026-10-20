import unittest

from versatile_load_manager.min_soc_control import MinSocOwnership, plan_min_soc_action


class MinSocOwnershipTests(unittest.TestCase):
    def test_first_override_captures_before_any_write(self):
        action = plan_min_soc_action(desired_soc=100, observed_soc=20, ownership=MinSocOwnership())
        self.assertEqual(action.action, "capture")
        self.assertEqual(action.ownership, MinSocOwnership(True, 20))

    def test_persisted_capture_then_requests_set(self):
        action = plan_min_soc_action(desired_soc=100, observed_soc=20, ownership=MinSocOwnership(True, 20))
        self.assertEqual(action.action, "set")
        self.assertEqual(action.value, 100)

    def test_restart_while_forced_keeps_ownership(self):
        action = plan_min_soc_action(desired_soc=100, observed_soc=100, ownership=MinSocOwnership(True, 20))
        self.assertEqual(action.action, "none")
        self.assertEqual(action.ownership.original_soc, 20)

    def test_end_of_catchup_restores_exact_original_value(self):
        action = plan_min_soc_action(desired_soc=None, observed_soc=100, ownership=MinSocOwnership(True, 37.5))
        self.assertEqual(action.action, "restore")
        self.assertEqual(action.value, 37.5)

    def test_ownership_released_only_after_restore_is_observed(self):
        action = plan_min_soc_action(desired_soc=None, observed_soc=37.5, ownership=MinSocOwnership(True, 37.5))
        self.assertEqual(action.action, "release")
        self.assertFalse(action.ownership.owned)

    def test_missing_original_value_fails_closed(self):
        action = plan_min_soc_action(desired_soc=None, observed_soc=100, ownership=MinSocOwnership(True, None))
        self.assertEqual(action.action, "fault")


if __name__ == "__main__":
    unittest.main()
