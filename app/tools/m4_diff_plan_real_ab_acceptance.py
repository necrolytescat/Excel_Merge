"""Run an isolated, fixed-Revision real-SVN A/B acceptance for M4."""
from __future__ import annotations

import argparse
from collections import Counter, defaultdict
from copy import deepcopy
from datetime import datetime
import hashlib
import json
import logging
from pathlib import Path
import sqlite3
from tempfile import TemporaryDirectory
from threading import Lock, current_thread
import time
from typing import Any, Callable
from uuid import uuid4

from app.schemas.diff_plan import (
    DiffPlanCreateRequestPayload,
    DiffPlanRunStartRequestPayload,
)
from app.services.batch_diff_service import DefaultBatchWorkbookRunner
from app.services.diff_plan_run_service import DiffPlanRunService
from app.services.diff_plan_run_store import DiffPlanRunStore, TERMINAL_RUN_STATUSES
from app.services.diff_plan_store import DiffPlanStore
from app.services.snapshot_content_cache import PersistentSnapshotContentCache
from app.services.snapshot_service import SnapshotService
from app.services.workbook_dataset_service import SVNWorkbookDatasetResolver
from app.services.workbook_diff_service import DatasetLayout, WorkbookDiffService
from app.services.workbook_execution_gate import WorkbookExecutionGate
from core.svn_provider import provider_from_config


PROJECT_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_CONFIG = PROJECT_ROOT / "config" / "settings.json"
DEFAULT_DATABASE = PROJECT_ROOT / "var" / "m4-diff-plan" / "diff-plan.sqlite3"
DEFAULT_SOURCE_RUN_ID = "0e891dcb-980d-4633-b31b-e032c2c9399c"
CONTENT_METHODS = {"read_bytes", "read_bytes_with_source", "export_files"}


class _CountingProvider:
    """Count redacted read-only calls and reject unfrozen SVN content access."""

    def __init__(self, inner: Any) -> None:
        self.inner = inner
        self._lock = Lock()
        self._calls: Counter[str] = Counter()
        self._failures: Counter[str] = Counter()
        self._seconds: defaultdict[str, float] = defaultdict(float)
        self._bytes: Counter[str] = Counter()
        self._phase_calls: Counter[str] = Counter()

    def __getattr__(self, name: str) -> Any:
        return getattr(self.inner, name)

    @staticmethod
    def _require_fixed(endpoint: Any) -> None:
        revision = getattr(endpoint, "revision", None)
        if not isinstance(revision, int) or isinstance(revision, bool) or revision <= 0:
            raise RuntimeError("M4 real A/B acceptance forbids HEAD access")

    @staticmethod
    def _phase() -> str:
        name = current_thread().name
        if name.startswith("m4-plan-item"):
            return "item"
        if name.startswith("m4-plan-prepare"):
            return "prepare"
        return "setup"

    def _call(
        self,
        method: str,
        operation: Callable[[], Any],
        *,
        byte_counter: Callable[[Any], int] | None = None,
    ) -> Any:
        phase = self._phase()
        with self._lock:
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
        self._require_fixed(source)
        self._require_fixed(target)
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
            item_calls = sum(
                count
                for key, count in self._phase_calls.items()
                if key.startswith("item.")
            )
            item_content_calls = sum(
                count
                for key, count in self._phase_calls.items()
                if key.startswith("item.")
                and key.rsplit(".", 1)[-1] in CONTENT_METHODS
            )
            return {
                "calls": dict(sorted(self._calls.items())),
                "failures": dict(sorted(self._failures.items())),
                "wall_seconds": {
                    key: round(value, 6)
                    for key, value in sorted(self._seconds.items())
                },
                "bytes": dict(sorted(self._bytes.items())),
                "phase_calls": dict(sorted(self._phase_calls.items())),
                "item_calls": item_calls,
                "item_content_calls": item_content_calls,
            }


