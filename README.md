# outlier-scan

Offline CSV anomaly screening using **only the Python standard library**. Run it directly with Python 3.10–3.14: no pip install, credentials, telemetry, or runtime network access.

This is statistical screening, **not causal explanation, forecasting, or proof that a value is wrong**. Every flag needs domain review.

## Quick start

Download the repository, then run the bundled example:

```bash
git clone https://github.com/wzdbzdnzssm/outlier-scan.git
cd outlier-scan
python3 scripts/anomaly_detector.py examples/synthetic_spike.csv
python3 scripts/anomaly_detector.py examples/synthetic_spike.csv --columns value --methods iqr,moving-avg --window 3
python3 scripts/anomaly_detector.py examples/synthetic_missing.csv --columns value --output report.json
python3 scripts/anomaly_detector.py --test
python3 -m unittest discover -s tests -v
```

All files under `examples/` are **synthetic fixtures**, not real observations or customer data. The spike fixture flags zero-based row 20 (CSV line 22).

[中文简明指南](README.zh-CN.md)

## CLI

```text
python3 scripts/anomaly_detector.py FILE.csv [options]
```

| Option | Default | Meaning |
| --- | --- | --- |
| `--methods` | `z-score,iqr,moving-avg` | Comma-separated method names |
| `--columns` | Auto-detect | Comma-separated, case-sensitive header names |
| `--z-threshold` | `3.0` | Absolute sample Z-score threshold, inclusive |
| `--iqr-multiplier` | `1.5` | IQR fence multiplier; points strictly beyond fences flag |
| `--window` | `5` | Number of prior rows in a moving baseline, integer >= 2 |
| `--ma-threshold` | `2.0` | Absolute deviation / prior-window sample standard deviation, inclusive |
| `--output` | stdout | Write strict UTF-8 JSON to a file (overwrites an existing file) |
| `--test` | Off | Run four built-in self checks without a CSV or external test files |

All thresholds must be **finite and positive**, even for methods not selected. Repeated columns and methods are deduplicated in first-seen order. Empty selections and unknown names are errors. CLI column names containing commas cannot be selected individually; the Python API supports them.

Exit status: `0` means a report was produced (even if flags or warnings exist); `1` means an input, output, or data error; `2` means argument syntax error. Errors go to stderr without tracebacks. `--output` leaves stdout empty. Do not use the input file as the output path.

## CSV and data-quality contract

- UTF-8, optionally with a BOM; comma-delimited with a required header. Surrounding header whitespace is stripped. Empty or duplicate names (after stripping), malformed quoting, excess fields, empty files, and header-only files are rejected. A header is mandatory: a headerless file cannot be distinguished reliably from ordinary string headers.
- Short records are padded with missing values. Fully empty physical lines are ignored; a quoted empty field or a comma-only record is a missing observation, not a skipped record.
- Blank values are `missing`; nonblank float syntax errors are `non_numeric`; NaN, Infinity and float overflows such as `1e9999` are `nonfinite`. These are excluded from calculations, not converted to zero. Counts for **every input column**, including unselected text columns, appear in `data_quality` and produce `invalid_values` warnings.
- Auto-detection selects columns with at least one finite number and at least 80% numeric values among finite numbers plus non-numeric nonblank strings. Missing and nonfinite cells do not enter this fraction. Use `--columns` to override. Entirely non-numeric/nonfinite input produces an empty analysis with `no_numeric_columns`, not an apparently clean numeric result.
- `row` is a zero-based data-record index after ignored blank lines. `csv_line` is the one-based **starting physical line** of the record, including the header; quoted multiline records are handled correctly.
- Output order is deterministic: column selection order, then row index; methods retain selection order. Reports contain no timestamps or absolute input paths. Same input/options on the same Python version produce identical JSON; floating-point last digits may differ between Python versions.

## Detection semantics

### Z-score (retrospective)

Uses the entire column's finite values and sample standard deviation (`n-1`), with at least three finite values. Constant columns produce no flags. Later observations **can change earlier flags**. Small samples can make a high threshold mathematically unreachable; no flags does not establish normality.

### IQR (retrospective)

Uses the entire column, at least four finite values, and linearly interpolated quartiles at `(n-1)*0.25` and `(n-1)*0.75`. Flags values beyond `Q1 - multiplier*IQR` or `Q3 + multiplier*IQR`. If IQR is zero, values outside the collapsed fence still flag: `[1,1,1,1,100]` flags `100`. Constant data does not flag. Later observations **can change earlier flags**.

### Moving average (past-only)

