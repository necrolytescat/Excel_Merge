"""M2-07 单机批量 Diff 编排、调度和恢复服务。"""
from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from contextlib import nullcontext
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import dataclass
from datetime import datetime, timezone
import json
import logging
from pathlib import PurePosixPath
from threading import Event, Lock, Thread
import time
from typing import Any, Protocol
from uuid import UUID, uuid4

from app.schemas.batch import (
    BatchCandidatePayload,
    BatchCandidateSidePayload,
    BatchCreateRequestPayload,
    BatchEndpointPayload,
    BatchOrchestrationErrorPayload,
    BatchRetryRequestPayload,
    BatchTaskDeleteResultPayload,
    BatchTaskListPayload,
    BatchTaskManagementPayload,
    BatchTaskPayload,
)
from app.schemas.diff import DiffResultPayload, serialize_diff_json
from app.schemas.workbook_compare import WorkbookCompareRequestPayload
from app.services.batch_store import (
    BatchDiffError,
    BatchStore,
    TERMINAL_TASK_STATUSES,
    canonical_json,
    json_hash,
)
from app.services.snapshot_service import SnapshotService
from app.services.workbook_dataset_service import (
    WorkbookCompareError,
    WorkbookDatasetResolver,
)
from app.services.workbook_diff_service import WorkbookDiffService
from app.services.workbook_execution_gate import WorkbookExecutionGate
from app.services.workbook_execution_scheduler import (
    PersistentWorkbookExecutionScheduler,
    WorkbookExecutionLease,
)


logger = logging.getLogger(__name__)


def _emit_batch_phase(
    task_id: str,
    phase: str,
    wall_ns: int,
    metrics: Mapping[str, int | str] | None = None,
) -> None:
    internal_metrics: dict[str, int | str] = {
        "schema_version": "m2.batch-phase-timing.v1",
        "task_id": str(task_id),
        "phase": str(phase),
        "wall_ns": max(0, int(wall_ns)),
    }
    if metrics:
        internal_metrics.update(metrics)
    logger.info(
        "M2 internal phase timing",
        extra={
            "event": "batch.phase_timing",
            "internal_metrics": internal_metrics,
        },
    )


class BatchCandidateResolver(Protocol):
    def prepare(
        self,
        source: BatchEndpointPayload,
        target: BatchEndpointPayload,
    ) -> list[BatchCandidatePayload]: ...


class BatchWorkbookRunner(Protocol):
    def run(
        self,
        source: BatchEndpointPayload,
        target: BatchEndpointPayload,
        workbook_path: str,
    ) -> bytes: ...


