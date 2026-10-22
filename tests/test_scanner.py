import contextlib
import io
import json
import math
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

from scripts import anomaly_detector as d

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / 'scripts' / 'anomaly_detector.py'


def strict_loads(text):
    def reject(value):
        raise ValueError(value)
    return json.loads(text, parse_constant=reject)


class APITests(unittest.TestCase):
    def scan(self, text, **kwargs):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'data.csv'
            path.write_text(text, encoding='utf-8')
            return d.scan_csv(path, **kwargs)

    def test_numeric_conversion(self):
        for value in (None, '', ' ', 'word', 'NaN', 'Infinity', '-inf'):
            self.assertIsNone(d.try_float(value))
        self.assertEqual(d.try_float(' 1.5 '), 1.5)

    def test_constant_data(self):
        for constant in (4.0, 0.1, 1e308):
            for length in (5, 6, 12):
                values, indices = [constant] * length, list(range(length))
                for method in (d.zscore_detect, d.iqr_detect, d.moving_avg_detect):
                    self.assertEqual(method(values, indices), {})

    def test_full_prior_window_and_zero_variance(self):
        values = [1, 1, 1, 10]
        result = d.moving_avg_detect(values, list(range(4)), window=3)
        self.assertEqual(set(result), {3})
        self.assertTrue(result[3]['baseline_zero_variance'])
        self.assertEqual(result[3]['moving_avg'], 1)
        strict_loads(json.dumps(result, allow_nan=False))
        self.assertEqual(d.moving_avg_detect([1, 10], [0, 1], window=2), {})

    def test_missing_rows_break_window(self):
        self.assertEqual(d.moving_avg_detect([1, 1, 10], [0, 2, 3], window=2), {})
        result = d.moving_avg_detect([1, 1, 1, 10], [0, 2, 3, 4], window=2)
        self.assertEqual(set(result), {4})

    def test_known_spike_all_methods(self):
        values = [1.0] * 20 + [100.0]
        for method in (d.zscore_detect, d.iqr_detect, d.moving_avg_detect):
            self.assertIn(20, method(values, list(range(len(values)))))

    def test_tiny_samples_and_summary(self):
        result = self.scan('a,b\n1,hello\n2,world\n', columns=['a', 'b'])
        self.assertEqual(result['summary']['columns_analyzed'], [])
        self.assertEqual(result['column_stats']['a']['count'], 2)
        self.assertTrue(any(w['code'] == 'insufficient_data' for w in result['warnings']))
        result = self.scan('a\n1\n2\n3\n', methods=['iqr'])
        self.assertEqual(result['summary']['columns_analyzed'], [])
        result = self.scan('a\n1\n2\n3\n', methods=['z-score'])
        self.assertEqual(result['summary']['columns_analyzed'], ['a'])

    def test_bom_missing_nonfinite_and_text_counts(self):
        result = self.scan('﻿x,y\n1,a\n,b\nNaN,c\nInfinity,d\nno,e\n2,f\n3,g\n', columns=['x'])
        self.assertEqual(result['data_quality']['x'], {
            'valid_numeric': 3, 'missing': 1, 'nonfinite': 2, 'non_numeric': 1})
        self.assertEqual(result['summary']['columns_analyzed'], ['x'])
        self.assertIn('y', result['data_quality'])
        strict_loads(json.dumps(result, allow_nan=False))

    def test_nonfinite_auto_detection_warns(self):
        result = self.scan('x\nNaN\nInfinity\n-inf\n')
        self.assertEqual(result['summary']['columns_analyzed'], [])
        self.assertEqual(result['data_quality']['x']['nonfinite'], 3)
        self.assertTrue(result['warnings'])

    def test_deduplication(self):
        result = self.scan('x\n1\n1\n1\n1\n9\n', columns=['x', 'x'], methods=['iqr', 'iqr'])
        self.assertEqual(result['summary']['methods_used'], ['iqr'])
        self.assertEqual(len(result['anomalies']), 1)
        self.assertEqual(result['anomalies'][0]['classification'], 'high_priority')
        self.assertNotIn('explainable', json.dumps(result))

    def test_row_and_physical_csv_line(self):
        result = self.scan('note,x\n"two\nlines",1\na,1\nb,1\nc,1\nd,9\n', columns=['x'], methods=['iqr'])
        anomaly = result['anomalies'][0]
        self.assertEqual((anomaly['row'], anomaly['csv_line']), (4, 7))

    def test_review_label_and_prior_baseline(self):
        result = d.classify_anomaly(1, 'x', 5, {'z-score': {'z_score': 3.1, 'severity': 'moderate'}})
        self.assertEqual(result['classification'], 'review')
        values = [1, 2, 1, 10]
        result = d.moving_avg_detect(values, list(range(4)), window=3)
        self.assertEqual(set(result), {3})
        self.assertAlmostEqual(result[3]['moving_avg'], 4 / 3)
        self.assertAlmostEqual(result[3]['baseline_std'], math.sqrt(1 / 3))
        self.assertEqual(d.moving_avg_detect(values[:3], [0, 1, 2], window=3), {})

    def test_moving_overflow_does_not_erase_previous_results(self):
        text = 'x\n1\n1\n1\n10\n1e308\n-1.7e308\n1e308\n'
        result = self.scan(text, methods=['moving-avg'], window=3)
        self.assertIn(3, [a['row'] for a in result['anomalies']])
        self.assertTrue(any(w['code'] == 'numeric_overflow' and w.get('row') == 5
                            for w in result['warnings']))
        self.assertEqual(result['summary']['columns_analyzed'], ['x'])
        strict_loads(json.dumps(result, allow_nan=False))

    def test_gap_warns_insufficient_for_moving_method(self):
        result = self.scan('x\n1\n\"\"\n1\n\"\"\n1\n', methods=['moving-avg'], window=2)
        self.assertEqual(result['summary']['columns_analyzed'], [])
        self.assertIn({'code': 'insufficient_data', 'column': 'x', 'method': 'moving-avg'}, result['warnings'])

    def test_short_rows_count_missing(self):
        result = self.scan('a,b\n1\n2,\n3,4\n', columns=['a'])
        self.assertEqual(result['data_quality']['b']['missing'], 2)

    def test_extreme_finite_values_strict_json(self):
        result = self.scan('x\n1e308\n-1e308\n1e308\n-1e308\n1e308\n', z_threshold=0.1)
        strict_loads(json.dumps(result, allow_nan=False))
        self.assertTrue(any(w['code'] == 'numeric_overflow' for w in result['warnings']))
        result = self.scan('x\n0\n0\n1e-300\n1e308\n', methods=['moving-avg'], window=3)
        strict_loads(json.dumps(result, allow_nan=False))

    def test_invalid_configuration(self):
        for key in ('z_threshold', 'iqr_multiplier', 'ma_threshold'):
            for value in (0, -1, math.nan, math.inf):
                with self.subTest(key=key, value=value), self.assertRaises(ValueError):
                    self.scan('x\n1\n2\n3\n', **{key: value})
        for window in (0, 1, 2.5, True):
            with self.assertRaises(ValueError):
                self.scan('x\n1\n2\n3\n', window=window)
        for kwargs in ({'methods': ['bad']}, {'methods': []}, {'columns': ['absent']}, {'columns': []}):
            with self.assertRaises(ValueError):
                self.scan('x\n1\n2\n3\n', **kwargs)
        for method, kwargs in ((d.zscore_detect, {'threshold': 0}),
                               (d.iqr_detect, {'multiplier': math.inf}),
                               (d.moving_avg_detect, {'window': 1})):
            with self.assertRaises(ValueError):
                method([1, 2, 3], [0, 1, 2], **kwargs)

    def test_invalid_csv(self):
        for text in ('', 'x\n', 'x,x\n1,2\n', 'x,\n1,2\n', 'x, \n1,2\n',
                     'x\n1,2\n', 'x\n"unclosed\n'):
            with self.subTest(text=text), self.assertRaises(ValueError):
                self.scan(text)

    def test_non_numeric_csv_has_quality_report(self):
        result = self.scan('word\nhello\nworld\n')
        self.assertEqual(result['summary']['columns_analyzed'], [])
        self.assertEqual(result['data_quality']['word']['non_numeric'], 2)
        self.assertTrue(any(w['code'] == 'no_numeric_columns' for w in result['warnings']))

    def test_permission_error_is_concise(self):
        err = io.StringIO()
        with mock.patch.object(d, 'read_csv', side_effect=PermissionError('denied')), contextlib.redirect_stderr(err):
            self.assertNotEqual(d.main(['missing.csv']), 0)
        self.assertIn('error:', err.getvalue())
        self.assertNotIn('Traceback', err.getvalue())

    def test_output_permission_error_is_concise(self):
        err = io.StringIO()
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'data.csv'
            path.write_text('x\n1\n2\n3\n')
            with mock.patch.object(d, 'write_output', side_effect=PermissionError('denied')), contextlib.redirect_stderr(err):
                self.assertNotEqual(d.main([str(path), '--output', str(Path(tmp) / 'out.json')]), 0)
        self.assertNotIn('Traceback', err.getvalue())


