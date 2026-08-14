"""Validate request-scoped phase timing with deterministic snapshot mocks."""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import statistics
import tempfile
import threading
import time
from typing import Any

from app.services.snapshot_content_cache import PersistentSnapshotContentCache
from app.services.snapshot_service import SnapshotService
from core.models import SvnInfo, TreeEntry


_REPOSITORY_URL = "mock://repository/branches/fix"
_REPOSITORY_UUID = "snapshot-phase-timing-fixture"
_CHANGED_FILES = 5
_RECORDS = [
    {
        "id": "BRANCH",
        "region": "KR",
        "track": "FIX",
        "label": "Timing branch",
        "url": _REPOSITORY_URL,
        "logical_scopes": ["TABLE"],
        "physical_path_filters": {"TABLE": "Source/Table"},
        "enabled": True,
    }
]


class _TimingProvider:
    def __init__(
        self,
        *,
        files: int,
        read_delay_seconds: float,
        list_delay_seconds: float,
        payload_bytes: int,
    ) -> None:
        self.files = files
        self.read_delay_seconds = read_delay_seconds
        self.list_delay_seconds = list_delay_seconds
        self.payload_bytes = payload_bytes
        self.info_calls = 0
        self.list_calls = 0
        self.read_calls = 0
        self.active_reads = 0
        self.peak_reads = 0
        self._lock = threading.Lock()

    def _last_changed_revision(self, revision: int, index: int) -> int:
        if revision >= 300 and _CHANGED_FILES <= index < _CHANGED_FILES * 2:
            return 300
        if revision >= 200 and index < _CHANGED_FILES:
            return 200
        return 100

    def _raw(self, revision: int, index: int) -> bytes:
        last_changed = self._last_changed_revision(revision, index)
        seed = f"Config{index:03d}.xlsx|r{last_changed}|".encode("ascii")
        repeats = (self.payload_bytes + len(seed) - 1) // len(seed)
        return (seed * repeats)[: self.payload_bytes]

    def info(self, endpoint):
        with self._lock:
            self.info_calls += 1
        if self.list_delay_seconds:
            time.sleep(self.list_delay_seconds)
        return SvnInfo(
            url=endpoint.url,
            repository_root="mock://repository",
            repository_uuid=_REPOSITORY_UUID,
            revision=str(endpoint.revision),
            last_changed_revision=str(endpoint.revision),
        )

    def list_tree(self, endpoint, prefix=""):
        with self._lock:
            self.list_calls += 1
        if self.list_delay_seconds:
            time.sleep(self.list_delay_seconds)
        revision = int(endpoint.revision)
        return [TreeEntry(path="Source/Table", kind="dir")] + [
            TreeEntry(
                path=f"Source/Table/Config{index:03d}.xlsx",
                kind="file",
                size=self.payload_bytes,
                revision=str(self._last_changed_revision(revision, index)),
            )
            for index in range(self.files)
        ]

    def read_bytes_with_source(self, endpoint, path):
        index = int(path.rsplit("Config", 1)[1].split(".", 1)[0])
        with self._lock:
            self.read_calls += 1
            self.active_reads += 1
            self.peak_reads = max(self.peak_reads, self.active_reads)
        try:
            if self.read_delay_seconds:
                time.sleep(self.read_delay_seconds)
            return self._raw(int(endpoint.revision), index), "mock_provider"
        finally:
            with self._lock:
                self.active_reads -= 1

    def read_bytes(self, endpoint, path):
        return self.read_bytes_with_source(endpoint, path)[0]


def _service(
    provider: _TimingProvider,
    *,
    cache_root: Path | None,
    timing_enabled: bool,
    sink: list[dict[str, Any]],
) -> SnapshotService:
    persistent = (
        PersistentSnapshotContentCache(
            cache_root,
            max_bytes=512 * 1024 * 1024,
            max_file_entries=10_000,
            max_tree_entries=32,
        )
        if cache_root is not None
        else None
    )
    return SnapshotService(
        provider,
        allowed_schemes=("mock",),
        max_workers=6,
        persistent_content_cache=persistent,
        reuse_configuration={"fixture": "snapshot-phase-timing.v1"},
        phase_timing_enabled=timing_enabled,
        phase_timing_sink=sink.append,
    )