class SnapshotBatchCandidateResolver:
    """在给定冻结 Revision 上重建完整 M1 Table Excel 候选。"""

    def __init__(
        self,
        snapshot_service: SnapshotService,
        endpoint_registry: Callable[[], Sequence[Mapping[str, Any]]],
        *,
        dataset_preparer: Callable[..., Any] | None = None,
    ):
        self.snapshot_service = snapshot_service
        self.endpoint_registry = endpoint_registry
        self.dataset_preparer = dataset_preparer

    def validate_endpoints(
        self,
        source: BatchEndpointPayload,
        target: BatchEndpointPayload,
    ) -> None:
        self._validate_endpoint_records(source, target, list(self.endpoint_registry()))

    @staticmethod
    def _validate_endpoint_records(
        source: BatchEndpointPayload,
        target: BatchEndpointPayload,
        records: Sequence[Mapping[str, Any]],
    ) -> None:
        if (
            source.endpoint_id == target.endpoint_id
            and source.revision == target.revision
        ):
            raise BatchDiffError(
                "BATCH_ENDPOINT_REVISIONS_MUST_DIFFER",
                "同一分支的左右 Revision 必须不同",
                status_code=422,
            )
        records = {
            str(record.get("id", "")): record
            for record in records
        }
        for endpoint in (source, target):
            record = records.get(endpoint.endpoint_id)
            if record is None:
                raise BatchDiffError(
                    "BATCH_ENDPOINT_NOT_FOUND",
                    "批量请求中的端点不存在",
                    status_code=404,
                )
            if not bool(record.get("enabled", True)):
                raise BatchDiffError(
                    "BATCH_ENDPOINT_DISABLED",
                    "批量请求中的端点未启用",
                    status_code=422,
                )

    @staticmethod
    def _relative_path(file_path: str, table_path: str) -> str:
        normalized_file = str(file_path).replace("\\", "/").strip("/")
        normalized_table = str(table_path).replace("\\", "/").strip("/")
        file_folded = normalized_file.casefold()
        table_folded = normalized_table.casefold()
        if file_folded == table_folded:
            return ""
        prefix = table_folded + "/"
        if file_folded.startswith(prefix):
            return normalized_file[len(normalized_table) + 1 :]
        return normalized_file

    @staticmethod
    def _side(file_payload) -> BatchCandidateSidePayload:
        if file_payload is None:
            return BatchCandidateSidePayload(
                exists=False,
                size_bytes=None,
                content_sha256=None,
                read_error=None,
            )
        error = None
        if file_payload.error is not None:
            error = {
                "code": file_payload.error.code,
                "message": file_payload.error.message,
            }
        return BatchCandidateSidePayload(
            exists=True,
            size_bytes=file_payload.size,
            content_sha256=None if error else file_payload.content_hash,
            read_error=error,
        )

    @classmethod
    def _candidate(
        cls,
        path: str,
        source_file,
        target_file,
    ) -> BatchCandidatePayload | None:
        source = cls._side(source_file)
        target = cls._side(target_file)
        if not source.exists:
            status = "right_only"
        elif not target.exists:
            status = "left_only"
        elif source.read_error is not None or target.read_error is not None:
            status = "read_error"
        elif source.content_sha256 == target.content_sha256:
            return None
        else:
            status = "modified"
        facts = {
            "path": path,
            "status": status,
            "source": source.model_dump(mode="json"),
            "target": target.model_dump(mode="json"),
        }
        return BatchCandidatePayload(
            **facts,
            fingerprint_sha256=json_hash(facts),
        )

    def prepare(
        self,
        source: BatchEndpointPayload,
        target: BatchEndpointPayload,
    ) -> list[BatchCandidatePayload]:
        candidates, _ = self._prepare(source, target, reuse=True)
        return candidates

    def prepare_fresh(
        self,
        source: BatchEndpointPayload,
        target: BatchEndpointPayload,
    ) -> list[BatchCandidatePayload]:
        """Retries revalidate SVN facts instead of replaying the page cache."""
        candidates, _ = self._prepare(source, target, reuse=False)
        return candidates

    def prepare_for_task(
        self,
        task_id: str,
        source: BatchEndpointPayload,
        target: BatchEndpointPayload,
        *,
        fresh: bool = False,
    ) -> tuple[list[BatchCandidatePayload], Any | None]:
        return self._prepare(
            source,
            target,
            reuse=not fresh,
            lease_id=f"m2:{task_id}",
            task_id=task_id,
        )

    def requires_dataset_lease(self) -> bool:
        return self.dataset_preparer is not None

    def restore_dataset_lease(
        self,
        task_id: str,
        source: BatchEndpointPayload,
        target: BatchEndpointPayload,
        candidates: Sequence[BatchCandidatePayload],
    ) -> Any | None:
        if self.dataset_preparer is None:
            return None
        owner = getattr(self.dataset_preparer, "__self__", None)
        acquire = getattr(owner, "acquire_frozen_pair_lease", None)
        if acquire is not None:
            lease = acquire(source, target, lease_id=f"m2:{task_id}")
            if lease is not None:
                return lease
        _, lease = self._prepare(
            source,
            target,
            reuse=True,
            lease_id=f"m2:{task_id}",
            task_id=task_id,
        )
        return lease

    def release_dataset_lease(self, lease: Any) -> bool:
        if self.dataset_preparer is None:
            return lease is None
        owner = getattr(self.dataset_preparer, "__self__", None)
        release = getattr(owner, "release_frozen_pair_lease", None)
        return bool(release is not None and release(lease))

    def _prepare(
        self,
        source: BatchEndpointPayload,
        target: BatchEndpointPayload,
        *,
        reuse: bool,
        lease_id: str | None = None,
        task_id: str | None = None,
    ) -> tuple[list[BatchCandidatePayload], Any | None]:
        phase_started = time.perf_counter_ns()
        records = list(self.endpoint_registry())
        self._validate_endpoint_records(source, target, records)
        if task_id is not None:
            _emit_batch_phase(
                task_id,
                "freeze_revisions",
                time.perf_counter_ns() - phase_started,
            )
        snapshot_arguments = {
            "source_id": source.endpoint_id,
            "source_revision": source.revision,
            "target_id": target.endpoint_id,
            "target_revision": target.revision,
        }
        phase_started = time.perf_counter_ns()
        snapshot = (
            self.snapshot_service.create_snapshot_at_revisions(
                records,
                **snapshot_arguments,
            )
            if reuse
            else self.snapshot_service.create_snapshot_at_revisions(
                records,
                **snapshot_arguments,
                reuse=False,
            )
        )
        if task_id is not None:
            _emit_batch_phase(
                task_id,
                "enumerate_dataset",
                time.perf_counter_ns() - phase_started,
                {
                    "source_file_count": len(snapshot.source.files),
                    "target_file_count": len(snapshot.target.files),
                },
            )
        source_table = snapshot.source.physical_path_filters["TABLE"]
        target_table = snapshot.target.physical_path_filters["TABLE"]
        source_files = {
            self._relative_path(item.path, source_table): item
            for item in snapshot.source.files
        }
        target_files = {
            self._relative_path(item.path, target_table): item
            for item in snapshot.target.files
        }
        candidates = []
        for path in sorted(set(source_files) | set(target_files), key=lambda item: (item.casefold(), item)):
            candidate = self._candidate(
                path,
                source_files.get(path),
                target_files.get(path),
            )
            if candidate is not None:
                candidates.append(candidate)
        dataset_lease = None
        if self.dataset_preparer is not None:
            prepare_kwargs: dict[str, Any] = {"lease_id": lease_id}
            if task_id is not None:
                def phase_sink(
                    phase: str,
                    wall_ns: int,
                    metrics: Mapping[str, int | str] | None = None,
                ) -> None:
                    _emit_batch_phase(task_id, phase, wall_ns, metrics)

                prepare_kwargs["phase_sink"] = phase_sink
            dataset_lease = self.dataset_preparer(
                source,
                target,
                candidates,
                **prepare_kwargs,
            )
        return candidates, dataset_lease


