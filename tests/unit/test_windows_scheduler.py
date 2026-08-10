from __future__ import annotations

from datetime import datetime, time, timedelta, timezone
import hashlib
import json
from pathlib import Path
import tempfile
from uuid import uuid4
import xml.etree.ElementTree as ET
from zoneinfo import ZoneInfo

import pytest

from app.monitor_runner import (
    MonitorRunnerService,
    main as runner_main,
    reconcile_inactive_scheduler,
    run_maintenance,
)
from app.monitor_scheduler_cli import _isolated_database
from app.schemas.monitor import (
    MonitorPublicErrorPayload,
    MonitorReportPayload,
    serialize_monitor_json,
)
from app.services.monitor_report_artifacts import FileSystemMonitorReportPublisher
from app.services.monitor_report_service import (
    REPORT_RETENTION,
    ReportDraft,
    render_monitor_report_html,
    report_reference,
)
from app.services.monitor_store import MonitorStateConflict, MonitorStore
from app.services.monitor_schedule import BoundarySpec, MonitorScheduleError
from app.services.monitor_task_service import CreateMonitorTask, MonitorTaskService
from core.svn_provider import SVNProviderError
from app.services.windows_scheduler import (
    MAINTENANCE_TASK_NAME,
    MONITOR_TASK_PREFIX,
    EXECUTION_TIME_LIMIT,
    MULTIPLE_INSTANCES_POLICY,
    RESTART_COUNT,
    RESTART_INTERVAL,
    TASK_NAMESPACE,
    FakeSchedulerGateway,
    MonitorSchedulerService,
    ScheduledMonitorTaskService,
    SchedulerGatewayError,
    _system_executable,
    current_windows_user,
    monitor_task_name,
    parse_scheduler_task_xml,
    scheduler_task_xml,
    validate_scheduler_task,
)


UTC = timezone.utc
NOW = datetime(2026, 8, 10, 9, 0, tzinfo=UTC)


def command(
    *,
    task_id=None,
    name="QA daily",
    trigger=time(18),
    end_at=None,
    effective_at=None,
):
    return CreateMonitorTask(
        task_id=task_id or str(uuid4()),
        name=name,
        endpoint_id="kr-fix",
        branch_label="KR Fix",
        repository_uuid=str(uuid4()),
        canonical_url="https://svn.example/repo/branches/kr-fix",
        repository_relative_path="branches/kr-fix",
        bound_revision=101,
        copy_boundary_revision=90,
        effective_at=effective_at or NOW - timedelta(hours=1),
        daily_trigger_time=trigger,
        end_at=end_at,
    )


def services(tmp_path):
    database = tmp_path / "state" / "monitor.sqlite3"
    store = MonitorStore(database)
    tasks = MonitorTaskService(store, clock=lambda: NOW)
    gateway = FakeSchedulerGateway()
    scheduler = MonitorSchedulerService(
        store,
        gateway,
        database_path=database,
        working_directory=tmp_path,
        python_executable=Path(__file__).resolve(),
        run_as="DOMAIN\\qa",
        clock=lambda: NOW,
    )
    return store, tasks, gateway, scheduler, ScheduledMonitorTaskService(tasks, scheduler)


def finish_due_permanently(store, task_id, *, now=NOW + timedelta(hours=1)):
    error = MonitorPublicErrorPayload(
        code="MONITOR_CONFIGURATION_INVALID",
        stage="snapshot",
        message="测试中的确定性失败",
        retryable=False,
    )
    for run in store.list_due_runs(task_id, now):
        claim = store.claim_run(
            run.run_id,
            now=now,
            lease_for=timedelta(minutes=5),
            trigger="scheduled",
        )
        if claim is not None:
            store.finish_run(
                run.run_id,
                claim.lease_token,
                now=now,
                status="failed",
                errors=[error],
            )


def test_create_modify_pause_resume_end_and_generation_are_synchronized(tmp_path):
    store, tasks, gateway, scheduler, lifecycle = services(tmp_path)
    created = lifecycle.create(command())
    task_id = str(created.task_id)
    system_name = monitor_task_name(task_id)
    assert created.status.value == "active"
    assert created.scheduler.sync_status.value == "synced"
    actual = gateway.inspect(system_name)
    assert actual.exists and actual.enabled and actual.login_trigger
    assert f"--task-id {task_id}" in actual.arguments
    assert "--generation 1" in actual.arguments

    modified = lifecycle.modify_schedule(
        task_id, daily_trigger_time=time(19, 30), end_at=None
    )
    assert modified.scheduler.generation == 2
    actual = gateway.inspect(system_name)
    assert actual.daily_trigger_time == time(19, 30)
    assert "--generation 2" in actual.arguments

    paused = lifecycle.pause(task_id)
    assert paused.status.value == "paused"
    assert paused.scheduler.generation == 3
    assert paused.scheduler.sync_status.value == "pending"
    assert gateway.inspect(system_name).enabled is True
    assert ("run_now", system_name) in gateway.operations

    tasks.clock = lambda: NOW + timedelta(minutes=1)
    resumed = lifecycle.resume(task_id)
    assert resumed.status.value == "active"
    assert resumed.scheduler.generation == 4
    assert gateway.inspect(system_name).enabled is True

    tasks.clock = lambda: NOW + timedelta(minutes=2)
    ended = lifecycle.end(task_id)
    assert ended.status.value == "ended"
    assert ended.scheduler.sync_status.value == "pending"
    assert gateway.inspect(system_name).exists
    assert gateway.operations.count(("run_now", system_name)) == 2
    finish_due_permanently(store, task_id, now=NOW + timedelta(minutes=3))
    settled = scheduler.sync_task(
        task_id,
        expected_generation=ended.scheduler.generation,
        trigger_final=False,
    )
    assert settled.status == "not_present"
    assert not gateway.inspect(system_name).exists
    assert store.get_task(task_id).scheduler_last_synced_at == NOW


