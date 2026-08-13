"""Measure bounded parallel construction of frozen snapshot sides."""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import statistics
import threading
import time
import tracemalloc
from typing import Any

from app.services.snapshot_service import SnapshotService
from core.models import TreeEntry


class _DelayedSnapshotProvider:
    def __init__(self, *, files_per_side: int, delay_seconds: float):
        self.files_per_side = files_per_side
        self.delay_seconds = delay_seconds
        self.active = 0
        self.max_active = 0
        self._lock = threading.Lock()

    def list_tree(self, endpoint, prefix=""):
        return [TreeEntry(path="Source/Table", kind="dir")] + [
            TreeEntry(
                path=f"Source/Table/Config{index:02d}.xlsx",
                kind="file",
                size=16,
                revision=str(endpoint.revision),
            )
            for index in range(self.files_per_side)
        ]

    def read_bytes(self, endpoint, path):
        with self._lock:
            self.active += 1
            self.max_active = max(self.max_active, self.active)
        try:
            time.sleep(self.delay_seconds)
            return f"{endpoint.url}|{endpoint.revision}|{path}".encode()
        finally:
            with self._lock:
                self.active -= 1


def _records():
    return [
        {
            "id": "SOURCE",
            "region": "KR",
            "track": "FIX",
            "label": "Source",
            "url": "mock://source",
            "logical_scopes": ["TABLE"],
            "physical_path_filters": {"TABLE": "Source/Table"},
            "enabled": True,
        },
        {
            "id": "TARGET",
            "region": "KR",
            "track": "FIX",
            "label": "Target",
            "url": "mock://target",
            "logical_scopes": ["TABLE"],
            "physical_path_filters": {"TABLE": "Source/Table"},
            "enabled": True,
        },
    ]


def _service(provider):
    return SnapshotService(provider, allowed_schemes=("mock",), max_workers=6)


def _run_serial(snapshot):
    source, target = _records()
    started = time.perf_counter()
    left = snapshot._snapshot_endpoint_at_revision(source, 101)
    right = snapshot._snapshot_endpoint_at_revision(target, 202)
    return time.perf_counter() - started, left, right


def _run_parallel(snapshot):
    started = time.perf_counter()
    result = snapshot.create_snapshot_at_revisions(
        _records(),
        source_id="SOURCE",
        source_revision=101,
        target_id="TARGET",
        target_revision=202,
    )
    return time.perf_counter() - started, result.source, result.target


def _snapshot_digest(source, target):
    values = [
        f"{side.endpoint_id}|{item.path}|{item.content_hash}"
        for side in (source, target)
        for item in side.files
    ]
    return hashlib.sha256("\n".join(values).encode()).hexdigest()


def run_acceptance(
    *,
    files_per_side: int = 24,
    delay_seconds: float = 0.02,
    rounds: int = 5,
) -> dict[str, Any]:
    serial_seconds: list[float] = []
    parallel_seconds: list[float] = []
    max_concurrency = 0
    digests: set[str] = set()
    tracemalloc.start()
    try:
        for _ in range(rounds):
            serial_provider = _DelayedSnapshotProvider(
                files_per_side=files_per_side,
                delay_seconds=delay_seconds,
            )
            elapsed, source, target = _run_serial(_service(serial_provider))
            serial_seconds.append(elapsed)
            digests.add(_snapshot_digest(source, target))

            parallel_provider = _DelayedSnapshotProvider(
                files_per_side=files_per_side,
                delay_seconds=delay_seconds,
            )
            elapsed, source, target = _run_parallel(_service(parallel_provider))
            parallel_seconds.append(elapsed)
            digests.add(_snapshot_digest(source, target))
            max_concurrency = max(max_concurrency, parallel_provider.max_active)
        _, peak_bytes = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()
    serial_p50 = statistics.median(serial_seconds)
    parallel_p50 = statistics.median(parallel_seconds)
    return {
        "schema_version": "m2.version-comparison-snapshot-parallel-acceptance.v1",
        "files_per_side": files_per_side,
        "rounds": rounds,
        "provider_delay_seconds": delay_seconds,
        "serial_p50_seconds": round(serial_p50, 6),
        "parallel_p50_seconds": round(parallel_p50, 6),
        "speedup": round(serial_p50 / parallel_p50, 3),
        "max_provider_concurrency": max_concurrency,
        "unique_semantic_digests": len(digests),
        "remaining_worker_threads": sum(
            thread.name.startswith("m1-snapshot-")
            for thread in threading.enumerate()
        ),
        "python_peak_allocated_bytes": peak_bytes,
        "writes": {
            "svn": False,
            "batch_database": False,
            "golden_fixture": False,
        },
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--files-per-side", type=int, default=24)
    parser.add_argument("--delay-seconds", type=float, default=0.02)
    parser.add_argument("--rounds", type=int, default=5)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args(argv)
    if args.files_per_side < 1 or args.delay_seconds < 0 or args.rounds < 1:
        parser.error("counts must be positive and delay non-negative")
    report = run_acceptance(
        files_per_side=args.files_per_side,
        delay_seconds=args.delay_seconds,
        rounds=args.rounds,
    )
    content = json.dumps(report, ensure_ascii=False, sort_keys=True, indent=2) + "\n"
    if args.output is not None:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(content, encoding="utf-8")
    print(content, end="")
    return 0 if (
        report["speedup"] >= 1.7
        and report["max_provider_concurrency"] <= 12
        and report["unique_semantic_digests"] == 1
        and report["remaining_worker_threads"] == 0
    ) else 1


if __name__ == "__main__":
    raise SystemExit(main())
