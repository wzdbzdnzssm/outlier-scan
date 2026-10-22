#!/usr/bin/env python3
"""Offline, standard-library CSV anomaly screening; derived from the MIT outlier-scan skill."""

import argparse
import csv
import json
import math
import statistics
import sys

METHODS = ('z-score', 'iqr', 'moving-avg')
CAVEAT = ('Review priorities are statistical heuristics, not causal explanations, '
          'proof of error, or forecasts.')


class NumericOverflow(ValueError):
    pass


class CSVRow(dict):
    def __init__(self, values, csv_line):
        super().__init__(values)
        self.csv_line = csv_line


def read_csv(filepath):
    rows = []
    with open(filepath, 'r', encoding='utf-8-sig', newline='') as stream:
        reader = csv.reader(stream, strict=True)
        try:
            headers = [name.strip() for name in next(reader)]
        except StopIteration:
            raise ValueError('CSV is empty or missing a header') from None
        except csv.Error as error:
            raise ValueError(f'malformed CSV: {error}') from None
        if not headers or any(not name for name in headers):
            raise ValueError('CSV headers must not be empty')
        if len(set(headers)) != len(headers):
            raise ValueError('CSV headers must be unique')
        while True:
            line = reader.line_num + 1
            try:
                fields = next(reader)
            except StopIteration:
                break
            except csv.Error as error:
                raise ValueError(f'malformed CSV near line {line}: {error}') from None
            if not fields:
                continue
            if len(fields) > len(headers):
                raise ValueError(f'CSV line {line} has more fields than headers')
            fields += [None] * (len(headers) - len(fields))
            rows.append(CSVRow(zip(headers, fields), line))
    if not rows:
        raise ValueError('CSV has no data rows')
    return headers, rows


def _value_kind(value):
    if value is None or not str(value).strip():
        return 'missing'
    try:
        number = float(str(value).strip())
    except (ValueError, OverflowError):
        return 'non_numeric'
    return 'valid_numeric' if math.isfinite(number) else 'nonfinite'


def try_float(value):
    if _value_kind(value) != 'valid_numeric':
        return None
    return float(str(value).strip())


def data_quality(headers, rows):
    result = {}
    for column in headers:
        counts = dict.fromkeys(('valid_numeric', 'missing', 'nonfinite', 'non_numeric'), 0)
        for row in rows:
            counts[_value_kind(row.get(column))] += 1
        result[column] = counts
    return result


def detect_numeric_columns(headers, rows):
    quality = data_quality(headers, rows)
    return [column for column in headers
            if quality[column]['valid_numeric'] > 0
            and quality[column]['valid_numeric'] / (
                quality[column]['valid_numeric'] + quality[column]['non_numeric']) >= 0.8]


def extract_column_values(rows, col):
    values, indices = [], []
    for index, row in enumerate(rows):
        value = try_float(row.get(col))
        if value is not None:
            values.append(value)
            indices.append(index)
    return values, indices


def _finite(value):
    if not math.isfinite(value):
        raise NumericOverflow('statistics exceed finite floating-point range')
    return value


def _interpolate(sorted_vals, idx):
    lower, upper = math.floor(idx), math.ceil(idx)
    if lower == upper or sorted_vals[lower] == sorted_vals[upper]:
        return sorted_vals[lower]
    fraction = idx - lower
    return _finite(sorted_vals[lower] * (1 - fraction) + sorted_vals[upper] * fraction)


def compute_stats(values):
    if len(values) < 2:
        return None
    ordered = sorted(values)
    q1 = _interpolate(ordered, (len(values) - 1) * 0.25)
    q3 = _interpolate(ordered, (len(values) - 1) * 0.75)
    return {
        'mean': _finite(statistics.mean(values)),
        'std': _finite(statistics.stdev(values)),
        'min': min(values), 'max': max(values),
        'q1': q1, 'median': _interpolate(ordered, (len(values) - 1) * 0.5),
        'q3': q3, 'iqr': _finite(q3 - q1), 'count': len(values),
    }


