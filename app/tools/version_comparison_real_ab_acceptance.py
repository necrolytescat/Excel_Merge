"""Run a fail-closed, read-only real-SVN A/B acceptance in isolated state."""
from __future__ import annotations

import argparse
from collections import Counter, defaultdict
from copy import deepcopy
import faulthandler
import hashlib
import json
import logging
from pathlib import Path
import sys
from tempfile import TemporaryDirectory
from threading import Lock
import time
from typing import Any, Callable
from uuid import uuid4

from app.schemas.batch import BatchCreateRequestPayload
from app.services.batch_diff_service import (
    BatchDiffService,
    DefaultBatchWorkbookRunner,
    SnapshotBatchCandidateResolver,
)
from app.services.batch_store import BatchStore, TERMINAL_TASK_STATUSES
from app.services.diff_performance import DiffPerformanceRecorder
from app.services.diff_performance_adapters import (
    TimedBatchCandidateResolver,
    TimedBatchStore,
    TimedBatchWorkbookRunner,
)
from app.services.offline_fixture import load_offline_fixture
from app.services.snapshot_content_cache import PersistentSnapshotContentCache
from app.services.snapshot_service import SnapshotService
from app.services.workbook_dataset_service import SVNWorkbookDatasetResolver
from app.services.workbook_diff_service import DatasetLayout, WorkbookDiffService
from app.tools.version_comparison_performance import DEFAULT_FIXTURE
from core.svn_provider import provider_from_config


PROJECT_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_CONFIG = PROJECT_ROOT / "config" / "settings.json"
EXPECTED_RESULT_SET_SHA256 = (
    "d9b9fd7f3c02ef6fc47081d03c7d670ee4bddfbb55ad2a45e10a95bf7e4fda0f"
)
CONTENT_METHODS = {"read_bytes", "read_bytes_with_source", "export_files"}