def test_sync_failure_preserves_new_configuration_and_exposes_only_public_error(tmp_path):
    store, tasks, gateway, scheduler, lifecycle = services(tmp_path)
    task = tasks.create(command(name="private password command stderr"))
    gateway.fail_next = "create_or_update"
    result = scheduler.sync_task(
        str(task.task_id), expected_generation=task.scheduler.generation
    )
    persisted = store.get_task(str(task.task_id))
    public = tasks.to_public_task(persisted)

    assert result.status == "error"
    assert persisted.name == "private password command stderr"
    assert persisted.scheduler_sync_status == "error"
    assert public.status.value == "scheduler_error"
    error = public.scheduler.last_error
    assert error.code.value == "MONITOR_SCHEDULER_SYNC_FAILED"
    dumped = error.model_dump_json()
    assert "command" not in dumped
    assert "stderr" not in dumped
    assert "password" not in dumped
    assert str(tmp_path) not in dumped


def test_paused_and_ended_keep_lifecycle_when_scheduler_sync_fails(tmp_path):
    store, tasks, gateway, scheduler, lifecycle = services(tmp_path)
    created = lifecycle.create(command())
    task_id = str(created.task_id)
    paused_pending = tasks.pause(task_id)
    gateway.fail_next = "create_or_update"
    scheduler.sync_task(task_id, expected_generation=paused_pending.scheduler.generation)
    paused = tasks.to_public_task(store.get_task(task_id))
    assert paused.status.value == "paused"
    assert paused.scheduler.sync_status.value == "error"
    assert paused.scheduler.last_error is not None

    finish_due_permanently(store, task_id)
    ended_pending = tasks.end(task_id)
    gateway.fail_next = "delete"
    scheduler.sync_task(task_id, expected_generation=ended_pending.scheduler.generation)
    ended = tasks.to_public_task(store.get_task(task_id))
    assert ended.status.value == "ended"
    assert ended.scheduler.sync_status.value == "error"
    assert ended.scheduler.last_error is not None


def test_inspection_detects_missing_action_trigger_identity_and_enabled_drift(tmp_path):
    store, tasks, gateway, scheduler, lifecycle = services(tmp_path)
    created = lifecycle.create(command())
    task_id = str(created.task_id)
    name = monitor_task_name(task_id)
    gateway.drift(
        name,
        arguments="-m other.module",
        working_directory=str(tmp_path / "elsewhere"),
        daily_trigger_time=time(5),
        run_as="DOMAIN\\other",
        enabled=False,
    )
    validation = scheduler.inspect_task(task_id)
    assert not validation.valid
    assert {
        "arguments",
        "working_directory",
        "daily_trigger_time",
        "run_as",
        "enabled",
    } <= set(validation.drift_fields)
    task = store.get_task(task_id)
    assert task.scheduler_sync_status == "drifted"
    assert task.scheduler_error.code.value == "MONITOR_SCHEDULER_SYNC_FAILED"

    gateway.tasks.pop(name)
    missing = scheduler.inspect_task(task_id)
    assert missing.drift_fields == ("missing",)


def test_pending_final_inspection_is_valid_for_paused_and_ended_tasks(tmp_path):
    store, tasks, gateway, scheduler, lifecycle = services(tmp_path)
    created = lifecycle.create(command())
    task_id = str(created.task_id)
    paused = lifecycle.pause(task_id)
    assert scheduler.inspect_task(task_id).valid
    assert store.get_task(task_id).scheduler_sync_status == "pending"

    ended = lifecycle.end(task_id)
    assert ended.status.value == "ended"
    assert scheduler.inspect_task(task_id).valid
    assert store.get_task(task_id).scheduler_sync_status == "pending"


def test_stale_sync_repairs_latest_generation_without_overwriting_store(tmp_path):
    store, tasks, gateway, scheduler, lifecycle = services(tmp_path)
    created = lifecycle.create(command())
    task_id = str(created.task_id)
    pending = tasks.modify_schedule(
        task_id, daily_trigger_time=time(19), end_at=None
    )
    assert pending.scheduler.generation == 2

    def advance_generation(_expected):
        gateway.on_create_or_update = None
        store.update_task(
            task_id,
            {
                "daily_trigger_time": "20:00:00",
                "generation": 3,
                "scheduler_sync_status": "pending",
                "scheduler_error": None,
            },
            NOW,
            expected_generation=2,
        )

    gateway.on_create_or_update = advance_generation
    stale = scheduler.sync_task(task_id, expected_generation=2)
    current = store.get_task(task_id)
    actual = gateway.inspect(monitor_task_name(task_id))
    assert stale.stale
    assert current.generation == 3
    assert current.daily_trigger_time == "20:00:00"
    assert current.scheduler_sync_status == "synced"
    assert actual.daily_trigger_time == time(20)
    assert "--generation 3" in actual.arguments


