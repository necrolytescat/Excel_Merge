"""Independent M3 monitor runner. This module has no FastAPI dependency."""
from __future__ import annotations

import argparse
from dataclasses import dataclass
from datetime import datetime, time, timedelta, timezone
import json
import os
from pathlib import Path, PurePosixPath
import sys
import threading
from typing import Callable, Literal, Protocol
from uuid import UUID

from app.schemas.monitor import (
    MonitorErrorCode,
    MonitorErrorStage,
    MonitorPublicErrorPayload,
    MonitorTimeIntervalPayload,
)
from app.services.branch_history_service import BranchHistoryService
from app.services.config_service import ConfigStore
from app.services.monitor_attribution_service import MonitorAttributionService
from app.services.monitor_diff_service import MonitorDiffService, SvnMonitorSnapshotReader
from app.services.monitor_report_service import (
    MonitorReportPublisher,
    MonitorReportPublishError,
    ReportDraft,
    publication_from_draft,
)
from app.services.monitor_report_artifacts import FileSystemMonitorReportPublisher
from app.services.monitor_store import (
    DEFAULT_DATABASE_PATH,
    MonitorLeaseLost,
    MonitorStateConflict,
    MonitorStore,
    RunRecord,
    TaskRecord,
)
from app.services.monitor_schedule import (
    BoundarySpec,
    MonitorScheduleError,
    next_scheduled_cutoff,
    require_utc,
    scheduled_boundaries,
)
from app.services.monitor_task_service import MonitorTaskService
from app.services.windows_scheduler import (
    MonitorSchedulerService,
    SchedulerGateway,
    WindowsSchedulerGateway,
)
from app.services.workbook_diff_service import DatasetLayout
from core.models import EndpointSpec
from core.svn_history import BranchIdentity
from core.svn_provider import SVNProviderError, provider_from_config


PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_CONFIG_PATH = PROJECT_ROOT / "config" / "settings.json"
LEASE_DURATION = timedelta(minutes=5)
MAX_AUTOMATIC_RETRIES = 3


@dataclass(frozen=True)
class EngineResult:
    draft: ReportDraft
    publisher: MonitorReportPublisher


class MonitorRunComputationFailed(RuntimeError):
    """The requested interval has no reliable reportable coverage."""

    def __init__(self, errors: tuple[MonitorPublicErrorPayload, ...]):
        if not errors:
            raise ValueError("failed monitor computation requires public errors")
        super().__init__("monitor interval has no reliable coverage")
        self.errors = errors


class MonitorRunnerConfigurationError(RuntimeError):
    """A deterministic local configuration failure with no public details."""


class ConfigurationFailureEngine:
    def __init__(self, errors: tuple[MonitorPublicErrorPayload, ...]):
        self.errors = errors

    def execute(
        self, run: RunRecord, task: TaskRecord, generated_at: datetime
    ) -> EngineResult:
        raise MonitorRunComputationFailed(self.errors)


class MonitorRunEngine(Protocol):
    def execute(self, run: RunRecord, task: TaskRecord, generated_at: datetime) -> EngineResult: ...


class MonitorRunEngineFactory(Protocol):
    def __call__(self, task: TaskRecord) -> MonitorRunEngine: ...


class P1MonitorRunEngine:
    def __init__(
        self,
        *,
        history: BranchHistoryService,
        endpoint: EndpointSpec,
        identity: BranchIdentity,
        diff_service: MonitorDiffService,
        attribution_service: MonitorAttributionService,
        publisher: MonitorReportPublisher,
        task_service: MonitorTaskService,
    ):
        self.history = history
        self.endpoint = endpoint
        self.identity = identity
        self.diff_service = diff_service
        self.attribution_service = attribution_service
        self.publisher = publisher
        self.task_service = task_service

    def execute(self, run: RunRecord, task: TaskRecord, generated_at: datetime) -> EngineResult:
        self.history.verify_branch_identity(self.endpoint, self.identity)
        start_revision = self.history.resolve_revision_at(self.identity, run.start_at)
        end_revision = self.history.resolve_revision_at(self.identity, run.end_at)
        net = self.diff_service.compare_revisions(start_revision, end_revision)
        commits = self.history.list_branch_commits(self.identity, run.start_at, run.end_at)
        attributed = self.attribution_service.attribute(
            net, start_revision=start_revision, commits=commits
        )
        if attributed.errors and attributed.reliable_workbook_count == 0:
            raise MonitorRunComputationFailed(attributed.errors)
        task_public = self.task_service.to_public_task(task)
        draft = self.publisher.render(
            run_id=run.run_id,
            task=task_public,
            interval=MonitorTimeIntervalPayload(
                start_at=run.start_at,
                end_at=run.end_at,
                logical_cutoff_at=run.end_at,
                boundary_kind=run.boundary_type.value,
            ),
            start_revision=start_revision,
            end_revision=end_revision,
            workbook_count=attributed.workbook_count,
            changes=attributed.changes,
            errors=attributed.errors,
            generated_at=generated_at,
        )
        return EngineResult(draft, self.publisher)