class _PhaseCapture(logging.Handler):
    def __init__(self) -> None:
        super().__init__()
        self._lock = Lock()
        self.records: list[dict[str, Any]] = []

    def emit(self, record: logging.LogRecord) -> None:
        if getattr(record, "event", None) != "m4.phase_timing":
            return
        metrics = getattr(record, "internal_metrics", None)
        if not isinstance(metrics, dict):
            return
        with self._lock:
            self.records.append(dict(metrics))

    def for_run(self, run_id: str) -> dict[str, Any]:
        wall: Counter[str] = Counter()
        calls: Counter[str] = Counter()
        with self._lock:
            records = [
                item for item in self.records if str(item.get("run_id")) == run_id
            ]
        for item in records:
            phase = str(item.get("phase", "unknown"))
            wall[phase] += max(0, int(item.get("wall_ns", 0)))
            calls[phase] += 1
        return {
            phase: {
                "calls": calls[phase],
                "wall_seconds_sum": round(total / 1_000_000_000, 6),
            }
            for phase, total in sorted(wall.items())
        }


def _load_source_run(database: Path, run_id: str) -> dict[str, Any]:
    uri = f"{database.resolve().as_uri()}?mode=ro"
    connection = sqlite3.connect(uri, uri=True)
    connection.row_factory = sqlite3.Row
    try:
        row = connection.execute(
            "SELECT * FROM diff_plan_runs WHERE run_id=?", (run_id,)
        ).fetchone()
        if row is None:
            raise RuntimeError("source M4 run was not found")
        if row["status"] not in TERMINAL_RUN_STATUSES:
            raise RuntimeError("source M4 run must be terminal")
        return {
            "run_id": str(row["run_id"]),
            "name": str(row["plan_name"]),
            "source_endpoint_id": str(row["source_endpoint_id"]),
            "target_endpoint_ids": json.loads(row["target_endpoint_ids_json"]),
            "workbook_paths": json.loads(row["workbook_paths_json"]),
            "source_revision": int(row["source_revision"]),
            "target_revisions": json.loads(row["target_revisions_json"]),
            "historical_started_at": row["started_at"],
            "historical_finished_at": row["finished_at"],
        }
    finally:
        connection.close()


def _elapsed_iso(started: str | None, finished: str | None) -> float | None:
    if not started or not finished:
        return None
    start = datetime.fromisoformat(started.replace("Z", "+00:00"))
    finish = datetime.fromisoformat(finished.replace("Z", "+00:00"))
    return round((finish - start).total_seconds(), 6)


def _round_config(
    base: dict[str, Any], root: Path, *, optimized: bool
) -> dict[str, Any]:
    config = deepcopy(base)
    svn = dict(config.get("svn") or {})
    if str(svn.get("provider", "")).casefold() != "cli":
        raise RuntimeError("M4 real A/B acceptance requires SVN CLI")
    svn["cache_dir"] = str(root / "svn-cache")
    config["svn"] = svn
    snapshot = dict(config.get("snapshot_reuse") or {})
    snapshot.update(
        {
            "frozen_dataset_enabled": optimized,
            "cross_branch_csv_reuse_enabled": False,
            "persistent_cache": {
                **dict(snapshot.get("persistent_cache") or {}),
                "enabled": True,
                "directory": str(root / "snapshot-cache"),
            },
        }
    )
    config["snapshot_reuse"] = snapshot
    config["manifest_parser"] = {
        **dict(config.get("manifest_parser") or {}),
        "ooxml_first_enabled": True,
    }
    config["diff_plan"] = {
        **dict(config.get("diff_plan") or {}),
        "database_path": str(root / "m4.sqlite3"),
        "frozen_dataset_enabled": optimized,
        "cleanup_interval_seconds": 3600,
    }
    config["batch_diff"] = {
        **dict(config.get("batch_diff") or {}),
        "state_directory": str(root / "m2-state"),
    }
    config["workbook_execution"] = {
        **dict(config.get("workbook_execution") or {}),
        "four_way_enabled": False,
    }
    config["operations"] = {
        **dict(config.get("operations") or {}),
        "logging": {"enabled": False},
    }
    return config