def _run(
    service: SnapshotService,
    *,
    source_revision: int,
    target_revision: int,
    request_context_id: str,
    reuse: bool = True,
):
    return service.create_snapshot_at_revisions(
        _RECORDS,
        source_id="BRANCH",
        source_revision=source_revision,
        target_id="BRANCH",
        target_revision=target_revision,
        reuse=reuse,
        request_context_id=request_context_id,
    )


def _digest(snapshot) -> str:
    payload = snapshot.model_dump(mode="json", exclude={"captured_at"})
    raw = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()


def _stage(metrics: dict[str, Any], name: str) -> dict[str, Any]:
    return metrics["stages"].get(
        name,
        {
            "calls": 0,
            "failures": 0,
            "wall_seconds": 0.0,
            "cpu_seconds": 0.0,
            "p50_seconds": 0.0,
            "p95_seconds": 0.0,
            "max_seconds": 0.0,
            "bytes": 0,
            "items": 0,
            "sources": {},
            "by_side": {},
        },
    )


def _assert_metric_shape(
    metrics: dict[str, Any],
    *,
    files: int,
    expected_mode: str,
    expected_list_calls: int,
    expected_provider_reads: int,
    expected_hits: int,
    expected_misses: int,
) -> None:
    request = metrics["request"]
    assert request["outcome"] == "succeeded"
    assert request["reuse_mode"] == expected_mode
    assert request["wall_seconds"] > 0
    assert request["cpu_seconds"] >= 0
    assert request["request_context_id"]
    if expected_mode == "process_hot":
        assert request["build_context_id"] is None
    else:
        assert request["build_context_id"]

    list_tree = metrics["summary"]["list_tree"]
    assert list_tree["calls"] == expected_list_calls
    assert list_tree["items"] == expected_list_calls * (files + 1)
    provider = metrics["summary"]["provider_read"]
    assert provider["calls"] == expected_provider_reads
    assert provider["failures"] == 0
    assert provider["p50_seconds"] <= provider["p95_seconds"]
    assert provider["p95_seconds"] <= provider["max_seconds"]
    assert provider["peak_concurrency"] <= 6

    lookup = metrics["summary"]["persistent_lookup"]
    assert lookup["sources"].get("hit", 0) == expected_hits
    assert lookup["sources"].get("miss", 0) == expected_misses
    assert _stage(metrics, "persistent.lookup.lock_wait")["calls"] == (
        expected_hits + expected_misses
    )
    assert _stage(metrics, "sha256.snapshot_content")["calls"] == (
        expected_provider_reads + expected_hits
    )

    summary = metrics["summary"]
    accounted = summary["critical_path_accounted_seconds"]
    unattributed = summary["unattributed_wall_seconds"]
    assert abs(request["wall_seconds"] - accounted - unattributed) <= 0.000003
    assert summary["stage_aggregate_seconds"] >= 0
    assert "不能直接相加" in summary["aggregation_note"]

    parallel = metrics["parallel"]
    expected_union = (
        parallel["source_wall_seconds"]
        + parallel["target_wall_seconds"]
        - parallel["overlap_seconds"]
    )
    assert abs(parallel["side_critical_path_seconds"] - expected_union) <= 0.000003
    assert metrics["endpoints"]["source"]["resolved_revision"] is not None
    assert metrics["endpoints"]["target"]["resolved_revision"] is not None


def _median(values: list[float]) -> float:
    return round(float(statistics.median(values)), 6)


