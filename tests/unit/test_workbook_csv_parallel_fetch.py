from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
import json
from pathlib import Path, PurePosixPath
import threading
import time

import pytest

from app.services.workbook_dataset_service import (
    SVNWorkbookDatasetResolver,
    WorkbookCompareError,
)
from core.models import EndpointSpec, TreeEntry
from core.svn_provider import SVNProviderError
from core.workbook_manifest_parser import ManifestEntry, WorkbookManifest


PROJECT_ROOT = Path(__file__).resolve().parents[2]
CONFIG = json.loads(
    (PROJECT_ROOT / "config" / "settings.json").read_text(encoding="utf-8")
)


def _manifest(*names: str) -> WorkbookManifest:
    return WorkbookManifest(
        entries=tuple(
            ManifestEntry(
                sheet_name=f"Sheet{index}",
                tbx_name=name,
                is_export="1",
                row_number=index + 2,
            )
            for index, name in enumerate(names)
        ),
        parser="test",
    )


class DelayedProvider:
    def __init__(
        self,
        *,
        delay_seconds: float = 0.02,
        missing: set[str] | None = None,
        failures: dict[str, str] | None = None,
        children: dict[str, list[str]] | None = None,
    ):
        self.delay_seconds = delay_seconds
        self.missing = missing or set()
        self.failures = failures or {}
        self.children = children or {}
        self.active = 0
        self.max_active = 0
        self.lock = threading.Lock()
        self.calls: list[str] = []

    def read_bytes(self, endpoint, path):
        clean = str(path).replace("\\", "/")
        with self.lock:
            self.active += 1
            self.max_active = max(self.max_active, self.active)
            self.calls.append(clean)
        try:
            time.sleep(self.delay_seconds)
            if clean in self.failures:
                raise SVNProviderError(self.failures[clean], clean)
            if clean in self.missing:
                raise SVNProviderError("SVN_PATH_NOT_FOUND", clean)
            return f"content:{clean}".encode()
        finally:
            with self.lock:
                self.active -= 1

    def list_children(self, endpoint, prefix=""):
        return [
            TreeEntry(path=path, kind="file")
            for path in self.children.get(str(prefix), [])
        ]


def _resolver(provider):
    return SVNWorkbookDatasetResolver(
        provider,
        lambda: [],
        CONFIG["dataset_layout"],
        allowed_schemes=("mock",),
    )


def _pair(resolver, manifest):
    return resolver._read_csv_files_pair(
        EndpointSpec(url="mock://left", revision=101),
        "left/TableCsv",
        manifest,
        EndpointSpec(url="mock://right", revision=202),
        "right/TableCsv",
        manifest,
    )


def test_csv_pair_fetch_is_bounded_and_at_least_twice_as_fast():
    manifest = _manifest(*(f"Config{index}" for index in range(8)))
    serial_provider = DelayedProvider()
    serial = _resolver(serial_provider)
    started = time.perf_counter()
    serial._read_csv_files(
        EndpointSpec(url="mock://left", revision=101),
        "left/TableCsv",
        manifest,
    )
    serial._read_csv_files(
        EndpointSpec(url="mock://right", revision=202),
        "right/TableCsv",
        manifest,
    )
    serial_seconds = time.perf_counter() - started

    parallel_provider = DelayedProvider()
    parallel = _resolver(parallel_provider)
    started = time.perf_counter()
    source, target = _pair(parallel, manifest)
    parallel_seconds = time.perf_counter() - started

    assert list(source) == [f"Config{index}.csv" for index in range(8)]
    assert list(target) == list(source)
    assert parallel_provider.max_active == 4
    assert serial_seconds / parallel_seconds >= 2


def test_csv_pair_uses_one_casefold_index_per_side_and_preserves_order():
    manifest = _manifest("First", "Second", "First")
    missing = {
        "left/TableCsv/First.csv",
        "right/TableCsv/First.csv",
    }
    provider = DelayedProvider(
        delay_seconds=0,
        missing=missing,
        children={
            "left/TableCsv": ["left/TableCsv/fIRSt.csv"],
            "right/TableCsv": ["right/TableCsv/fIRSt.csv"],
        },
    )
    resolver = _resolver(provider)

    source, target = _pair(resolver, manifest)

    assert list(source) == ["First.csv", "Second.csv"]
    assert list(target) == ["First.csv", "Second.csv"]
    assert provider.calls.count("left/TableCsv/fIRSt.csv") == 1
    assert provider.calls.count("right/TableCsv/fIRSt.csv") == 1


def test_csv_pair_reports_source_manifest_error_before_target_completion_order():
    manifest = _manifest("First", "Second")
    provider = DelayedProvider(
        delay_seconds=0.005,
        failures={
            "left/TableCsv/First.csv": "SVN_TIMEOUT",
            "right/TableCsv/Second.csv": "SVN_AUTH_FAILED",
        },
    )
    resolver = _resolver(provider)

    with pytest.raises(WorkbookCompareError) as caught:
        _pair(resolver, manifest)

    assert caught.value.code == "DIFF_DATASET_READ_FAILED"
    assert caught.value.__cause__.code == "SVN_TIMEOUT"


def test_csv_pair_closes_worker_threads_after_failure():
    manifest = _manifest(*(f"Config{index}" for index in range(8)))
    provider = DelayedProvider(
        delay_seconds=0.005,
        failures={"left/TableCsv/Config0.csv": "SVN_TIMEOUT"},
    )
    resolver = _resolver(provider)

    with pytest.raises(WorkbookCompareError):
        _pair(resolver, manifest)

    assert provider.active == 0
    assert not any(
        thread.name.startswith("m2-csv-read")
        for thread in threading.enumerate()
    )


def test_two_workbooks_cap_provider_reads_at_eight():
    manifest = _manifest(*(f"Config{index}" for index in range(12)))
    provider = DelayedProvider(delay_seconds=0.01)
    resolver = _resolver(provider)

    with ThreadPoolExecutor(max_workers=2) as executor:
        futures = [executor.submit(_pair, resolver, manifest) for _ in range(2)]
        for future in futures:
            future.result()

    assert 4 < provider.max_active <= 8