def test_same_generation_failure_then_success_converges_error_to_physical_synced(tmp_path):
    store, tasks, gateway, scheduler, lifecycle = services(tmp_path)
    created = lifecycle.create(command())
    task_id = str(created.task_id)
    pending = tasks.modify_schedule(
        task_id, daily_trigger_time=time(19), end_at=None
    )

    def fail_competing_sync(_expected):
        gateway.on_create_or_update = None
        gateway.fail_next = "create_or_update"
        failed = scheduler.sync_task(
            task_id, expected_generation=pending.scheduler.generation
        )
        assert failed.status == "error"

    gateway.on_create_or_update = fail_competing_sync
    succeeded = scheduler.sync_task(
        task_id, expected_generation=pending.scheduler.generation
    )
    assert succeeded.status == "synced"
    assert store.get_task(task_id).scheduler_sync_status == "synced"
    assert gateway.inspect(monitor_task_name(task_id)).daily_trigger_time == time(19)


def test_same_generation_success_then_failure_cannot_overwrite_synced_fact(tmp_path):
    store, tasks, gateway, scheduler, lifecycle = services(tmp_path)
    created = lifecycle.create(command())
    task_id = str(created.task_id)
    gateway.fail_next = "create_or_update"
    result = scheduler.sync_task(
        task_id, expected_generation=created.scheduler.generation
    )
    assert result.status == "synced"
    assert store.get_task(task_id).scheduler_sync_status == "synced"
    assert gateway.inspect(monitor_task_name(task_id)).exists


def test_fast_final_completion_cannot_be_overwritten_back_to_pending(tmp_path):
    store, tasks, gateway, scheduler, lifecycle = services(tmp_path)
    created = lifecycle.create(command())
    task_id = str(created.task_id)
    pending = tasks.pause(task_id)

    def complete_during_run_now(_name):
        gateway.on_run_now = None
        finish_due_permanently(store, task_id)
        settled = scheduler.sync_task(
            task_id,
            expected_generation=pending.scheduler.generation,
            trigger_final=False,
        )
        assert settled.status == "synced"

    gateway.on_run_now = complete_during_run_now
    result = scheduler.sync_task(
        task_id, expected_generation=pending.scheduler.generation
    )
    assert result.status == "synced"
    assert store.get_task(task_id).scheduler_sync_status == "synced"
    assert gateway.inspect(monitor_task_name(task_id)).enabled is False


def test_retryable_final_keeps_task_enabled_until_three_automatic_retries(tmp_path):
    store, tasks, gateway, scheduler, lifecycle = services(tmp_path)
    created = lifecycle.create(command())
    task_id = str(created.task_id)
    paused = lifecycle.pause(task_id)
    run = store.list_due_runs(task_id, NOW)[-1]
    retryable = MonitorPublicErrorPayload(
        code="MONITOR_SVN_TIMEOUT",
        stage="history",
        message="测试中的临时失败",
        retryable=True,
    )

    for index in range(4):
        claim = store.claim_run(
            run.run_id,
            now=NOW + timedelta(minutes=index),
            lease_for=timedelta(minutes=5),
            trigger="scheduled" if index == 0 else "automatic_retry",
        )
        assert claim is not None
        store.finish_run(
            run.run_id,
            claim.lease_token,
            now=NOW + timedelta(minutes=index),
            status="failed",
            errors=[retryable],
        )
        result = scheduler.sync_task(
            task_id,
            expected_generation=paused.scheduler.generation,
            trigger_final=False,
        )
        actual = gateway.inspect(monitor_task_name(task_id))
        if index < 3:
            assert result.status == "pending"
            assert actual.enabled is True
        else:
            assert result.status == "synced"
            assert actual.enabled is False


def test_inactive_task_keeps_retrying_older_missed_run_after_final_is_terminal(tmp_path):
    store, tasks, gateway, scheduler, lifecycle = services(tmp_path)
    created = lifecycle.create(command())
    task_id = str(created.task_id)
    pause_at = NOW + timedelta(hours=1, minutes=5)
    tasks.clock = lambda: pause_at
    scheduler.clock = lambda: pause_at
    paused = lifecycle.pause(task_id)
    scheduled, final = store.list_due_runs(task_id, pause_at)
    assert scheduled.boundary_type.value == "scheduled"
    assert final.boundary_type.value == "pause"

    retryable = MonitorPublicErrorPayload(
        code="MONITOR_SVN_TIMEOUT",
        stage="history",
        message="测试中的遗漏日报临时失败",
        retryable=True,
    )
    permanent = MonitorPublicErrorPayload(
        code="MONITOR_CONFIGURATION_INVALID",
        stage="snapshot",
        message="测试中的最终报告确定性终态",
        retryable=False,
    )
    for run, error in ((scheduled, retryable), (final, permanent)):
        claim = store.claim_run(
            run.run_id,
            now=pause_at,
            lease_for=timedelta(minutes=5),
            trigger="scheduled",
        )
        assert claim is not None
        store.finish_run(
            run.run_id,
            claim.lease_token,
            now=pause_at,
            status="failed",
            errors=[error],
        )

    pending = scheduler.sync_task(
        task_id,
        expected_generation=paused.scheduler.generation,
        trigger_final=False,
    )
    actual = gateway.inspect(monitor_task_name(task_id))
    assert pending.status == "pending"
    assert actual.enabled is True
    assert f"--generation {paused.scheduler.generation}" in actual.arguments

    end_at = pause_at + timedelta(minutes=5)
    tasks.clock = lambda: end_at
    scheduler.clock = lambda: end_at
    ended = lifecycle.end(task_id)
    actual = gateway.inspect(monitor_task_name(task_id))
    assert ended.scheduler.sync_status.value == "pending"
    assert actual.enabled is True
    assert f"--generation {ended.scheduler.generation}" in actual.arguments


