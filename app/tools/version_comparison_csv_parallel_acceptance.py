"""Measure bounded CSV fetch concurrency with a deterministic local provider."""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import statistics
import threading
import time
import tracemalloc
from typing import Any

from app.services.workbook_dataset_service import SVNWorkbookDatasetResolver
from core.models import EndpointSpec
from core.workbook_manifest_parser import ManifestEntry, WorkbookManifest


PROJECT_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_CONFIG = PROJECT_ROOT / "config" / "settings.json"


class _DelayedProvider:
    def __init__(self, delay_seconds: float, payload_bytes: int):
        self.delay_seconds = delay_seconds
        self.payload = b"x" * payload_bytes
        self.active = 0
        self.max_active = 0
        self._lock = threading.Lock()

    def read_bytes(self, endpoint, path):
        with self._lock:
            self.active += 1
            self.max_active = max(self.max_active, self.active)
        try:
            time.sleep(self.delay_seconds)
            return self.payload
        finally:
            with self._lock:
                self.active -= 1

    def list_children(self, endpoint, prefix=""):
        return []


def _manifest(file_count: int) -> WorkbookManifest:
    return WorkbookManifest(
        entries=tuple(
            ManifestEntry(
                sheet_name=f"Sheet{index}",
                tbx_name=f"Config{index}",
                is_export="1",
                row_number=index + 2,
            )
            for index in range(file_count)
        ),
        parser="acceptance",
    )


def _resolver(provider, layout):
    return SVNWorkbookDatasetResolver(
        provider,
        lambda: [],
        layout,
        allowed_schemes=("mock",),
    )


def _run_serial(resolver, manifest) -> float:
    started = time.perf_counter()
    resolver._read_csv_files(
        EndpointSpec(url="mock://left", revision=101),
        "left/TableCsv",
        manifest,
    )
    resolver._read_csv_files(
        EndpointSpec(url="mock://right", revision=202),
        "right/TableCsv",
        manifest,
    )
    return time.perf_counter() - started


def _run_parallel(resolver, manifest) -> float:
    started = time.perf_counter()
    resolver._read_csv_files_pair(
        EndpointSpec(url="mock://left", revision=101),
        "left/TableCsv",
        manifest,
        EndpointSpec(url="mock://right", revision=202),
        "right/TableCsv",
        manifest,
    )
    return time.perf_counter() - started


def run_acceptance(
    *,
    file_count: int = 32,
    delay_seconds: float = 0.01,
    payload_bytes: int = 1024,
    rounds: int = 5,
) -> dict[str, Any]:
    config = json.loads(DEFAULT_CONFIG.read_text(encoding="utf-8"))
    layout = config["dataset_layout"]
    manifest = _manifest(file_count)
    serial_seconds: list[float] = []
    parallel_seconds: list[float] = []
    max_concurrency = 0
    tracemalloc.start()
    try:
        for _ in range(rounds):
            serial_provider = _DelayedProvider(delay_seconds, payload_bytes)
            serial_seconds.append(
                _run_serial(_resolver(serial_provider, layout), manifest)
            )
            parallel_provider = _DelayedProvider(delay_seconds, payload_bytes)
            parallel_seconds.append(
                _run_parallel(_resolver(parallel_provider, layout), manifest)
            )
            max_concurrency = max(max_concurrency, parallel_provider.max_active)
        _, peak_bytes = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()
    serial_p50 = statistics.median(serial_seconds)
    parallel_p50 = statistics.median(parallel_seconds)
    remaining_threads = sum(
        thread.name.startswith("m2-csv-read")
        for thread in threading.enumerate()
    )
    return {
        "schema_version": "m2.version-comparison-csv-parallel-acceptance.v1",
        "file_count_per_side": file_count,
        "rounds": rounds,
        "provider_delay_seconds": delay_seconds,
        "payload_bytes_per_file": payload_bytes,
        "serial_p50_seconds": round(serial_p50, 6),
        "parallel_p50_seconds": round(parallel_p50, 6),
        "speedup": round(serial_p50 / parallel_p50, 3),
        "max_provider_concurrency": max_concurrency,
        "remaining_worker_threads": remaining_threads,
        "python_peak_allocated_bytes": peak_bytes,
        "writes": {
            "svn": False,
            "batch_database": False,
            "golden_fixture": False,
        },
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--files-per-side", type=int, default=32)
    parser.add_argument("--delay-seconds", type=float, default=0.01)
    parser.add_argument("--payload-bytes", type=int, default=1024)
    parser.add_argument("--rounds", type=int, default=5)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args(argv)
    if (
        args.files_per_side < 1
        or args.delay_seconds < 0
        or args.payload_bytes < 0
        or args.rounds < 1
    ):
        parser.error("counts must be positive and sizes/delay non-negative")
    report = run_acceptance(
        file_count=args.files_per_side,
        delay_seconds=args.delay_seconds,
        payload_bytes=args.payload_bytes,
        rounds=args.rounds,
    )
    content = json.dumps(report, ensure_ascii=False, sort_keys=True, indent=2) + "\n"
    if args.output is not None:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(content, encoding="utf-8")
    print(content, end="")
    return 0 if (
        report["speedup"] >= 2
        and report["max_provider_concurrency"] <= 4
        and report["remaining_worker_threads"] == 0
    ) else 1


if __name__ == "__main__":
    raise SystemExit(main())
