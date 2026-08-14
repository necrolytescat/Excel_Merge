from app.tools.version_comparison_persistent_snapshot_acceptance import (
    run_acceptance,
)


def test_persistent_snapshot_performance_acceptance_uses_five_round_medians():
    report = run_acceptance(
        rounds=5,
        read_delay_seconds=0.002,
        list_delay_seconds=0.001,
    )
    scenarios = report["scenarios"]
    assert scenarios["cold"]["read_calls"] == [60] * 5
    assert scenarios["cold"]["list_calls"] == [2] * 5
    assert scenarios["same_process_hot"]["read_calls"] == [0] * 5
    assert scenarios["same_process_hot"]["list_calls"] == [0] * 5
    assert scenarios["restart_same_revisions"]["read_calls"] == [0] * 5
    assert scenarios["restart_same_revisions"]["list_calls"] == [2] * 5
    assert scenarios["restart_five_changes"]["read_calls"] == [5] * 5
    assert scenarios["restart_five_changes"]["list_calls"] == [2] * 5
    assert (
        scenarios["same_process_hot"]["median_seconds"]
        < scenarios["cold"]["median_seconds"]
    )
    assert (
        scenarios["restart_same_revisions"]["median_seconds"]
        < scenarios["cold"]["median_seconds"]
    )
    assert (
        scenarios["restart_five_changes"]["median_seconds"]
        < scenarios["cold"]["median_seconds"]
    )