def test_structured_xml_and_action_reject_injection_and_hold_frozen_settings(tmp_path):
    store, tasks, gateway, scheduler, lifecycle = services(tmp_path)
    attack = '"; calc.exe & <script>'
    created = lifecycle.create(command(name=attack))
    expected = scheduler.expected(store.get_task(str(created.task_id)))
    raw = scheduler_task_xml(expected)
    parsed = parse_scheduler_task_xml(expected.name, raw)
    text = raw.decode("utf-16")

    assert attack not in text
    assert "svn.example" not in text
    assert MONITOR_TASK_PREFIX in expected.name
    assert parsed.arguments == expected.action.arguments
    assert parsed.restart_interval == RESTART_INTERVAL
    assert parsed.restart_count == RESTART_COUNT
    assert parsed.execution_time_limit == EXECUTION_TIME_LIMIT
    assert parsed.multiple_instances_policy == MULTIPLE_INSTANCES_POLICY
    assert parsed.start_when_available is True
    declaration_mismatch = raw.decode("utf-16").encode("utf-8")
    assert (
        parse_scheduler_task_xml(expected.name, declaration_mismatch).arguments
        == expected.action.arguments
    )
    with pytest.raises(ValueError):
        scheduler.maintenance_expected(name="Unsafe & task")


@pytest.mark.parametrize(
    ("path", "attribute", "value", "drift_fields"),
    (
        (".//t:CalendarTrigger/t:Enabled", None, "false", ("daily_trigger_enabled",)),
        (".//t:LogonTrigger/t:Enabled", None, "false", ("login_trigger_enabled",)),
        (
            ".//t:LogonTrigger/t:UserId",
            None,
            "S-1-5-21-OTHER",
            ("login_trigger_user_id",),
        ),
        (".//t:TimeTrigger/t:Enabled", None, "false", ("end_trigger_enabled",)),
        (".//t:Principal/t:LogonType", None, "Password", ("logon_type",)),
        (".//t:Principal/t:RunLevel", None, "HighestAvailable", ("run_level",)),
        (
            ".//t:Actions",
            "Context",
            "OtherPrincipal",
            ("actions_context", "principal_binding"),
        ),
        (".//t:Principal", "id", "OtherPrincipal", ("principal_binding",)),
    ),
)
def test_xml_trigger_and_principal_drift_is_detected(
    tmp_path, path, attribute, value, drift_fields
):
    store, tasks, gateway, scheduler, lifecycle = services(tmp_path)
    created = lifecycle.create(command(end_at=NOW + timedelta(hours=1)))
    expected = scheduler.expected(store.get_task(str(created.task_id)))
    root = ET.fromstring(scheduler_task_xml(expected))
    element = root.find(path, {"t": TASK_NAMESPACE})
    assert element is not None
    if attribute is None:
        element.text = value
    else:
        element.set(attribute, value)
    actual = parse_scheduler_task_xml(
        expected.name,
        ET.tostring(root, encoding="utf-16", xml_declaration=True),
    )

    assert validate_scheduler_task(expected, actual).drift_fields == drift_fields


def test_configured_end_has_exact_one_time_trigger_and_terminal_cleanup(tmp_path):
    store, tasks, gateway, scheduler, lifecycle = services(tmp_path)
    end_at = NOW + timedelta(hours=1)
    created = lifecycle.create(command(end_at=end_at))
    task_id = str(created.task_id)
    expected = scheduler.expected(store.get_task(task_id))
    parsed = parse_scheduler_task_xml(expected.name, scheduler_task_xml(expected))
    assert parsed.end_trigger_at == end_at

    tasks.clock = lambda: end_at
    scheduler.clock = lambda: end_at
    tasks.materialize_due(task_id, now=end_at)
    ended = store.get_task(task_id)
    assert ended.lifecycle == "ended"
    pending = scheduler.sync_task(
        task_id,
        expected_generation=ended.generation,
        trigger_final=False,
    )
    assert pending.status == "pending"
    assert gateway.inspect(monitor_task_name(task_id)).enabled is True
    finish_due_permanently(store, task_id, now=end_at)
    status = reconcile_inactive_scheduler(
        task_id=task_id,
        database_path=store.database_path,
        working_directory=tmp_path,
        gateway=gateway,
    )
    assert status == "not_present"
    assert not gateway.inspect(monitor_task_name(task_id)).exists


