from app.tools import version_comparison_snapshot_parallel_acceptance as tool


def test_acceptance_reports_bounded_speedup_and_no_external_writes():
    report = tool.run_acceptance(
        files_per_side=24,
        delay_seconds=0.01,
        rounds=3,
    )

    assert report["speedup"] >= 1.7
    assert 6 < report["max_provider_concurrency"] <= 12
    assert report["unique_semantic_digests"] == 1
    assert report["remaining_worker_threads"] == 0
    assert report["writes"] == {
        "svn": False,
        "batch_database": False,
        "golden_fixture": False,
    }