def _positive(name, value):
    if isinstance(value, bool) or not math.isfinite(value) or value <= 0:
        raise ValueError(f'{name} must be finite and positive')


def _window(window):
    if isinstance(window, bool) or not isinstance(window, int) or window < 2:
        raise ValueError('window must be an integer >= 2')


def zscore_detect(values, indices, threshold=3.0):
    _positive('z_threshold', threshold)
    if len(values) < 3:
        return {}
    mean = _finite(statistics.mean(values))
    std = _finite(statistics.stdev(values))
    if std == 0:
        return {}
    anomalies = {}
    for value, index in zip(values, indices):
        z = _finite(_finite(value - mean) / std)
        if abs(z) >= threshold:
            anomalies[index] = {
                'z_score': z, 'mean': mean, 'std': std,
                'severity': 'extreme' if abs(z) / threshold >= 1.5 else 'moderate',
            }
    return anomalies


def iqr_detect(values, indices, multiplier=1.5):
    _positive('iqr_multiplier', multiplier)
    if len(values) < 4:
        return {}
    ordered = sorted(values)
    q1 = _interpolate(ordered, (len(values) - 1) * 0.25)
    q3 = _interpolate(ordered, (len(values) - 1) * 0.75)
    iqr = _finite(q3 - q1)
    lower = _finite(q1 - multiplier * iqr)
    upper = _finite(q3 + multiplier * iqr)
    anomalies = {}
    for value, index in zip(values, indices):
        if value < lower or value > upper:
            distance = _finite(max(q1 - value, value - q3))
            extreme = iqr == 0 or distance / iqr > max(3.0, multiplier)
            anomalies[index] = {
                'iqr_bounds': [lower, upper], 'q1': q1, 'q3': q3, 'iqr': iqr,
                'severity': 'extreme' if extreme else 'moderate',
            }
    return anomalies


def _eligible_windows(indices, window):
    # Strictly increasing original row indices keep missing observations from closing gaps.
    if any(b <= a for a, b in zip(indices, indices[1:])):
        raise ValueError('indices must be strictly increasing')
    return [i for i in range(window, len(indices))
            if indices[i] - indices[i - window] == window]


def moving_avg_detect(values, indices, window=5, threshold=2.0, warnings=None):
    _window(window)
    _positive('ma_threshold', threshold)
    if len(values) != len(indices):
        raise ValueError('values and indices must have equal lengths')
    anomalies = {}
    for i in _eligible_windows(indices, window):
        prior = values[i - window:i]
        try:
            mean = _finite(statistics.mean(prior))
            std = _finite(statistics.stdev(prior))
            deviation = abs(_finite(values[i] - mean))
        except (OverflowError, NumericOverflow):
            if warnings is not None:
                warnings.append({'code': 'numeric_overflow', 'row': indices[i]})
            continue
        if deviation == 0:
            continue
        ratio = deviation / std if std else None
        if std == 0 or ratio >= threshold:
            ratio_overflow = ratio is not None and not math.isfinite(ratio)
            anomalies[indices[i]] = {
                'moving_avg': mean, 'baseline_std': std, 'deviation': deviation,
                'deviation_ratio': None if ratio_overflow else ratio,
                'baseline_zero_variance': std == 0, 'ratio_overflow': ratio_overflow,
                'window': window,
                'severity': 'extreme' if std == 0 or ratio / threshold >= 2 else 'moderate',
            }
    return anomalies


def classify_anomaly(row_idx, column, value, method_results):
    methods = list(method_results)
    high = len(methods) >= 2 or any(info['severity'] == 'extreme' for info in method_results.values())
    return {
        'row': row_idx, 'column': column, 'value': value,
        'methods_detected': methods, 'num_methods': len(methods),
        'details': {method: {key: val for key, val in info.items() if key != 'severity'}
                    for method, info in method_results.items()},
        'classification': 'high_priority' if high else 'review',
        'reason': ('Multiple methods or an extreme statistical deviation; prioritize review.'
                   if high else 'Single-method statistical deviation; review in context.'),
    }