class P1MonitorRunEngineFactory:
    def __init__(
        self,
        *,
        history: BranchHistoryService,
        layout: DatasetLayout,
        table_directory_name: str,
        csv_directory_name: str,
        publisher: MonitorReportPublisher,
        task_service: MonitorTaskService,
    ):
        self.history = history
        self.layout = layout
        self.table_directory_name = table_directory_name
        self.csv_directory_name = csv_directory_name
        self.publisher = publisher
        self.task_service = task_service

    def _table_directory(self, identity: BranchIdentity, revision: int) -> str:
        candidates: set[str] = set()
        for entry in self.history.list_paths_at_revision(identity, revision):
            path = PurePosixPath(entry.path)
            for parent in path.parents:
                value = parent.as_posix()
                if value != "." and parent.name.casefold() == self.table_directory_name.casefold():
                    candidates.add(value)
        if not candidates:
            raise SVNProviderError("SVN_SCOPE_NOT_FOUND", "固定分支未找到 Table 目录")
        return sorted(candidates, key=lambda value: (value.count("/"), value.casefold()))[0]

    def __call__(self, task: TaskRecord) -> P1MonitorRunEngine:
        identity = BranchIdentity(
            canonical_url=task.canonical_url,
            repository_root="",
            repository_uuid=task.repository_uuid,
            repository_relative_path=task.repository_relative_path,
            bound_revision=task.bound_revision,
        )
        endpoint = EndpointSpec(
            url=task.canonical_url,
            revision="HEAD",
            label=task.branch_label,
        )
        actual_identity = self.history.verify_branch_identity(endpoint, identity)
        table_directory = self._table_directory(actual_identity, task.bound_revision)
        reader = SvnMonitorSnapshotReader(
            self.history,
            actual_identity,
            self.layout,
            table_directory=table_directory,
            csv_directory_name=self.csv_directory_name,
        )
        diff = MonitorDiffService(reader)
        return P1MonitorRunEngine(
            history=self.history,
            endpoint=endpoint,
            identity=identity,
            diff_service=diff,
            attribution_service=MonitorAttributionService(diff),
            publisher=self.publisher,
            task_service=self.task_service,
        )


@dataclass(frozen=True)
class RunnerResult:
    processed: int
    succeeded: int
    failed: int
    retryable_failures: int

    @property
    def exit_category(self) -> Literal["ok", "temporary_failure", "permanent_failure", "noop"]:
        if self.processed == 0 and self.retryable_failures:
            return "temporary_failure"
        if self.processed == 0:
            return "noop"
        if self.failed == 0:
            return "ok"
        return "temporary_failure" if self.retryable_failures else "permanent_failure"


@dataclass(frozen=True)
class MaintenanceResult:
    task_count: int
    cleaned_artifact_count: int
    failed_task_count: int

    @property
    def exit_category(self) -> Literal["ok", "temporary_failure"]:
        return "temporary_failure" if self.failed_task_count else "ok"


