"""Phase 5 API orchestration for M3 monitoring."""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
import hashlib
import json
from pathlib import Path
from threading import Event, Lock, Thread
from typing import Any, Callable, Protocol
from uuid import UUID, uuid5

from app.monitor_runner import MonitorRunnerService
from app.schemas.monitor import (
    MonitorCommandRequestPayload,
    MonitorEndpointOptionPayload,
    MonitorEndpointOptionsPayload,
    MonitorRunListPayload,
    MonitorRunRetryRequestPayload,
    MonitorRetryAcceptedPayload,
    MonitorTaskCreateRequestPayload,
    MonitorTaskListItemPayload,
    MonitorTaskListPayload,
    MonitorTaskPatchRequestPayload,
    MonitorTaskPayload,
)
from app.services.branch_history_service import BranchHistoryService
from app.services.monitor_api_contract import (
    MonitorCursorError,
    canonical_json,
    decode_cursor,
    encode_cursor,
)
from app.services.monitor_report_artifacts import FileSystemMonitorReportPublisher
from app.services.monitor_report_service import (
    MonitorReportReferenceError,
    render_legacy_compatible_report_html,
)
from app.services.monitor_store import (
    MonitorIdempotencyConflict,
    MonitorStateConflict,
    MonitorStore,
)
from app.services.monitor_task_service import CreateMonitorTask, MonitorTaskService
from app.services.windows_scheduler import (
    MonitorSchedulerService,
    ScheduledMonitorTaskService,
)
from app.services.workbook_diff_service import DatasetLayout
from core.models import EndpointSpec
from core.svn_provider import SVNProviderError


COMMAND_NAMESPACE = UUID("9d286897-6779-44be-aad5-8d7b51de541e")


class MonitorWebError(RuntimeError):
    def __init__(self, code: str, message: str, status_code: int):
        super().__init__(message)
        self.code = code
        self.message = message
        self.status_code = status_code