def _validate(methods, z_threshold, iqr_multiplier, window, ma_threshold):
    methods = list(dict.fromkeys(methods))
    if not methods or any(method not in METHODS for method in methods):
        raise ValueError('methods must be a nonempty selection of: ' + ', '.join(METHODS))
    _positive('z_threshold', z_threshold)
    _positive('iqr_multiplier', iqr_multiplier)
    _positive('ma_threshold', ma_threshold)
    _window(window)
    return methods


def run_detection(rows, columns, methods, z_threshold=3.0, iqr_multiplier=1.5,
                  window=5, ma_threshold=2.0, warnings=None):
    methods = _validate(methods, z_threshold, iqr_multiplier, window, ma_threshold)
    warnings = warnings if warnings is not None else []
    anomalies, column_stats = [], {}
    detectors = {
        'z-score': lambda values, indices: zscore_detect(values, indices, z_threshold),
        'iqr': lambda values, indices: iqr_detect(values, indices, iqr_multiplier),
    }
    for col in dict.fromkeys(columns):
        values, indices = extract_column_values(rows, col)
        stats = {'count': len(values)}
        try:
            stats.update(compute_stats(values) or {})
        except (OverflowError, NumericOverflow):
            warnings.append({'code': 'numeric_overflow', 'column': col, 'method': 'column_stats'})
        row_methods, processed = {}, []
        for method in methods:
            eligible = (len(values) >= 3 if method == 'z-score' else
                        len(values) >= 4 if method == 'iqr' else
                        bool(_eligible_windows(indices, window)))
            if not eligible:
                warnings.append({'code': 'insufficient_data', 'column': col, 'method': method})
                continue
            try:
                if method == 'moving-avg':
                    window_warnings = []
                    results = moving_avg_detect(values, indices, window, ma_threshold, window_warnings)
                    warnings.extend(dict(item, column=col, method=method) for item in window_warnings)
                    if len(window_warnings) == len(_eligible_windows(indices, window)):
                        continue
                else:
                    results = detectors[method](values, indices)
            except (OverflowError, NumericOverflow):
                warnings.append({'code': 'numeric_overflow', 'column': col, 'method': method})
                continue
            processed.append(method)
            for index, info in results.items():
                row_methods.setdefault(index, {})[method] = info
        for index, results in sorted(row_methods.items()):
            anomaly = classify_anomaly(index, col, try_float(rows[index].get(col)), results)
            anomaly['csv_line'] = getattr(rows[index], 'csv_line', None)
            anomalies.append(anomaly)
        stats.update(anomaly_count=len(row_methods), methods_analyzed=processed)
        column_stats[col] = stats
    return anomalies, column_stats


def build_output(rows, columns, methods, anomalies, column_stats):
    return {
        'summary': {
            'total_rows': len(rows), 'columns_requested': list(dict.fromkeys(columns)),
            'columns_analyzed': [col for col, stats in column_stats.items() if stats['methods_analyzed']],
            'methods_used': list(dict.fromkeys(methods)),
            'total_anomalies': len(anomalies),
            'high_priority': sum(a['classification'] == 'high_priority' for a in anomalies),
            'review': sum(a['classification'] == 'review' for a in anomalies),
        },
        'caveat': CAVEAT, 'anomalies': anomalies, 'column_stats': column_stats,
    }


