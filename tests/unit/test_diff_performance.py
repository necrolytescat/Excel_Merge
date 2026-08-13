from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.schemas.diff import serialize_diff_json
from app.services.diff_performance import DiffPerformanceRecorder
from app.services.diff_performance_adapters import (
    TimedBatchStore,
    TimedSVNProvider,
    TimedWorkbookDiffService,
)
from app.services.workbook_diff_service import DatasetLayout, WorkbookDiffService
from app.tools import version_comparison_performance as benchmark


PROJECT_ROOT = Path(__file__).resolve().parents[2]
SOURCE_DIR = PROJECT_ROOT / "tests" / "excel" / "left"
TARGET_DIR = PROJECT_ROOT / "tests" / "excel" / "right"
WORKBOOK = "AtlasConfig.xlsm"
CONFIG = json.loads(
    (PROJECT_ROOT / "config" / "settings.json").read_text(encoding="utf-8")
)


def test_disabled_recorder_is_empty_and_validates_only_enabled_metrics():
    recorder = DiffPerformanceRecorder()
    with recorder.phase("not valid when enabled"):
        recorder.increment("also invalid", 3)

    assert recorder.snapshot() == {
        "schema_version": "m2.diff-performance.v1",
        "enabled": False,
        "phases": {},
        "counters": {},
        "values": {},
    }

    enabled = DiffPerformanceRecorder(enabled=True)
    with pytest.raises(ValueError):
        enabled.increment("invalid metric")


def test_enabled_recorder_aggregates_concurrent_calls():
    recorder = DiffPerformanceRecorder(enabled=True)

    def record() -> None:
        with recorder.phase("worker.total"):
            recorder.increment("worker.calls")

    with ThreadPoolExecutor(max_workers=4) as executor:
        list(executor.map(lambda _: record(), range(40)))

    snapshot = recorder.snapshot()
    assert snapshot["phases"]["worker.total"]["calls"] == 40
    assert snapshot["counters"]["worker.calls"] == 40


def test_timed_diff_is_byte_identical_and_reports_expected_stages():
    layout = DatasetLayout.from_config(CONFIG["dataset_layout"])
    expected = serialize_diff_json(
        WorkbookDiffService(layout).compare_local(SOURCE_DIR, TARGET_DIR, WORKBOOK)
    )
    recorder = DiffPerformanceRecorder(enabled=True)
    actual = serialize_diff_json(
        TimedWorkbookDiffService(layout, recorder).compare_local(
            SOURCE_DIR,
            TARGET_DIR,
            WORKBOOK,
        )
    )

    snapshot = recorder.snapshot()
    assert actual == expected
    assert snapshot["phases"]["diff.total"]["calls"] == 1
    assert snapshot["phases"]["diff.manifest_parse"]["calls"] == 2
    assert snapshot["phases"]["diff.csv_read_parse"]["calls"] == 32
    assert snapshot["phases"]["diff.sheet_total"]["calls"] == 16
    assert snapshot["counters"]["diff.csv_read_parse_calls"] == 32


def test_timed_provider_records_aggregate_calls_without_identities():
    class Provider:
        def list_tree(self, endpoint, prefix=""):
            return [prefix]

        def list_children(self, endpoint, prefix=""):
            return [prefix]

        def read_bytes(self, endpoint, path):
            return b"content"

    recorder = DiffPerformanceRecorder(enabled=True)
    provider = TimedSVNProvider(Provider(), recorder)
    endpoint = SimpleNamespace(url="secret://repository", revision=123)

    assert provider.list_tree(endpoint, "secret/path") == ["secret/path"]
    assert provider.list_children(endpoint, "secret/path") == ["secret/path"]
    assert provider.read_bytes(endpoint, "secret/Table/A.xlsm") == b"content"
    assert provider.read_bytes(endpoint, "secret/TableCsv/A.csv") == b"content"

    encoded = json.dumps(recorder.snapshot(), ensure_ascii=False)
    assert "secret" not in encoded
    assert recorder.snapshot()["counters"] == {
        "svn.csv_read_bytes": 7,
        "svn.csv_read_calls": 1,
        "svn.list_children_calls": 1,
        "svn.list_tree_calls": 1,
        "svn.workbook_read_bytes": 7,
        "svn.workbook_read_calls": 1,
    }


def test_timed_store_records_result_compression_and_fsync(tmp_path):
    recorder = DiffPerformanceRecorder(enabled=True)
    store = TimedBatchStore(tmp_path / "state", performance=recorder)

    result = store.write_result_blob("task", "item", b"{}")

    assert (store.state_directory / result["result_path"]).is_file()
    snapshot = recorder.snapshot()
    assert snapshot["phases"]["store.result_gzip_fsync"]["calls"] == 1
    assert snapshot["counters"]["store.result_input_bytes"] == 2


def test_benchmark_marks_first_round_cold_and_never_claims_external_writes(
    tmp_path,
    monkeypatch,
):
    fixture_path = tmp_path / "fixture.m2fixture"
    fixture_path.write_bytes(b"fixture")
    fake_fixture = SimpleNamespace(
        archive_sha256="a" * 64,
        inputs=SimpleNamespace(inputs=[1, 2]),
        golden_results={"one": b"{}"},
    )
    monkeypatch.setattr(benchmark, "load_offline_fixture", lambda raw: fake_fixture)

    def fake_round(fixture, *, cache_state):
        return {
            "cache_state": cache_state,
            "matched_count": 1,
            "mismatched_count": 0,
        }

    monkeypatch.setattr(benchmark, "_run_round", fake_round)

    report = benchmark.run_benchmark(fixture_path, rounds=3)

    assert report["schema_version"] == "m2.version-comparison-performance.v1"
    assert [run["cache_state"] for run in report["runs"]] == [
        "cold",
        "warm",
        "warm",
    ]
    assert report["writes"] == {
        "svn": False,
        "batch_database": False,
        "golden_fixture": False,
    }
