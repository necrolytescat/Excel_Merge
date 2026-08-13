"""Benchmark version comparison with an existing offline Replay fixture."""
from __future__ import annotations

import argparse
import ctypes
from ctypes import wintypes
import hashlib
import json
import math
import os
from pathlib import Path, PurePosixPath
import statistics
import sys
import time
from typing import Any

from app.schemas.diff import serialize_diff_json
from app.services.diff_performance import DiffPerformanceRecorder
from app.services.diff_performance_adapters import TimedWorkbookDiffService
from app.services.diff_performance_probe import probe_diff_functions
from app.services.offline_fixture import LoadedOfflineFixture, load_offline_fixture
from app.services.workbook_diff_service import DatasetLayout


PROJECT_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_FIXTURE = PROJECT_ROOT / "var" / "m2-fixtures" / "d3c-be317423.m2fixture"


def _peak_working_set_bytes() -> int | None:
    if sys.platform != "win32":
        return None
    try:
        class ProcessMemoryCounters(ctypes.Structure):
            _fields_ = [
                ("cb", wintypes.DWORD),
                ("PageFaultCount", wintypes.DWORD),
                ("PeakWorkingSetSize", ctypes.c_size_t),
                ("WorkingSetSize", ctypes.c_size_t),
                ("QuotaPeakPagedPoolUsage", ctypes.c_size_t),
                ("QuotaPagedPoolUsage", ctypes.c_size_t),
                ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t),
                ("QuotaNonPagedPoolUsage", ctypes.c_size_t),
                ("PagefileUsage", ctypes.c_size_t),
                ("PeakPagefileUsage", ctypes.c_size_t),
            ]

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        psapi = ctypes.WinDLL("psapi", use_last_error=True)
        kernel32.GetCurrentProcess.restype = wintypes.HANDLE
        psapi.GetProcessMemoryInfo.argtypes = [
            wintypes.HANDLE,
            ctypes.POINTER(ProcessMemoryCounters),
            wintypes.DWORD,
        ]
        psapi.GetProcessMemoryInfo.restype = wintypes.BOOL
        counters = ProcessMemoryCounters()
        counters.cb = ctypes.sizeof(counters)
        ok = psapi.GetProcessMemoryInfo(
            kernel32.GetCurrentProcess(), ctypes.byref(counters), counters.cb
        )
        return int(counters.PeakWorkingSetSize) if ok else None
    except (AttributeError, OSError):
        return None


def _process_io_bytes() -> tuple[int, int] | None:
    if sys.platform != "win32":
        return None
    try:
        class IoCounters(ctypes.Structure):
            _fields_ = [
                ("ReadOperationCount", ctypes.c_ulonglong),
                ("WriteOperationCount", ctypes.c_ulonglong),
                ("OtherOperationCount", ctypes.c_ulonglong),
                ("ReadTransferCount", ctypes.c_ulonglong),
                ("WriteTransferCount", ctypes.c_ulonglong),
                ("OtherTransferCount", ctypes.c_ulonglong),
            ]

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.GetCurrentProcess.restype = wintypes.HANDLE
        kernel32.GetProcessIoCounters.argtypes = [
            wintypes.HANDLE,
            ctypes.POINTER(IoCounters),
        ]
        kernel32.GetProcessIoCounters.restype = wintypes.BOOL
        counters = IoCounters()
        ok = kernel32.GetProcessIoCounters(
            kernel32.GetCurrentProcess(), ctypes.byref(counters)
        )
        if not ok:
            return None
        return int(counters.ReadTransferCount), int(counters.WriteTransferCount)
    except (AttributeError, OSError):
        return None


def _percentile(values: list[float], percentile: float) -> float:
    ordered = sorted(values)
    index = max(
        0,
        min(len(ordered) - 1, math.ceil(len(ordered) * percentile) - 1),
    )
    return ordered[index]


