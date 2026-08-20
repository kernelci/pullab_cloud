"""Unit tests for benchmark regression/improvement classification."""

__authors__ = ["Norbert Manthey <nmanthey@amazon.de>"]
__copyright__ = "Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved."
# SPDX-License-Identifier: Apache-2.0

from kernel_ci_cloud_labs.core.benchmark_analyzer import MetricComparison, MetricStats


def _cmp(base_values, tip_values, unit="lps", more_is_better=True):
    return MetricComparison(
        metric="test.metric",
        unit=unit,
        more_is_better=more_is_better,
        base=MetricStats(base_values),
        tip=MetricStats(tip_values),
    )


class TestChangeClassification:
    def test_regression_more_is_better_goes_down(self):
        # Throughput (more is better) drops significantly -> regression.
        c = _cmp([1000, 1005, 995, 1002], [800, 795, 805, 798], more_is_better=True)
        assert c.is_regression is True
        assert c.is_improvement is False

    def test_improvement_more_is_better_goes_up(self):
        # Throughput (more is better) rises significantly -> improvement.
        c = _cmp([800, 795, 805, 798], [1000, 1005, 995, 1002], more_is_better=True)
        assert c.is_improvement is True
        assert c.is_regression is False

    def test_regression_less_is_better_goes_up(self):
        # Latency (less is better) rises significantly -> regression.
        c = _cmp([10, 10.1, 9.9, 10.0], [20, 20.1, 19.9, 20.0], more_is_better=False)
        assert c.is_regression is True
        assert c.is_improvement is False

    def test_improvement_less_is_better_goes_down(self):
        # Latency (less is better) drops significantly -> improvement.
        c = _cmp([20, 20.1, 19.9, 20.0], [10, 10.1, 9.9, 10.0], more_is_better=False)
        assert c.is_improvement is True
        assert c.is_regression is False

    def test_noise_is_neither(self):
        # Tiny change within noise -> neither regression nor improvement.
        c = _cmp([1000, 1001, 999, 1000], [1000, 1002, 998, 1001], more_is_better=True)
        assert c.is_regression is False
        assert c.is_improvement is False
