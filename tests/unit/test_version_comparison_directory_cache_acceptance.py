from __future__ import annotations

from app.tools.version_comparison_directory_cache_acceptance import run_acceptance


def test_directory_cache_acceptance_reduces_calls_and_preserves_scope():
    report = run_acceptance(workbook_count=5, delay_seconds=0.001)

    assert report["uncached"]["list_tree_calls"] == 10
    assert report["uncached"]["list_children_calls"] == 10
    assert report["cached"]["list_tree_calls"] == 2
    assert report["cached"]["list_children_calls"] == 2
    assert report["writes"] == {
        "svn": False,
        "batch_database": False,
        "golden_fixture": False,
    }