class _CountingProvider:
    """Count only redacted, read-only SVN operations and reject HEAD."""

    def __init__(self, inner: Any) -> None:
        self.inner = inner
        self._lock = Lock()
        self._phase = "setup"
        self._calls: Counter[str] = Counter()
        self._failures: Counter[str] = Counter()
        self._seconds: defaultdict[str, float] = defaultdict(float)
        self._bytes: Counter[str] = Counter()
        self._phase_calls: Counter[str] = Counter()

    def __getattr__(self, name: str) -> Any:
        return getattr(self.inner, name)

    def set_phase(self, phase: str) -> None:
        with self._lock:
            self._phase = phase

    @staticmethod
    def _require_fixed(endpoint: Any) -> None:
        revision = getattr(endpoint, "revision", None)
        if not isinstance(revision, int) or isinstance(revision, bool) or revision <= 0:
            raise RuntimeError("real A/B acceptance forbids HEAD or non-fixed Revision")

    def _call(
        self,
        method: str,
        operation: Callable[[], Any],
        *,
        byte_counter: Callable[[Any], int] | None = None,
    ) -> Any:
        with self._lock:
            phase = self._phase
            self._calls[method] += 1
            self._phase_calls[f"{phase}.{method}"] += 1
        started = time.perf_counter()
        try:
            result = operation()
        except Exception:
            with self._lock:
                self._failures[method] += 1
            raise
        finally:
            elapsed = time.perf_counter() - started
            with self._lock:
                self._seconds[method] += elapsed
        if byte_counter is not None:
            with self._lock:
                self._bytes[method] += max(0, int(byte_counter(result)))
        return result

    def info(self, endpoint: Any) -> Any:
        self._require_fixed(endpoint)
        return self._call("info", lambda: self.inner.info(endpoint))

    def list_tree(self, endpoint: Any, prefix: str = "") -> Any:
        self._require_fixed(endpoint)
        return self._call("list_tree", lambda: self.inner.list_tree(endpoint, prefix))

    def list_children(self, endpoint: Any, prefix: str = "") -> Any:
        self._require_fixed(endpoint)
        return self._call(
            "list_children", lambda: self.inner.list_children(endpoint, prefix)
        )

    def resolve_branch_identity(self, endpoint: Any) -> Any:
        self._require_fixed(endpoint)
        return self._call(
            "resolve_branch_identity",
            lambda: self.inner.resolve_branch_identity(endpoint),
        )

    def summarize_frozen_tree_diff(
        self,
        source: Any,
        source_root: str,
        target: Any,
        target_root: str,
    ) -> Any:
        source_revision = getattr(source, "revision", None)
        target_revision = getattr(target, "revision", None)
        if not all(
            isinstance(value, int) and not isinstance(value, bool) and value > 0
            for value in (source_revision, target_revision)
        ):
            raise RuntimeError("real A/B acceptance forbids unfrozen diff evidence")
        return self._call(
            "summarize_frozen_tree_diff",
            lambda: self.inner.summarize_frozen_tree_diff(
                source, source_root, target, target_root
            ),
        )

    def read_bytes_with_source(self, endpoint: Any, path: str) -> tuple[bytes, str]:
        self._require_fixed(endpoint)
        return self._call(
            "read_bytes_with_source",
            lambda: self.inner.read_bytes_with_source(endpoint, path),
            byte_counter=lambda result: len(result[0]),
        )

    def read_bytes(self, endpoint: Any, path: str) -> bytes:
        self._require_fixed(endpoint)
        return self._call(
            "read_bytes",
            lambda: self.inner.read_bytes(endpoint, path),
            byte_counter=len,
        )

    def export_files(self, endpoint: Any, prefix: str, paths: list[str]) -> Any:
        self._require_fixed(endpoint)
        return self._call(
            "export_files",
            lambda: self.inner.export_files(endpoint, prefix, paths),
            byte_counter=lambda result: int(getattr(result, "exported_bytes", 0)),
        )

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            item_content_calls = sum(
                count
                for key, count in self._phase_calls.items()
                if key.startswith("item.") and key.rsplit(".", 1)[-1] in CONTENT_METHODS
            )
            return {
                "calls": dict(sorted(self._calls.items())),
                "failures": dict(sorted(self._failures.items())),
                "wall_seconds": {
                    key: round(value, 6) for key, value in sorted(self._seconds.items())
                },
                "bytes": dict(sorted(self._bytes.items())),
                "phase_calls": dict(sorted(self._phase_calls.items())),
                "item_content_calls": item_content_calls,
            }


class _ObservedCandidateResolver:
    def __init__(self, inner: Any, provider: _CountingProvider) -> None:
        self.inner = inner
        self.provider = provider

    def __getattr__(self, name: str) -> Any:
        return getattr(self.inner, name)

    def prepare(self, source: Any, target: Any) -> Any:
        self.provider.set_phase("prepare")
        try:
            with self.inner.performance.phase("batch.prepare"):
                return self.inner.inner.prepare(source, target)
        finally:
            self.provider.set_phase("between")

    def prepare_for_task(
        self,
        task_id: str,
        source: Any,
        target: Any,
        *,
        fresh: bool,
    ) -> Any:
        self.provider.set_phase("prepare")
        try:
            with self.inner.performance.phase("batch.prepare"):
                return self.inner.inner.prepare_for_task(
                    task_id,
                    source,
                    target,
                    fresh=fresh,
                )
        finally:
            self.provider.set_phase("between")


class _ObservedWorkbookRunner:
    def __init__(self, inner: Any, provider: _CountingProvider) -> None:
        self.inner = inner
        self.provider = provider

    def run(self, source: Any, target: Any, workbook_path: str) -> bytes:
        self.provider.set_phase("item")
        try:
            return self.inner.run(source, target, workbook_path)
        finally:
            self.provider.set_phase("between")