def _public_error(error: Exception) -> MonitorPublicErrorPayload:
    if isinstance(error, MonitorRunnerConfigurationError):
        return MonitorPublicErrorPayload(
            code=MonitorErrorCode.CONFIGURATION_INVALID,
            stage=MonitorErrorStage.SNAPSHOT,
            message="监控运行配置无效",
            retryable=False,
        )
    if isinstance(error, MonitorScheduleError):
        return MonitorPublicErrorPayload(
            code=MonitorErrorCode.CONFIGURATION_INVALID,
            stage=MonitorErrorStage.SNAPSHOT,
            message="监控时间边界配置无效",
            retryable=False,
        )
    if isinstance(error, MonitorReportPublishError):
        retryable = error.retryable
        return MonitorPublicErrorPayload(
            code=MonitorErrorCode.REPORT_PUBLISH_FAILED,
            stage=MonitorErrorStage.REPORT_PUBLISH,
            message=(
                "报告文件暂时无法发布，运行可重试"
                if retryable
                else "报告产物存在确定性冲突，运行已停止"
            ),
            retryable=retryable,
        )
    if isinstance(error, SVNProviderError):
        code = str(error.code).upper()
        if "TIMEOUT" in code or "NETWORK" in code or "NOT_REACHABLE" in code:
            return MonitorPublicErrorPayload(
                code=MonitorErrorCode.SVN_TIMEOUT,
                stage=MonitorErrorStage.HISTORY,
                message="SVN 暂时不可用，运行可重试",
                retryable=True,
            )
        if "AUTH" in code:
            return MonitorPublicErrorPayload(
                code=MonitorErrorCode.SVN_AUTH_FAILED,
                stage=MonitorErrorStage.BRANCH_IDENTITY,
                message="SVN 身份验证失败",
                retryable=False,
            )
        if "BINDING" in code:
            return MonitorPublicErrorPayload(
                code=MonitorErrorCode.BRANCH_BINDING_INVALID,
                stage=MonitorErrorStage.BRANCH_IDENTITY,
                message="固定 SVN 分支绑定已失效",
                retryable=False,
            )
        if code == "SVN_BRANCH_NOT_FOUND":
            return MonitorPublicErrorPayload(
                code=MonitorErrorCode.BRANCH_BINDING_INVALID,
                stage=MonitorErrorStage.BRANCH_IDENTITY,
                message="固定 SVN 分支不可用",
                retryable=False,
            )
        if code == "SVN_BRANCH_NOT_FOUND_AT_BOUNDARY":
            return MonitorPublicErrorPayload(
                code=MonitorErrorCode.BRANCH_BINDING_INVALID,
                stage=MonitorErrorStage.HISTORY,
                message="报告时间边界不在固定分支有效历史内",
                retryable=False,
            )
        if code == "SVN_HISTORY_INVALID":
            return MonitorPublicErrorPayload(
                code=MonitorErrorCode.CONFIGURATION_INVALID,
                stage=MonitorErrorStage.HISTORY,
                message="固定 SVN 分支历史响应无效",
                retryable=False,
            )
        if code in {"SVN_CLI_NOT_FOUND", "SVN_NOT_FOUND"}:
            return MonitorPublicErrorPayload(
                code=MonitorErrorCode.CONFIGURATION_INVALID,
                stage=MonitorErrorStage.BRANCH_IDENTITY,
                message="SVN 只读客户端或端点配置无效",
                retryable=False,
            )
        if code in {"SVN_DECODE_ERROR", "SVN_INVALID_REVISION"}:
            return MonitorPublicErrorPayload(
                code=MonitorErrorCode.CONFIGURATION_INVALID,
                stage=MonitorErrorStage.HISTORY,
                message="固定 SVN 分支历史数据无效",
                retryable=False,
            )
        if code == "SVN_PATH_NOT_FOUND":
            return MonitorPublicErrorPayload(
                code=MonitorErrorCode.PARSE_FAILED,
                stage=MonitorErrorStage.SNAPSHOT,
                message="固定 Revision 快照路径不可用",
                retryable=False,
            )
        if "CONFIG" in code or "SCOPE" in code:
            return MonitorPublicErrorPayload(
                code=MonitorErrorCode.CONFIGURATION_INVALID,
                stage=MonitorErrorStage.SNAPSHOT,
                message="监控数据布局配置无效",
                retryable=False,
            )
    return MonitorPublicErrorPayload(
        code=MonitorErrorCode.INTERNAL_ERROR,
        stage=MonitorErrorStage.REPORT_PUBLISH,
        message="监控运行发生内部错误",
        retryable=False,
    )


