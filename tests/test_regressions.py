import unittest

from scripts import anomaly_detector as detector


class ReproducedDefects(unittest.TestCase):
    def test_nonfinite_is_not_a_number(self):
        for value in ('NaN', 'nan', 'inf', '-Infinity', '1e9999'):
            with self.subTest(value=value):
                self.assertIsNone(detector.try_float(value))

    def test_zero_iqr_detects_genuine_deviation(self):
        self.assertEqual(set(detector.iqr_detect([1, 1, 1, 1, 100], list(range(5)))), {4})

    def test_moving_average_prefix_invariance(self):
        values = [1, 2, 1, 2, 10, 1, 2]
        before = detector.moving_avg_detect(values, list(range(len(values))), window=3)
        extended = values + [10000]
        after = detector.moving_avg_detect(extended, list(range(len(extended))), window=3)
        self.assertIn(4, before)
        self.assertEqual(before, {i: result for i, result in after.items() if i < len(values)})

    def test_future_numeric_overflow_preserves_prior_moving_verdicts(self):
        values = [1, 2, 1, 2, 10, 1, 2]
        before = detector.moving_avg_detect(values, list(range(7)), window=3)
        extended = values + [1e308, -1e308, 1e308]
        after = detector.moving_avg_detect(extended, list(range(len(extended))), window=3)
        self.assertEqual(before, {i: result for i, result in after.items() if i < 7})


if __name__ == '__main__':
    unittest.main()