def test_global_maintenance_ensure_inspect_repair_delete_are_idempotent(tmp_path):
    store, tasks, gateway, scheduler, lifecycle = services(tmp_path)
    ended = lifecycle.create(command())
    lifecycle.end(str(ended.task_id))
    assert all(task.lifecycle == "ended" for task in store.list_tasks())

    assert scheduler.ensure_maintenance().valid
    assert scheduler.ensure_maintenance().valid
    actual = gateway.inspect(MAINTENANCE_TASK_NAME)
    assert actual.exists and actual.enabled
    assert "--maintenance" in actual.arguments
    assert "--task-id" not in actual.arguments
    assert "--config" not in actual.arguments
    assert scheduler.inspect_maintenance().valid

    gateway.drift(MAINTENANCE_TASK_NAME, restart_count=1)
    assert not scheduler.inspect_maintenance().valid
    assert scheduler.ensure_maintenance().valid
    assert scheduler.delete_maintenance().exists is False
    assert scheduler.delete_maintenance().exists is False


def test_fake_enable_disable_run_and_command_failures_keep_fact_state(tmp_path):
    store, tasks, gateway, scheduler, lifecycle = services(tmp_path)
    created = lifecycle.create(command())
    name = monitor_task_name(str(created.task_id))
    assert gateway.disable(name).enabled is False
    with pytest.raises(SchedulerGatewayError):
        gateway.run_now(name)
    assert gateway.enable(name).enabled is True
    assert gateway.run_now(name).exists
    gateway.fail_next = "inspect"
    validation = scheduler.inspect_task(str(created.task_id))
    assert validation.drift_fields == ("inspection_failed",)
    assert store.get_task(str(created.task_id)).scheduler_sync_status == "error"


def test_maintenance_cli_does_not_read_missing_application_config(tmp_path):
    database = tmp_path / "only-store" / "monitor.sqlite3"
    assert runner_main(
        [
            "--maintenance",
            "--database",
            str(database),
            "--config",
            str(tmp_path / "does-not-exist.json"),
        ]
    ) == 0
    assert database.exists()


def _pending_cli_task(tmp_path):
    now = datetime.now(UTC).replace(microsecond=0)
    cutoff = now - timedelta(minutes=5)
    created_at = cutoff - timedelta(minutes=5)
    effective_at = cutoff - timedelta(minutes=10)
    trigger = cutoff.astimezone(ZoneInfo("Asia/Shanghai")).time().replace(tzinfo=None)
    database = tmp_path / "runner-state" / "monitor.sqlite3"
    store = MonitorStore(database)
    tasks = MonitorTaskService(store, clock=lambda: created_at)
    task = tasks.create(command(trigger=trigger, effective_at=effective_at))
    assert store.list_runs(str(task.task_id)) == []
    return database, task, cutoff


def test_scheduler_runner_missing_config_materializes_and_records_permanent_failure(
    tmp_path, monkeypatch, capsys
):
    database, task, cutoff = _pending_cli_task(tmp_path)
    config = tmp_path / "private-missing-settings.json"
    provider_calls = []
    monkeypatch.setattr(
        "app.monitor_runner.provider_from_config",
        lambda value: provider_calls.append(value),
    )

    assert runner_main(
        [
            "--task-id",
            str(task.task_id),
            "--generation",
            str(task.scheduler.generation),
            "--database",
            str(database),
            "--config",
            str(config),
            "--scheduler-managed",
        ]
    ) == 0

    store = MonitorStore(database)
    runs = store.list_runs(str(task.task_id))
    assert len(runs) == 1
    run = runs[0]
    assert run.end_at == cutoff
    assert run.status == "failed"
    assert run.attempt_count == 1
    assert run.errors[0].code.value == "MONITOR_CONFIGURATION_INVALID"
    assert run.errors[0].stage.value == "snapshot"
    assert run.errors[0].retryable is False
    assert run.report_ref is None
    assert store.attempts(run.run_id)[0]["trigger"] == "scheduled"
    assert provider_calls == []
    captured = capsys.readouterr()
    public_output = captured.out + captured.err
    assert "Traceback" not in public_output
    assert str(config) not in public_output
    assert "private-missing" not in public_output
    assert not (database.parent / "reports" / str(task.task_id) / "history").exists()


def test_scheduler_runner_missing_config_without_due_run_is_noop(
    tmp_path, monkeypatch, capsys
):
    now = datetime.now(UTC).replace(microsecond=0)
    database = tmp_path / "noop-state" / "monitor.sqlite3"
    store = MonitorStore(database)
    tasks = MonitorTaskService(store, clock=lambda: now)
    task = tasks.create(command(effective_at=now))
    monkeypatch.setattr(
        "app.monitor_runner.provider_from_config",
        lambda value: pytest.fail("SVN provider must not be constructed"),
    )
    config = tmp_path / "private-noop-settings.json"

    assert runner_main(
        [
            "--task-id",
            str(task.task_id),
            "--generation",
            str(task.scheduler.generation),
            "--database",
            str(database),
            "--config",
            str(config),
            "--scheduler-managed",
        ]
    ) == 0
    assert MonitorStore(database).list_runs(str(task.task_id)) == []
    captured = capsys.readouterr()
    assert "Traceback" not in captured.out + captured.err
    assert str(config) not in captured.out + captured.err