class _LeaseGuard:
    """Renew a lease during blocking SVN/file work and expose loss as a CAS failure."""

    def __init__(
        self,
        store: MonitorStore,
        run_id: str,
        lease_token: str,
        *,
        clock: Callable[[], datetime],
        lease_duration: timedelta,
    ):
        self.store = store
        self.run_id = run_id
        self.lease_token = lease_token
        self.clock = clock
        self.lease_duration = lease_duration
        self._stop = threading.Event()
        self._lost = threading.Event()
        self._thread = threading.Thread(
            target=self._keepalive,
            name=f"m3-lease-{run_id[:8]}",
            daemon=True,
        )

    def __enter__(self) -> "_LeaseGuard":
        self._thread.start()
        return self

    def __exit__(self, exc_type, exc, traceback) -> None:
        self._stop.set()
        self._thread.join(timeout=2)

    def _keepalive(self) -> None:
        interval = max(1.0, self.lease_duration.total_seconds() / 3)
        while not self._stop.wait(interval):
            try:
                renewed = self.store.renew_lease(
                    self.run_id,
                    self.lease_token,
                    now=require_utc(self.clock()),
                    lease_for=self.lease_duration,
                )
            except Exception:
                renewed = False
            if not renewed:
                self._lost.set()
                return

    def ensure_owned(self) -> None:
        if self._lost.is_set():
            raise MonitorLeaseLost("monitor run lease was lost")
        if not self.store.renew_lease(
            self.run_id,
            self.lease_token,
            now=require_utc(self.clock()),
            lease_for=self.lease_duration,
        ):
            self._lost.set()
            raise MonitorLeaseLost("monitor run lease was lost")