class DefaultBatchWorkbookRunner:
    def __init__(
        self,
        dataset_resolver: WorkbookDatasetResolver,
        diff_service: WorkbookDiffService,
        execution_gate: WorkbookExecutionGate | None = None,
    ):
        self.dataset_resolver = dataset_resolver
        self.diff_service = diff_service
        self.execution_gate = execution_gate

    def run(
        self,
        source: BatchEndpointPayload,
        target: BatchEndpointPayload,
        workbook_path: str,
    ) -> bytes:
        payload = WorkbookCompareRequestPayload(
            schema_version="m2.workbook-compare.request.v1",
            request_id=uuid4(),
            source=source.model_dump(),
            target=target.model_dump(),
            workbook_path=workbook_path,
        )
        workbook_name = PurePosixPath(workbook_path).name
        gate = self.execution_gate.acquire() if self.execution_gate else nullcontext()
        with gate:
            with self.dataset_resolver.resolve(payload) as dataset:
                if (
                    getattr(self.diff_service, "supports_preparsed_manifests", False)
                    and dataset.source_manifest is not None
                    and dataset.target_manifest is not None
                ):
                    result = self.diff_service.compare_local(
                        dataset.source_directory,
                        dataset.target_directory,
                        workbook_name,
                        source_manifest=dataset.source_manifest,
                        target_manifest=dataset.target_manifest,
                    )
                else:
                    result = self.diff_service.compare_local(
                        dataset.source_directory,
                        dataset.target_directory,
                        workbook_name,
                    )
                return serialize_diff_json(result)