def _run_round(
    base_config: dict[str, Any],
    source: dict[str, Any],
    root: Path,
    *,
    optimized: bool,
) -> tuple[dict[str, Any], dict[int, bytes]]:
    config = _round_config(base_config, root, optimized=optimized)
    provider = _CountingProvider(provider_from_config(config))
    capture = _PhaseCapture()
    logger = logging.getLogger("app.services.diff_plan_run_service")
    old_level = logger.level
    logger.setLevel(logging.INFO)
    logger.addHandler(capture)
    svn_config = dict(config.get("svn") or {})
    snapshot_config = dict(config.get("snapshot_reuse") or {})
    persistent_config = dict(snapshot_config.get("persistent_cache") or {})
    layout_config = config.get("dataset_layout")
    if not isinstance(layout_config, dict):
        raise RuntimeError("dataset_layout is required")
    registry = SnapshotService.normalize_registry(
        svn_config.get("endpoint_registry") or []
    )
    cache = PersistentSnapshotContentCache(
        root / "snapshot-cache",
        enabled=True,
        max_bytes=int(persistent_config.get("max_bytes", 2 * 1024 * 1024 * 1024)),
        max_file_entries=int(persistent_config.get("max_file_entries", 20_000)),
        max_tree_entries=int(persistent_config.get("max_tree_entries", 256)),
    )
    allowed_schemes = tuple(
        svn_config.get(
            "allowed_schemes", ("http", "https", "svn", "svn+ssh", "file")
        )
    )
    snapshot_service = SnapshotService(
        provider,
        allowed_schemes=allowed_schemes,
        max_workers=int(config.get("max_workers", 6)),
        content_read_workers=int(snapshot_config.get("content_read_workers", 12)),
        bulk_export_enabled=bool(snapshot_config.get("bulk_export_enabled", True)),
        bulk_export_min_files=int(snapshot_config.get("bulk_export_min_files", 8)),
        bulk_export_min_ratio=float(snapshot_config.get("bulk_export_min_ratio", 0.5)),
        preview_limit=int(svn_config.get("content_preview_max_bytes", 262144)),
        reuse_ttl_seconds=float(snapshot_config.get("ttl_seconds", 300)),
        reuse_max_entries=int(snapshot_config.get("max_entries", 8)),
        reuse_configuration={
            "dataset_layout": layout_config,
            "manifest_parser": {"ooxml_first_enabled": True},
            "frozen_dataset": {
                "enabled": optimized,
                "cross_branch_csv_reuse_enabled": False,
            },
        },
        persistent_content_cache=cache,
        phase_timing_enabled=False,
    )
    resolver = SVNWorkbookDatasetResolver(
        provider,
        lambda: registry,
        layout_config,
        allowed_schemes=allowed_schemes,
        snapshot_content_reader=snapshot_service.read_cached_snapshot_bytes,
        snapshot_content_lookup=(
            snapshot_service.lookup_cached_snapshot_file if optimized else None
        ),
        snapshot_service=snapshot_service if optimized else None,
        cross_branch_csv_reuse_enabled=False,
        ooxml_first=True,
    )
    runner = DefaultBatchWorkbookRunner(
        resolver,
        WorkbookDiffService(
            DatasetLayout.from_config(layout_config), ooxml_first=True
        ),
        WorkbookExecutionGate(2),
    )
    plan_store = DiffPlanStore(root / "m4.sqlite3")
    run_store = DiffPlanRunStore(root / "m4.sqlite3", root / "results")
    service = DiffPlanRunService(
        plan_store=plan_store,
        run_store=run_store,
        snapshot_service=snapshot_service,
        provider=provider,
        endpoint_registry=lambda: registry,
        workbook_runner=runner,
        item_concurrency=2,
        cleanup_interval_seconds=3600,
        dataset_preparer=resolver.prepare_frozen_pair if optimized else None,
    )
    try:
        plan, created = plan_store.create(
            DiffPlanCreateRequestPayload(
                schema_version="m4.diff-plan-create.request.v1",
                request_id=uuid4(),
                name="M4 isolated real A/B acceptance",
                source_endpoint_id=source["source_endpoint_id"],
                target_endpoint_ids=source["target_endpoint_ids"],
                workbook_paths=source["workbook_paths"],
            )
        )
        if not created:
            raise RuntimeError("isolated M4 plan was not created")
        revisions = {
            source["source_endpoint_id"]: source["source_revision"],
            **source["target_revisions"],
        }
        started = time.perf_counter()
        cpu_started = time.process_time()
        run, created = service.start_run(
            plan.plan_id,
            DiffPlanRunStartRequestPayload(
                schema_version="m4.diff-plan-run-start.request.v1",
                request_id=uuid4(),
                revisions=revisions,
            ),
        )
        if not created:
            raise RuntimeError("isolated M4 run was not created")
        preparation_seconds: float | None = None
        first_result_seconds: float | None = None
        next_progress = 10.0
        while run.status not in TERMINAL_RUN_STATUSES:
            time.sleep(0.02)
            run = service.get_run(run.run_id)
            elapsed = time.perf_counter() - started
            if preparation_seconds is None and run.status in {
                "running",
                "cancelling",
                *TERMINAL_RUN_STATUSES,
            }:
                preparation_seconds = elapsed
            if first_result_seconds is None and any(
                item.result_ref is not None for item in run.items
            ):
                first_result_seconds = elapsed
            if elapsed >= next_progress:
                states = Counter(item.status for item in run.items)
                print(
                    json.dumps(
                        {
                            "event": "m4.real_ab.progress",
                            "mode": "optimized" if optimized else "legacy",
                            "elapsed_seconds": round(elapsed, 3),
                            "status": run.status,
                            "item_states": dict(sorted(states.items())),
                        },
                        sort_keys=True,
                    ),
                    flush=True,
                )
                next_progress += 10.0
        total_seconds = time.perf_counter() - started
        result_bytes: dict[int, bytes] = {}
        items: list[dict[str, Any]] = []
        for item in sorted(run.items, key=lambda value: value.ordinal):
            digest = None
            if item.result_ref is not None:
                content, digest = service.load_result(item.result_ref)
                result_bytes[item.ordinal] = content
            items.append(
                {
                    "ordinal": item.ordinal,
                    "workbook_path": item.workbook_path,
                    "target_endpoint_id": item.target_endpoint_id,
                    "status": item.status,
                    "candidate_status": item.candidate_status,
                    "source_exists": item.source_exists,
                    "target_exists": item.target_exists,
                    "source_sha256": item.source_sha256,
                    "target_sha256": item.target_sha256,
                    "diff_status": item.diff_status,
                    "diff_error_count": item.diff_error_count,
                    "result_sha256": digest,
                    "error": item.error.model_dump(mode="json") if item.error else None,
                }
            )
        canonical = json.dumps(
            items, ensure_ascii=False, sort_keys=True, separators=(",", ":")
        ).encode("utf-8")
        provider_metrics = provider.snapshot()
        result = {
            "mode": "optimized" if optimized else "legacy",
            "status": run.status,
            "item_count": len(run.items),
            "processed_count": run.progress.processed_items,
            "state_counts": dict(sorted(Counter(item.status for item in run.items).items())),
            "total_seconds": round(total_seconds, 6),
            "preparation_seconds": round(preparation_seconds or total_seconds, 6),
            "item_stage_seconds": round(
                max(0.0, total_seconds - (preparation_seconds or total_seconds)), 6
            ),
            "first_result_seconds": round(first_result_seconds or 0.0, 6),
            "cpu_seconds": round(time.process_time() - cpu_started, 6),
            "result_contract_sha256": hashlib.sha256(canonical).hexdigest(),
            "result_count": len(result_bytes),
            "item_svn_calls_total": provider_metrics["item_calls"],
            "item_svn_content_calls": provider_metrics["item_content_calls"],
            "svn": provider_metrics,
            "phases": capture.for_run(str(run.run_id)),
            "errors": [error.model_dump(mode="json") for error in run.errors],
        }
        return result, result_bytes
    finally:
        service.close()
        logger.removeHandler(capture)
        logger.setLevel(old_level)