class MonitorRunnerService:
    def __init__(
        self,
        store: MonitorStore,
        task_service: MonitorTaskService,
        engine_factory: MonitorRunEngineFactory,
        *,
        clock: Callable[[], datetime] | None = None,
        lease_duration: timedelta = LEASE_DURATION,
        report_maintenance: Callable[[str, datetime], None] | None = None,
    ):
        self.store = store
        self.task_service = task_service
        self.engine_factory = engine_factory
        self.clock = clock or (lambda: datetime.now(timezone.utc))
        self.lease_duration = lease_duration
        self.report_maintenance = report_maintenance

    def _now(self) -> datetime:
        return require_utc(self.clock())

    def _maintain_reports(self, now: datetime) -> None:
        if self.report_maintenance is None:
            return
        for task in self.store.list_tasks():
            try:
                self.report_maintenance(task.task_id, now)
            except Exception:
                continue

    def run_task(self, task_id: str, generation: int) -> RunnerResult:
        UUID(task_id)
        now = self._now()
        legacy_final_end: datetime | None = None
        self._maintain_reports(now)
        task = self.store.get_task(task_id)
        if task is None:
            return RunnerResult(0, 0, 0, 0)
        if task.lifecycle == "active":
            if task.generation != generation:
                return RunnerResult(0, 0, 0, 0)
            try:
                self.task_service.materialize_due(task_id, now=now)
            except MonitorStateConflict:
                return RunnerResult(0, 0, 0, 0)
            except MonitorScheduleError as error:
                existing = [
                    run
                    for run in self.store.list_due_runs(task_id, now)
                    if run.generation == task.generation
                    and (
                        run.status in {"queued", "running"}
                        or (
                            run.status == "failed"
                            and any(public.retryable for public in run.errors)
                            and self.store.automatic_retry_count(run.run_id)
                            < MAX_AUTOMATIC_RETRIES
                        )
                    )
                ]
                try:
                    failure_runs = self._materialization_failure_runs(task, now)
                except Exception:
                    return RunnerResult(0, 0, 0, 1)
                existing_ids = {run.run_id for run in existing}
                failure_runs = [
                    run for run in failure_runs if run.run_id not in existing_ids
                ]
                if not existing and not failure_runs:
                    return RunnerResult(0, 0, 0, 0)
                self.store.heartbeat(task_id, now)
                normal_result = (
                    self._execute(existing, None)
                    if existing
                    else RunnerResult(0, 0, 0, 0)
                )
                failure_result = (
                    self._execute(
                        failure_runs,
                        None,
                        failure_errors=(_public_error(error),),
                    )
                    if failure_runs
                    else RunnerResult(0, 0, 0, 0)
                )
                return RunnerResult(
                    normal_result.processed + failure_result.processed,
                    normal_result.succeeded + failure_result.succeeded,
                    normal_result.failed + failure_result.failed,
                    normal_result.retryable_failures
                    + failure_result.retryable_failures,
                )
            except Exception:
                return RunnerResult(0, 0, 0, 1)
            task = self.store.get_task(task_id)
            if task is None:
                return RunnerResult(0, 0, 0, 0)
        elif generation != task.generation:
            if task.lifecycle not in {"paused", "ended"}:
                return RunnerResult(0, 0, 0, 0)
            terminal_cutoff = (
                task.paused_at if task.lifecycle == "paused" else task.end_at
            )
            matching_final = [
                run
                for run in self.store.list_due_runs(task_id, now)
                if run.generation == generation
                and (
                    run.boundary_type.value in {"pause", "end"}
                    or (
                        terminal_cutoff is not None
                        and run.end_at == terminal_cutoff
                    )
                )
            ]
            if not matching_final:
                return RunnerResult(0, 0, 0, 0)
            legacy_final_end = max(run.end_at for run in matching_final)
        elif task.lifecycle not in {"active", "paused", "ended"}:
            return RunnerResult(0, 0, 0, 0)

        runs = [
            run for run in self.store.list_due_runs(task_id, now)
            if (
                generation == task.generation
                or run.generation == generation
                or (
                    legacy_final_end is not None
                    and run.end_at <= legacy_final_end
                )
            )
            if run.status in {"queued", "running"}
            or (
                run.status == "failed"
                and any(error.retryable for error in run.errors)
                and self.store.automatic_retry_count(run.run_id) < MAX_AUTOMATIC_RETRIES
            )
        ]
        if not runs:
            return RunnerResult(0, 0, 0, 0)

        self.store.heartbeat(task_id, now)
        return self._execute(runs, None)

    def _materialization_failure_runs(
        self, task: TaskRecord, now: datetime
    ) -> list[RunRecord]:
        anchor = max(
            self.store.latest_boundary(task.task_id).boundary_at,
            task.schedule_effective_at,
        )
        if anchor >= now:
            return []
        trigger = time.fromisoformat(task.daily_trigger_time)
        cutoff = next_scheduled_cutoff(
            after=anchor,
            trigger=trigger,
            end_at=task.end_at,
        )
        if cutoff is None or cutoff > now:
            return []
        candidates = scheduled_boundaries(
            after=anchor,
            due_at=cutoff,
            trigger=trigger,
            generation=task.generation,
            end_at=task.end_at,
        )
        if not candidates:
            return []
        first = candidates[0]
        failure_boundary = BoundarySpec(
            first.boundary_at,
            first.boundary_type,
            task.generation,
            "materialization_failure",
        )
        final_end = task.end_at is not None and first.boundary_at == task.end_at
        try:
            if final_end:
                self.store.transition_task(
                    task.task_id,
                    boundaries=[failure_boundary],
                    updates={
                        "lifecycle": "ended",
                        "generation": task.generation + 1,
                        "scheduler_desired_state": "disabled",
                        "scheduler_sync_status": "pending",
                        "scheduler_error": None,
                        "ended_at": task.end_at,
                        "ended_reason": "configured",
                    },
                    now=now,
                    expected_generation=task.generation,
                    expected_lifecycle="active",
                )
            else:
                self.store.append_boundaries(
                    task.task_id,
                    [failure_boundary],
                    now,
                    expected_generation=task.generation,
                    expected_lifecycle="active",
                )
        except MonitorStateConflict:
            pass
        return [
            run
            for run in self.store.list_due_runs(task.task_id, now)
            if run.end_at == failure_boundary.boundary_at
            and run.generation == task.generation
            and run.status in {"queued", "running"}
        ]

    def run_run(
        self,
        run_id: str,
        *,
        trigger: Literal["automatic_retry", "manual_retry"] = "manual_retry",
    ) -> RunnerResult:
        UUID(run_id)
        self._maintain_reports(self._now())
        run = self.store.get_run(run_id)
        if run is None:
            return RunnerResult(0, 0, 0, 0)
        return self._execute([run], trigger)

    def _execute(
        self,
        runs: list[RunRecord],
        trigger: str | None,
        *,
        failure_errors: tuple[MonitorPublicErrorPayload, ...] | None = None,
    ) -> RunnerResult:
        processed = succeeded = failed = retryable = 0
        for run in runs:
            now = self._now()
            requested_trigger = trigger or (
                "automatic_retry" if run.status == "failed" else "scheduled"
            )
            claim = self.store.claim_run(
                run.run_id,
                now=now,
                lease_for=self.lease_duration,
                trigger=requested_trigger,
            )
            if claim is None:
                continue
            processed += 1
            task = self.store.get_task(run.task_id)
            finalizing = False
            try:
                with _LeaseGuard(
                    self.store,
                    run.run_id,
                    claim.lease_token,
                    clock=self.clock,
                    lease_duration=self.lease_duration,
                ) as lease:
                    if failure_errors is not None:
                        raise MonitorRunComputationFailed(failure_errors)
                    engine = self.engine_factory(task)
                    publisher = getattr(engine, "publisher", None)
                    result = None
                    if publisher is None:
                        result = engine.execute(
                            claim.run,
                            task,
                            claim.run.started_at or claim.run.created_at,
                        )
                        publisher = result.publisher
                    manifest = self.store.get_publication(run.run_id)
                    draft = None
                    if manifest is not None and hasattr(publisher, "load_registered"):
                        try:
                            draft = publisher.load_registered(
                                task_id=manifest.task_id,
                                run_id=manifest.run_id,
                                logical_cutoff_at=claim.run.end_at,
                                reference=manifest.report_ref,
                                json_sha256=manifest.json_sha256,
                                html_sha256=manifest.html_sha256,
                                report_expires_at=manifest.report_expires_at,
                            )
                        except Exception:
                            draft = None
                    if draft is None:
                        if result is None:
                            result = engine.execute(
                                claim.run,
                                task,
                                claim.run.started_at or claim.run.created_at,
                            )
                        draft = result.draft
                        publisher = result.publisher
                    lease.ensure_owned()
                    publication = publication_from_draft(draft)
                    self.store.prepare_publication(
                        run.run_id,
                        claim.lease_token,
                        now=self._now(),
                        status=publication.status,
                        start_revision=publication.start_revision,
                        end_revision=publication.end_revision,
                        summary=publication.run_summary.model_dump(mode="json"),
                        errors=list(publication.errors),
                        report_ref=publication.report_ref,
                        json_sha256=publication.report_sha256,
                        html_sha256=draft.html_sha256,
                        report_expires_at=publication.report_expires_at,
                    )
                    lease.ensure_owned()
                    publisher.publish_history(
                        draft, ensure_owned=lease.ensure_owned
                    )
                    lease.ensure_owned()
                    publisher.activate_latest(
                        draft, ensure_owned=lease.ensure_owned
                    )
                    lease.ensure_owned()
                    finalizing = True
                    self.store.finalize_publication(
                        run.run_id,
                        claim.lease_token,
                        now=self._now(),
                    )
                succeeded += 1
            except MonitorLeaseLost:
                processed -= 1
            except Exception as error:
                public_errors = (
                    list(error.errors)
                    if isinstance(error, MonitorRunComputationFailed)
                    else [_public_error(error)]
                )
                if finalizing:
                    failed += 1
                    retryable += 1
                    continue
                try:
                    self.store.finish_run(
                        run.run_id,
                        claim.lease_token,
                        now=self._now(),
                        status="failed",
                        errors=public_errors,
                    )
                except MonitorLeaseLost:
                    processed -= 1
                    continue
                except Exception:
                    failed += 1
                    retryable += 1
                    continue
                failed += 1
                has_retry_budget = (
                    self.store.automatic_retry_count(run.run_id)
                    < MAX_AUTOMATIC_RETRIES
                )
                retryable += int(
                    any(public.retryable for public in public_errors)
                    and has_retry_budget
                )
        return RunnerResult(processed, succeeded, failed, retryable)


