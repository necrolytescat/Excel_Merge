"""M4 冻结 Revision、准备矩阵执行项并复用 M2 单工作簿执行器。"""
from __future__ import annotations

from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import dataclass
import hashlib
import json
import logging
from pathlib import PurePosixPath
from threading import Event, Lock, Thread
import time
from types import SimpleNamespace
from typing import Any
from uuid import UUID

from app.schemas.batch import BatchEndpointPayload
from app.schemas.diff import DiffResultPayload
from app.schemas.diff_plan import (
    DiffPlanRunCommandRequestPayload,
    DiffPlanRunListPayload,
    DiffPlanRunPayload,
    DiffPlanRunRetryRequestPayload,
    DiffPlanRunStartRequestPayload,
)
from app.services.batch_diff_service import DefaultBatchWorkbookRunner
from app.services.diff_plan_run_store import (
    DiffPlanRunStore,
    RETRYABLE_ITEM_STATUSES,
    TERMINAL_RUN_STATUSES,
)
from app.services.diff_plan_store import DiffPlanError, DiffPlanStore, _hash
from app.services.snapshot_service import SnapshotService
from app.services.workbook_dataset_service import WorkbookCompareError
from app.services.workbook_execution_scheduler import (
    PersistentWorkbookExecutionScheduler,
    WorkbookExecutionLease,
)
from core.models import EndpointSpec
from core.svn_provider import SVNProvider, SVNProviderError, normalize_relative_path


logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class _EndpointSnapshot:
    endpoint: EndpointSpec
    table_path: str
    files: dict[str, str]


@dataclass
class _RunningPlanItem:
    item_id: str
    lease_token: str
    execution_lease: WorkbookExecutionLease | None
    last_heartbeat: float