class EndpointRegistry(Protocol):
    def __call__(self) -> list[dict[str, Any]]: ...


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _instant(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat(timespec="microseconds").replace(
        "+00:00", "Z"
    )


class MonitorWebService:
    def __init__(
        self,
        *,
        store: MonitorStore,
        tasks: MonitorTaskService,
        scheduled_tasks: ScheduledMonitorTaskService,
        scheduler: MonitorSchedulerService,
        history: BranchHistoryService,
        endpoint_registry: EndpointRegistry,
        dataset_layout: dict[str, Any] | None,
        runner: MonitorRunnerService,
        publisher: FileSystemMonitorReportPublisher,
        clock: Callable[[], datetime] | None = None,
    ):
        self.store = store
        self.tasks = tasks
        self.scheduled_tasks = scheduled_tasks
        self.scheduler = scheduler
        self.history = history
        self.endpoint_registry = endpoint_registry
        self.dataset_layout = dataset_layout
        self.runner = runner
        self.publisher = publisher
        self.clock = clock or _utc_now
        self._executor = ThreadPoolExecutor(max_workers=2, thread_name_prefix="m3-web-retry")
        self._dispatcher_stop = Event()
        self._dispatcher_wakeup = Event()
        self._dispatcher_lock = Lock()
        self._dispatcher_thread: Thread | None = None
        self._closed = False

    def close(self) -> None:
        with self._dispatcher_lock:
            if self._closed:
                return
            self._closed = True
            thread = self._dispatcher_thread
            self._dispatcher_stop.set()
            self._dispatcher_wakeup.set()
        if thread is not None:
            thread.join()
        self._executor.shutdown(wait=True, cancel_futures=False)

    def start_retry_dispatcher(self) -> None:
        with self._dispatcher_lock:
            if self._closed:
                raise RuntimeError("monitor web service is closed")
            if self._dispatcher_thread is not None:
                return
            self._dispatcher_stop.clear()
            thread = Thread(
                target=self._retry_dispatch_loop,
                name="m3-web-retry-dispatcher",
                daemon=True,
            )
            self._dispatcher_thread = thread
            thread.start()

    def wake_retry_dispatcher(self) -> None:
        self._dispatcher_wakeup.set()

    def _retry_dispatch_loop(self) -> None:
        while not self._dispatcher_stop.is_set():
            self._dispatcher_wakeup.clear()
            try:
                self.dispatch_retry_intents()
                now = self._now()
                wakeup_at = self.store.next_retry_intent_wakeup(now=now)
            except Exception:
                self._dispatcher_wakeup.wait(1.0)
                continue
            timeout = (
                max(0.0, (wakeup_at - now).total_seconds())
                if wakeup_at is not None
                else None
            )
            self._dispatcher_wakeup.wait(timeout)

    def _now(self) -> datetime:
        return self.clock().astimezone(timezone.utc)

    @staticmethod
    def _payload_hash(payload: Any) -> str:
        data = payload.model_dump(mode="json", exclude={"request_id"})
        return hashlib.sha256(canonical_json(data)).hexdigest()

    @staticmethod
    def _payload_json(payload: Any) -> str:
        return canonical_json(payload.model_dump(mode="json")).decode("utf-8")

    @staticmethod
    def _command_target(method: str, path: str) -> str:
        return method.upper() + " " + path

    def _run_command(
        self,
        *,
        request_id: UUID,
        method: str,
        target: str,
        payload_hash: str,
        payload_json: str,
        response_status: int,
        action: Callable[[], MonitorTaskPayload],
    ) -> tuple[MonitorTaskPayload, int]:
        try:
            command = self.store.claim_command(
                request_id=str(request_id),
                method=method,
                target=target,
                payload_hash=payload_hash,
                payload_json=payload_json,
                now=self._now(),
            )
        except MonitorIdempotencyConflict as error:
            raise MonitorWebError(
                "MONITOR_IDEMPOTENCY_CONFLICT",
                "request_id 已用于其他版本监控请求",
                409,
            ) from error
        except MonitorStateConflict as error:
            raise MonitorWebError(
                "MONITOR_STATE_CONFLICT", "该资源仍有未完成操作", 409
            ) from error
        if command.state == "completed":
            if int(command.response_status) >= 400:
                envelope = json.loads(command.response_json)
                error = envelope["error"]
                raise MonitorWebError(
                    error["code"], error["message"], int(command.response_status)
                )
            return (
                MonitorTaskPayload.model_validate_json(command.response_json),
                int(command.response_status),
            )
        try:
            result = action()
        except MonitorWebError as error:
            self._complete_error(request_id, error)
            raise
        except (KeyError, ValueError, MonitorStateConflict) as error:
            if isinstance(error, KeyError):
                public = MonitorWebError(
                    "MONITOR_TASK_NOT_FOUND", "监控任务不存在", 404
                )
            else:
                public = MonitorWebError(
                    "MONITOR_STATE_CONFLICT", "当前任务状态不允许此操作", 409
                )
            self._complete_error(request_id, public)
            raise public from error
        serialized = json.dumps(
            result.model_dump(mode="json"),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        self.store.complete_command(
            str(request_id),
            response_status=response_status,
            response_json=serialized,
            now=self._now(),
        )
        return result, response_status

    def _complete_error(self, request_id: UUID, error: MonitorWebError) -> None:
        response_json = json.dumps(
            {"error": {"code": error.code, "message": error.message}},
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        self.store.complete_command(
            str(request_id),
            response_status=error.status_code,
            response_json=response_json,
            now=self._now(),
        )

    def endpoint_options(self) -> MonitorEndpointOptionsPayload:
        items = [
            MonitorEndpointOptionPayload(
                endpoint_id=str(item.get("id", "")),
                label=str(item.get("label", "")),
            )
            for item in self.endpoint_registry()
            if bool(item.get("enabled", True))
        ]
        items.sort(key=lambda item: (item.label.casefold(), item.endpoint_id.casefold()))
        return MonitorEndpointOptionsPayload(items=items)

    def _endpoint(self, endpoint_id: str) -> dict[str, Any]:
        for endpoint in self.endpoint_registry():
            if str(endpoint.get("id")) != endpoint_id:
                continue
            if not bool(endpoint.get("enabled", True)):
                raise MonitorWebError(
                    "MONITOR_ENDPOINT_DISABLED", "所选 SVN 端点已停用", 409
                )
            return endpoint
        raise MonitorWebError("MONITOR_ENDPOINT_NOT_FOUND", "所选 SVN 端点不存在", 404)

    def _validate_layout(self) -> None:
        try:
            if not isinstance(self.dataset_layout, dict):
                raise ValueError
            DatasetLayout.from_config(self.dataset_layout)
            workbook = dict(self.dataset_layout["workbook_source"])
            csv_export = dict(self.dataset_layout["csv_export"])
            if not str(workbook["directory_name"]).strip():
                raise ValueError
            if not str(csv_export["directory_name"]).strip():
                raise ValueError
        except (KeyError, TypeError, ValueError) as error:
            raise MonitorWebError(
                "MONITOR_DATASET_CONFIGURATION_INVALID",
                "版本监控数据布局配置无效",
                409,
            ) from error

    def create_task(
        self, payload: MonitorTaskCreateRequestPayload
    ) -> tuple[MonitorTaskPayload, int]:
        task_id = str(uuid5(COMMAND_NAMESPACE, str(payload.request_id)))
        target = self._command_target("POST", "/api/monitor/tasks")

        def create_first() -> MonitorTaskPayload:
            endpoint = self._endpoint(payload.endpoint_id)
            self._validate_layout()
            spec = EndpointSpec(
                url=str(endpoint.get("url", "")),
                revision="HEAD",
                label=str(endpoint.get("label", payload.endpoint_id)),
            )
            try:
                identity = self.history.resolve_branch_identity(spec)
                copy_boundary = self.history.resolve_copy_boundary(identity)
                effective_revision = self.history.resolve_revision_at(
                    identity, payload.effective_at
                )
                UUID(identity.repository_uuid)
                if (
                    copy_boundary.revision <= 0
                    or effective_revision < copy_boundary.revision
                ):
                    raise ValueError("effective revision predates branch")
            except SVNProviderError as error:
                if error.code in {
                    "SVN_TIMEOUT",
                    "SVN_AUTH_FAILED",
                    "SVN_CLI_NOT_FOUND",
                    "SVN_PROVIDER_UNAVAILABLE",
                }:
                    raise MonitorWebError(
                        "MONITOR_SERVICE_UNAVAILABLE",
                        "SVN 只读服务暂时不可用",
                        503,
                    ) from error
                raise MonitorWebError(
                    "MONITOR_BRANCH_CONFIGURATION_INVALID",
                    "固定 SVN 分支配置无效",
                    409,
                ) from error
            except (TypeError, ValueError) as error:
                raise MonitorWebError(
                    "MONITOR_BRANCH_CONFIGURATION_INVALID",
                    "固定 SVN 分支配置无效",
                    409,
                ) from error
            command = CreateMonitorTask(
                task_id=task_id,
                name=payload.name,
                endpoint_id=payload.endpoint_id,
                branch_label=str(endpoint.get("label", payload.endpoint_id)),
                repository_uuid=identity.repository_uuid,
                canonical_url=identity.canonical_url,
                repository_relative_path=identity.repository_relative_path,
                bound_revision=identity.bound_revision,
                copy_boundary_revision=copy_boundary.revision,
                effective_at=payload.effective_at,
                end_at=payload.end_at,
                daily_trigger_time=payload.daily_trigger_time,
            )
            return self.scheduled_tasks.create(command)

        return self._run_command(
            request_id=payload.request_id,
            method="POST",
            target=target,
            payload_hash=self._payload_hash(payload),
            payload_json=self._payload_json(payload),
            response_status=201,
            action=create_first,
        )

    def patch_task(
        self, task_id: UUID, payload: MonitorTaskPatchRequestPayload
    ) -> tuple[MonitorTaskPayload, int]:
        target = self._command_target("PATCH", f"/api/monitor/tasks/{task_id}")

        def modify_when_ready() -> MonitorTaskPayload:
            current = self.tasks.to_public_task(
                self.tasks._require_task(str(task_id))
            )
            if current.status == "syncing":
                raise MonitorWebError(
                    "MONITOR_STATE_CONFLICT",
                    "任务正在同步系统调度，请稍后再试",
                    409,
                )
            return self.scheduled_tasks.modify_schedule(
                str(task_id),
                daily_trigger_time=payload.daily_trigger_time,
                end_at=payload.end_at,
            )

        return self._run_command(
            request_id=payload.request_id,
            method="PATCH",
            target=target,
            payload_hash=self._payload_hash(payload),
            payload_json=self._payload_json(payload),
            response_status=200,
            action=modify_when_ready,
        )

    def task_command(
        self, task_id: UUID, command_name: str, request_id: UUID
    ) -> tuple[MonitorTaskPayload, int]:
        actions = {
            "pause": self.scheduled_tasks.pause,
            "resume": self.scheduled_tasks.resume,
            "end": self.scheduled_tasks.end,
            "archive": self.scheduled_tasks.archive,
        }
        if command_name == "scheduler-sync":
            action = lambda value: self.scheduler_sync(value)
        else:
            action = actions[command_name]
        target = self._command_target(
            "POST", f"/api/monitor/tasks/{task_id}/{command_name}"
        )
        payload_hash = hashlib.sha256(b"{}").hexdigest()
        payload_json = canonical_json(
            {
                "schema_version": "m3.monitor-command.request.v1",
                "request_id": str(request_id),
            }
        ).decode("utf-8")
        return self._run_command(
            request_id=request_id,
            method="POST",
            target=target,
            payload_hash=payload_hash,
            payload_json=payload_json,
            response_status=200,
            action=lambda: action(str(task_id)),
        )

    def scheduler_sync(self, task_id: str) -> MonitorTaskPayload:
        task = self.store.get_task(task_id)
        if task is None:
            raise KeyError(task_id)
        validation = self.scheduler.inspect_task(task_id)
        if not validation.valid:
            current = self.store.get_task(task_id)
            self.scheduler.sync_task(
                task_id,
                expected_generation=current.generation,
                trigger_final=True,
            )
        return self.tasks.to_public_task(self.tasks._require_task(task_id))

    def get_task(self, task_id: UUID | str) -> MonitorTaskPayload:
        try:
            return self.tasks.to_public_task(self.tasks._require_task(str(task_id)))
        except KeyError as error:
            raise MonitorWebError("MONITOR_TASK_NOT_FOUND", "监控任务不存在", 404) from error

    @staticmethod
    def _list_item(task: MonitorTaskPayload) -> MonitorTaskListItemPayload:
        data = task.model_dump(mode="json")
        for field in ("schema_version", "paused_at", "ended_at", "archived_at"):
            data.pop(field, None)
        return MonitorTaskListItemPayload.model_validate(data)

    def list_tasks(
        self,
        *,
        limit: int,
        cursor: str | None,
        statuses: list[str] | None,
        query: str | None,
    ) -> MonitorTaskListPayload:
        normalized_statuses = sorted(set(statuses or []))
        filters = {"status": normalized_statuses, "q": (query or "").strip().casefold()}
        before_created_at = None
        before_task_id = None
        if cursor:
            try:
                created_at, before_task_id = decode_cursor(
                    cursor, scope="tasks", filters=filters, sort_size=2
                )
                before_created_at = datetime.fromisoformat(
                    created_at.replace("Z", "+00:00")
                )
            except MonitorCursorError as error:
                raise MonitorWebError(
                    "MONITOR_INVALID_CURSOR", "任务列表游标无效", 400
                ) from error
        records = self.store.list_task_page(
            limit=limit + 1,
            statuses=normalized_statuses,
            query=filters["q"],
            before_created_at=before_created_at,
            before_task_id=before_task_id,
        )
        has_more = len(records) > limit
        records = records[:limit]
        overviews = self.store.task_run_overviews(
            [record.task_id for record in records], now=self._now()
        )
        page = [
            self.tasks.to_public_task(
                record,
                overview=overviews[record.task_id],
            )
            for record in records
        ]
        next_cursor = None
        if has_more and page:
            last = page[-1]
            next_cursor = encode_cursor(
                scope="tasks",
                filters=filters,
                sort_values=[_instant(last.created_at), str(last.task_id)],
            )
        return MonitorTaskListPayload(
            items=[self._list_item(item) for item in page],
            next_cursor=next_cursor,
            has_more=has_more,
            as_of=self._now(),
        )

    def list_runs(
        self, task_id: UUID, *, limit: int, cursor: str | None
    ) -> MonitorRunListPayload:
        if self.store.get_task(str(task_id)) is None:
            raise MonitorWebError("MONITOR_TASK_NOT_FOUND", "监控任务不存在", 404)
        filters: dict[str, Any] = {}
        scope = f"runs:{task_id}"
        before_end_at = None
        before_run_id = None
        if cursor:
            try:
                cutoff, before_run_id = decode_cursor(
                    cursor, scope=scope, filters=filters, sort_size=2
                )
                before_end_at = datetime.fromisoformat(
                    cutoff.replace("Z", "+00:00")
                )
            except MonitorCursorError as error:
                raise MonitorWebError(
                    "MONITOR_INVALID_CURSOR", "运行列表游标无效", 400
                ) from error
        records = self.store.list_run_page(
            str(task_id),
            limit=limit + 1,
            before_end_at=before_end_at,
            before_run_id=before_run_id,
        )
        has_more = len(records) > limit
        records = records[:limit]
        attempts = self.store.attempts_for_runs(
            [record.run_id for record in records]
        )
        page = [
            self.tasks.public_run_record(record, attempts[record.run_id])
            for record in records
        ]
        next_cursor = None
        if has_more and page:
            last = page[-1]
            next_cursor = encode_cursor(
                scope=scope,
                filters=filters,
                sort_values=[
                    _instant(last.interval.logical_cutoff_at),
                    str(last.run_id),
                ],
            )
        return MonitorRunListPayload(
            items=page,
            next_cursor=next_cursor,
            has_more=has_more,
            as_of=self._now(),
        )

    def accept_retry(
        self, run_id: UUID, request_id: UUID
    ) -> tuple[MonitorRetryAcceptedPayload, int]:
        def accepted_response_json(task_id: str) -> str:
            accepted = MonitorRetryAcceptedPayload(
                request_id=request_id,
                task_id=UUID(task_id),
                run_id=run_id,
            )
            return json.dumps(
                accepted.model_dump(mode="json"),
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            )

        not_found_response_json = json.dumps(
            {
                "error": {
                    "code": "MONITOR_RUN_NOT_FOUND",
                    "message": "监控运行不存在",
                }
            },
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        conflict_response_json = json.dumps(
            {
                "error": {
                    "code": "MONITOR_STATE_CONFLICT",
                    "message": "当前运行状态不允许人工重试",
                }
            },
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        target = self._command_target("POST", f"/api/monitor/runs/{run_id}/retry")
        payload_json = canonical_json(
            {
                "schema_version": "m3.monitor-run-retry.request.v1",
                "request_id": str(request_id),
            }
        ).decode("utf-8")
        try:
            command = self.store.accept_retry_intent(
                request_id=str(request_id),
                run_id=str(run_id),
                method="POST",
                target=target,
                payload_hash=hashlib.sha256(b"{}").hexdigest(),
                payload_json=payload_json,
                accepted_response_json=accepted_response_json,
                not_found_response_json=not_found_response_json,
                conflict_response_json=conflict_response_json,
                now=self._now(),
            )
        except MonitorIdempotencyConflict as error:
            raise MonitorWebError(
                "MONITOR_IDEMPOTENCY_CONFLICT",
                "request_id 已用于其他版本监控请求",
                409,
            ) from error
        if int(command.response_status) >= 400:
            envelope = json.loads(command.response_json)
            error = envelope["error"]
            raise MonitorWebError(
                error["code"], error["message"], int(command.response_status)
            )
        self.wake_retry_dispatcher()
        return MonitorRetryAcceptedPayload.model_validate_json(command.response_json), 202

    def dispatch_retry_intents(self) -> int:
        intents = self.store.claim_retry_intents(
            now=self._now(), lease_for=timedelta(minutes=5)
        )
        for intent in intents:
            future = self._executor.submit(self._dispatch_retry, intent)
            future.add_done_callback(lambda _: self.wake_retry_dispatcher())
        return len(intents)

    def recover_pending_commands(self) -> int:
        pending = self.store.list_pending_commands()
        for command in pending:
            try:
                data = json.loads(command.payload_json)
                if command.target == "POST /api/monitor/tasks":
                    self.create_task(MonitorTaskCreateRequestPayload.model_validate(data))
                    continue
                if command.target.startswith("PATCH /api/monitor/tasks/"):
                    task_id = UUID(command.target.removeprefix("PATCH /api/monitor/tasks/"))
                    self.patch_task(
                        task_id,
                        MonitorTaskPatchRequestPayload.model_validate(data),
                    )
                    continue
                retry_prefix = "POST /api/monitor/runs/"
                if (
                    command.target.startswith(retry_prefix)
                    and command.target.endswith("/retry")
                ):
                    run_id_text = command.target[
                        len(retry_prefix) : -len("/retry")
                    ]
                    request = MonitorRunRetryRequestPayload.model_validate(data)
                    self.accept_retry(UUID(run_id_text), request.request_id)
                    continue
                prefix = "POST /api/monitor/tasks/"
                if command.target.startswith(prefix):
                    suffix = command.target.removeprefix(prefix)
                    task_id_text, command_name = suffix.rsplit("/", 1)
                    if command_name in {
                        "pause",
                        "resume",
                        "end",
                        "archive",
                        "scheduler-sync",
                    }:
                        request = MonitorCommandRequestPayload.model_validate(data)
                        self.task_command(
                            UUID(task_id_text), command_name, request.request_id
                        )
            except Exception:
                continue
        remaining = {
            command.request_id for command in self.store.list_pending_commands()
        }
        return sum(command.request_id not in remaining for command in pending)

    def _dispatch_retry(self, intent) -> None:
        try:
            self.runner.run_run(intent.run_id, trigger="manual_retry")
        except Exception:
            return
        self.store.finish_retry_intent(
            intent.request_id, intent.lease_token, now=self._now()
        )

    def _report_records(self, run_id: str):
        run = self.store.get_run(run_id)
        if run is None:
            raise MonitorWebError("MONITOR_RUN_NOT_FOUND", "监控运行不存在", 404)
        publication = self.store.get_publication(run_id)
        if (
            publication is None
            or publication.state != "activated"
            or run.report_ref is None
            or run.report_ref != publication.report_ref
            or run.report_sha256 != publication.json_sha256
            or run.report_expires_at != publication.report_expires_at
            or run.task_id != publication.task_id
            or run.status != publication.status
            or run.start_revision != publication.start_revision
            or run.end_revision != publication.end_revision
            or run.summary != publication.summary
            or run.errors != publication.errors
        ):
            raise MonitorWebError("MONITOR_REPORT_NOT_FOUND", "监控报告不存在", 404)
        return run, publication

    def _load_report(self, run_id: str, *, allow_expired: bool) -> tuple[bytes, str]:
        run, publication = self._report_records(run_id)
        if not allow_expired and self.publisher.is_expired(
            publication.report_expires_at, now=self._now()
        ):
            raise MonitorWebError("MONITOR_REPORT_EXPIRED", "监控报告已过期", 410)
        try:
            resolved = self.publisher.resolve(
                task_id=run.task_id,
                run_id=run.run_id,
                logical_cutoff_at=run.end_at,
                reference=publication.report_ref,
                expected_json_sha256=publication.json_sha256,
                expected_html_sha256=publication.html_sha256,
            )
        except MonitorReportReferenceError as error:
            raise MonitorWebError(
                "MONITOR_REPORT_NOT_FOUND", "监控报告不可用", 404
            ) from error
        content = render_legacy_compatible_report_html(resolved.offline_html)
        return content, hashlib.sha256(content).hexdigest()

    def load_run_report(self, run_id: UUID) -> tuple[bytes, str]:
        return self._load_report(str(run_id), allow_expired=False)

    def load_latest_report(self, task_id: UUID) -> tuple[bytes, str]:
        task = self.get_task(task_id)
        if task.latest_report is None:
            raise MonitorWebError("MONITOR_REPORT_NOT_FOUND", "监控报告不存在", 404)
        run, publication = self._report_records(str(task.latest_report.run_id))
        if run.task_id != str(task_id):
            raise MonitorWebError("MONITOR_REPORT_NOT_FOUND", "监控报告不存在", 404)
        try:
            content, _ = self.publisher.resolve_latest_html(
                task_id=run.task_id,
                run_id=run.run_id,
                logical_cutoff_at=run.end_at,
                reference=publication.report_ref,
                expected_html_sha256=publication.html_sha256,
            )
            content = render_legacy_compatible_report_html(content)
            return content, hashlib.sha256(content).hexdigest()
        except MonitorReportReferenceError as error:
            raise MonitorWebError(
                "MONITOR_REPORT_NOT_FOUND", "监控报告不可用", 404
            ) from error