def build_runner(
    *,
    database_path: str | Path = DEFAULT_DATABASE_PATH,
    config_path: str | Path = DEFAULT_CONFIG_PATH,
) -> MonitorRunnerService:
    store = MonitorStore(database_path)
    tasks = MonitorTaskService(store)
    publisher = FileSystemMonitorReportPublisher(
        Path(database_path).parent / "reports"
    )
    try:
        config = ConfigStore(Path(config_path)).read()
        dataset = config.get("dataset_layout")
        if not isinstance(dataset, dict):
            raise MonitorRunnerConfigurationError
        provider = provider_from_config(config)
        history = BranchHistoryService(provider)
        workbook_source = dict(dataset["workbook_source"])
        csv_export = dict(dataset["csv_export"])
        factory: MonitorRunEngineFactory = P1MonitorRunEngineFactory(
            history=history,
            layout=DatasetLayout.from_config(dataset),
            table_directory_name=str(workbook_source["directory_name"]),
            csv_directory_name=str(csv_export["directory_name"]),
            publisher=publisher,
            task_service=tasks,
        )
    except (KeyError, OSError, RuntimeError, TypeError, ValueError, SVNProviderError):
        errors = (_public_error(MonitorRunnerConfigurationError()),)
        factory = lambda task: ConfigurationFailureEngine(errors)
    return MonitorRunnerService(
        store,
        tasks,
        factory,
        report_maintenance=lambda task_id, now: publisher.cleanup_expired(
            task_id, now=now
        ),
    )