@dataclass
class _PreparedTask:
    items: list[
        tuple[
            BatchCandidatePayload,
            str | None,
            BatchOrchestrationErrorPayload | None,
        ]
    ]
    dataset_lease: Any | None = None


@dataclass
class _RunningItem:
    item_id: str
    lease_token: str
    started_monotonic: float
    last_heartbeat: float
    timed_out: bool = False
    execution_lease: WorkbookExecutionLease | None = None


class BatchDiffService:
    def __init__(
        self,
        store: BatchStore,
        candidate_resolver: BatchCandidateResolver,
        workbook_runner: BatchWorkbookRunner,
        *,
        poll_interval_seconds: float = 0.1,
        item_timeout_seconds: float = 600,
        heartbeat_seconds: float = 20,
        cleanup_interval_seconds: float = 21600,
        execution_scheduler: PersistentWorkbookExecutionScheduler | None = None,
        item_concurrency: int = 2,
    ):
        self.store = store
        self.candidate_resolver = candidate_resolver
        self.workbook_runner = workbook_runner
        self.poll_interval_seconds = max(0.02, float(poll_interval_seconds))
        self.item_timeout_seconds = max(0.1, float(item_timeout_seconds))
        self.heartbeat_seconds = max(0.05, float(heartbeat_seconds))
        self.cleanup_interval_seconds = max(1, float(cleanup_interval_seconds))
        self.execution_scheduler = execution_scheduler
        self.item_concurrency = max(1, int(item_concurrency))
        self.store.configure_execution_policy(
            global_concurrency=self.item_concurrency,
            per_task_concurrency=(
                min(
                    self.item_concurrency,
                    self.execution_scheduler.per_flow_limit,
                )
                if self.execution_scheduler is not None
                else 1
            ),
        )
        self._stop_event = Event()
        self._start_lock = Lock()
        self._started = False
        self._scheduler: Thread | None = None
        self._preparation_executor = ThreadPoolExecutor(
            max_workers=1,
            thread_name_prefix="m2-batch-prepare",
        )
        self._item_executor = ThreadPoolExecutor(
            max_workers=self.item_concurrency,
            thread_name_prefix="m2-batch-item",
        )
        self._preparation_futures: dict[Future, str] = {}
        self._item_futures: dict[Future, _RunningItem] = {}
        self._dataset_lease_lock = Lock()
        self._dataset_leases: dict[str, Any] = {}

    def start(self) -> None:
        if self._started:
            return
        with self._start_lock:
            if self._started:
                return
            self.store.recover()
            self.store.cleanup_expired()
            self._scheduler = Thread(
                target=self._scheduler_loop,
                name="m2-batch-scheduler",
                daemon=True,
            )
            self._scheduler.start()
            self._started = True

    def close(self) -> None:
        self._stop_event.set()
        if self._scheduler is not None:
            self._scheduler.join(timeout=2)
        self._preparation_executor.shutdown(wait=True, cancel_futures=True)
        self._item_executor.shutdown(wait=True, cancel_futures=True)
        self._release_all_dataset_leases()

    def create_task(
        self,
        payload: BatchCreateRequestPayload,
    ) -> tuple[BatchTaskPayload, bool]:
        if (
            payload.source.endpoint_id == payload.target.endpoint_id
            and payload.source.revision == payload.target.revision
        ):
            raise BatchDiffError(
                "BATCH_ENDPOINT_REVISIONS_MUST_DIFFER",
                "同一分支的左右 Revision 必须不同",
                status_code=422,
            )
        validator = getattr(self.candidate_resolver, "validate_endpoints", None)
        if validator is not None:
            validator(payload.source, payload.target)
        request_hash = json_hash(
            {
                "source": payload.source.model_dump(mode="json"),
                "target": payload.target.model_dump(mode="json"),
            }
        )
        task_id, created = self.store.create_task(
            request_id=payload.request_id,
            request_hash=request_hash,
            source=payload.source,
            target=payload.target,
        )
        task = self.store.get_task(task_id)
        self.start()
        return task, created

    def get_task(self, task_id: UUID | str) -> BatchTaskPayload:
        self.start()
        return self.store.get_task(str(task_id))

    def get_task_management(
        self,
        task_id: UUID | str,
    ) -> BatchTaskManagementPayload:
        self.start()
        return self.store.get_task_management(str(task_id))

    def list_tasks(
        self,
        *,
        limit: int,
        cursor: str | None,
        statuses: Sequence[str] | None,
        query: str | None,
        created_from: datetime | None,
        created_to: datetime | None,
    ) -> BatchTaskListPayload:
        def normalize(value: datetime | None) -> str | None:
            if value is None:
                return None
            if value.tzinfo is None:
                raise BatchDiffError(
                    "BATCH_INVALID_TIME_RANGE",
                    "创建时间范围必须包含时区",
                    status_code=400,
                )
            return value.astimezone(timezone.utc).isoformat(
                timespec="microseconds"
            ).replace("+00:00", "Z")

        start = normalize(created_from)
        end = normalize(created_to)
        if start and end and start > end:
            raise BatchDiffError(
                "BATCH_INVALID_TIME_RANGE",
                "创建时间起点不能晚于终点",
                status_code=400,
            )
        self.start()
        return self.store.list_tasks(
            limit=limit,
            cursor=cursor,
            statuses=statuses,
            query=query,
            created_from=start,
            created_to=end,
        )

    def cancel_task(
        self,
        task_id: UUID | str,
        *,
        request_id: UUID,
        reason: str | None,
    ) -> BatchTaskPayload:
        self.start()
        self.store.cancel_task(
            task_id=str(task_id),
            request_id=request_id,
            reason=reason,
        )
        return self.store.get_task(str(task_id))

    def delete_task(
        self,
        task_id: UUID | str,
        *,
        request_id: UUID,
        reason: str | None,
    ) -> BatchTaskDeleteResultPayload:
        self.start()
        result = self.store.delete_task(
            task_id=str(task_id),
            request_id=request_id,
            reason=reason,
        )
        self._release_dataset_lease(str(task_id))
        return result

    @staticmethod
    def _default_retryable(item) -> bool:
        return (
            item.status in {"orchestration_failed", "cancelled"}
            or (item.status == "skipped" and item.candidate.status == "read_error")
        )

    @staticmethod
    def _explicit_retryable(item) -> bool:
        return BatchDiffService._default_retryable(item) or item.status == "business_failed"

    def retry_task(
        self,
        task_id: UUID | str,
        payload: BatchRetryRequestPayload,
    ) -> tuple[BatchTaskPayload, bool]:
        self.start()
        parent = self.store.get_task(str(task_id))
        if parent.status not in TERMINAL_TASK_STATUSES:
            raise BatchDiffError(
                "BATCH_TASK_NOT_RETRYABLE",
                "仅终态批量任务可以重试",
                status_code=409,
            )
        by_id = {item.item_id: item for item in parent.items}
        if payload.item_ids is None:
            selected = [item for item in parent.items if self._default_retryable(item)]
        else:
            missing = [item_id for item_id in payload.item_ids if item_id not in by_id]
            if missing:
                raise BatchDiffError(
                    "BATCH_ITEM_NOT_RETRYABLE",
                    "重试项不存在或不属于当前任务",
                    status_code=422,
                )
            selected = [by_id[item_id] for item_id in payload.item_ids]
            if any(not self._explicit_retryable(item) for item in selected):
                raise BatchDiffError(
                    "BATCH_ITEM_NOT_RETRYABLE",
                    "选中的单项不可重试",
                    status_code=422,
                )
        if not selected:
            raise BatchDiffError(
                "BATCH_ITEM_NOT_RETRYABLE",
                "当前任务没有可重试的单项",
                status_code=422,
            )
        selected.sort(key=lambda item: item.ordinal)
        selection = [
            {
                "path": item.candidate.path,
                "retry_of_item_id": str(item.item_id),
                "candidate": item.candidate.model_dump(mode="json"),
            }
            for item in selected
        ]
        request_hash = json_hash(
            {
                "retry_of_task_id": str(parent.task_id),
                "item_ids": [str(item.item_id) for item in selected],
            }
        )
        child_id, created = self.store.create_task(
            request_id=payload.request_id,
            request_hash=request_hash,
            source=parent.source,
            target=parent.target,
            candidate_scope="retry_subset",
            retry_of_task_id=parent.task_id,
            retry_selection=selection,
        )
        return self.store.get_task(child_id), created

    def load_result(self, result_ref: str) -> tuple[bytes, str]:
        self.start()
        if not result_ref.startswith("m2r_") or len(result_ref) != 26:
            raise BatchDiffError(
                "BATCH_RESULT_NOT_FOUND",
                "批量结果不存在",
                status_code=404,
            )
        return self.store.load_result(result_ref)

    def _prepare_task(
        self,
        task: dict[str, Any],
    ) -> _PreparedTask:
        source = BatchEndpointPayload(
            endpoint_id=task["source_endpoint_id"],
            revision=task["source_revision"],
        )
        target = BatchEndpointPayload(
            endpoint_id=task["target_endpoint_id"],
            revision=task["target_revision"],
        )
        fresh = task["candidate_scope"] == "retry_subset"
        task_preparer = getattr(
            self.candidate_resolver,
            "prepare_for_task",
            None,
        )
        if task_preparer is not None:
            candidates, dataset_lease = task_preparer(
                task["task_id"],
                source,
                target,
                fresh=fresh,
            )
        else:
            fresh_preparer = getattr(
                self.candidate_resolver,
                "prepare_fresh",
                None,
            )
            candidates = (
                fresh_preparer(source, target)
                if fresh and fresh_preparer is not None
                else self.candidate_resolver.prepare(source, target)
            )
            dataset_lease = None
        if task["candidate_scope"] == "all":
            return _PreparedTask(
                [(candidate, None, None) for candidate in candidates],
                dataset_lease,
            )
        by_path = {candidate.path: candidate for candidate in candidates}
        prepared = []
        for selected in task.get("retry_selection") or []:
            candidate = by_path.get(selected["path"])
            initial_error = None
            if candidate is None:
                candidate = BatchCandidatePayload.model_validate(selected["candidate"])
                initial_error = BatchOrchestrationErrorPayload(
                    code="BATCH_RETRY_CANDIDATE_NOT_FOUND",
                    message="冻结输入下未能重新确认该候选",
                    retryable=True,
                )
            prepared.append((candidate, selected["retry_of_item_id"], initial_error))
        return _PreparedTask(prepared, dataset_lease)

    def _execute_item(self, claim: dict[str, Any]) -> None:
        item_id = claim["item_id"]
        lease_token = claim["lease_token"]
        try:
            source = BatchEndpointPayload(
                endpoint_id=claim["source_endpoint_id"],
                revision=claim["source_revision"],
            )
            target = BatchEndpointPayload(
                endpoint_id=claim["target_endpoint_id"],
                revision=claim["target_revision"],
            )
            content = self.workbook_runner.run(
                source,
                target,
                claim["candidate"]["path"],
            )
            parsed = DiffResultPayload.model_validate_json(content)
        except WorkbookCompareError as exc:
            self.store.fail_item(
                item_id=item_id,
                lease_token=lease_token,
                code="BATCH_ITEM_DATASET_FAILED",
                message=exc.message,
                retryable=True,
            )
            return
        except Exception:
            logger.exception("批量单项执行失败 task_id=%s item_id=%s", claim["task_id"], item_id)
            self.store.fail_item(
                item_id=item_id,
                lease_token=lease_token,
                code="BATCH_ITEM_UNEXPECTED",
                message="单工作簿批量编排失败",
                retryable=True,
            )

            return

        result_path = None
        try:
            result = self.store.write_result_blob(claim["task_id"], item_id, content)
            result_path = result["result_path"]
            committed = self.store.complete_item_result(
                item_id=item_id,
                lease_token=lease_token,
                diff_status=parsed.workbook.status,
                diff_error_count=parsed.summary.error_count,
                result=result,
            )
            if not committed:
                self.store.remove_result_blob(result_path)
        except Exception:
            logger.exception(
                "批量结果保存失败 task_id=%s item_id=%s",
                claim["task_id"],
                item_id,
            )
            self.store.remove_result_blob(result_path)
            self.store.fail_item(
                item_id=item_id,
                lease_token=lease_token,
                code="BATCH_ITEM_RESULT_STORE_FAILED",
                message="单工作簿结果保存失败",
                retryable=True,
            )

    def _release_lease_object(self, lease: Any | None) -> None:
        if lease is None:
            return
        release = getattr(
            self.candidate_resolver,
            "release_dataset_lease",
            None,
        )
        if release is None or not release(lease):
            logger.warning("Frozen dataset lease release failed")

    def _register_dataset_lease(self, task_id: str, lease: Any) -> None:
        with self._dataset_lease_lock:
            previous = self._dataset_leases.get(task_id)
            if previous is None:
                self._dataset_leases[task_id] = lease
                return
        if previous == lease:
            return
        self._release_lease_object(lease)

    def _release_dataset_lease(self, task_id: str) -> None:
        with self._dataset_lease_lock:
            lease = self._dataset_leases.pop(task_id, None)
        self._release_lease_object(lease)

    def _release_all_dataset_leases(self) -> None:
        with self._dataset_lease_lock:
            leases = list(self._dataset_leases.values())
            self._dataset_leases.clear()
        for lease in leases:
            self._release_lease_object(lease)

    def _release_terminal_dataset_leases(self) -> None:
        with self._dataset_lease_lock:
            task_ids = list(self._dataset_leases)
        for task_id in task_ids:
            try:
                terminal = (
                    self.store.get_task(task_id).status
                    in TERMINAL_TASK_STATUSES
                )
            except BatchDiffError:
                terminal = True
            if terminal:
                phase_started = time.perf_counter_ns()
                self._release_dataset_lease(task_id)
                _emit_batch_phase(
                    task_id,
                    "finalize",
                    time.perf_counter_ns() - phase_started,
                )

    def _ensure_dataset_lease(self, task_id: str) -> bool:
        with self._dataset_lease_lock:
            if task_id in self._dataset_leases:
                return True
        requires = getattr(
            self.candidate_resolver,
            "requires_dataset_lease",
            None,
        )
        if requires is not None and not requires():
            return True
        restore = getattr(
            self.candidate_resolver,
            "restore_dataset_lease",
            None,
        )
        if restore is None:
            return True
        try:
            task = self.store.get_task(task_id)
            if task.status in TERMINAL_TASK_STATUSES:
                return False
            lease = restore(
                task_id,
                task.source,
                task.target,
                [item.candidate for item in task.items],
            )
        except Exception:
            logger.exception(
                "Frozen dataset lease restore failed task_id=%s",
                task_id,
            )
            return False
        if lease is None:
            return False
        self._register_dataset_lease(task_id, lease)
        return True

    def _harvest_preparations(self) -> None:
        for future, task_id in list(self._preparation_futures.items()):
            if not future.done():
                continue
            del self._preparation_futures[future]
            prepared = None
            try:
                prepared = future.result()
                self.store.complete_preparation(task_id, prepared.items)
                if prepared.dataset_lease is not None:
                    self._register_dataset_lease(
                        task_id,
                        prepared.dataset_lease,
                    )
            except BatchDiffError as exc:
                if prepared is not None:
                    self._release_lease_object(prepared.dataset_lease)
                self.store.fail_preparation(
                    task_id,
                    code=exc.code,
                    message=exc.message,
                    retryable=exc.status_code >= 500,
                )
            except Exception:
                if prepared is not None:
                    self._release_lease_object(prepared.dataset_lease)
                logger.exception("批量候选准备失败 task_id=%s", task_id)
                self.store.fail_preparation(
                    task_id,
                    code="BATCH_CANDIDATE_PREPARATION_FAILED",
                    message="无法准备冻结 Revision 的工作簿候选",
                    retryable=True,
                )

    def _harvest_items(self) -> None:
        now = time.monotonic()
        for future, running in list(self._item_futures.items()):
            if future.done():
                del self._item_futures[future]
                try:
                    future.result()
                except Exception:
                    logger.exception("批量 Worker 未捕获异常 item_id=%s", running.item_id)
                continue
            elapsed = now - running.started_monotonic
            if not running.timed_out and elapsed >= self.item_timeout_seconds:
                running.timed_out = True
                self.store.fail_item(
                    item_id=running.item_id,
                    lease_token=running.lease_token,
                    code="BATCH_ITEM_TIMEOUT",
                    message=f"单工作簿处理超过 {int(self.item_timeout_seconds)} 秒",
                    retryable=True,
                )
            if now - running.last_heartbeat >= self.heartbeat_seconds:
                if not running.timed_out:
                    self.store.renew_lease(running.item_id, running.lease_token)
                if running.execution_lease is not None:
                    running.execution_lease.renew()
                running.last_heartbeat = now

    def _claim_scheduled_item(
        self,
    ) -> tuple[dict[str, Any] | None, WorkbookExecutionLease | None]:
        task_ids = self.store.runnable_task_ids()
        if self.execution_scheduler is None:
            for task_id in task_ids:
                if not self._ensure_dataset_lease(task_id):
                    continue
                claim = self.store.claim_next_item(
                    task_id=task_id,
                    global_limit=self.item_concurrency,
                    per_task_limit=1,
                )
                if claim is not None:
                    return claim, None
            return None, None
        self.execution_scheduler.sync_demands(
            "m2", [f"m2:{task_id}" for task_id in task_ids]
        )
        for task_id in task_ids:
            if not self._ensure_dataset_lease(task_id):
                continue
            lease = self.execution_scheduler.try_acquire(f"m2:{task_id}")
            if lease is None:
                continue
            claim = self.store.claim_next_item(
                task_id=task_id,
                global_limit=self.item_concurrency,
                per_task_limit=self.item_concurrency,
            )
            if claim is not None:
                return claim, lease
            lease.release()
        return None, None

    def _execute_item_in_slot(
        self,
        claim: dict[str, Any],
        execution_lease: WorkbookExecutionLease | None,
    ) -> None:
        phase_started = time.perf_counter_ns()
        try:
            self._execute_item(claim)
        finally:
            _emit_batch_phase(
                claim["task_id"],
                "compare_items",
                time.perf_counter_ns() - phase_started,
            )
            if execution_lease is not None:
                execution_lease.release()

    def _scheduler_loop(self) -> None:
        last_cleanup = time.monotonic()
        while not self._stop_event.is_set():
            try:
                self._harvest_preparations()
                self._harvest_items()
                self._release_terminal_dataset_leases()
                self.store.recover_expired_leases()
                if not self._preparation_futures:
                    task = self.store.claim_preparation()
                    if task is not None:
                        future = self._preparation_executor.submit(self._prepare_task, task)
                        self._preparation_futures[future] = task["task_id"]
                while len(self._item_futures) < self.item_concurrency:
                    claim, execution_lease = self._claim_scheduled_item()
                    if claim is None:
                        break
                    future = self._item_executor.submit(
                        self._execute_item_in_slot,
                        claim,
                        execution_lease,
                    )
                    current = time.monotonic()
                    self._item_futures[future] = _RunningItem(
                        item_id=claim["item_id"],
                        lease_token=claim["lease_token"],
                        started_monotonic=current,
                        last_heartbeat=current,
                        execution_lease=execution_lease,
                    )
                if time.monotonic() - last_cleanup >= self.cleanup_interval_seconds:
                    self.store.cleanup_expired()
                    last_cleanup = time.monotonic()
            except Exception:
                logger.exception("批量调度循环异常")
            self._stop_event.wait(self.poll_interval_seconds)