def scan_csv(filepath, columns=None, methods=METHODS, z_threshold=3.0,
             iqr_multiplier=1.5, window=5, ma_threshold=2.0):
    methods = _validate(methods, z_threshold, iqr_multiplier, window, ma_threshold)
    headers, rows = read_csv(filepath)
    if columns is None:
        columns = detect_numeric_columns(headers, rows)
    else:
        columns = list(dict.fromkeys(columns))
        if not columns or any(col not in headers for col in columns):
            raise ValueError('unknown or empty columns selection; available: ' + ', '.join(headers))
    warnings = []
    if not columns:
        warnings.append({'code': 'no_numeric_columns'})
    quality = data_quality(headers, rows)
    for col, counts in quality.items():
        if counts['missing'] or counts['nonfinite'] or counts['non_numeric']:
            warnings.append({'code': 'invalid_values', 'column': col,
                             'message': 'See data_quality counts; excluded from numeric analysis.'})
    anomalies, stats = run_detection(rows, columns, methods, z_threshold, iqr_multiplier,
                                    window, ma_threshold, warnings)
    output = build_output(rows, columns, methods, anomalies, stats)
    output.update(data_quality=quality, warnings=warnings)
    return output


def write_output(text, filepath=None):
    if filepath:
        with open(filepath, 'w', encoding='utf-8', newline='\n') as stream:
            stream.write(text + '\n')
    else:
        sys.stdout.write(text + '\n')
        sys.stdout.flush()


def self_test():
    import unittest

    class Checks(unittest.TestCase):
        def test_finite(self):
            self.assertIsNone(try_float('NaN'))
            self.assertIsNone(try_float('Infinity'))

        def test_iqr(self):
            self.assertEqual(set(iqr_detect([1, 1, 1, 1, 100], list(range(5)))), {4})

        def test_moving_prefix(self):
            values = [1, 2, 1, 2, 10, 1, 2]
            before = moving_avg_detect(values, list(range(7)), window=3)
            after = moving_avg_detect(values + [10000], list(range(8)), window=3)
            self.assertIn(4, before)
            self.assertEqual(before, {i: info for i, info in after.items() if i < 7})

        def test_constant_and_gap(self):
            self.assertEqual(moving_avg_detect([1] * 8, list(range(8))), {})
            self.assertEqual(moving_avg_detect([1, 1, 10], [0, 2, 3], window=2), {})
            json.dumps(moving_avg_detect([1, 1, 10], [0, 1, 2], window=2), allow_nan=False)

    result = unittest.TextTestRunner(stream=sys.stdout, verbosity=2).run(
        unittest.defaultTestLoader.loadTestsFromTestCase(Checks))
    return 0 if result.wasSuccessful() else 1


def main(argv=None):
    parser = argparse.ArgumentParser(description='Offline CSV anomaly screening (not causal explanation).')
    parser.add_argument('csv_file', nargs='?', help='UTF-8 CSV with a header')
    parser.add_argument('--test', action='store_true', help='run built-in self verification')
    parser.add_argument('--methods', default=','.join(METHODS), help='comma-separated methods: ' + ','.join(METHODS))
    parser.add_argument('--columns', help='comma-separated column names; default: auto-detect')
    parser.add_argument('--z-threshold', type=float, default=3.0)
    parser.add_argument('--iqr-multiplier', type=float, default=1.5)
    parser.add_argument('--window', type=int, default=5)
    parser.add_argument('--ma-threshold', type=float, default=2.0)
    parser.add_argument('--output', help='write JSON to this file instead of stdout')
    args = parser.parse_args(argv)
    if args.test:
        if args.csv_file:
            parser.error('--test does not accept a CSV file')
        return self_test()
    if not args.csv_file:
        parser.error('a CSV file is required unless --test is used')
    try:
        result = scan_csv(
            args.csv_file,
            columns=[col.strip() for col in args.columns.split(',')] if args.columns is not None else None,
            methods=[method.strip() for method in args.methods.split(',')],
            z_threshold=args.z_threshold, iqr_multiplier=args.iqr_multiplier,
            window=args.window, ma_threshold=args.ma_threshold,
        )
        text = json.dumps(result, ensure_ascii=False, indent=2, allow_nan=False)
        write_output(text, args.output)
    except (OSError, ValueError, csv.Error, OverflowError) as error:
        print(f'outlier-scan: error: {error}', file=sys.stderr)
        return 1
    return 0


if __name__ == '__main__':
    sys.exit(main())