class CLITests(unittest.TestCase):
    def run_cli(self, text=None, *args):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'input.csv'
            if text is not None:
                path.write_text(text, encoding='utf-8')
            return subprocess.run([sys.executable, str(SCRIPT), str(path), *args],
                                  capture_output=True, text=True)

    def test_cli_spike_and_determinism(self):
        text = 'x\n' + '1\n' * 20 + '100\n'
        first, second = self.run_cli(text), self.run_cli(text)
        self.assertEqual(first.returncode, 0, first.stderr)
        self.assertEqual(first.stdout, second.stdout)
        result = strict_loads(first.stdout)
        self.assertEqual(result['anomalies'][-1]['row'], 20)

    def test_cli_input_errors(self):
        for text in (None, '', 'x\n', 'x,x\n1,2\n', 'x\n1,2\n'):
            result = self.run_cli(text)
            self.assertNotEqual(result.returncode, 0)
            self.assertIn('error:', result.stderr)
            self.assertNotIn('Traceback', result.stderr)
            self.assertEqual(result.stdout, '')

    def test_cli_validation(self):
        for args in (('--methods', 'bad'), ('--columns', 'bad'), ('--methods', ''),
                     ('--columns', ''), ('--z-threshold', 'nan'), ('--iqr-multiplier', 'inf'),
                     ('--ma-threshold', '-1'), ('--window', '1'), ('--window', '2.5')):
            result = self.run_cli('x\n1\n2\n3\n', *args)
            self.assertNotEqual(result.returncode, 0)
            self.assertNotIn('Traceback', result.stderr)

    def test_cli_non_numeric_and_nonfinite(self):
        for text in ('x\nhello\nworld\n', '﻿x\nNaN\nInfinity\n1\n2\n3\n'):
            result = self.run_cli(text)
            self.assertEqual(result.returncode, 0, result.stderr)
            strict_loads(result.stdout)

    def test_output_success_and_failure(self):
        with tempfile.TemporaryDirectory() as tmp:
            output = Path(tmp) / 'result.json'
            result = self.run_cli('x\n1\n2\n3\n', '--output', str(output))
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(result.stdout, '')
            strict_loads(output.read_text())
            result = self.run_cli('x\n1\n2\n3\n', '--output', tmp)
            self.assertNotEqual(result.returncode, 0)
            self.assertNotIn('Traceback', result.stderr)
            result = self.run_cli('x\n1\n2\n3\n', '--output', str(Path(tmp) / 'absent' / 'out.json'))
            self.assertNotEqual(result.returncode, 0)

    def test_self_verification(self):
        result = subprocess.run([sys.executable, str(SCRIPT), '--test'], capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('OK', result.stdout)


if __name__ == '__main__':
    unittest.main()