class _PhaseCapture(logging.Handler):
    def __init__(self) -> None:
        super().__init__()
        self._lock = Lock()
        self.records: list[dict[str, Any]] = []

    def emit(self, record: logging.LogRecord) -> None:
        metrics = getattr(record, "internal_metrics", None)
        if getattr(record, "event", None) != "batch.phase_timing":
            return
        if not isinstance(metrics, dict):
            return
        with self._lock:
            self.records.append(dict(metrics))
        print(
            json.dumps(
                {
                    "event": "batch.phase_timing",
                    "phase": str(metrics.get("phase", "unknown")),
                    "wall_seconds": round(
                        max(0, int(metrics.get("wall_ns", 0))) / 1_000_000_000,
                        6,
                    ),
                },
                sort_keys=True,
            ),
            file=sys.stderr,
            flush=True,
        )

    def for_task(self, task_id: str) -> dict[str, Any]:
        totals: Counter[str] = Counter()
        calls: Counter[str] = Counter()
        with self._lock:
            records = [
                item for item in self.records if str(item.get("task_id")) == task_id
            ]
        for item in records:
            phase = str(item.get("phase", "unknown"))
            totals[phase] += max(0, int(item.get("wall_ns", 0)))
            calls[phase] += 1
        return {
            phase: {
                "calls": calls[phase],
                "wall_seconds_sum": round(total / 1_000_000_000, 6),
            }
            for phase, total in sorted(totals.items())
        }


