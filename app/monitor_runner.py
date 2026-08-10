"""Independent M3 monitor runner. This module has no FastAPI dependency."""
from __future__ import annotations

import argparse
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path, PurePosixPath
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
    CanonicalJsonReferencePublisher,
    MonitorReportPublisher,
    ReportPublication,
)
from app.services.monitor_store import (
    DEFAULT_DATABASE_PATH,
    MonitorLeaseLost,
    MonitorStateConflict,
    MonitorStore,
    RunRecord,
    TaskRecord,
)
from app.services.monitor_schedule import require_utc
from app.services.monitor_task_service import MonitorTaskService
from app.services.workbook_diff_service import DatasetLayout
from core.models import EndpointSpec
from core.svn_history import BranchIdentity
from core.svn_provider import SVNProviderError, provider_from_config


PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_CONFIG_PATH = PROJECT_ROOT / "config" / "settings.json"
LEASE_DURATION = timedelta(minutes=30)
MAX_AUTOMATIC_RETRIES = 3


@dataclass(frozen=True)
class EngineResult:
    publication: ReportPublication


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
        task_public = self.task_service.to_public_task(task)
        publication = self.publisher.publish(
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
        return EngineResult(publication)


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
        if self.processed == 0:
            return "noop"
        if self.failed == 0:
            return "ok"
        return "temporary_failure" if self.retryable_failures else "permanent_failure"


def _public_error(error: Exception) -> MonitorPublicErrorPayload:
    if isinstance(error, SVNProviderError):
        code = str(error.code).upper()
        if "TIMEOUT" in code or "NETWORK" in code:
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


class MonitorRunnerService:
    def __init__(
        self,
        store: MonitorStore,
        task_service: MonitorTaskService,
        engine_factory: MonitorRunEngineFactory,
        *,
        clock: Callable[[], datetime] | None = None,
        lease_duration: timedelta = LEASE_DURATION,
    ):
        self.store = store
        self.task_service = task_service
        self.engine_factory = engine_factory
        self.clock = clock or (lambda: datetime.now(timezone.utc))
        self.lease_duration = lease_duration

    def _now(self) -> datetime:
        return require_utc(self.clock())

    def run_task(self, task_id: str, generation: int) -> RunnerResult:
        UUID(task_id)
        task = self.store.get_task(task_id)
        if task is None:
            return RunnerResult(0, 0, 0, 0)
        now = self._now()
        if task.lifecycle == "active":
            if task.generation != generation:
                return RunnerResult(0, 0, 0, 0)
            try:
                self.task_service.materialize_due(task_id, now=now)
            except MonitorStateConflict:
                return RunnerResult(0, 0, 0, 0)
            task = self.store.get_task(task_id)
            if task is None:
                return RunnerResult(0, 0, 0, 0)
        elif generation != task.generation:
            if task.lifecycle != "ended" or task.ended_reason != "configured":
                return RunnerResult(0, 0, 0, 0)
            matching_final = any(
                run.generation == generation
                and (run.boundary_type.value == "end" or run.end_at == task.end_at)
                for run in self.store.list_due_runs(task_id, now)
            )
            if not matching_final:
                return RunnerResult(0, 0, 0, 0)
        elif task.lifecycle not in {"active", "paused", "ended"}:
            return RunnerResult(0, 0, 0, 0)

        runs = [
            run for run in self.store.list_due_runs(task_id, now)
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

    def run_run(
        self,
        run_id: str,
        *,
        trigger: Literal["automatic_retry", "manual_retry"] = "manual_retry",
    ) -> RunnerResult:
        UUID(run_id)
        run = self.store.get_run(run_id)
        if run is None:
            return RunnerResult(0, 0, 0, 0)
        return self._execute([run], trigger)

    def _execute(self, runs: list[RunRecord], trigger: str | None) -> RunnerResult:
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
            try:
                result = self.engine_factory(task).execute(claim.run, task, now)
                publication = result.publication
                self.store.finish_run(
                    run.run_id,
                    claim.lease_token,
                    now=self._now(),
                    status=publication.status,
                    errors=list(publication.errors),
                    start_revision=publication.start_revision,
                    end_revision=publication.end_revision,
                    summary=publication.run_summary.model_dump(mode="json"),
                    report_ref=publication.report_ref,
                    report_sha256=publication.report_sha256,
                    report_expires_at=publication.report_expires_at,
                )
                succeeded += 1
            except MonitorLeaseLost:
                processed -= 1
            except Exception as error:
                public = _public_error(error)
                try:
                    self.store.finish_run(
                        run.run_id,
                        claim.lease_token,
                        now=self._now(),
                        status="failed",
                        errors=[public],
                    )
                except MonitorLeaseLost:
                    processed -= 1
                    continue
                failed += 1
                has_retry_budget = (
                    self.store.automatic_retry_count(run.run_id)
                    < MAX_AUTOMATIC_RETRIES
                )
                retryable += int(public.retryable and has_retry_budget)
        return RunnerResult(processed, succeeded, failed, retryable)


def build_runner(
    *,
    database_path: str | Path = DEFAULT_DATABASE_PATH,
    config_path: str | Path = DEFAULT_CONFIG_PATH,
) -> MonitorRunnerService:
    config = ConfigStore(Path(config_path)).read()
    dataset = config.get("dataset_layout")
    if not isinstance(dataset, dict):
        raise RuntimeError("dataset_layout is required for the monitor runner")
    provider = provider_from_config(config)
    store = MonitorStore(database_path)
    tasks = MonitorTaskService(store)
    publisher = CanonicalJsonReferencePublisher()
    history = BranchHistoryService(provider)
    workbook_source = dict(dataset["workbook_source"])
    csv_export = dict(dataset["csv_export"])
    factory = P1MonitorRunEngineFactory(
        history=history,
        layout=DatasetLayout.from_config(dataset),
        table_directory_name=str(workbook_source["directory_name"]),
        csv_directory_name=str(csv_export["directory_name"]),
        publisher=publisher,
        task_service=tasks,
    )
    return MonitorRunnerService(store, tasks, factory)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run due M3 version monitoring reports")
    target = parser.add_mutually_exclusive_group(required=True)
    target.add_argument("--task-id")
    target.add_argument("--run-id")
    parser.add_argument("--generation", type=int)
    parser.add_argument("--automatic-retry", action="store_true")
    parser.add_argument("--database", default=os.environ.get("EXCEL_MERGE_MONITOR_DB", str(DEFAULT_DATABASE_PATH)))
    parser.add_argument("--config", default=str(DEFAULT_CONFIG_PATH))
    args = parser.parse_args(argv)
    runner = build_runner(database_path=args.database, config_path=args.config)
    if args.task_id:
        if args.generation is None or args.generation <= 0:
            parser.error("--task-id requires a positive --generation")
        result = runner.run_task(args.task_id, args.generation)
    else:
        result = runner.run_run(
            args.run_id,
            trigger="automatic_retry" if args.automatic_retry else "manual_retry",
        )
    print(json.dumps(result.__dict__, ensure_ascii=False, separators=(",", ":")))
    return {"ok": 0, "noop": 0, "temporary_failure": 75, "permanent_failure": 1}[
        result.exit_category
    ]


if __name__ == "__main__":
    raise SystemExit(main())
