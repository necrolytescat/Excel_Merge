from __future__ import annotations

import csv
from datetime import datetime, time, timezone
from io import StringIO

import pytest

from app.monitor_runner import (
    MonitorRunnerService,
    P1MonitorRunEngine,
    RunnerResult,
)
from app.schemas.monitor import MonitorReportPayload
from app.services.branch_history_service import BranchHistoryService
from app.services.monitor_attribution_service import MonitorAttributionService
from app.services.monitor_diff_service import (
    MonitorDiffService,
    MonitorSnapshot,
    MonitorWorkbookSnapshot,
)
from app.services.monitor_report_service import (
    CanonicalJsonReferencePublisher,
    MonitorReportPublishError,
)
from app.services.monitor_report_artifacts import FileSystemMonitorReportPublisher
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


def p1_factory(tasks, publisher):
    history = BranchHistoryService(HistoryProvider())
    diff = MonitorDiffService(Reader())
    return lambda record: P1MonitorRunEngine(
        history=history,
        endpoint=None,
        identity=IDENTITY,
        diff_service=diff,
        attribution_service=MonitorAttributionService(diff),
        publisher=publisher,
        task_service=tasks,
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


def test_filesystem_runner_publishes_history_latest_and_activated_manifest(tmp_path):
    clock = Clock(instant(10))
    database = tmp_path / "persistent" / "monitor.sqlite3"
    store = MonitorStore(database)
    tasks = MonitorTaskService(store, clock=clock)
    task = create_task(tasks)
    publisher = FileSystemMonitorReportPublisher(database.parent / "reports")
    runner = MonitorRunnerService(
        store, tasks, p1_factory(tasks, publisher), clock=clock
    )

    result = runner.run_task(str(task.task_id), task.scheduler.generation)

    assert result.exit_category == "ok"
    run = store.list_runs(str(task.task_id))[0]
    manifest = store.get_publication(run.run_id)
    assert run.status == "succeeded"
    assert manifest.state == "activated"
    task_dir = database.parent / "reports" / str(task.task_id)
    assert (task_dir / "history" / "20260810-180000.json").exists()
    assert (task_dir / "history" / "20260810-180000.html").exists()
    assert (task_dir / "latest.html").exists()


def test_crash_after_latest_before_finalize_recovers_same_bytes_without_web(
    tmp_path, monkeypatch
):
    clock = Clock(instant(10))
    database = tmp_path / "crash" / "monitor.sqlite3"
    store = MonitorStore(database)
    tasks = MonitorTaskService(store, clock=clock)
    task = create_task(tasks)
    reports = database.parent / "reports"
    publisher = FileSystemMonitorReportPublisher(reports)
    runner = MonitorRunnerService(
        store, tasks, p1_factory(tasks, publisher), clock=clock
    )

    def crash_finalize(*args, **kwargs):
        raise OSError("simulated process exit after latest")

    monkeypatch.setattr(store, "finalize_publication", crash_finalize)
    first = runner.run_task(str(task.task_id), task.scheduler.generation)
    run = store.list_runs(str(task.task_id))[0]
    manifest = store.get_publication(run.run_id)
    history_json = (
        reports
        / str(task.task_id)
        / "history"
        / "20260810-180000.json"
    )
    first_bytes = history_json.read_bytes()
    assert first.exit_category == "temporary_failure"
    assert run.status == "running"
    assert manifest.state == "prepared"
    assert (reports / str(task.task_id) / "latest.html").exists()

    clock.value = instant(10, 6)
    recovered_store = MonitorStore(database)
    recovered_tasks = MonitorTaskService(recovered_store, clock=clock)
    recovered_publisher = FileSystemMonitorReportPublisher(reports)
    recovered_runner = MonitorRunnerService(
        recovered_store,
        recovered_tasks,
        p1_factory(recovered_tasks, recovered_publisher),
        clock=clock,
    )
    second = recovered_runner.run_task(
        str(task.task_id), task.scheduler.generation
    )
    recovered = recovered_store.list_runs(str(task.task_id))[0]

    assert second.exit_category == "ok"
    assert recovered.status == "succeeded"
    assert recovered.report_ref == manifest.report_ref
    assert recovered.report_sha256 == manifest.json_sha256
    assert history_json.read_bytes() == first_bytes
    assert len(list(history_json.parent.glob("*.json"))) == 1
    assert len(list(history_json.parent.glob("*.html"))) == 1


def test_transient_latest_failure_is_retryable_and_reuses_prepared_report(
    tmp_path, monkeypatch
):
    clock = Clock(instant(10))
    database = tmp_path / "latest-retry" / "monitor.sqlite3"
    store = MonitorStore(database)
    tasks = MonitorTaskService(store, clock=clock)
    task = create_task(tasks)
    publisher = FileSystemMonitorReportPublisher(database.parent / "reports")
    runner = MonitorRunnerService(
        store, tasks, p1_factory(tasks, publisher), clock=clock
    )
    real_activate = publisher.activate_latest
    monkeypatch.setattr(
        publisher,
        "activate_latest",
        lambda draft, **kwargs: (_ for _ in ()).throw(
            MonitorReportPublishError("simulated locked latest")
        ),
    )

    first = runner.run_task(str(task.task_id), task.scheduler.generation)
    failed = store.list_runs(str(task.task_id))[0]
    manifest = store.get_publication(failed.run_id)
    assert first.exit_category == "temporary_failure"
    assert failed.status == "failed"
    assert failed.errors[0].code == "MONITOR_REPORT_PUBLISH_FAILED"
    assert failed.report_ref is None
    assert manifest.state == "prepared"

    monkeypatch.setattr(publisher, "activate_latest", real_activate)
    clock.value = instant(10, 10)
    second = runner.run_task(str(task.task_id), task.scheduler.generation)
    recovered = store.list_runs(str(task.task_id))[0]
    assert second.exit_category == "ok"
    assert recovered.status == "succeeded"
    assert recovered.report_ref == manifest.report_ref
    assert recovered.attempt_count == 2


def test_lost_lease_cannot_prepare_history_or_latest(tmp_path, monkeypatch):
    clock = Clock(instant(10))
    database = tmp_path / "lost" / "monitor.sqlite3"
    store = MonitorStore(database)
    tasks = MonitorTaskService(store, clock=clock)
    task = create_task(tasks)
    reports = database.parent / "reports"
    publisher = FileSystemMonitorReportPublisher(reports)
    runner = MonitorRunnerService(
        store, tasks, p1_factory(tasks, publisher), clock=clock
    )
    monkeypatch.setattr(store, "renew_lease", lambda *args, **kwargs: False)

    result = runner.run_task(str(task.task_id), task.scheduler.generation)

    assert result.exit_category == "noop"
    assert store.get_publication(
        store.list_runs(str(task.task_id))[0].run_id
    ) is None
    assert not reports.exists()


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


def test_pause_and_user_end_final_runs_recover_once_under_current_generation(tmp_path):
    clock = Clock(instant(9, 30))
    store = MonitorStore(tmp_path / "noop.sqlite3")
    tasks = MonitorTaskService(store, clock=clock)
    task = create_task(tasks)
    runner = MonitorRunnerService(
        store, tasks, p1_factory(tasks, CanonicalJsonReferencePublisher()), clock=clock
    )
    assert runner.run_task(str(task.task_id), task.scheduler.generation + 1).exit_category == "noop"
    paused = tasks.pause(str(task.task_id))
    boundary_count = len(store.list_boundaries(str(task.task_id)))
    assert runner.run_task(str(task.task_id), task.scheduler.generation).exit_category == "noop"
    assert runner.run_task(str(task.task_id), paused.scheduler.generation).exit_category == "ok"
    assert runner.run_task(str(task.task_id), paused.scheduler.generation).exit_category == "noop"
    assert len(store.list_boundaries(str(task.task_id))) == boundary_count

    clock.value = instant(9, 35)
    resumed = tasks.resume(str(task.task_id))
    clock.value = instant(9, 40)
    ended = tasks.end(str(task.task_id))
    boundary_count = len(store.list_boundaries(str(task.task_id)))
    assert runner.run_task(str(task.task_id), resumed.scheduler.generation).exit_category == "noop"
    assert runner.run_task(str(task.task_id), ended.scheduler.generation).exit_category == "ok"
    assert runner.run_task(str(task.task_id), ended.scheduler.generation).exit_category == "noop"
    assert len(store.list_boundaries(str(task.task_id))) == boundary_count


def test_configured_end_old_action_recovers_after_transition_crash(tmp_path):
    clock = Clock(instant(9))
    store = MonitorStore(tmp_path / "configured-crash.sqlite3")
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
    old_generation = task.scheduler.generation
    clock.value = instant(10)
    pending = tasks.materialize_due(str(task.task_id))
    assert pending and store.get_task(str(task.task_id)).lifecycle == "ended"
    runner = MonitorRunnerService(
        store, tasks, p1_factory(tasks, CanonicalJsonReferencePublisher()), clock=clock
    )
    boundary_count = len(store.list_boundaries(str(task.task_id)))
    assert runner.run_task(str(task.task_id), old_generation).exit_category == "ok"
    assert runner.run_task(str(task.task_id), old_generation).exit_category == "noop"
    assert len(store.list_boundaries(str(task.task_id))) == boundary_count


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
    initial = tasks.public_run(run_id)
    assert initial.errors[0].message == "SVN 暂时不可用，运行可重试"

    second = runner.run_task(str(task.task_id), task.scheduler.generation)
    third = runner.run_task(str(task.task_id), task.scheduler.generation)
    fourth = runner.run_task(str(task.task_id), task.scheduler.generation)
    exhausted = runner.run_task(str(task.task_id), task.scheduler.generation)
    run = tasks.public_run(run_id)
    assert second.exit_category == third.exit_category == "temporary_failure"
    assert fourth.exit_category == "permanent_failure"
    assert exhausted.exit_category == "noop"
    assert str(run.run_id) == run_id
    assert run.attempt_count == 4
    assert [attempt.trigger for attempt in run.attempts] == [
        "scheduled", "automatic_retry", "automatic_retry", "automatic_retry",
    ]
    assert run.interval == initial.interval
    dumped = run.model_dump_json()
    assert "private command stderr" not in dumped
    assert "lease" not in dumped
    assert "sqlite" not in dumped


def test_permanent_task_failure_is_not_automatically_retried(tmp_path):
    clock = Clock(instant(10))
    store = MonitorStore(tmp_path / "permanent.sqlite3")
    tasks = MonitorTaskService(store, clock=clock)
    task = create_task(tasks)

    class PermanentFailure:
        def execute(self, run, task_record, generated_at):
            raise SVNProviderError("SVN_AUTH_FAILED", "private auth details")

    runner = MonitorRunnerService(
        store, tasks, lambda record: PermanentFailure(), clock=clock
    )
    assert runner.run_task(str(task.task_id), task.scheduler.generation).exit_category == "permanent_failure"
    assert runner.run_task(str(task.task_id), task.scheduler.generation).exit_category == "noop"
    assert tasks.public_run(store.list_runs(str(task.task_id))[0].run_id).attempt_count == 1


def test_exit_category_depends_on_remaining_retryable_failures():
    assert RunnerResult(2, 0, 2, 1).exit_category == "temporary_failure"
    assert RunnerResult(2, 0, 2, 0).exit_category == "permanent_failure"


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