def _scenario_summary(samples: list[dict[str, Any]]) -> dict[str, Any]:
    provider = [sample["summary"]["provider_read"] for sample in samples]
    lookup = [sample["summary"]["persistent_lookup"] for sample in samples]
    return {
        "runs": len(samples),
        "reuse_modes": sorted({sample["request"]["reuse_mode"] for sample in samples}),
        "wall_p50_seconds": _median(
            [sample["request"]["wall_seconds"] for sample in samples]
        ),
        "cpu_p50_seconds": _median(
            [sample["request"]["cpu_seconds"] for sample in samples]
        ),
        "source_wall_p50_seconds": _median(
            [sample["parallel"]["source_wall_seconds"] for sample in samples]
        ),
        "target_wall_p50_seconds": _median(
            [sample["parallel"]["target_wall_seconds"] for sample in samples]
        ),
        "overlap_p50_seconds": _median(
            [sample["parallel"]["overlap_seconds"] for sample in samples]
        ),
        "endpoint_info_calls": sorted(
            {sample["summary"]["endpoint_info"]["calls"] for sample in samples}
        ),
        "list_calls": sorted(
            {sample["summary"]["list_tree"]["calls"] for sample in samples}
        ),
        "list_entries": sorted(
            {sample["summary"]["list_tree"]["items"] for sample in samples}
        ),
        "persistent_hits": sorted(
            {item["sources"].get("hit", 0) for item in lookup}
        ),
        "persistent_misses": sorted(
            {item["sources"].get("miss", 0) for item in lookup}
        ),
        "provider_calls": sorted({item["calls"] for item in provider}),
        "provider_wall_p50_seconds": _median(
            [item["wall_seconds"] for item in provider]
        ),
        "provider_sample_p50_seconds": _median(
            [item["p50_seconds"] for item in provider]
        ),
        "provider_sample_p95_seconds": _median(
            [item["p95_seconds"] for item in provider]
        ),
        "provider_sample_max_seconds": _median(
            [item["max_seconds"] for item in provider]
        ),
        "provider_bytes": sorted({item["bytes"] for item in provider}),
        "provider_peak_concurrency": max(
            item["peak_concurrency"] for item in provider
        ),
        "sha256_calls": sorted(
            {sample["summary"]["sha256"]["calls"] for sample in samples}
        ),
        "sha256_bytes": sorted(
            {sample["summary"]["sha256"]["bytes"] for sample in samples}
        ),
        "blob_write_calls": sorted(
            {_stage(sample, "blob.temp_write")["calls"] for sample in samples}
        ),
        "blob_fsync_wall_p50_seconds": _median(
            [_stage(sample, "blob.fsync")["wall_seconds"] for sample in samples]
        ),
        "blob_lock_wait_wall_p50_seconds": _median(
            [_stage(sample, "blob.lock_wait")["wall_seconds"] for sample in samples]
        ),
        "index_serialize_wall_p50_seconds": _median(
            [_stage(sample, "index.serialize")["wall_seconds"] for sample in samples]
        ),
        "index_fsync_wall_p50_seconds": _median(
            [_stage(sample, "index.fsync")["wall_seconds"] for sample in samples]
        ),
        "pairing_wall_p50_seconds": _median(
            [
                _stage(sample, "pairing.future_wait")["wall_seconds"]
                + _stage(sample, "pairing.executor_shutdown")["wall_seconds"]
                for sample in samples
            ]
        ),
        "sorting_wall_p50_seconds": _median(
            [
                _stage(sample, "sort.entries")["wall_seconds"]
                + _stage(sample, "sort.files")["wall_seconds"]
                for sample in samples
            ]
        ),
        "response_wall_p50_seconds": _median(
            [
                _stage(sample, "response.endpoint")["wall_seconds"]
                + _stage(sample, "response.snapshot")["wall_seconds"]
                for sample in samples
            ]
        ),
        "critical_path_accounted_p50_seconds": _median(
            [
                sample["summary"]["critical_path_accounted_seconds"]
                for sample in samples
            ]
        ),
        "unattributed_wall_p50_seconds": _median(
            [sample["summary"]["unattributed_wall_seconds"] for sample in samples]
        ),
    }