def _run_round(
    fixture: LoadedOfflineFixture,
    *,
    cache_state: str,
) -> dict[str, Any]:
    performance = DiffPerformanceRecorder(enabled=True)
    service = TimedWorkbookDiffService(
        DatasetLayout.from_config(fixture.manifest.dataset_layout),
        performance,
    )
    wall_started = time.perf_counter()
    cpu_started = time.process_time()
    io_started = _process_io_bytes()
    first_result_seconds: float | None = None
    matched = 0
    mismatched = 0
    output_bytes = 0
    result_hashes: list[str] = []
    item_seconds: list[float] = []

    with probe_diff_functions(performance):
        for result_manifest in fixture.manifest.results:
            item_started = time.perf_counter()
            item_id = str(result_manifest.item_id)
            workbook_name = PurePosixPath(result_manifest.workbook_path).name
            with performance.phase("replay.materialize"):
                dataset = fixture.materialize(result_manifest.workbook_path)
                dataset.__enter__()
            try:
                assert dataset.source_directory is not None
                assert dataset.target_directory is not None
                result = service.compare_local(
                    dataset.source_directory,
                    dataset.target_directory,
                    workbook_name,
                )
                with performance.phase("diff.serialize"):
                    content = serialize_diff_json(result)
            finally:
                with performance.phase("replay.cleanup"):
                    dataset.__exit__(None, None, None)
            output_bytes += len(content)
            digest = hashlib.sha256(content).hexdigest()
            result_hashes.append(digest)
            if content == fixture.golden_results[item_id]:
                matched += 1
            else:
                mismatched += 1
            if first_result_seconds is None:
                first_result_seconds = time.perf_counter() - wall_started
            item_seconds.append(time.perf_counter() - item_started)

    wall_seconds = time.perf_counter() - wall_started
    cpu_seconds = time.process_time() - cpu_started
    io_finished = _process_io_bytes()
    io_delta = None
    if io_started is not None and io_finished is not None:
        io_delta = {
            "read_bytes": io_finished[0] - io_started[0],
            "write_bytes": io_finished[1] - io_started[1],
        }
    return {
        "cache_state": cache_state,
        "workbook_count": len(fixture.manifest.results),
        "matched_count": matched,
        "mismatched_count": mismatched,
        "first_result_seconds": round(first_result_seconds or 0.0, 6),
        "all_results_seconds": round(wall_seconds, 6),
        "cpu_seconds": round(cpu_seconds, 6),
        "cpu_wall_ratio": round(cpu_seconds / wall_seconds, 6) if wall_seconds else 0.0,
        "peak_working_set_bytes": _peak_working_set_bytes(),
        "process_io": io_delta,
        "output_bytes": output_bytes,
        "item_seconds": {
            "min": round(min(item_seconds), 6),
            "p50": round(statistics.median(item_seconds), 6),
            "p95": round(_percentile(item_seconds, 0.95), 6),
            "max": round(max(item_seconds), 6),
        },
        "result_set_sha256": hashlib.sha256(
            "\n".join(result_hashes).encode("ascii")
        ).hexdigest(),
        "performance": performance.snapshot(),
    }


def run_benchmark(fixture_path: Path, *, rounds: int = 2) -> dict[str, Any]:
    raw = fixture_path.read_bytes()
    load_wall_started = time.perf_counter()
    load_cpu_started = time.process_time()
    fixture = load_offline_fixture(raw)
    load_wall = time.perf_counter() - load_wall_started
    load_cpu = time.process_time() - load_cpu_started
    runs = [
        _run_round(fixture, cache_state="cold" if index == 0 else "warm")
        for index in range(rounds)
    ]
    return {
        "schema_version": "m2.version-comparison-performance.v1",
        "fixture": {
            "archive_sha256": fixture.archive_sha256,
            "archive_size_bytes": len(raw),
            "input_file_count": len(fixture.inputs.inputs),
            "golden_result_count": len(fixture.golden_results),
        },
        "cache_definition": {
            "cold": "first recompute in a new benchmark process",
            "warm": "subsequent recompute in the same process",
            "svn_cache": "not exercised by offline Replay",
        },
        "load": {
            "wall_seconds": round(load_wall, 6),
            "cpu_seconds": round(load_cpu, 6),
        },
        "environment": {
            "python": sys.version.split()[0],
            "platform": sys.platform,
            "process_id": os.getpid(),
        },
        "runs": runs,
        "writes": {
            "svn": False,
            "batch_database": False,
            "golden_fixture": False,
        },
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--fixture", type=Path, default=DEFAULT_FIXTURE)
    parser.add_argument("--rounds", type=int, default=2)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args(argv)
    if args.rounds < 1:
        parser.error("--rounds must be at least 1")
    try:
        report = run_benchmark(args.fixture, rounds=args.rounds)
    except (OSError, ValueError) as error:
        print(json.dumps({"status": "failed", "code": type(error).__name__}))
        return 2
    content = json.dumps(report, ensure_ascii=False, sort_keys=True, indent=2) + "\n"
    if args.output is not None:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(content, encoding="utf-8")
    print(content, end="")
    return 0 if all(run["mismatched_count"] == 0 for run in report["runs"]) else 1


if __name__ == "__main__":
    raise SystemExit(main())