def test_manual_retry_invalid_config_reuses_original_run_and_interval(
    tmp_path, monkeypatch, capsys
):
    database, task, cutoff = _pending_cli_task(tmp_path)
    store = MonitorStore(database)
    MonitorTaskService(store).materialize_due(str(task.task_id), now=cutoff)
    original = store.list_runs(str(task.task_id))[0]
    config = tmp_path / "private-invalid-settings.json"
    config.write_text("{private invalid json", encoding="utf-8")
    monkeypatch.setattr(
        "app.monitor_runner.provider_from_config",
        lambda value: pytest.fail("SVN provider must not be constructed"),
    )

    assert runner_main(
        [
            "--run-id",
            original.run_id,
            "--database",
            str(database),
            "--config",
            str(config),
        ]
    ) == 1

    failed = MonitorStore(database).get_run(original.run_id)
    assert failed is not None
    assert (failed.start_at, failed.end_at) == (original.start_at, original.end_at)
    assert failed.status == "failed"
    assert failed.attempt_count == 1
    assert failed.errors[0].code.value == "MONITOR_CONFIGURATION_INVALID"
    assert MonitorStore(database).attempts(original.run_id)[0]["trigger"] == "manual_retry"
    assert failed.report_ref is None
    captured = capsys.readouterr()
    public_output = captured.out + captured.err
    assert "Traceback" not in public_output
    assert str(config) not in public_output
    assert "private invalid" not in public_output


def test_materialize_error_records_only_next_cutoff_without_publishing_or_skipping(
    tmp_path, monkeypatch, capsys
):
    database, task, cutoff = _pending_cli_task(tmp_path)
    config = tmp_path / "private-missing-settings.json"

    def fail_materialize(self, task_id, *, now=None):
        raise MonitorScheduleError("private recovery window details")

    monkeypatch.setattr(MonitorTaskService, "materialize_due", fail_materialize)
    assert runner_main(
        [
            "--task-id",
            str(task.task_id),
            "--generation",
            str(task.scheduler.generation),
            "--database",
            str(database),
            "--config",
            str(config),
            "--scheduler-managed",
        ]
    ) == 0

    store = MonitorStore(database)
    runs = store.list_runs(str(task.task_id))
    boundaries = store.list_boundaries(str(task.task_id))
    assert len(runs) == 1
    assert runs[0].end_at == cutoff
    assert runs[0].status == "failed"
    assert runs[0].errors[0].code.value == "MONITOR_CONFIGURATION_INVALID"
    assert runs[0].report_ref is None
    assert boundaries[-1].boundary_at == cutoff
    assert all(boundary.boundary_at <= cutoff for boundary in boundaries)
    assert not (database.parent / "reports" / str(task.task_id) / "history").exists()
    captured = capsys.readouterr()
    public_output = captured.out + captured.err
    assert "Traceback" not in public_output
    assert "private recovery" not in public_output


def test_real_recovery_limit_failure_records_first_cutoff_without_skipping(tmp_path):
    store = MonitorStore(tmp_path / "recovery-limit" / "monitor.sqlite3")
    tasks = MonitorTaskService(store, clock=lambda: NOW)
    task = tasks.create(command(effective_at=NOW))
    future = NOW + timedelta(days=31)
    runner = MonitorRunnerService(
        store,
        tasks,
        lambda record: pytest.fail("report engine must not run"),
        clock=lambda: future,
    )

    result = runner.run_task(str(task.task_id), task.scheduler.generation)

    assert result.exit_category == "permanent_failure"
    runs = store.list_runs(str(task.task_id))
    assert len(runs) == 1
    assert runs[0].start_at == NOW
    assert runs[0].end_at == NOW + timedelta(hours=1)
    assert runs[0].status == "failed"
    assert runs[0].attempt_count == 1
    assert runs[0].errors[0].code.value == "MONITOR_CONFIGURATION_INVALID"
    assert runs[0].report_ref is None
    assert store.latest_boundary(str(task.task_id)).boundary_at == runs[0].end_at


def test_recovery_limit_uses_schedule_effective_anchor_after_trigger_change(tmp_path):
    store = MonitorStore(tmp_path / "schedule-anchor" / "monitor.sqlite3")
    clock = [NOW]
    tasks = MonitorTaskService(store, clock=lambda: clock[0])
    task = tasks.create(command(effective_at=NOW))
    clock[0] = NOW + timedelta(minutes=30)
    changed = tasks.modify_schedule(
        str(task.task_id),
        daily_trigger_time=time(17, 15),
        end_at=None,
    )
    future = clock[0] + timedelta(days=31)
    runner = MonitorRunnerService(
        store,
        tasks,
        lambda record: pytest.fail("report engine must not run"),
        clock=lambda: future,
    )

    result = runner.run_task(str(task.task_id), changed.scheduler.generation)

    assert result.exit_category == "permanent_failure"
    run = store.list_runs(str(task.task_id))[0]
    assert run.end_at == datetime(2026, 8, 11, 9, 15, tzinfo=UTC)
    assert run.end_at > clock[0]


