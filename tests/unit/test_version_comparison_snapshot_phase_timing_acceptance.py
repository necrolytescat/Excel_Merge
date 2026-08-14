from app.tools import version_comparison_snapshot_phase_timing_acceptance as tool


def test_acceptance_covers_four_cache_states_and_low_overhead():
    report = tool.run_acceptance(
        files=12,
        delay_seconds=0.001,
        payload_bytes=4096,
        rounds=10,
    )

    assert report["rounds"] == 10
    assert report["changed_files"] == 5
    assert report["assertions"] == "passed"
    assert report["instrumentation_overhead"]["overhead_percent"] < 3
    assert (
        report["instrumentation_overhead"]["parallel_overlap_p50_seconds"] > 0
    )
    assert report["scenarios"]["cold"]["provider_calls"] == [17]
    assert report["scenarios"]["process_hot"]["provider_calls"] == [0]
    assert report["scenarios"]["restart_same_revision"]["persistent_hits"] == [17]
    assert report["scenarios"]["restart_five_changes"]["provider_calls"] == [5]
    assert report["writes"] == {
        "svn": False,
        "official_snapshot_cache": False,
        "batch_database": False,
        "golden_fixture": False,
        "temporary_mock_cache": True,
    }
