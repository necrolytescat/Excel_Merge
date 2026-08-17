from pathlib import Path
from types import SimpleNamespace

from app.tools import version_comparison_manifest_reuse_acceptance as tool


def test_acceptance_report_aggregates_rounds_without_external_writes(
    tmp_path,
    monkeypatch,
):
    fixture_path = tmp_path / "fixture.m2fixture"
    fixture_path.write_bytes(b"fixture")
    fixture = SimpleNamespace(
        archive_sha256="a" * 64,
        manifest=SimpleNamespace(dataset_layout={}, results=[1, 2]),
        golden_results={"1": b"{}", "2": b"{}"},
    )
    monkeypatch.setattr(tool, "load_offline_fixture", lambda raw: fixture)
    monkeypatch.setattr(tool.DatasetLayout, "from_config", lambda config: object())
    monkeypatch.setattr(tool, "_peak_working_set_bytes", lambda: 123)
    runs = iter(
        [
            {
                "legacy_equivalent_seconds": 10.0,
                "reused_equivalent_seconds": 6.0,
                "saved_seconds": 4.0,
                "speedup": 1.667,
                "matched_count": 2,
                "mismatched_count": 0,
                "result_set_sha256": tool.EXPECTED_RESULT_SET_SHA256,
            },
            {
                "legacy_equivalent_seconds": 12.0,
                "reused_equivalent_seconds": 7.0,
                "saved_seconds": 5.0,
                "speedup": 1.714,
                "matched_count": 2,
                "mismatched_count": 0,
                "result_set_sha256": tool.EXPECTED_RESULT_SET_SHA256,
            },
        ]
    )
    monkeypatch.setattr(tool, "_run_round", lambda fixture, layout: next(runs))

    report = tool.run_acceptance(Path(fixture_path), rounds=2)

    assert report["summary"] == {
        "requested_rounds": 2,
        "completed_rounds": 2,
        "expected_result_set_sha256": tool.EXPECTED_RESULT_SET_SHA256,
        "all_rounds_passed": True,
        "legacy_equivalent_p50_seconds": 11.0,
        "reused_equivalent_p50_seconds": 6.5,
        "saved_p50_seconds": 4.5,
        "speedup_p50": 1.691,
        "peak_working_set_bytes": 123,
        "unique_result_set_sha256": 1,
    }
    assert report["writes"] == {
        "svn": False,
        "batch_database": False,
        "golden_fixture": False,
    }