For row `i`, baseline mean and sample standard deviation use exactly the preceding `window` data records, **excluding row i and all future rows**. A full prior window is required. All baseline records and the current observation must be finite numbers and contiguous in original data-record indices. Missing, non-numeric, or nonfinite observations break the window: it resumes only after a full consecutive valid baseline. It never compresses separated numeric values into an artificial contiguous window.

A nonzero deviation from a zero-variance baseline flags with `baseline_zero_variance: true` and `deviation_ratio: null` (not Infinity). Equal values do not flag. Ratios beyond float range are also `null`, with `ratio_overflow: true`. These indicators distinguish undefined or unrepresentable ratios from ordinary finite ones.

Appending observations does not change earlier **moving-average** verdicts/details. Whole-column statistics, Z/IQR results, data quality, and combined review priorities are not prefix-invariant. Row order is the only time axis: this is **not calendar-aware**, does not parse dates, and does not infer sampling frequency or missing dates. Sort and validate time order yourself.

## Reading the report

- `summary.columns_requested`: selected/autodetected columns.
- `summary.columns_analyzed`: only columns for which at least one selected detector actually completed an eligible analysis, not merely columns requested or having descriptive statistics.
- `column_stats`: finite-value counts, descriptive statistics where representable, `methods_analyzed`, and anomaly counts. Selected tiny/all-missing columns remain visible with counts.
- `warnings`: `insufficient_data` per ineligible column/method, `invalid_values`, `no_numeric_columns`, or `numeric_overflow`. A skipped detector is not evidence of no anomalies. Inspect warnings before acting on a report.
- `anomalies`: one item per flagged row/column, method-specific details and a review priority.
- `data_quality`: mutually exclusive per-column counts whose sum equals `total_rows`.

`high_priority` means two or more methods flag a value, or a method calls its deviation extreme. Extreme means absolute Z-score >= 1.5 times its threshold; IQR distance from the nearer quartile > `max(3, multiplier)*IQR` (or outside a zero-IQR fence); moving ratio >= twice its threshold (or nonzero deviation with zero baseline variance). Other flags are `review`. These are **heuristic triage labels**, not calibrated risk probabilities, independent corroboration, or causal claims. No value is labeled “explainable.”

Finite inputs can still overflow statistical arithmetic. Unrepresentable whole-column statistics or detectors are skipped with `numeric_overflow`; a moving-window overflow skips **only that verdict** and warns with its row, preserving previous verdicts. Strict serialization uses `allow_nan=False`: JSON never contains non-standard NaN/Infinity tokens.

## Python API

```python
from scripts.anomaly_detector import scan_csv

report = scan_csv(
    'examples/synthetic_spike.csv',
    columns=['value'],
    methods=['iqr', 'moving-avg'],
    window=3,
)
print(report['summary'])
```

`scan_csv` is the supported high-level boundary: it validates CSV/options and returns a JSON-compatible dictionary. It raises `ValueError` for invalid data/configuration and `OSError` for file access errors. Lower-level `try_float`, `zscore_detect`, `iqr_detect`, and `moving_avg_detect` are available for finite numeric sequences with matching, strictly increasing integer original-row indices. Moving-average overflow skips are optionally collected via its `warnings` list argument. These helpers do not impute data or parse CSV strings; use `extract_column_values` when preparing CSV-derived values.

## Limits and safety

- Reads the entire CSV into memory. Z/IQR sorting is O(n log n); moving baselines recompute statistics in O(n * window). Not a streaming or large-data engine.
- Float precision, tiny samples, seasonality, trends, skew, correlated methods, and multiple comparisons can all affect false positives/negatives. Thresholds are not universal confidence levels. Preprocess units and timestamps and review data quality.
- No runtime network calls, telemetry, subprocesses, packages, or credential discovery. Only the specified input is read and the optional output is written. Reports can contain sensitive **input values and headers**: protect them accordingly. Writing an output file is not atomic and can leave a partial file on I/O failure.
- GitHub Actions installs the selected Python via hosted actions; CI infrastructure uses the network, unlike the scanner runtime.

## Development and provenance

Run the unittest command above and `--test` before changes. CI covers Python 3.10, 3.11, 3.12, 3.13, and 3.14 with no pip dependencies, plus both synthetic examples. Tests cover the original nonfinite/IQR/future-data defects, public API, CLI validation and failures, missing data, strict JSON, and original row positions.

Derived from the original `outlier-scan` skill's `scripts/anomaly_detector.py`. Its original metadata declares `license: MIT`; the unmodified public `SKILL.md` is retained at [provenance/SKILL.md](provenance/SKILL.md) as evidence and historical context. That document describes the old behavior, **not this product's contract**. No private skill state or configuration was copied. The supplied original files named no copyright holder; no upstream identity is invented here. See [LICENSE](LICENSE).
