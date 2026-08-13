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


logger = logging.getLogger(__name__)


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
    ):
        self.snapshot_service = snapshot_service
        self.endpoint_registry = endpoint_registry

    def validate_endpoints(
        self,
        source: BatchEndpointPayload,
        target: BatchEndpointPayload,
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
            for record in self.endpoint_registry()
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
        self.validate_endpoints(source, target)
        snapshot = self.snapshot_service.create_snapshot_at_revisions(
            list(self.endpoint_registry()),
            source_id=source.endpoint_id,
            source_revision=source.revision,
            target_id=target.endpoint_id,
            target_revision=target.revision,
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
        return candidates


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
class _RunningItem:
    item_id: str
    lease_token: str
    started_monotonic: float
    last_heartbeat: float
    timed_out: bool = False


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
    ):
        self.store = store
        self.candidate_resolver = candidate_resolver
        self.workbook_runner = workbook_runner
        self.poll_interval_seconds = max(0.02, float(poll_interval_seconds))
        self.item_timeout_seconds = max(0.1, float(item_timeout_seconds))
        self.heartbeat_seconds = max(0.05, float(heartbeat_seconds))
        self.cleanup_interval_seconds = max(1, float(cleanup_interval_seconds))
        self._stop_event = Event()
        self._start_lock = Lock()
        self._started = False
        self._scheduler: Thread | None = None
        self._preparation_executor = ThreadPoolExecutor(
            max_workers=1,
            thread_name_prefix="m2-batch-prepare",
        )
        self._item_executor = ThreadPoolExecutor(
            max_workers=2,
            thread_name_prefix="m2-batch-item",
        )
        self._preparation_futures: dict[Future, str] = {}
        self._item_futures: dict[Future, _RunningItem] = {}

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
        return self.store.delete_task(
            task_id=str(task_id),
            request_id=request_id,
            reason=reason,
        )

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
    ) -> list[
        tuple[
            BatchCandidatePayload,
            str | None,
            BatchOrchestrationErrorPayload | None,
        ]
    ]:
        source = BatchEndpointPayload(
            endpoint_id=task["source_endpoint_id"],
            revision=task["source_revision"],
        )
        target = BatchEndpointPayload(
            endpoint_id=task["target_endpoint_id"],
            revision=task["target_revision"],
        )
        candidates = self.candidate_resolver.prepare(source, target)
        if task["candidate_scope"] == "all":
            return [(candidate, None, None) for candidate in candidates]
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
        return prepared

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

    def _harvest_preparations(self) -> None:
        for future, task_id in list(self._preparation_futures.items()):
            if not future.done():
                continue
            del self._preparation_futures[future]
            try:
                self.store.complete_preparation(task_id, future.result())
            except BatchDiffError as exc:
                self.store.fail_preparation(
                    task_id,
                    code=exc.code,
                    message=exc.message,
                    retryable=exc.status_code >= 500,
                )
            except Exception:
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
                continue
            if not running.timed_out and now - running.last_heartbeat >= self.heartbeat_seconds:
                if self.store.renew_lease(running.item_id, running.lease_token):
                    running.last_heartbeat = now

    def _scheduler_loop(self) -> None:
        last_cleanup = time.monotonic()
        while not self._stop_event.is_set():
            try:
                self._harvest_preparations()
                self._harvest_items()
                self.store.recover_expired_leases()
                if not self._preparation_futures:
                    task = self.store.claim_preparation()
                    if task is not None:
                        future = self._preparation_executor.submit(self._prepare_task, task)
                        self._preparation_futures[future] = task["task_id"]
                while len(self._item_futures) < 2:
                    claim = self.store.claim_next_item()
                    if claim is None:
                        break
                    future = self._item_executor.submit(self._execute_item, claim)
                    current = time.monotonic()
                    self._item_futures[future] = _RunningItem(
                        item_id=claim["item_id"],
                        lease_token=claim["lease_token"],
                        started_monotonic=current,
                        last_heartbeat=current,
                    )
                if time.monotonic() - last_cleanup >= self.cleanup_interval_seconds:
                    self.store.cleanup_expired()
                    last_cleanup = time.monotonic()
            except Exception:
                logger.exception("批量调度循环异常")
            self._stop_event.wait(self.poll_interval_seconds)