class _RoundHarness:
    def __init__(
        self,
        *,
        config: dict[str, Any],
        root: Path,
        optimized: bool,
        fixture: Any,
    ) -> None:
        self.fixture = fixture
        self.optimized = optimized
        self.performance = DiffPerformanceRecorder(enabled=True)
        isolated = deepcopy(config)
        svn_config = dict(isolated.get("svn", {}))
        if str(svn_config.get("provider", "")).casefold() != "cli":
            raise RuntimeError("real A/B acceptance requires the SVN CLI provider")
        svn_config["cache_dir"] = str(root / "svn-cache")
        isolated["svn"] = svn_config
        isolated["snapshot_reuse"] = {
            **(
                isolated.get("snapshot_reuse", {})
                if isinstance(isolated.get("snapshot_reuse"), dict)
                else {}
            ),
            "frozen_dataset_enabled": optimized,
            "cross_branch_csv_reuse_enabled": False,
        }
        isolated["manifest_parser"] = {
            **(
                isolated.get("manifest_parser", {})
                if isinstance(isolated.get("manifest_parser"), dict)
                else {}
            ),
            "ooxml_first_enabled": optimized,
        }
        isolated["workbook_execution"] = {
            **(
                isolated.get("workbook_execution", {})
                if isinstance(isolated.get("workbook_execution"), dict)
                else {}
            ),
            "four_way_enabled": False,
        }
        self.config = isolated
        self.registry = list(svn_config.get("endpoint_registry", []))
        registered = {str(record.get("id", "")) for record in self.registry}
        requested = {
            fixture.task.source.endpoint_id,
            fixture.task.target.endpoint_id,
        }
        if not requested <= registered:
            raise RuntimeError("fixture endpoints are not registered")
        for endpoint in (fixture.task.source, fixture.task.target):
            if (
                not isinstance(endpoint.revision, int)
                or isinstance(endpoint.revision, bool)
                or endpoint.revision <= 0
            ):
                raise RuntimeError("fixture must use fixed positive Revisions")

        raw_provider = provider_from_config(isolated)
        self.provider = _CountingProvider(raw_provider)
        snapshot_config = isolated["snapshot_reuse"]
        self.cache = PersistentSnapshotContentCache(
            root / "snapshot-cache",
            enabled=True,
            max_bytes=int(snapshot_config.get("persistent_cache", {}).get(
                "max_bytes", 2 * 1024 * 1024 * 1024
            )),
            max_file_entries=int(snapshot_config.get("persistent_cache", {}).get(
                "max_file_entries", 20_000
            )),
            max_tree_entries=int(snapshot_config.get("persistent_cache", {}).get(
                "max_tree_entries", 256
            )),
        )
        allowed_schemes = tuple(svn_config.get(
            "allowed_schemes", ("http", "https", "svn", "svn+ssh", "file")
        ))
        self.snapshot_metrics: list[dict[str, Any]] = []
        snapshot_service = SnapshotService(
            self.provider,
            allowed_schemes=allowed_schemes,
            max_workers=int(isolated.get("max_workers", 6)),
            content_read_workers=int(snapshot_config.get("content_read_workers", 12)),
            bulk_export_enabled=bool(snapshot_config.get("bulk_export_enabled", True)),
            bulk_export_min_files=int(snapshot_config.get("bulk_export_min_files", 8)),
            bulk_export_min_ratio=float(snapshot_config.get("bulk_export_min_ratio", 0.5)),
            preview_limit=int(svn_config.get("content_preview_max_bytes", 262144)),
            reuse_ttl_seconds=float(snapshot_config.get("ttl_seconds", 300)),
            reuse_max_entries=int(snapshot_config.get("max_entries", 8)),
            reuse_configuration={
                "dataset_layout": isolated.get("dataset_layout"),
                "manifest_parser": {"ooxml_first_enabled": optimized},
                "frozen_dataset": {
                    "enabled": optimized,
                    "cross_branch_csv_reuse_enabled": False,
                },
            },
            persistent_content_cache=self.cache,
            phase_timing_enabled=True,
            phase_timing_sink=self.snapshot_metrics.append,
        )
        layout_config = isolated.get("dataset_layout")
        if not isinstance(layout_config, dict):
            raise RuntimeError("dataset_layout is required")
        resolver = SVNWorkbookDatasetResolver(
            self.provider,
            lambda: self.registry,
            layout_config,
            allowed_schemes=allowed_schemes,
            snapshot_content_reader=snapshot_service.read_cached_snapshot_bytes,
            snapshot_content_lookup=(
                snapshot_service.lookup_cached_snapshot_file if optimized else None
            ),
            snapshot_service=snapshot_service if optimized else None,
            cross_branch_csv_reuse_enabled=False,
            ooxml_first=optimized,
        )
        candidate = SnapshotBatchCandidateResolver(
            snapshot_service,
            lambda: self.registry,
            dataset_preparer=(resolver.prepare_frozen_pair if optimized else None),
        )
        candidate = TimedBatchCandidateResolver(candidate, self.performance)
        candidate = _ObservedCandidateResolver(candidate, self.provider)
        runner = DefaultBatchWorkbookRunner(
            resolver,
            WorkbookDiffService(
                DatasetLayout.from_config(layout_config),
                ooxml_first=optimized,
            ),
        )
        runner = TimedBatchWorkbookRunner(runner, self.performance)
        runner = _ObservedWorkbookRunner(runner, self.provider)
        store = TimedBatchStore(root / "batch-state", performance=self.performance)
        self.service = BatchDiffService(
            store,
            candidate,
            runner,
            poll_interval_seconds=0.02,
            item_concurrency=1,
        )
        self.phase_capture = _PhaseCapture()
        self.phase_logger = logging.getLogger("app.services.batch_diff_service")
        self.phase_logger_previous_level = self.phase_logger.level
        self.phase_logger.setLevel(logging.INFO)
        self.phase_logger.addHandler(self.phase_capture)

    def close(self) -> None:
        self.service.close()
        self.phase_logger.removeHandler(self.phase_capture)
        self.phase_logger.setLevel(self.phase_logger_previous_level)

    def run(self, *, cache_state: str) -> dict[str, Any]:
        faulthandler.dump_traceback_later(180, repeat=False)
        provider_before = self.provider.snapshot()
        wall_started = time.perf_counter()
        cpu_started = time.process_time()
        first_result_seconds: float | None = None
        prepared_seconds: float | None = None
        payload = BatchCreateRequestPayload(
            schema_version="m2.batch-create.request.v1",
            request_id=uuid4(),
            source=self.fixture.task.source,
            target=self.fixture.task.target,
        )
        task, created = self.service.create_task(payload)
        if not created:
            raise RuntimeError("isolated A/B task was not created")
        next_progress_seconds = 10.0
        while task.status not in TERMINAL_TASK_STATUSES:
            time.sleep(0.02)
            task = self.service.get_task(task.task_id)
            elapsed = time.perf_counter() - wall_started
            if elapsed >= next_progress_seconds:
                item_states = Counter(item.status for item in task.items)
                with self.service._dataset_lease_lock:
                    service_dataset_lease_count = len(
                        self.service._dataset_leases
                    )
                with self.cache._lock:
                    cache_dataset_lease_count = len(self.cache._leases)
                print(
                    json.dumps(
                        {
                            "event": "batch.progress",
                            "elapsed_seconds": round(elapsed, 3),
                            "status": task.status,
                            "item_count": len(task.items),
                            "item_states": dict(sorted(item_states.items())),
                            "service_dataset_lease_count": (
                                service_dataset_lease_count
                            ),
                            "cache_dataset_lease_count": cache_dataset_lease_count,
                        },
                        sort_keys=True,
                    ),
                    file=sys.stderr,
                    flush=True,
                )
                next_progress_seconds += 10.0
            if prepared_seconds is None and task.items:
                prepared_seconds = elapsed
            if first_result_seconds is None and any(
                item.result_ref is not None for item in task.items
            ):
                first_result_seconds = elapsed

        golden = {
            item.candidate.path: self.fixture.golden_results[str(item.item_id)]
            for item in self.fixture.task.items
            if str(item.item_id) in self.fixture.golden_results
        }
        matched = 0
        mismatched = 0
        result_hashes: list[str] = []
        for item in task.items:
            expected = golden.get(item.candidate.path)
            if item.result_ref is None or expected is None:
                mismatched += 1
                continue
            content, digest = self.service.load_result(item.result_ref)
            result_hashes.append(digest)
            if content == expected:
                matched += 1
            else:
                mismatched += 1
        result_set_sha256 = hashlib.sha256(
            "\n".join(result_hashes).encode("ascii")
        ).hexdigest()
        provider_after = self.provider.snapshot()
        provider_delta: dict[str, Any] = {}
        for field in ("calls", "failures", "bytes", "phase_calls"):
            before_values = Counter(provider_before.get(field, {}))
            after_values = Counter(provider_after.get(field, {}))
            provider_delta[field] = dict(sorted((after_values - before_values).items()))
        provider_delta["wall_seconds"] = {
            key: round(
                float(value)
                - float(provider_before.get("wall_seconds", {}).get(key, 0.0)),
                6,
            )
            for key, value in sorted(provider_after.get("wall_seconds", {}).items())
            if float(value)
            - float(provider_before.get("wall_seconds", {}).get(key, 0.0))
            > 0
        }
        phase_calls_before = Counter(provider_before.get("phase_calls", {}))
        phase_calls_after = Counter(provider_after.get("phase_calls", {}))
        item_content_calls = sum(
            count
            for key, count in (phase_calls_after - phase_calls_before).items()
            if key.startswith("item.") and key.rsplit(".", 1)[-1] in CONTENT_METHODS
        )
        faulthandler.cancel_dump_traceback_later()
        return {
            "mode": "optimized" if self.optimized else "legacy",
            "cache_state": cache_state,
            "task_status": task.status,
            "workbook_count": len(task.items),
            "matched_count": matched,
            "mismatched_count": mismatched,
            "result_set_sha256": result_set_sha256,
            "prepared_seconds": round(prepared_seconds or 0.0, 6),
            "first_result_seconds": round(first_result_seconds or 0.0, 6),
            "all_results_seconds": round(time.perf_counter() - wall_started, 6),
            "cpu_seconds": round(time.process_time() - cpu_started, 6),
            "item_svn_content_calls": item_content_calls,
            "svn": provider_delta,
            "batch_phases": self.phase_capture.for_task(str(task.task_id)),
            "performance": self.performance.snapshot(),
            "gate_passed": (
                task.status == "completed"
                and len(task.items) == 55
                and matched == 55
                and mismatched == 0
                and result_set_sha256 == EXPECTED_RESULT_SET_SHA256
                and (not self.optimized or item_content_calls == 0)
            ),
        }


