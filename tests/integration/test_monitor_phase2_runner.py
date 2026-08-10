from __future__ import annotations

import csv
from datetime import datetime, time, timezone
from io import StringIO

import pytest

from app.monitor_runner import (
    MonitorRunnerService,
    P1MonitorRunEngine,
)
from app.schemas.monitor import MonitorReportPayload
from app.services.branch_history_service import BranchHistoryService
from app.services.monitor_attribution_service import MonitorAttributionService
from app.services.monitor_diff_service import (
    MonitorDiffService,
    MonitorSnapshot,
    MonitorWorkbookSnapshot,
)
from app.services.monitor_report_service import CanonicalJsonReferencePublisher
from app.services.monitor_store import MonitorStore
from app.services.monitor_task_service import CreateMonitorTask, MonitorTaskService
from core.svn_history import BranchCommit, BranchCopyBoundary, BranchIdentity
from core.svn_provider import SVNProviderError
from core.table_csv_parser import parse_table_csv


UTC = timezone.utc


class Clock:
    def __init__(self, value):
        self.value = value

    def __call__(self):
        return self.value


def instant(hour, minute=0):
    return datetime(2026, 8, 10, hour, minute, tzinfo=UTC)


IDENTITY = BranchIdentity(
    canonical_url="https://svn.example/repo/branches/kr-fix",
    repository_root="https://svn.example/repo",
    repository_uuid="20000000-0000-4000-8000-000000000001",
    repository_relative_path="branches/kr-fix",
    bound_revision=101,
)


def table(hp):
    output = StringIO(newline="")
    csv.writer(output, lineterminator="\n").writerows(
        [
            ["ID", "生命值"], ["Id", "Hp"], ["uint32", "uint32"],
            ["All", "Client"], ["meta", "meta"], ["meta", "meta"],
            ["meta", "meta"], ["100", str(hp)],
        ]
    )
    return parse_table_csv(output.getvalue().encode(), "Role.csv")


class Reader:
    def __init__(self):
        self.snapshots = {
            100: MonitorSnapshot(
                revision=100,
                workbooks={"Combat.xlsm": MonitorWorkbookSnapshot(sheets={"Role": table(100)})},
            ),
            101: MonitorSnapshot(
                revision=101,
                workbooks={"Combat.xlsm": MonitorWorkbookSnapshot(sheets={"Role": table(110)})},
            ),
        }

    def load_snapshot(self, revision):
        return self.snapshots[revision]


class HistoryProvider:
    def resolve_branch_identity(self, endpoint):
        return IDENTITY

    def resolve_revision_at(self, identity, instant_at):
        return 100 if instant_at < instant(10) else 101

    def list_branch_commits(self, identity, start, end):
        return [
            BranchCommit(
                revision=101,
                author="alice",
                changed_at=instant(9, 45),
                message="adjust hp",
            )
        ]

    def resolve_copy_boundary(self, identity):
        return BranchCopyBoundary(90)


def create_task(service):
    return service.create(
        CreateMonitorTask(
            name="KR daily",
            endpoint_id="kr-fix",
            branch_label="KR Fix",
            repository_uuid=IDENTITY.repository_uuid,
            canonical_url=IDENTITY.canonical_url,
            repository_relative_path=IDENTITY.repository_relative_path,
            bound_revision=101,
            copy_boundary_revision=90,
            effective_at=instant(9),
            daily_trigger_time=time(18),
        )
    )


def test_runner_executes_real_p1_diff_and_attribution_without_web(tmp_path):
    clock = Clock(instant(10))
    store = MonitorStore(tmp_path / "runner" / "monitor.sqlite3")
    tasks = MonitorTaskService(store, clock=clock)
    task = create_task(tasks)
    publisher = CanonicalJsonReferencePublisher()
    history = BranchHistoryService(HistoryProvider())
    diff = MonitorDiffService(Reader())

    def factory(record):
        return P1MonitorRunEngine(
            history=history,
            endpoint=None,
            identity=IDENTITY,
            diff_service=diff,
            attribution_service=MonitorAttributionService(diff),
            publisher=publisher,
            task_service=tasks,
        )

    runner = MonitorRunnerService(store, tasks, factory, clock=clock)
    result = runner.run_task(str(task.task_id), task.scheduler.generation)

    assert result.exit_category == "ok"
    run = tasks.public_run(store.list_runs(str(task.task_id))[0].run_id)
    assert run.status == "succeeded"
    assert run.attempt_count == 1
    assert run.start_revision == 100
    assert run.end_revision == 101
    assert run.summary.change_count == 1
    raw = publisher.results[run.report_ref]
    report = MonitorReportPayload.model_validate_json(raw)
    assert report.changes[0].attribution.author == "alice"
    assert report.interval.start_at == instant(9)
    assert report.interval.end_at == instant(10)


def test_late_runner_start_keeps_the_original_logical_cutoff(tmp_path):
    clock = Clock(instant(10))
    store = MonitorStore(tmp_path / "late.sqlite3")
    tasks = MonitorTaskService(store, clock=clock)
    task = create_task(tasks)
    publisher = CanonicalJsonReferencePublisher()
    history = BranchHistoryService(HistoryProvider())
    diff = MonitorDiffService(Reader())
    factory = lambda record: P1MonitorRunEngine(
        history=history,
        endpoint=None,
        identity=IDENTITY,
        diff_service=diff,
        attribution_service=MonitorAttributionService(diff),
        publisher=publisher,
        task_service=tasks,
    )
    clock.value = instant(11, 30)
    result = MonitorRunnerService(store, tasks, factory, clock=clock).run_task(
        str(task.task_id), task.scheduler.generation
    )
    assert result.exit_category == "ok"
    run = tasks.public_run(store.list_runs(str(task.task_id))[0].run_id)
    assert run.interval.end_at == instant(10)
    assert run.finished_at == instant(11, 30)


