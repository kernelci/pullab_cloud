"""Benchmark regression detection for kernel CI test results."""

__authors__ = ["Max Hubmann <mxhbm@amazon.de>", "Norbert Manthey <nmanthey@amazon.de>"]
__copyright__ = "Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved."
# SPDX-License-Identifier: Apache-2.0

import csv
import io
import math
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

from kernel_ci_cloud_labs.core.logging_config import get_logger

logger = get_logger(__name__)

# Significance threshold for p-values
P_VALUE_THRESHOLD = 0.05
# Cohen's d threshold for meaningful effect size
COHENS_D_THRESHOLD = 0.5


@dataclass
class MetricStats:
    """Descriptive statistics for a single metric's value distribution."""

    values: List[float]
    mean: float = 0.0
    median: float = 0.0
    stddev: float = 0.0
    cv: float = 0.0  # coefficient of variation

    def __post_init__(self):
        n = len(self.values)
        if n == 0:
            return
        self.mean = sum(self.values) / n
        sorted_v = sorted(self.values)
        if n % 2 == 1:
            self.median = sorted_v[n // 2]
        else:
            self.median = (sorted_v[n // 2 - 1] + sorted_v[n // 2]) / 2.0
        if n > 1:
            variance = sum((x - self.mean) ** 2 for x in self.values) / (n - 1)
            self.stddev = math.sqrt(variance)
        self.cv = (self.stddev / abs(self.mean)) if self.mean != 0 else 0.0


@dataclass
class MetricComparison:
    """Statistical comparison of base vs tip for one metric."""

    metric: str
    unit: str
    more_is_better: bool
    base: MetricStats
    tip: MetricStats
    pct_change: float = 0.0
    t_statistic: float = 0.0
    t_pvalue: float = 1.0
    u_statistic: float = 0.0
    u_pvalue: float = 1.0
    cohens_d: float = 0.0
    is_regression: bool = False

    def __post_init__(self):
        if self.base.mean != 0:
            self.pct_change = ((self.tip.mean - self.base.mean) / abs(self.base.mean)) * 100.0
        self._compute_tests()
        self._detect_regression()

    def _compute_tests(self):
        """Compute t-test, Mann-Whitney U, and Cohen's d."""
        base_v, tip_v = self.base.values, self.tip.values
        if len(base_v) < 2 or len(tip_v) < 2:
            return

        # Welch's t-test (unequal variance)
        self.t_statistic, self.t_pvalue = _welch_t_test(base_v, tip_v)
        # Mann-Whitney U test
        self.u_statistic, self.u_pvalue = _mann_whitney_u(base_v, tip_v)
        # Cohen's d (pooled)
        self.cohens_d = _cohens_d(base_v, tip_v)

    def _detect_regression(self):
        """A regression requires significant p-value AND meaningful effect size."""
        significant = self.t_pvalue < P_VALUE_THRESHOLD or self.u_pvalue < P_VALUE_THRESHOLD
        meaningful = abs(self.cohens_d) >= COHENS_D_THRESHOLD
        if not (significant and meaningful):
            self.is_regression = False
            return
        # Direction check: regression means performance got worse
        if self.more_is_better:
            self.is_regression = self.pct_change < 0
        else:
            self.is_regression = self.pct_change > 0


@dataclass
class TestBenchmarkResult:
    """Benchmark analysis result for a single test across all VMs."""

    test_name: str
    comparisons: List[MetricComparison] = field(default_factory=list)
    base_kernel: str = ""
    tip_kernel: str = ""

    @property
    def regressions(self) -> List[MetricComparison]:
        return [c for c in self.comparisons if c.is_regression]

    @property
    def has_regression(self) -> bool:
        return len(self.regressions) > 0


@dataclass
class PipelineBenchmarkSummary:
    """Summary of benchmark analysis across all tests in a pipeline run."""

    test_results: List[TestBenchmarkResult] = field(default_factory=list)
    total_tests: int = 0
    successful_tests: int = 0
    failed_tests: int = 0
    failed_test_names: List[str] = field(default_factory=list)
    tests_with_regression: int = 0
    regression_test_names: List[str] = field(default_factory=list)


class BenchmarkAnalyzer:
    """Downloads benchmark CSVs from S3 and performs regression analysis."""

    def __init__(self, s3_client, bucket: str, run_prefix: str):
        self.s3 = s3_client
        self.bucket = bucket
        self.run_prefix = run_prefix

    def analyze(
        self, test_names: List[str], vm_success_map: Optional[Dict[str, bool]] = None
    ) -> PipelineBenchmarkSummary:
        """Analyze benchmark results for all tests that produced CSV files.

        Args:
            test_names: list of test names from the run
            vm_success_map: optional dict of {test_name: success_bool}
        """
        summary = PipelineBenchmarkSummary()
        summary.total_tests = len(test_names)

        if vm_success_map:
            summary.successful_tests = sum(1 for v in vm_success_map.values() if v)
            summary.failed_tests = sum(1 for v in vm_success_map.values() if not v)
            summary.failed_test_names = [k for k, v in vm_success_map.items() if not v]

        for test_name in test_names:
            result = self._analyze_test(test_name)
            if result:
                summary.test_results.append(result)
                if result.has_regression:
                    summary.tests_with_regression += 1
                    summary.regression_test_names.append(test_name)

        return summary

    def _analyze_test(self, test_name: str) -> Optional[TestBenchmarkResult]:
        """Analyze a single test by downloading its benchmark CSVs from all VMs."""
        prefix = f"{self.run_prefix}/test_{test_name}/output/"
        base_rows, tip_rows = [], []

        try:
            paginator = self.s3.get_paginator("list_objects_v2")
            for page in paginator.paginate(Bucket=self.bucket, Prefix=prefix):
                for obj in page.get("Contents", []):
                    key = obj["Key"]
                    if not key.endswith(".csv") or "benchmark-" not in key:
                        continue
                    rows = self._download_csv(key)
                    if "benchmark-base-" in key:
                        base_rows.extend(rows)
                    elif "benchmark-tip-" in key:
                        tip_rows.extend(rows)
        except Exception as e:
            logger.warning("Failed to read benchmark data for test '%s': %s", test_name, e)
            return None

        logger.info("Test '%s': %d base rows, %d tip rows", test_name, len(base_rows), len(tip_rows))

        if not base_rows or not tip_rows:
            return None

        return self._compare(test_name, base_rows, tip_rows)

    def _download_csv(self, key: str) -> List[Dict[str, str]]:
        """Download and parse a CSV file from S3."""
        resp = self.s3.get_object(Bucket=self.bucket, Key=key)
        content = resp["Body"].read().decode("utf-8")
        reader = csv.DictReader(io.StringIO(content))
        return list(reader)

    def _compare(self, test_name: str, base_rows: List[dict], tip_rows: List[dict]) -> TestBenchmarkResult:
        """Compare base vs tip metrics across all VMs."""
        result = TestBenchmarkResult(test_name=test_name)

        # Extract kernel versions
        if base_rows:
            result.base_kernel = base_rows[0].get("kernel_version", "unknown")
        if tip_rows:
            result.tip_kernel = tip_rows[0].get("kernel_version", "unknown")

        # Group values by metric
        base_by_metric = _group_by_metric(base_rows)
        tip_by_metric = _group_by_metric(tip_rows)

        # Compare metrics present in both
        for metric in sorted(set(base_by_metric) & set(tip_by_metric)):
            b_vals, b_unit, b_mib = base_by_metric[metric]
            t_vals, _, _ = tip_by_metric[metric]

            comparison = MetricComparison(
                metric=metric,
                unit=b_unit,
                more_is_better=b_mib,
                base=MetricStats(b_vals),
                tip=MetricStats(t_vals),
            )
            result.comparisons.append(comparison)

        return result


def _group_by_metric(rows: List[dict]) -> Dict[str, Tuple[List[float], str, bool]]:
    """Group CSV rows by metric name, returning (values, unit, more_is_better)."""
    groups: Dict[str, Tuple[List[float], str, bool]] = {}
    for row in rows:
        metric = row.get("metric", "")
        if not metric:
            continue
        try:
            val = float(row.get("value", 0))
        except (ValueError, TypeError):
            continue
        unit = row.get("unit", "")
        mib = row.get("more_is_better", "true").lower() == "true"
        if metric not in groups:
            groups[metric] = ([], unit, mib)
        groups[metric][0].append(val)
    return groups


# --- Pure-Python statistical tests (no scipy/numpy dependency) ---


def _welch_t_test(a: List[float], b: List[float]) -> Tuple[float, float]:
    """Welch's t-test for two independent samples with unequal variance."""
    n_a, n_b = len(a), len(b)
    if n_a < 2 or n_b < 2:
        return 0.0, 1.0

    mean_a = sum(a) / n_a
    mean_b = sum(b) / n_b
    var_a = sum((x - mean_a) ** 2 for x in a) / (n_a - 1)
    var_b = sum((x - mean_b) ** 2 for x in b) / (n_b - 1)

    se = math.sqrt(var_a / n_a + var_b / n_b)
    if se == 0:
        return 0.0, 1.0

    t_stat = (mean_a - mean_b) / se

    # Welch-Satterthwaite degrees of freedom
    num = (var_a / n_a + var_b / n_b) ** 2
    denom = (var_a / n_a) ** 2 / (n_a - 1) + (var_b / n_b) ** 2 / (n_b - 1)
    df = num / denom if denom > 0 else 1.0

    p_value = _t_distribution_two_tailed_p(abs(t_stat), df)
    return t_stat, p_value


def _mann_whitney_u(a: List[float], b: List[float]) -> Tuple[float, float]:
    """Mann-Whitney U test (two-tailed, normal approximation for n >= 8)."""
    n_a, n_b = len(a), len(b)

    # Rank all values together
    combined = [(v, 0) for v in a] + [(v, 1) for v in b]
    combined.sort(key=lambda x: x[0])

    # Assign ranks with tie handling
    ranks = [0.0] * len(combined)
    i = 0
    while i < len(combined):
        j = i
        while j < len(combined) and combined[j][0] == combined[i][0]:
            j += 1
        avg_rank = (i + j + 1) / 2.0  # 1-based average rank
        for k in range(i, j):
            ranks[k] = avg_rank
        i = j

    r_a = sum(ranks[i] for i in range(len(combined)) if combined[i][1] == 0)
    u_a = r_a - n_a * (n_a + 1) / 2.0
    u_b = n_a * n_b - u_a
    u_stat = min(u_a, u_b)

    # Normal approximation
    mu = n_a * n_b / 2.0
    sigma = math.sqrt(n_a * n_b * (n_a + n_b + 1) / 12.0)
    if sigma == 0:
        return u_stat, 1.0

    z = (u_stat - mu) / sigma
    p_value = 2.0 * _normal_cdf(z)  # two-tailed
    return u_stat, p_value


def _cohens_d(a: List[float], b: List[float]) -> float:
    """Cohen's d effect size with pooled standard deviation."""
    n_a, n_b = len(a), len(b)
    if n_a < 2 or n_b < 2:
        return 0.0

    mean_a = sum(a) / n_a
    mean_b = sum(b) / n_b
    var_a = sum((x - mean_a) ** 2 for x in a) / (n_a - 1)
    var_b = sum((x - mean_b) ** 2 for x in b) / (n_b - 1)

    pooled_std = math.sqrt(((n_a - 1) * var_a + (n_b - 1) * var_b) / (n_a + n_b - 2))
    if pooled_std == 0:
        return 0.0
    return (mean_a - mean_b) / pooled_std


def _normal_cdf(z: float) -> float:
    """Approximate standard normal CDF using Abramowitz & Stegun."""
    return 0.5 * math.erfc(-z / math.sqrt(2))


def _t_distribution_two_tailed_p(t: float, df: float) -> float:
    """Approximate two-tailed p-value for t-distribution.

    Uses the regularized incomplete beta function approximation.
    For large df, converges to normal distribution.
    """
    if df <= 0:
        return 1.0
    # For large df, use normal approximation
    if df > 100:
        return 2.0 * (1.0 - _normal_cdf(t))

    # Beta function approximation: p = I(df/(df+t^2), df/2, 1/2)
    x = df / (df + t * t)
    p = _regularized_incomplete_beta(x, df / 2.0, 0.5)
    return max(0.0, min(1.0, p))


def _regularized_incomplete_beta(x: float, a: float, b: float, max_iter: int = 200) -> float:
    """Regularized incomplete beta function via continued fraction (Lentz's method)."""
    if x <= 0:
        return 0.0
    if x >= 1:
        return 1.0

    # Use the log-beta for numerical stability
    lbeta = math.lgamma(a) + math.lgamma(b) - math.lgamma(a + b)
    front = math.exp(a * math.log(x) + b * math.log(1.0 - x) - lbeta) / a

    # Continued fraction
    f = 1.0
    c = 1.0
    d = 1.0 - (a + b) * x / (a + 1.0)
    if abs(d) < 1e-30:
        d = 1e-30
    d = 1.0 / d
    f = d

    for m in range(1, max_iter + 1):
        # Even step
        numerator = m * (b - m) * x / ((a + 2 * m - 1) * (a + 2 * m))
        d = 1.0 + numerator * d
        if abs(d) < 1e-30:
            d = 1e-30
        c = 1.0 + numerator / c
        if abs(c) < 1e-30:
            c = 1e-30
        d = 1.0 / d
        f *= d * c

        # Odd step
        numerator = -((a + m) * (a + b + m) * x) / ((a + 2 * m) * (a + 2 * m + 1))
        d = 1.0 + numerator * d
        if abs(d) < 1e-30:
            d = 1e-30
        c = 1.0 + numerator / c
        if abs(c) < 1e-30:
            c = 1e-30
        d = 1.0 / d
        delta = d * c
        f *= delta

        if abs(delta - 1.0) < 1e-10:
            break

    return front * f


def log_benchmark_summary(summary: PipelineBenchmarkSummary):
    """Log a human-readable benchmark analysis summary."""
    if not summary.test_results:
        logger.info("No benchmark data found for this run")
        return

    logger.info("")
    logger.info("=" * 60)
    logger.info("BENCHMARK REGRESSION ANALYSIS")
    logger.info("=" * 60)

    for result in summary.test_results:
        logger.info("")
        logger.info("Test: %s", result.test_name)
        logger.info("  Base kernel: %s", result.base_kernel)
        logger.info("  Tip kernel:  %s", result.tip_kernel)
        logger.info("  Metrics compared: %d", len(result.comparisons))

        if result.regressions:
            logger.info("  ⚠ REGRESSIONS DETECTED: %d", len(result.regressions))
            for c in result.regressions:
                logger.info(
                    "    %s: base=%.2f±%.2f (cv: %.2f) → tip=%.2f±%.2f (cv: %.2f) %s (%+.1f%%) "
                    "[t-test p=%.4f, U-test p=%.4f, Cohen's d=%.2f]",
                    c.metric,
                    c.base.mean,
                    c.base.stddev,
                    c.base.cv,
                    c.tip.mean,
                    c.tip.stddev,
                    c.tip.cv,
                    c.unit,
                    c.pct_change,
                    c.t_pvalue,
                    c.u_pvalue,
                    c.cohens_d,
                )
        else:
            logger.info("  ✓ No regressions detected")

    logger.info("")
    logger.info("-" * 60)
    logger.info(
        "Tests with benchmarks: %d | Regressions found: %d",
        len(summary.test_results),
        summary.tests_with_regression,
    )
    if summary.regression_test_names:
        logger.info("Tests with regressions: %s", ", ".join(summary.regression_test_names))
    logger.info("=" * 60)

    # NOTIFICATION HOOK: Add downstream notifications here, e.g.:
    #   - Publish to SNS topic for regression alerts
    #   - Post to KernelCI KCIDB for centralized reporting
    #   - Send Slack/email notifications
    #   - Trigger follow-up CI jobs for bisection
    # The PipelineBenchmarkSummary dataclass provides structured data
    # for building notification payloads (regression_test_names,
    # per-metric stats, p-values, effect sizes).
