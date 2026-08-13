from __future__ import annotations

from app.tools.version_comparison_csv_parallel_acceptance import run_acceptance


def test_csv_parallel_acceptance_reports_speed_and_resource_bounds():
    report = run_acceptance(
        file_count=8,
        delay_seconds=0.002,
        payload_bytes=16,
        rounds=2,
    )

    assert report["speedup"] >= 2
    assert report["max_provider_concurrency"] == 4
    assert report["remaining_worker_threads"] == 0
    assert report["writes"] == {
        "svn": False,
        "batch_database": False,
        "golden_fixture": False,
    }