class DiffPlanRunService:
    def __init__(
        self,
        *,
        plan_store: DiffPlanStore,
        run_store: DiffPlanRunStore,
        snapshot_service: SnapshotService,
        provider: SVNProvider,
        endpoint_registry,
        workbook_runner: DefaultBatchWorkbookRunner,
        poll_interval_seconds: float = 0.1,
        execution_scheduler: PersistentWorkbookExecutionScheduler | None = None,
        item_concurrency: int = 2,
        heartbeat_seconds: float = 20.0,
        cleanup_interval_seconds: float = 3600,
    ):
        self.plan_store = plan_store
        self.run_store = run_store
        self.snapshot_service = snapshot_service
        self.provider = provider
        self.endpoint_registry = endpoint_registry
        self.workbook_runner = workbook_runner
        self.poll_interval_seconds = max(0.02, float(poll_interval_seconds))
        self.cleanup_interval_seconds = max(60, float(cleanup_interval_seconds))
        self.execution_scheduler = execution_scheduler
        self.item_concurrency = max(1, int(item_concurrency))
        self.heartbeat_seconds = max(0.05, float(heartbeat_seconds))
        self._next_cleanup_at = 0.0
        self._stop = Event()
        self._start_lock = Lock()
        self._started = False
        self._scheduler: Thread | None = None
        self._preparation_executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="m4-plan-prepare")
        self._item_executor = ThreadPoolExecutor(
            max_workers=self.item_concurrency, thread_name_prefix="m4-plan-item"
        )
        self._preparations: dict[Future, str] = {}
        self._items: dict[Future, _RunningPlanItem] = {}

    def start(self) -> None:
        if self._started:
            return
        with self._start_lock:
            if self._started:
                return
            self.run_store.recover()
            self._cleanup_expired_results()
            self._scheduler = Thread(target=self._loop, name="m4-plan-scheduler", daemon=True)
            self._scheduler.start()
            self._started = True

    def close(self) -> None:
        self._stop.set()
        if self._scheduler:
            self._scheduler.join(timeout=2)
        self._preparation_executor.shutdown(wait=True, cancel_futures=True)
        self._item_executor.shutdown(wait=True, cancel_futures=True)

    def _records(self) -> dict[str, dict[str, Any]]:
        return {
            str(record.get("id", "")): record
            for record in SnapshotService.normalize_registry([dict(value) for value in self.endpoint_registry()])
        }

    def _freeze(self, endpoint_id: str, requested, records: dict[str, dict[str, Any]]) -> int:
        record = records.get(endpoint_id)
        if record is None:
            raise DiffPlanError("DIFF_PLAN_ENDPOINT_NOT_FOUND", "运行包含不存在的分支", status_code=404)
        if not bool(record.get("enabled", True)):
            raise DiffPlanError("DIFF_PLAN_ENDPOINT_DISABLED", "运行包含已停用的分支", status_code=422)
        revision = self.snapshot_service.freeze_head(record) if requested == "HEAD" else requested
        if not isinstance(revision, int) or isinstance(revision, bool) or revision <= 0:
            raise DiffPlanError("DIFF_PLAN_INVALID_REVISION", "分支没有返回有效 Revision", status_code=422)
        return revision

    def start_run(self, plan_id: UUID | str, payload: DiffPlanRunStartRequestPayload) -> tuple[DiffPlanRunPayload, bool]:
        plan = self.plan_store.get(plan_id)
        if plan.archived:
            raise DiffPlanError("DIFF_PLAN_ARCHIVED", "归档计划不能启动新运行", status_code=409)
        allowed = {plan.source_endpoint_id, *plan.target_endpoint_ids}
        unknown = set(payload.revisions) - allowed
        if unknown:
            raise DiffPlanError("DIFF_PLAN_INVALID_REVISION_ENDPOINT", "Revision 设置包含计划外分支", status_code=422)
        requested = {endpoint_id: payload.revisions.get(endpoint_id, "HEAD") for endpoint_id in allowed}
        request_hash = _hash({"plan_id": str(plan.plan_id), "revisions": requested})
        replay = self.run_store.replay_run(payload.request_id, request_hash)
        if replay is not None:
            self.start()
            return replay, False
        records = self._records()
        source_revision = self._freeze(plan.source_endpoint_id, requested[plan.source_endpoint_id], records)
        target_revisions = {
            target: self._freeze(target, requested[target], records)
            for target in plan.target_endpoint_ids
        }
        run, created = self.run_store.create_run(
            request_id=payload.request_id,
            request_hash=request_hash,
            plan=plan,
            source_revision=source_revision,
            target_revisions=target_revisions,
        )
        self.start()
        return run, created

    def get_run(self, run_id: UUID | str) -> DiffPlanRunPayload:
        self.start()
        return self.run_store.get_run(run_id)

    def list_runs(self, plan_id: UUID | str) -> DiffPlanRunListPayload:
        self.plan_store.get(plan_id)
        self.start()
        return self.run_store.list_runs(plan_id)

    def cancel(self, run_id: UUID | str, payload: DiffPlanRunCommandRequestPayload) -> DiffPlanRunPayload:
        self.start()
        return self.run_store.cancel(run_id, payload.request_id)

    def retry(self, run_id: UUID | str, payload: DiffPlanRunRetryRequestPayload) -> tuple[DiffPlanRunPayload, bool]:
        parent = self.run_store.get_run(run_id)
        if parent.status not in TERMINAL_RUN_STATUSES:
            raise DiffPlanError("DIFF_PLAN_RUN_NOT_RETRYABLE", "仅终态运行可以重试", status_code=409)
        by_id = {item.item_id: item for item in parent.items}
        if payload.item_ids is None:
            selected = [item for item in parent.items if item.status in RETRYABLE_ITEM_STATUSES]
        else:
            if len(set(payload.item_ids)) != len(payload.item_ids) or any(item_id not in by_id for item_id in payload.item_ids):
                raise DiffPlanError("DIFF_PLAN_ITEM_NOT_RETRYABLE", "重试项不存在、重复或不属于当前运行", status_code=422)
            selected = [by_id[item_id] for item_id in payload.item_ids]
            if any(item.status not in RETRYABLE_ITEM_STATUSES for item in selected):
                raise DiffPlanError("DIFF_PLAN_ITEM_NOT_RETRYABLE", "选中的执行项不可重试", status_code=422)
        if not selected:
            raise DiffPlanError("DIFF_PLAN_ITEM_NOT_RETRYABLE", "当前运行没有可重试执行项", status_code=422)
        selected.sort(key=lambda item: item.ordinal)
        plan_snapshot = SimpleNamespace(
            plan_id=parent.plan_id,
            version=parent.plan_version,
            name=parent.plan_name,
            source_endpoint_id=parent.source_endpoint_id,
            target_endpoint_ids=list(dict.fromkeys(item.target_endpoint_id for item in selected)),
            workbook_paths=list(dict.fromkeys(item.workbook_path for item in selected)),
        )
        request_hash = _hash({
            "retry_of_run_id": str(parent.run_id),
            "item_ids": [str(item.item_id) for item in selected],
        })
        run, created = self.run_store.create_run(
            request_id=payload.request_id,
            request_hash=request_hash,
            plan=plan_snapshot,
            source_revision=parent.source_revision,
            target_revisions={target: parent.target_revisions[target] for target in plan_snapshot.target_endpoint_ids},
            retry_of_run_id=parent.run_id,
            retry_items=selected,
        )
        self.start()
        return run, created

    def load_result(self, result_ref: str) -> tuple[bytes, str]:
        self.start()
        if not result_ref.startswith("m4r_") or len(result_ref) != 26:
            raise DiffPlanError("DIFF_PLAN_RESULT_NOT_FOUND", "运行明细不存在", status_code=404)
        return self.run_store.load_result(result_ref)

    def _endpoint_snapshot(self, endpoint_id: str, revision: int, records: dict[str, dict[str, Any]]) -> _EndpointSnapshot:
        record = records[endpoint_id]
        endpoint = EndpointSpec(url=str(record["url"]), revision=revision, label=str(record.get("label", endpoint_id)))
        entries = self.provider.list_tree(endpoint)
        table_path = normalize_relative_path(self.snapshot_service.resolve_scope_paths(record, revision, entries=entries)["TABLE"])
        prefix = table_path.casefold() + "/"
        files = {}
        for entry in entries:
            normalized = normalize_relative_path(entry.path)
            if entry.kind != "file" or not normalized.casefold().startswith(prefix):
                continue
            relative = normalized[len(table_path) + 1:]
            files.setdefault(relative.casefold(), normalized)
        return _EndpointSnapshot(endpoint=endpoint, table_path=table_path, files=files)

    def _prepare(self, raw: dict[str, Any]) -> None:
        run_id = raw["run_id"]
        try:
            run = self.run_store.get_run(run_id)
            records = self._records()
            endpoint_snapshots = {
                run.source_endpoint_id: self._endpoint_snapshot(run.source_endpoint_id, run.source_revision, records),
                **{
                    target: self._endpoint_snapshot(target, revision, records)
                    for target, revision in run.target_revisions.items()
                },
            }
            hash_cache: dict[tuple[str, str], tuple[bool, str | None, dict[str, Any] | None]] = {}

            def side(endpoint_id: str, workbook_path: str):
                key = (endpoint_id, workbook_path.casefold())
                if key in hash_cache:
                    return hash_cache[key]
                snapshot = endpoint_snapshots[endpoint_id]
                physical = snapshot.files.get(workbook_path.casefold())
                if physical is None:
                    value = (False, None, None)
                else:
                    try:
                        content = self.provider.read_bytes(snapshot.endpoint, physical)
                        value = (True, hashlib.sha256(content).hexdigest(), None)
                    except SVNProviderError as exc:
                        value = (True, None, {"code": exc.code, "message": exc.message, "retryable": True})
                hash_cache[key] = value
                return value

            for item in run.items:
                source_exists, source_hash, source_error = side(run.source_endpoint_id, item.workbook_path)
                target_exists, target_hash, target_error = side(item.target_endpoint_id, item.workbook_path)
                error = source_error or target_error
                if error:
                    status, candidate = "read_failed", "read_error"
                elif not source_exists and not target_exists:
                    status, candidate = "both_missing", "both_missing"
                elif not source_exists:
                    status, candidate = "source_missing", "right_only"
                elif not target_exists:
                    status, candidate = "target_missing", "left_only"
                elif source_hash == target_hash:
                    status, candidate = "identical", "identical"
                else:
                    status, candidate = "queued", "modified"
                self.run_store.update_candidate(
                    str(item.item_id), status=status, candidate_status=candidate,
                    source_exists=source_exists, target_exists=target_exists,
                    source_sha256=source_hash, target_sha256=target_hash, error=error,
                )
            self.run_store.finish_preparation(run_id)
        except Exception as exc:
            logger.exception("M4 运行准备失败 run_id=%s", run_id)
            code = exc.code if isinstance(exc, (DiffPlanError, SVNProviderError)) else "DIFF_PLAN_PREPARATION_FAILED"
            message = exc.message if isinstance(exc, (DiffPlanError, SVNProviderError)) else "计划运行准备失败"
            self.run_store.finish_preparation(run_id, {"code": code, "message": message, "retryable": True})

    def _execute(self, claim: dict[str, Any]) -> None:
        item_id = claim["item_id"]
        lease_token = claim["lease_token"]
        result_path = None
        try:
            revisions = json.loads(claim["target_revisions_json"])
            source = BatchEndpointPayload(
                endpoint_id=claim["source_endpoint_id"],
                revision=claim["source_revision"],
            )
            target = BatchEndpointPayload(
                endpoint_id=claim["target_endpoint_id"],
                revision=revisions[claim["target_endpoint_id"]],
            )
            content = self.workbook_runner.run(
                source, target, claim["workbook_path"]
            )
            parsed = DiffResultPayload.model_validate_json(content)
            result = self.run_store.write_result(claim["run_id"], item_id, content)
            result_path = result["result_path"]
            if parsed.workbook.status == "unchanged":
                status = "semantic_equal"
            elif parsed.workbook.status == "modified":
                status = "changed"
            else:
                status = "business_failed"
            committed = self.run_store.complete_item(
                item_id,
                lease_token=lease_token,
                status=status,
                diff_status=parsed.workbook.status,
                diff_error_count=parsed.summary.error_count,
                result=result,
                error=None if status != "business_failed" else {
                    "code": "M2_DIFF_" + parsed.workbook.status.upper(),
                    "message": "工作簿包含业务解析错误，详细原因已保留",
                    "retryable": True,
                },
            )
            if not committed:
                self.run_store.remove_result(result_path)
        except WorkbookCompareError as exc:
            self.run_store.complete_item(
                item_id,
                lease_token=lease_token,
                status="read_failed",
                error={
                    "code": exc.code,
                    "message": exc.message,
                    "retryable": True,
                },
            )
        except Exception:
            logger.exception("M4 单工作簿执行失败 item_id=%s", item_id)
            self.run_store.remove_result(result_path)
            self.run_store.complete_item(
                item_id,
                lease_token=lease_token,
                status="orchestration_failed",
                error={
                    "code": "DIFF_PLAN_ITEM_UNEXPECTED",
                    "message": "单工作簿运行编排失败",
                    "retryable": True,
                },
            )
    def _harvest(self) -> None:
        for future, run_id in list(self._preparations.items()):
            if future.done():
                self._preparations.pop(future, None)
                try:
                    future.result()
                except Exception:
                    logger.exception("M4 准备 Worker 异常 run_id=%s", run_id)
        now = time.monotonic()
        for future, running in list(self._items.items()):
            if future.done():
                self._items.pop(future, None)
                try:
                    future.result()
                except Exception:
                    logger.exception(
                        "M4 执行 Worker 异常 item_id=%s", running.item_id
                    )
                continue
            if now - running.last_heartbeat >= self.heartbeat_seconds:
                self.run_store.renew_lease(
                    running.item_id, running.lease_token
                )
                if running.execution_lease is not None:
                    running.execution_lease.renew()
                running.last_heartbeat = now

    def _claim_scheduled_item(
        self,
    ) -> tuple[dict[str, Any] | None, WorkbookExecutionLease | None]:
        if self.execution_scheduler is None:
            return self.run_store.claim_item(), None
        run_ids = self.run_store.runnable_run_ids()
        self.execution_scheduler.sync_demands(
            "m4", [f"m4:{run_id}" for run_id in run_ids]
        )
        for run_id in run_ids:
            lease = self.execution_scheduler.try_acquire(f"m4:{run_id}")
            if lease is None:
                continue
            claim = self.run_store.claim_item(run_id)
            if claim is not None:
                return claim, lease
            lease.release()
        return None, None

    def _execute_in_slot(
        self,
        claim: dict[str, Any],
        execution_lease: WorkbookExecutionLease | None,
    ) -> None:
        try:
            self._execute(claim)
        finally:
            if execution_lease is not None:
                execution_lease.release()
    def _cleanup_expired_results(self) -> None:
        self._next_cleanup_at = time.monotonic() + self.cleanup_interval_seconds
        try:
            cleaned = self.run_store.cleanup_expired_results()
            if cleaned["expired_result_count"]:
                logger.info(
                    "M4 到期明细清理完成 files=%s bytes=%s",
                    cleaned["removed_file_count"],
                    cleaned["removed_size_bytes"],
                    extra={"event": "m4.result_cleanup"},
                )
        except Exception:
            logger.error(
                "M4 到期明细清理失败，运行调度继续",
                extra={"event": "m4.result_cleanup"},
            )

    def _loop(self) -> None:
        while not self._stop.is_set():
            try:
                self._harvest()
                if time.monotonic() >= self._next_cleanup_at:
                    self._cleanup_expired_results()
                if not self._preparations:
                    raw = self.run_store.claim_preparation()
                    if raw:
                        future = self._preparation_executor.submit(self._prepare, raw)
                        self._preparations[future] = raw["run_id"]
                self.run_store.recover_expired_leases()
                while len(self._items) < self.item_concurrency:
                    claim, execution_lease = self._claim_scheduled_item()
                    if not claim:
                        break
                    future = self._item_executor.submit(
                        self._execute_in_slot,
                        claim,
                        execution_lease,
                    )
                    self._items[future] = _RunningPlanItem(
                        item_id=claim["item_id"],
                        lease_token=claim["lease_token"],
                        execution_lease=execution_lease,
                        last_heartbeat=time.monotonic(),
                    )
            except Exception:
                logger.exception("M4 调度循环异常")
            self._stop.wait(self.poll_interval_seconds)