def _overhead_probe(
    *,
    files: int,
    delay_seconds: float,
    payload_bytes: int,
    rounds: int,
) -> dict[str, Any]:
    disabled: list[float] = []
    enabled: list[float] = []
    timed_samples: list[dict[str, Any]] = []
    probe_delay = max(0.05, delay_seconds)
    for round_index in range(rounds):
        order = (False, True) if round_index % 2 == 0 else (True, False)
        for timing_enabled in order:
            provider = _TimingProvider(
                files=files,
                read_delay_seconds=probe_delay,
                list_delay_seconds=probe_delay / 10,
                payload_bytes=payload_bytes,
            )
            sink: list[dict[str, Any]] = []
            service = _service(
                provider,
                cache_root=None,
                timing_enabled=timing_enabled,
                sink=sink,
            )
            started = time.perf_counter()
            _run(
                service,
                source_revision=100,
                target_revision=200,
                request_context_id=f"overhead-{round_index}-{timing_enabled}",
                reuse=False,
            )
            elapsed = time.perf_counter() - started
            (enabled if timing_enabled else disabled).append(elapsed)
            if timing_enabled:
                timed_samples.append(sink[-1])
    disabled_p50 = statistics.median(disabled)
    enabled_p50 = statistics.median(enabled)
    overhead_percent = max(0.0, (enabled_p50 / disabled_p50 - 1.0) * 100.0)
    assert overhead_percent < 3.0
    assert all(
        sample["parallel"]["overlap_seconds"] > 0
        for sample in timed_samples
    )
    return {
        "delay_seconds": probe_delay,
        "disabled_p50_seconds": round(disabled_p50, 6),
        "enabled_p50_seconds": round(enabled_p50, 6),
        "overhead_percent": round(overhead_percent, 3),
        "parallel_overlap_p50_seconds": _median(
            [sample["parallel"]["overlap_seconds"] for sample in timed_samples]
        ),
    }


