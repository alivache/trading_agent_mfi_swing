import unittest

from overnight_policy import should_force_close_at_eod, should_enter_swing_signal


class OvernightPolicyTests(unittest.TestCase):
    def test_hold_overnight_when_enabled(self):
        self.assertFalse(should_force_close_at_eod(20, 0, False))
        self.assertFalse(should_force_close_at_eod(19, 50, False))

    def test_close_at_eod_when_enabled(self):
        self.assertTrue(should_force_close_at_eod(20, 0, True))
        self.assertTrue(should_force_close_at_eod(19, 45, True))
        self.assertFalse(should_force_close_at_eod(14, 30, True))

    def test_enter_signal_requires_strong_bullish_conditions(self):
        self.assertTrue(should_enter_swing_signal(0.03, 60, 0.7, 1.1, 0.03, 60, True))
        self.assertFalse(should_enter_swing_signal(0.01, 40, 0.5, 0.7, 0.005, 50, True))
        self.assertFalse(should_enter_swing_signal(0.03, 60, 0.7, 1.1, 0.03, 60, False))


if __name__ == "__main__":
    unittest.main()