def run_acceptance(
    *, config_path: Path, database_path: Path, source_run_id: str
) -> dict[str, Any]:
    config = json.loads(config_path.read_text(encoding="utf-8"))
    if not isinstance(config, dict):
        raise RuntimeError("config root must be an object")
    source = _load_source_run(database_path, source_run_id)
    registered = {
        str(item.get("id", ""))
        for item in (config.get("svn", {}).get("endpoint_registry") or [])
    }
    required = {source["source_endpoint_id"], *source["target_endpoint_ids"]}
    if not required <= registered:
        raise RuntimeError("source run contains an unregistered endpoint")
    with TemporaryDirectory(prefix="excel-merge-m4-real-ab-") as temporary:
        root = Path(temporary)
        legacy, legacy_bytes = _run_round(
            config, source, root / "legacy", optimized=False
        )
        optimized, optimized_bytes = _run_round(
            config, source, root / "optimized", optimized=True
        )
        temporary_state_removed = False
    temporary_state_removed = not root.exists()
    result_bytes_equal = (
        legacy_bytes.keys() == optimized_bytes.keys()
        and all(legacy_bytes[key] == optimized_bytes[key] for key in legacy_bytes)
    )
    total_speedup = (
        round(legacy["total_seconds"] / optimized["total_seconds"], 3)
        if optimized["total_seconds"] > 0
        else None
    )
    item_speedup = (
        round(
            legacy["item_stage_seconds"] / optimized["item_stage_seconds"], 3
        )
        if optimized["item_stage_seconds"] > 0
        else None
    )
    historical_seconds = _elapsed_iso(
        source["historical_started_at"], source["historical_finished_at"]
    )
    gate_passed = bool(
        temporary_state_removed
        and legacy["status"] in {"completed", "completed_with_failures"}
        and optimized["status"] == legacy["status"]
        and optimized["item_count"] == legacy["item_count"]
        and optimized["state_counts"] == legacy["state_counts"]
        and optimized["result_contract_sha256"]
        == legacy["result_contract_sha256"]
        and result_bytes_equal
        and optimized["item_svn_calls_total"] == 0
        and optimized["item_svn_content_calls"] == 0
    )
    return {
        "schema_version": "m4.diff-plan-real-ab-acceptance.v1",
        "scope": {
            "source_run_id": source["run_id"],
            "fixed_revisions_only": True,
            "workbook_count": len(source["workbook_paths"]),
            "target_count": len(source["target_endpoint_ids"]),
            "cross_branch_csv_reuse_enabled": False,
            "four_way_enabled": False,
            "formal_configuration_modified": False,
            "formal_database_modified": False,
        },
        "historical_total_seconds": historical_seconds,
        "runs": [legacy, optimized],
        "comparison": {
            "total_speedup": total_speedup,
            "total_reduction_percent": round(
                (1 - optimized["total_seconds"] / legacy["total_seconds"]) * 100,
                2,
            )
            if legacy["total_seconds"] > 0
            else None,
            "item_stage_speedup": item_speedup,
            "result_bytes_equal": result_bytes_equal,
            "result_contract_equal": optimized["result_contract_sha256"]
            == legacy["result_contract_sha256"],
        },
        "temporary_state_removed": temporary_state_removed,
        "gate_passed": gate_passed,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    parser.add_argument("--database", type=Path, default=DEFAULT_DATABASE)
    parser.add_argument("--source-run-id", default=DEFAULT_SOURCE_RUN_ID)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args(argv)
    try:
        report = run_acceptance(
            config_path=args.config,
            database_path=args.database,
            source_run_id=args.source_run_id,
        )
    except Exception:
        logging.exception("M4 real A/B acceptance failed")
        print(json.dumps({"status": "failed", "code": "m4_real_ab_internal_error"}))
        return 2
    content = json.dumps(report, ensure_ascii=False, sort_keys=True, indent=2) + "\n"
    if args.output is not None:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(content, encoding="utf-8")
    print(content, end="")
    return 0 if report["gate_passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