def test_recovery_limit_final_end_transitions_task_atomically(tmp_path):
    store = MonitorStore(tmp_path / "recovery-end" / "monitor.sqlite3")
    tasks = MonitorTaskService(store, clock=lambda: NOW)
    end_at = NOW + timedelta(minutes=30)
    task = tasks.create(command(effective_at=NOW, end_at=end_at))
    runner = MonitorRunnerService(
        store,
        tasks,
        lambda record: pytest.fail("report engine must not run"),
        clock=lambda: NOW + timedelta(days=31),
    )

    result = runner.run_task(str(task.task_id), task.scheduler.generation)

    persisted = store.get_task(str(task.task_id))
    assert result.exit_category == "permanent_failure"
    assert persisted is not None
    assert persisted.lifecycle == "ended"
    assert persisted.generation == task.scheduler.generation + 1
    assert persisted.scheduler_desired_state == "disabled"
    assert store.list_runs(str(task.task_id))[0].end_at == end_at


def test_materialize_fallback_does_not_claim_same_cutoff_from_new_generation(
    tmp_path, monkeypatch
):
    store = MonitorStore(tmp_path / "fallback-generation" / "monitor.sqlite3")
    tasks = MonitorTaskService(store, clock=lambda: NOW)
    task = tasks.create(command(effective_at=NOW))
    original_append = store.append_boundaries

    def concurrent_append(task_id, specs, now, **kwargs):
        store.update_task(
            task_id,
            {"generation": task.scheduler.generation + 1},
            now,
            expected_generation=task.scheduler.generation,
            expected_lifecycle="active",
        )
        original_append(
            task_id,
            [
                BoundarySpec(
                    spec.boundary_at,
                    spec.boundary_type,
                    task.scheduler.generation + 1,
                    "concurrent_generation",
                )
                for spec in specs
            ],
            now,
            expected_generation=task.scheduler.generation + 1,
            expected_lifecycle="active",
        )
        raise MonitorStateConflict("concurrent generation won")

    monkeypatch.setattr(store, "append_boundaries", concurrent_append)
    runner = MonitorRunnerService(
        store,
        tasks,
        lambda record: pytest.fail("report engine must not run"),
        clock=lambda: NOW + timedelta(days=31),
    )

    result = runner.run_task(str(task.task_id), task.scheduler.generation)

    runs = store.list_runs(str(task.task_id))
    assert result.exit_category == "noop"
    assert len(runs) == 1
    assert runs[0].generation == task.scheduler.generation + 1
    assert runs[0].status == "queued"
    assert runs[0].attempt_count == 0


def test_existing_due_run_keeps_own_engine_error_when_materialize_fails(tmp_path):
    created_at = NOW + timedelta(hours=1)
    store = MonitorStore(tmp_path / "existing-due" / "monitor.sqlite3")
    tasks = MonitorTaskService(store, clock=lambda: created_at)
    task = tasks.create(command(effective_at=NOW))
    original = store.list_runs(str(task.task_id))[0]

    class AuthFailureEngine:
        def execute(self, run, task_record, generated_at):
            raise SVNProviderError("SVN_AUTH_FAILED", "private authentication")

    runner = MonitorRunnerService(
        store,
        tasks,
        lambda record: AuthFailureEngine(),
        clock=lambda: created_at + timedelta(days=31),
    )

    result = runner.run_task(str(task.task_id), task.scheduler.generation)

    runs = store.list_runs(str(task.task_id))
    persisted_original = store.get_run(original.run_id)
    assert result.exit_category == "permanent_failure"
    assert len(runs) == 2
    assert persisted_original is not None
    assert persisted_original.errors[0].code.value == "MONITOR_SVN_AUTH_FAILED"
    synthesized = next(run for run in runs if run.run_id != original.run_id)
    assert synthesized.errors[0].code.value == "MONITOR_CONFIGURATION_INVALID"


def test_unknown_materialize_error_is_retryable_without_fake_counts_or_attempts(
    tmp_path, monkeypatch
):
    database, task, _ = _pending_cli_task(tmp_path)
    store = MonitorStore(database)
    tasks = MonitorTaskService(store)

    def fail_materialize(task_id, *, now=None):
        raise OSError("private transient store failure")

    monkeypatch.setattr(tasks, "materialize_due", fail_materialize)
    runner = MonitorRunnerService(
        store,
        tasks,
        lambda record: pytest.fail("report engine must not run"),
    )

    result = runner.run_task(str(task.task_id), task.scheduler.generation)

    assert result.exit_category == "temporary_failure"
    assert result.processed == result.failed == 0
    assert result.retryable_failures == 1
    assert store.list_runs(str(task.task_id)) == []