def run_maintenance(
    *,
    database_path: str | Path = DEFAULT_DATABASE_PATH,
    now: datetime | None = None,
) -> MaintenanceResult:
    """Run retention cleanup without loading SVN or application configuration."""
    store = MonitorStore(database_path)
    publisher = FileSystemMonitorReportPublisher(
        Path(database_path).parent / "reports"
    )
    current = require_utc(now or datetime.now(timezone.utc))
    cleaned = failed = 0
    tasks = store.list_tasks()
    for task in tasks:
        try:
            cleaned += len(publisher.cleanup_expired(task.task_id, now=current))
        except Exception:
            failed += 1
    return MaintenanceResult(len(tasks), cleaned, failed)


def reconcile_inactive_scheduler(
    *,
    task_id: str,
    database_path: str | Path,
    working_directory: str | Path,
    gateway: SchedulerGateway | None = None,
) -> str:
    store = MonitorStore(database_path)
    task = store.get_task(str(UUID(task_id)))
    if task is None or task.lifecycle == "active":
        return "not_required"
    scheduler = MonitorSchedulerService(
        store,
        gateway or WindowsSchedulerGateway(),
        database_path=database_path,
        working_directory=working_directory,
        python_executable=sys.executable,
    )
    return scheduler.sync_task(
        task.task_id,
        expected_generation=task.generation,
        trigger_final=False,
    ).status


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run due M3 version monitoring reports")
    target = parser.add_mutually_exclusive_group(required=True)
    target.add_argument("--task-id")
    target.add_argument("--run-id")
    target.add_argument("--maintenance", action="store_true")
    parser.add_argument("--generation", type=int)
    parser.add_argument("--automatic-retry", action="store_true")
    parser.add_argument("--scheduler-managed", action="store_true")
    parser.add_argument("--database", default=os.environ.get("EXCEL_MERGE_MONITOR_DB", str(DEFAULT_DATABASE_PATH)))
    parser.add_argument("--config", default=str(DEFAULT_CONFIG_PATH))
    args = parser.parse_args(argv)
    if args.maintenance:
        result = run_maintenance(database_path=args.database)
    else:
        runner = build_runner(database_path=args.database, config_path=args.config)
    if args.task_id:
        if args.generation is None or args.generation <= 0:
            parser.error("--task-id requires a positive --generation")
        result = runner.run_task(args.task_id, args.generation)
    elif args.run_id:
        result = runner.run_run(
            args.run_id,
            trigger="automatic_retry" if args.automatic_retry else "manual_retry",
        )
    scheduler_status = "not_required"
    if args.scheduler_managed and args.task_id:
        scheduler_status = reconcile_inactive_scheduler(
            task_id=args.task_id,
            database_path=args.database,
            working_directory=Path.cwd(),
        )
    print(json.dumps(result.__dict__, ensure_ascii=False, separators=(",", ":")))
    if scheduler_status in {"error", "stale"}:
        return 75
    if args.scheduler_managed and result.exit_category == "permanent_failure":
        return 0
    return {
        "ok": 0,
        "noop": 0,
        "temporary_failure": 75,
        "permanent_failure": 1,
    }[result.exit_category]


if __name__ == "__main__":
    raise SystemExit(main())