def test_runner_executes_configured_final_run_before_task_stops(tmp_path):
    clock = Clock(instant(9))
    store = MonitorStore(tmp_path / "configured-end.sqlite3")
    tasks = MonitorTaskService(store, clock=clock)
    task = tasks.create(
        CreateMonitorTask(
            name="KR final", endpoint_id="kr-fix", branch_label="KR Fix",
            repository_uuid=IDENTITY.repository_uuid, canonical_url=IDENTITY.canonical_url,
            repository_relative_path=IDENTITY.repository_relative_path,
            bound_revision=101, copy_boundary_revision=90,
            effective_at=instant(8), daily_trigger_time=time(18), end_at=instant(10),
        )
    )
    publisher = CanonicalJsonReferencePublisher()
    history = BranchHistoryService(HistoryProvider())
    diff = MonitorDiffService(Reader())
    factory = lambda record: P1MonitorRunEngine(
        history=history, endpoint=None, identity=IDENTITY, diff_service=diff,
        attribution_service=MonitorAttributionService(diff), publisher=publisher,
        task_service=tasks,
    )
    clock.value = instant(10)
    result = MonitorRunnerService(store, tasks, factory, clock=clock).run_task(
        str(task.task_id), task.scheduler.generation
    )
    assert result.exit_category == "ok"
    assert store.get_task(str(task.task_id)).lifecycle == "ended"
    assert tasks.public_run(store.list_runs(str(task.task_id))[-1].run_id).status == "succeeded"


def test_old_generation_and_paused_or_ended_task_triggers_are_noops(tmp_path):
    clock = Clock(instant(9, 30))
    store = MonitorStore(tmp_path / "noop.sqlite3")
    tasks = MonitorTaskService(store, clock=clock)
    task = create_task(tasks)

    class NeverFactory:
        def __call__(self, record):
            raise AssertionError("no engine should be created")

    runner = MonitorRunnerService(store, tasks, NeverFactory(), clock=clock)
    assert runner.run_task(str(task.task_id), task.scheduler.generation + 1).exit_category == "noop"
    paused = tasks.pause(str(task.task_id))
    assert runner.run_task(str(task.task_id), paused.scheduler.generation).exit_category == "noop"
    clock.value = instant(9, 40)
    ended = tasks.end(str(task.task_id))
    assert runner.run_task(str(task.task_id), ended.scheduler.generation).exit_category == "noop"


def test_runner_failure_classification_and_retry_reuse_original_run(tmp_path):
    clock = Clock(instant(10))
    store = MonitorStore(tmp_path / "retry.sqlite3")
    tasks = MonitorTaskService(store, clock=clock)
    task = create_task(tasks)
    run_id = store.list_runs(str(task.task_id))[0].run_id

    class FailingEngine:
        def execute(self, run, task_record, generated_at):
            raise SVNProviderError("SVN_TIMEOUT", "private command stderr")

    runner = MonitorRunnerService(store, tasks, lambda task_record: FailingEngine(), clock=clock)
    first = runner.run_task(str(task.task_id), task.scheduler.generation)
    assert first.exit_category == "temporary_failure"
    assert tasks.public_run(run_id).errors[0].message == "SVN 暂时不可用，运行可重试"

    second = runner.run_run(run_id, trigger="automatic_retry")
    third = runner.run_run(run_id, trigger="manual_retry")
    run = tasks.public_run(run_id)
    assert second.exit_category == third.exit_category == "temporary_failure"
    assert str(run.run_id) == run_id
    assert run.attempt_count == 3
    assert [attempt.trigger for attempt in run.attempts] == [
        "scheduled", "automatic_retry", "manual_retry",
    ]
    dumped = run.model_dump_json()
    assert "private command stderr" not in dumped
    assert "lease" not in dumped
    assert "sqlite" not in dumped


def test_credentials_are_rejected_and_public_task_has_no_internal_paths(tmp_path):
    clock = Clock(instant(10))
    store = MonitorStore(tmp_path / "secret-location.sqlite3")
    tasks = MonitorTaskService(store, clock=clock)
    with pytest.raises(ValueError, match="credentials"):
        tasks.create(CreateMonitorTask(
            name="KR daily", endpoint_id="kr-fix", branch_label="KR Fix",
            repository_uuid=IDENTITY.repository_uuid,
            canonical_url="https://user:password@svn.example/repo/branches/kr-fix",
            repository_relative_path=IDENTITY.repository_relative_path,
            bound_revision=101, copy_boundary_revision=90,
            effective_at=instant(9), daily_trigger_time=time(18),
        ))
    payload = tasks.create(
        CreateMonitorTask(
            name="KR daily", endpoint_id="kr-fix", branch_label="KR Fix",
            repository_uuid=IDENTITY.repository_uuid, canonical_url=IDENTITY.canonical_url,
            repository_relative_path=IDENTITY.repository_relative_path,
            bound_revision=101, copy_boundary_revision=90,
            effective_at=instant(9), daily_trigger_time=time(18),
        )
    )
    dumped = payload.model_dump_json()
    assert "canonical" not in dumped
    assert "password" not in dumped
    assert "sqlite" not in dumped
    assert "lease" not in dumped