def run_acceptance(
    *,
    files: int = 24,
    delay_seconds: float = 0.003,
    payload_bytes: int = 32 * 1024,
    rounds: int = 10,
) -> dict[str, Any]:
    if files < _CHANGED_FILES * 2:
        raise ValueError("files must be at least 10")
    if rounds < 10:
        raise ValueError("rounds must be at least 10")
    samples: dict[str, list[dict[str, Any]]] = {
        "cold": [],
        "process_hot": [],
        "restart_same_revision": [],
        "restart_five_changes": [],
    }
    semantic_digests: set[str] = set()
    changed_digests: set[str] = set()
    with tempfile.TemporaryDirectory(prefix="snapshot-phase-timing-") as temporary:
        root = Path(temporary)
        for round_index in range(rounds):
            cache_root = root / f"round-{round_index}" / "snapshot"

            cold_sink: list[dict[str, Any]] = []
            cold_provider = _TimingProvider(
                files=files,
                read_delay_seconds=delay_seconds,
                list_delay_seconds=delay_seconds / 2,
                payload_bytes=payload_bytes,
            )
            cold_service = _service(
                cold_provider,
                cache_root=cache_root,
                timing_enabled=True,
                sink=cold_sink,
            )
            cold = _run(
                cold_service,
                source_revision=100,
                target_revision=200,
                request_context_id=f"cold-{round_index}",
            )
            cold_metrics = cold_sink[-1]
            _assert_metric_shape(
                cold_metrics,
                files=files,
                expected_mode="same_branch_incremental",
                expected_list_calls=2,
                expected_provider_reads=files + _CHANGED_FILES,
                expected_hits=0,
                expected_misses=files + _CHANGED_FILES,
            )
            samples["cold"].append(cold_metrics)
            semantic_digests.add(_digest(cold))

            hot = _run(
                cold_service,
                source_revision=100,
                target_revision=200,
                request_context_id=f"hot-{round_index}",
            )
            hot_metrics = cold_sink[-1]
            _assert_metric_shape(
                hot_metrics,
                files=files,
                expected_mode="process_hot",
                expected_list_calls=0,
                expected_provider_reads=0,
                expected_hits=0,
                expected_misses=0,
            )
            samples["process_hot"].append(hot_metrics)
            semantic_digests.add(_digest(hot))

            restart_sink: list[dict[str, Any]] = []
            restart_provider = _TimingProvider(
                files=files,
                read_delay_seconds=delay_seconds,
                list_delay_seconds=delay_seconds / 2,
                payload_bytes=payload_bytes,
            )
            restarted = _service(
                restart_provider,
                cache_root=cache_root,
                timing_enabled=True,
                sink=restart_sink,
            )
            restart_snapshot = _run(
                restarted,
                source_revision=100,
                target_revision=200,
                request_context_id=f"restart-{round_index}",
            )
            restart_metrics = restart_sink[-1]
            _assert_metric_shape(
                restart_metrics,
                files=files,
                expected_mode="same_branch_incremental",
                expected_list_calls=2,
                expected_provider_reads=0,
                expected_hits=files + _CHANGED_FILES,
                expected_misses=0,
            )
            samples["restart_same_revision"].append(restart_metrics)
            semantic_digests.add(_digest(restart_snapshot))

            changed_sink: list[dict[str, Any]] = []
            changed_provider = _TimingProvider(
                files=files,
                read_delay_seconds=delay_seconds,
                list_delay_seconds=delay_seconds / 2,
                payload_bytes=payload_bytes,
            )
            changed_service = _service(
                changed_provider,
                cache_root=cache_root,
                timing_enabled=True,
                sink=changed_sink,
            )
            changed = _run(
                changed_service,
                source_revision=200,
                target_revision=300,
                request_context_id=f"changed-{round_index}",
            )
            changed_metrics = changed_sink[-1]
            _assert_metric_shape(
                changed_metrics,
                files=files,
                expected_mode="same_branch_incremental",
                expected_list_calls=2,
                expected_provider_reads=_CHANGED_FILES,
                expected_hits=files,
                expected_misses=_CHANGED_FILES,
            )
            samples["restart_five_changes"].append(changed_metrics)
            changed_digests.add(_digest(changed))

    assert len(semantic_digests) == 1
    assert len(changed_digests) == 1
    overhead = _overhead_probe(
        files=files,
        delay_seconds=delay_seconds,
        payload_bytes=payload_bytes,
        rounds=rounds,
    )
    return {
        "schema_version": "m2.snapshot-phase-timing-acceptance.v1",
        "rounds": rounds,
        "files": files,
        "changed_files": _CHANGED_FILES,
        "read_delay_seconds": delay_seconds,
        "payload_bytes": payload_bytes,
        "scenarios": {
            name: _scenario_summary(values)
            for name, values in samples.items()
        },
        "instrumentation_overhead": overhead,
        "semantic_digest_count": len(semantic_digests),
        "changed_semantic_digest_count": len(changed_digests),
        "writes": {
            "svn": False,
            "official_snapshot_cache": False,
            "batch_database": False,
            "golden_fixture": False,
            "temporary_mock_cache": True,
        },
        "assertions": "passed",
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--files", type=int, default=24)
    parser.add_argument("--delay-seconds", type=float, default=0.003)
    parser.add_argument("--payload-bytes", type=int, default=32 * 1024)
    parser.add_argument("--rounds", type=int, default=10)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args(argv)
    if args.delay_seconds < 0 or args.payload_bytes < 1:
        parser.error("delay must be non-negative and payload bytes positive")
    report = run_acceptance(
        files=args.files,
        delay_seconds=args.delay_seconds,
        payload_bytes=args.payload_bytes,
        rounds=args.rounds,
    )
    content = json.dumps(report, ensure_ascii=False, sort_keys=True, indent=2) + "\n"
    if args.output is not None:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(content, encoding="utf-8")
    print(content, end="")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