def test_scheduler_managed_unknown_materialize_error_exits_for_windows_retry(
    tmp_path, monkeypatch, capsys
):
    database, task, _ = _pending_cli_task(tmp_path)

    def fail_materialize(self, task_id, *, now=None):
        raise OSError("private transient store failure")

    monkeypatch.setattr(MonitorTaskService, "materialize_due", fail_materialize)
    exit_code = runner_main(
        [
            "--task-id",
            str(task.task_id),
            "--generation",
            str(task.scheduler.generation),
            "--database",
            str(database),
            "--config",
            str(tmp_path / "private-missing-settings.json"),
            "--scheduler-managed",
        ]
    )

    captured = capsys.readouterr()
    payload = json.loads(captured.out)
    assert exit_code == 75
    assert payload["processed"] == payload["failed"] == 0
    assert payload["retryable_failures"] == 1
    assert MonitorStore(database).list_runs(str(task.task_id)) == []
    assert "Traceback" not in captured.out + captured.err
    assert "private transient" not in captured.out + captured.err


def test_materialize_error_without_due_cutoff_is_noop_and_keeps_boundary(tmp_path, monkeypatch):
    store = MonitorStore(tmp_path / "materialize-noop" / "monitor.sqlite3")
    tasks = MonitorTaskService(store, clock=lambda: NOW)
    task = tasks.create(command(effective_at=NOW))
    original_boundary = store.latest_boundary(str(task.task_id))

    def fail_materialize(task_id, *, now=None):
        raise MonitorScheduleError("private not-due details")

    monkeypatch.setattr(tasks, "materialize_due", fail_materialize)
    runner = MonitorRunnerService(
        store,
        tasks,
        lambda record: pytest.fail("report engine must not run"),
        clock=lambda: NOW,
    )

    result = runner.run_task(str(task.task_id), task.scheduler.generation)

    assert result.exit_category == "noop"
    assert store.list_runs(str(task.task_id)) == []
    assert store.latest_boundary(str(task.task_id)) == original_boundary


def test_isolated_cli_state_root_rejects_production_and_is_uuid_scoped(tmp_path):
    test_id = uuid4()
    database = _isolated_database(test_id)
    assert str(test_id) in str(database)
    assert database.name == "monitor.sqlite3"
    assert Path(database).parent.parent == Path(tempfile.gettempdir()).resolve()


def test_windows_identity_and_scheduler_binary_come_from_trusted_system_sources():
    assert current_windows_user().startswith("S-1-")
    schtasks = Path(_system_executable("schtasks.exe"))
    assert schtasks.is_absolute()
    assert schtasks.name.casefold() == "schtasks.exe"
    assert schtasks.parent.name.casefold() == "system32"


def test_task_id_is_normalized_before_persistence_and_system_naming(tmp_path):
    store, tasks, gateway, scheduler, lifecycle = services(tmp_path)
    canonical = uuid4()
    created = lifecycle.create(command(task_id=f"{{{str(canonical).upper()}}}"))
    assert str(created.task_id) == str(canonical)
    assert store.get_task(str(canonical)) is not None
    assert gateway.inspect(monitor_task_name(canonical)).exists


def test_tampered_stored_task_name_cannot_expand_scheduler_operation_scope(tmp_path):
    store, tasks, gateway, scheduler, lifecycle = services(tmp_path)
    created = lifecycle.create(command())
    task_id = str(created.task_id)
    operations = list(gateway.operations)
    with store._connect() as connection:
        connection.execute(
            "UPDATE monitor_tasks SET windows_task_name=? WHERE task_id=?",
            ("ExcelMerge-M3-Monitor-00000000-0000-4000-8000-000000000000", task_id),
        )
    with pytest.raises(SchedulerGatewayError):
        scheduler.sync_task(task_id, expected_generation=created.scheduler.generation)
    assert gateway.operations == operations


def test_standalone_maintenance_cleans_ended_task_and_keeps_latest(tmp_path):
    store, tasks, gateway, scheduler, lifecycle = services(tmp_path)
    created = lifecycle.create(command())
    task_id = str(created.task_id)
    lifecycle.end(task_id)
    assert {task.lifecycle for task in store.list_tasks()} == {"ended"}

    example = (
        Path(__file__).parents[2]
        / "docs"
        / "contracts"
        / "m3.monitor-report.v1.example.json"
    )
    data = json.loads(example.read_text(encoding="utf-8"))
    data["task_id"] = task_id
    report = MonitorReportPayload.model_validate(data)
    canonical = serialize_monitor_json(report)
    html = render_monitor_report_html(report)
    draft = ReportDraft(
        payload=report,
        canonical_json=canonical,
        offline_html=html,
        report_ref=report_reference(report.report_id),
        json_sha256=hashlib.sha256(canonical).hexdigest(),
        html_sha256=hashlib.sha256(html).hexdigest(),
        report_expires_at=report.generated_at + REPORT_RETENTION,
    )
    publisher = FileSystemMonitorReportPublisher(
        store.database_path.parent / "reports"
    )
    publisher.publish_history(draft)
    publisher.activate_latest(draft)
    history = (
        store.database_path.parent
        / "reports"
        / task_id
        / "history"
        / "20260810-180000.html"
    )
    latest = history.parent.parent / "latest.html"
    assert history.exists() and latest.exists()

    result = run_maintenance(
        database_path=store.database_path,
        now=report.generated_at + timedelta(days=31),
    )
    assert result.task_count == 1
    assert result.cleaned_artifact_count == 1
    assert result.failed_task_count == 0
    assert not history.exists()
    assert latest.exists()
