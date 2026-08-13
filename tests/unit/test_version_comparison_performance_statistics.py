from __future__ import annotations

from app.tools.version_comparison_performance import _percentile


def test_percentile_uses_nearest_rank_without_understating_p95():
    values = list(range(1, 56))

    assert _percentile(values, 0.50) == 28
    assert _percentile(values, 0.95) == 53