def _load_config(path: Path) -> dict[str, Any]:
    raw = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(raw, dict):
        raise RuntimeError("config root must be an object")
    return raw


def run_acceptance(
    *,
    config_path: Path,
    fixture_path: Path,
    cold_pairs: int,
    warm_reruns: int,
    include_legacy: bool = True,
) -> dict[str, Any]:
    config = _load_config(config_path)
    fixture = load_offline_fixture(fixture_path.read_bytes())
    runs: list[dict[str, Any]] = []
    temporary_root: Path | None = None
    with TemporaryDirectory(prefix="excel-merge-real-ab-") as temporary:
        temporary_root = Path(temporary)
        for pair in range(cold_pairs):
            if include_legacy:
                legacy = _RoundHarness(
                    config=config,
                    root=temporary_root / f"pair-{pair + 1}-legacy",
                    optimized=False,
                    fixture=fixture,
                )
                try:
                    runs.append(legacy.run(cache_state="cold"))
                finally:
                    legacy.close()
                if not runs[-1]["gate_passed"]:
                    break

            optimized = _RoundHarness(
                config=config,
                root=temporary_root / f"pair-{pair + 1}-optimized",
                optimized=True,
                fixture=fixture,
            )
            try:
                runs.append(optimized.run(cache_state="cold"))
                if runs[-1]["gate_passed"]:
                    for _ in range(warm_reruns):
                        runs.append(optimized.run(cache_state="warm"))
                        if not runs[-1]["gate_passed"]:
                            break
            finally:
                optimized.close()
            if not runs[-1]["gate_passed"]:
                break
    temporary_state_removed = bool(
        temporary_root is not None and not temporary_root.exists()
    )
    legacy_seconds = [
        run["all_results_seconds"]
        for run in runs
        if run["mode"] == "legacy" and run["cache_state"] == "cold"
    ]
    optimized_cold_seconds = [
        run["all_results_seconds"]
        for run in runs
        if run["mode"] == "optimized" and run["cache_state"] == "cold"
    ]
    optimized_warm_seconds = [
        run["all_results_seconds"]
        for run in runs
        if run["mode"] == "optimized" and run["cache_state"] == "warm"
    ]
    speedup = None
    if legacy_seconds and optimized_cold_seconds and optimized_cold_seconds[-1] > 0:
        speedup = round(legacy_seconds[-1] / optimized_cold_seconds[-1], 3)
    report = {
        "schema_version": "m2.version-comparison-real-ab-acceptance.v1",
        "scope": {
            "fixed_revisions_only": True,
            "svn_writes": False,
            "formal_configuration_modified": False,
            "formal_task_state_modified": False,
            "cross_branch_csv_reuse_enabled": False,
            "four_way_enabled": False,
            "legacy_included": include_legacy,
        },
        "runs": runs,
        "summary": {
            "legacy_cold_seconds": legacy_seconds,
            "optimized_cold_seconds": optimized_cold_seconds,
            "optimized_warm_seconds": optimized_warm_seconds,
            "latest_cold_speedup": speedup,
            "cold_target_seconds": 109.0,
            "warm_target_seconds": 43.7,
        },
        "temporary_state_removed": temporary_state_removed,
    }
    report["gate_passed"] = bool(
        temporary_state_removed
        and runs
        and all(run["gate_passed"] for run in runs)
    )
    return report


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    parser.add_argument("--fixture", type=Path, default=DEFAULT_FIXTURE)
    parser.add_argument("--cold-pairs", type=int, default=1)
    parser.add_argument("--warm-reruns", type=int, default=1)
    parser.add_argument("--optimized-only", action="store_true")
    parser.add_argument("--output", type=Path)
    args = parser.parse_args(argv)
    if args.cold_pairs < 1 or args.warm_reruns < 0:
        parser.error("cold-pairs must be >= 1 and warm-reruns must be >= 0")
    try:
        report = run_acceptance(
            config_path=args.config,
            fixture_path=args.fixture,
            cold_pairs=args.cold_pairs,
            warm_reruns=args.warm_reruns,
            include_legacy=not args.optimized_only,
        )
    except Exception:
        print(json.dumps({"status": "failed", "code": "real_ab_internal_error"}))
        return 2
    content = json.dumps(report, ensure_ascii=False, sort_keys=True, indent=2) + "\n"
    if args.output is not None:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(content, encoding="utf-8")
    print(content, end="")
    return 0 if report["gate_passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
