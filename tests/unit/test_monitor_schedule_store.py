from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, time, timedelta, timezone
import sqlite3
from uuid import uuid4

import pytest

from app.schemas.monitor import MonitorPublicErrorPayload
from app.services.monitor_schedule import (
    BoundarySpec,
    BoundaryType,
    MonitorScheduleError,
    scheduled_boundaries,
)
from app.services.monitor_store import (
    MIGRATION_1,
    MonitorLeaseLost,
    MonitorStateConflict,
    MonitorStore,
)
from app.services.monitor_task_service import CreateMonitorTask, MonitorTaskService


UTC = timezone.utc


class Clock:
    def __init__(self, value: datetime):
        self.value = value

    def __call__(self):
        return self.value


def at(day: int, hour: int, minute: int = 0) -> datetime:
    return datetime(2026, 8, day, hour, minute, tzinfo=UTC)


def command(*, effective_at: datetime, trigger=time(18), end_at=None, task_id=None):
    return CreateMonitorTask(
        task_id=task_id,
        name="KR daily",
        endpoint_id="kr-fix",
        branch_label="KR Fix",
        repository_uuid="20000000-0000-4000-8000-000000000001",
        canonical_url="https://svn.example/repo/branches/kr-fix",
        repository_relative_path="branches/kr-fix",
        bound_revision=110,
        copy_boundary_revision=90,
        effective_at=effective_at,
        daily_trigger_time=trigger,
        end_at=end_at,
    )


@pytest.fixture
def state(tmp_path):
    clock = Clock(at(10, 10))
    store = MonitorStore(tmp_path / "isolated" / "monitor.sqlite3")
    return store, MonitorTaskService(store, clock=clock), clock


def test_database_is_isolated_versioned_wal_and_foreign_keys(tmp_path):
    path = tmp_path / "m3" / "monitor.sqlite3"
    store = MonitorStore(path)
    assert path.exists()
    assert not (tmp_path / "var" / "m2-batch").exists()
    with store._connect() as connection:
        assert connection.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
        assert connection.execute("PRAGMA foreign_keys").fetchone()[0] == 1
        assert [row[0] for row in connection.execute(
            "SELECT version FROM monitor_schema_migrations ORDER BY version"
        ).fetchall()] == [1, 2, 3, 4, 5, 6]
        assert connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='monitor_commands'"
        ).fetchone()
        assert connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='monitor_retry_outbox'"
        ).fetchone()
        assert connection.execute(
            """SELECT 1 FROM sqlite_master
               WHERE type='index' AND name='monitor_retry_outbox_run_active_idx'"""
        ).fetchone()
        columns = {
            row[1] for row in connection.execute("PRAGMA table_info(monitor_tasks)")
        }
        assert "ended_reason" in columns
        assert {
            row[1]
            for row in connection.execute(
                "PRAGMA table_info(monitor_run_publications)"
            )
        } >= {"run_id", "state", "json_sha256", "html_sha256"}


def test_version_one_database_migrates_without_recreating_tables(tmp_path):
    path = tmp_path / "upgrade" / "monitor.sqlite3"
    path.parent.mkdir(parents=True)
    connection = sqlite3.connect(path)
    connection.execute(
        "CREATE TABLE monitor_schema_migrations "
        "(version INTEGER PRIMARY KEY, applied_at TEXT NOT NULL)"
    )
    for statement in MIGRATION_1:
        connection.execute(statement)
    connection.execute(
        "INSERT INTO monitor_schema_migrations(version, applied_at) VALUES (1, ?)",
        ("2026-08-10T00:00:00.000000Z",),
    )
    connection.commit()
    connection.close()

    store = MonitorStore(path)
    with store._connect() as upgraded:
        assert [row[0] for row in upgraded.execute(
            "SELECT version FROM monitor_schema_migrations ORDER BY version"
        )] == [1, 2, 3, 4, 5, 6]
        assert "ended_reason" in {
            row[1] for row in upgraded.execute("PRAGMA table_info(monitor_tasks)")
        }
        assert upgraded.execute(
            "SELECT COUNT(*) FROM monitor_run_publications"
        ).fetchone()[0] == 0


def test_version_six_migration_collapses_legacy_duplicate_active_retries(tmp_path):
    path = tmp_path / "retry-upgrade" / "monitor.sqlite3"
    store = MonitorStore(path)
    clock = Clock(at(10, 10))
    tasks = MonitorTaskService(store, clock=clock)
    task = tasks.create(command(effective_at=at(10, 9)))
    clock.value = at(10, 11)
    tasks.pause(str(task.task_id))
    run = store.list_runs(str(task.task_id))[-1]
    claim = store.claim_run(
        run.run_id,
        now=clock.value,
        lease_for=timedelta(minutes=5),
        trigger="manual_retry",
    )
    store.finish_run(
        run.run_id,
        claim.lease_token,
        now=clock.value,
        status="failed",
        errors=[
            MonitorPublicErrorPayload(
                code="MONITOR_PARSE_FAILED",
                stage="csv_parse",
                message="parse failed",
                retryable=False,
            )
        ],
    )
    first = str(uuid4())
    second = str(uuid4())
    store.accept_retry_intent(
        request_id=first,
        run_id=run.run_id,
        method="POST",
        target=f"POST /api/monitor/runs/{run.run_id}/retry",
        payload_hash="first",
        payload_json="{}",
        response_status=202,
        response_json="{}",
        conflict_response_json=(
            '{"error":{"code":"MONITOR_STATE_CONFLICT",'
            '"message":"当前运行状态不允许人工重试"}}'
        ),
        now=clock.value,
    )
    with store._transaction(write=True) as connection:
        connection.execute("DROP INDEX monitor_retry_outbox_run_active_idx")
        connection.execute("DELETE FROM monitor_schema_migrations WHERE version=6")
        timestamp = clock.value.isoformat().replace("+00:00", "Z")
        connection.execute(
            """INSERT INTO monitor_commands
               (request_id,method,target,payload_hash,payload_json,state,
                response_status,response_json,created_at,updated_at)
               VALUES (?,?,?,?,?,'completed',202,'{}',?,?)""",
            (
                second,
                "POST",
                f"POST /api/monitor/runs/{run.run_id}/retry",
                "second",
                "{}",
                timestamp,
                timestamp,
            ),
        )
        connection.execute(
            """INSERT INTO monitor_retry_outbox
               (request_id,task_id,run_id,state,created_at,updated_at)
               VALUES (?,?,?,'pending',?,?)""",
            (second, str(task.task_id), run.run_id, timestamp, timestamp),
        )

    upgraded = MonitorStore(path)
    with upgraded._connect() as connection:
        assert connection.execute(
            """SELECT COUNT(*) FROM monitor_retry_outbox
               WHERE run_id=? AND state IN ('pending','dispatching')""",
            (run.run_id,),
        ).fetchone()[0] == 1
        assert connection.execute(
            "SELECT COUNT(*) FROM monitor_retry_outbox WHERE run_id=?",
            (run.run_id,),
        ).fetchone()[0] == 2
        assert connection.execute(
            """SELECT 1 FROM sqlite_master
               WHERE type='index' AND name='monitor_retry_outbox_run_active_idx'"""
        ).fetchone()


def test_migration_fails_closed_for_legacy_noncanonical_task_uuid(tmp_path):
    path = tmp_path / "unsafe-upgrade" / "monitor.sqlite3"
    path.parent.mkdir(parents=True)
    connection = sqlite3.connect(path)
    connection.execute(
        "CREATE TABLE monitor_schema_migrations "
        "(version INTEGER PRIMARY KEY, applied_at TEXT NOT NULL)"
    )
    for statement in MIGRATION_1:
        connection.execute(statement)
    connection.execute(
        """INSERT INTO monitor_tasks (
            task_id,name,lifecycle,endpoint_id,branch_label,repository_uuid,
            canonical_url,repository_relative_path,bound_revision,
            copy_boundary_revision,effective_at,schedule_effective_at,end_at,
            daily_trigger_time,timezone,generation,scheduler_desired_state,
            scheduler_sync_status,created_at,updated_at
        ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (
            "{10000000-0000-4000-8000-000000000001}",
            "legacy",
            "active",
            "endpoint",
            "Branch",
            "20000000-0000-4000-8000-000000000001",
            "https://svn.example/repo/branches/test",
            "branches/test",
            10,
            1,
            "2026-08-10T00:00:00.000000Z",
            "2026-08-10T00:00:00.000000Z",
            None,
            "18:00:00",
            "Asia/Shanghai",
            1,
            "enabled",
            "pending",
            "2026-08-10T00:00:00.000000Z",
            "2026-08-10T00:00:00.000000Z",
        ),
    )
    connection.execute(
        "INSERT INTO monitor_schema_migrations(version, applied_at) VALUES (1, ?)",
        ("2026-08-10T00:00:00.000000Z",),
    )
    connection.commit()
    connection.close()

    with pytest.raises(RuntimeError, match="non-canonical task identity"):
        MonitorStore(path)


def test_expired_lease_cannot_renew_finish_or_prepare_and_new_worker_takes_over(
    state,
):
    store, service, clock = state
    task_id = str(service.create(command(effective_at=at(10, 9))).task_id)
    run_id = store.list_runs(task_id)[0].run_id
    first = store.claim_run(
        run_id,
        now=clock.value,
        lease_for=timedelta(minutes=1),
        trigger="scheduled",
    )
    clock.value += timedelta(minutes=2)
    assert not store.renew_lease(
        run_id,
        first.lease_token,
        now=clock.value,
        lease_for=timedelta(minutes=1),
    )
    error = MonitorPublicErrorPayload(
        code="MONITOR_REPORT_PUBLISH_FAILED",
        stage="report_publish",
        message="locked",
        retryable=True,
    )
    with pytest.raises(MonitorLeaseLost):
        store.finish_run(
            run_id,
            first.lease_token,
            now=clock.value,
            status="failed",
            errors=[error],
        )
    second = store.claim_run(
        run_id,
        now=clock.value,
        lease_for=timedelta(minutes=5),
        trigger="automatic_retry",
    )
    assert second is not None
    with pytest.raises(MonitorLeaseLost):
        store.prepare_publication(
            run_id,
            first.lease_token,
            now=clock.value,
            status="succeeded",
            start_revision=100,
            end_revision=101,
            summary={
                "workbook_count": 1,
                "changed_workbook_count": 0,
                "change_count": 0,
                "error_count": 0,
            },
            errors=[],
            report_ref="m3r_abcdefghijklmnopqrstuv",
            json_sha256="a" * 64,
            html_sha256="b" * 64,
            report_expires_at=clock.value + timedelta(days=30),
        )


def test_publication_manifest_is_idempotent_conflict_safe_and_finalized_atomically(
    state,
):
    store, service, clock = state
    task_id = str(service.create(command(effective_at=at(10, 9))).task_id)
    run_id = store.list_runs(task_id)[0].run_id
    claim = store.claim_run(
        run_id,
        now=clock.value,
        lease_for=timedelta(minutes=5),
        trigger="scheduled",
    )
    values = {
        "status": "succeeded",
        "start_revision": 100,
        "end_revision": 101,
        "summary": {
            "workbook_count": 1,
            "changed_workbook_count": 0,
            "change_count": 0,
            "error_count": 0,
        },
        "errors": [],
        "report_ref": "m3r_abcdefghijklmnopqrstuv",
        "json_sha256": "a" * 64,
        "html_sha256": "b" * 64,
        "report_expires_at": clock.value + timedelta(days=30),
    }
    first = store.prepare_publication(
        run_id, claim.lease_token, now=clock.value, **values
    )
    again = store.prepare_publication(
        run_id, claim.lease_token, now=clock.value, **values
    )
    assert first == again
    with pytest.raises(MonitorStateConflict):
        store.prepare_publication(
            run_id,
            claim.lease_token,
            now=clock.value,
            **{**values, "html_sha256": "c" * 64},
        )
    finished = store.finalize_publication(
        run_id, claim.lease_token, now=clock.value
    )
    assert finished.status == "succeeded"
    assert finished.report_ref == values["report_ref"]
    assert store.get_publication(run_id).state == "activated"


def test_first_short_interval_is_left_open_right_closed_and_public_is_unsynced(state):
    store, service, _ = state
    task = service.create(command(effective_at=at(10, 9, 30)))
    runs = store.list_runs(str(task.task_id))
    assert [(run.start_at, run.end_at) for run in runs] == [(at(10, 9, 30), at(10, 10))]
    public = service.public_run(runs[0].run_id)
    assert public.interval.start_inclusive is False
    assert public.interval.end_inclusive is True
    assert public.status == "queued"
    assert task.status == "syncing"
    assert task.scheduler.sync_status == "pending"
    assert task.scheduler.desired_state == "enabled"


def test_task_page_translates_public_statuses_and_keeps_query_cursor_scope(state):
    store, service, clock = state

    def create_named(name: str) -> str:
        base = command(effective_at=at(10, 9), task_id=str(uuid4()))
        return str(
            service.create(
                CreateMonitorTask(**{**base.__dict__, "name": name})
            ).task_id
        )

    active_id = create_named("Target active")
    active = store.get_task(active_id)
    store.update_task(
        active_id,
        {
            "scheduler_sync_status": "synced",
            "scheduler_last_synced_at": clock.value,
        },
        clock.value,
        expected_generation=active.generation,
        expected_lifecycle="active",
    )
    syncing_id = create_named("Target syncing")
    error_id = create_named("Target scheduler error")
    error_task = store.get_task(error_id)
    store.update_task(
        error_id,
        {
            "scheduler_sync_status": "error",
            "scheduler_error": MonitorPublicErrorPayload(
                code="MONITOR_SCHEDULER_SYNC_FAILED",
                stage="scheduler",
                message="计划任务同步失败",
                retryable=True,
            ),
        },
        clock.value,
        expected_generation=error_task.generation,
        expected_lifecycle="active",
    )
    paused_id = create_named("Target paused")
    service.pause(paused_id)

    assert [item.task_id for item in store.list_task_page(
        limit=10, statuses=["active"]
    )] == [active_id]
    assert [item.task_id for item in store.list_task_page(
        limit=10, statuses=["syncing"]
    )] == [syncing_id]
    assert [item.task_id for item in store.list_task_page(
        limit=10, statuses=["scheduler_error"]
    )] == [error_id]
    combined = store.list_task_page(
        limit=10,
        statuses=["active", "paused", "scheduler_error"],
        query="target",
    )
    assert {item.task_id for item in combined} == {
        active_id,
        paused_id,
        error_id,
    }
    first = store.list_task_page(
        limit=2,
        statuses=["active", "paused", "scheduler_error"],
        query="target",
    )
    second = store.list_task_page(
        limit=2,
        statuses=["active", "paused", "scheduler_error"],
        query="target",
        before_created_at=first[-1].created_at,
        before_task_id=first[-1].task_id,
    )
    assert len(first) == 2
    assert {item.task_id for item in first}.isdisjoint(
        {item.task_id for item in second}
    )
    with pytest.raises(ValueError, match="unknown public"):
        store.list_task_page(limit=10, statuses=["not-a-status"])


def test_shanghai_daily_cutoffs_cross_utc_date_and_end_is_unique():
    specs = scheduled_boundaries(
        after=at(9, 15),
        due_at=at(11, 11),
        trigger=time(0, 30),
        generation=1,
        end_at=at(11, 10, 30),
    )
    assert [(item.boundary_at, item.boundary_type) for item in specs] == [
        (at(9, 16, 30), BoundaryType.SCHEDULED),
        (at(10, 16, 30), BoundaryType.SCHEDULED),
        (at(11, 10, 30), BoundaryType.END),
    ]
    same = scheduled_boundaries(
        after=at(9, 9), due_at=at(10, 10), trigger=time(18),
        generation=1, end_at=at(10, 10),
    )
    assert len(same) == 2
    assert same[-1].boundary_type == BoundaryType.SCHEDULED


def test_pause_resume_skips_paused_period_and_pause_run_is_short(state):
    store, service, clock = state
    task = service.create(command(effective_at=at(10, 9)))
    task_id = str(task.task_id)
    clock.value = at(10, 11)
    paused = service.pause(task_id)
    assert paused.status == "paused"
    assert [run.boundary_type for run in store.list_runs(task_id)] == [
        BoundaryType.SCHEDULED, BoundaryType.PAUSE,
    ]
    clock.value = at(11, 1)
    resumed = service.resume(task_id)
    assert resumed.status == "syncing"
    clock.value = at(11, 10)
    service.materialize_due(task_id)
    runs = store.list_runs(task_id)
    assert runs[-1].start_at == at(11, 1)
    assert runs[-1].end_at == at(11, 10)
    assert all(not (run.start_at < at(11, 1) < run.end_at) for run in runs)


def test_end_immediately_creates_final_short_run_and_stops_future_cutoffs(state):
    store, service, clock = state
    task_id = str(service.create(command(effective_at=at(10, 9))).task_id)
    clock.value = at(10, 11)
    ended = service.end(task_id)
    assert ended.status == "ended"
    assert ended.schedule.next_logical_cutoff_at is None
    assert store.list_runs(task_id)[-1].boundary_type == BoundaryType.END
    clock.value = at(12, 12)
    assert service.materialize_due(task_id) == []


def test_trigger_change_preserves_old_boundaries_and_uses_new_generation(state):
    store, service, clock = state
    task_id = str(service.create(command(effective_at=at(9, 9))).task_id)
    old = store.list_runs(task_id)
    clock.value = at(10, 11)
    updated = service.modify_schedule(
        task_id, daily_trigger_time=time(20), end_at=None
    )
    assert updated.scheduler.generation == 2
    clock.value = at(11, 12)
    service.materialize_due(task_id)
    runs = store.list_runs(task_id)
    assert runs[: len(old)] == old
    assert (runs[len(old)].start_at, runs[len(old)].end_at) == (at(10, 10), at(10, 12))
    assert runs[-1].start_at == at(10, 12)
    assert runs[-1].end_at == at(11, 12)


def test_trigger_change_does_not_backfill_a_new_cutoff_that_already_passed(state):
    store, service, clock = state
    task_id = str(service.create(command(effective_at=at(9, 9))).task_id)
    clock.value = at(10, 10)
    service.modify_schedule(task_id, daily_trigger_time=time(17), end_at=None)
    clock.value = at(10, 11)
    assert service.materialize_due(task_id) == []
    clock.value = at(11, 9)
    service.materialize_due(task_id)
    latest = store.list_runs(task_id)[-1]
    assert (latest.start_at, latest.end_at) == (at(10, 10), at(11, 9))


def test_repeated_create_and_lifecycle_commands_are_idempotent(state):
    store, service, clock = state
    task_id = str(uuid4())
    create = command(effective_at=at(10, 9), task_id=task_id)
    assert service.create(create) == service.create(create)
    clock.value = at(10, 11)
    assert service.pause(task_id) == service.pause(task_id)
    assert len(store.list_runs(task_id)) == 2
    clock.value = at(10, 12)
    assert service.resume(task_id) == service.resume(task_id)
    assert [item.boundary_type for item in store.list_boundaries(task_id)].count(
        BoundaryType.RESUME
    ) == 1


def test_same_task_id_with_a_different_branch_label_is_not_idempotent(state):
    _, service, _ = state
    task_id = str(uuid4())
    original = command(effective_at=at(10, 9), task_id=task_id)
    service.create(original)
    with pytest.raises(ValueError, match="different monitor task"):
        service.create(
            CreateMonitorTask(
                **{
                    **original.__dict__,
                    "branch_label": "A different public label",
                }
            )
        )


def test_ending_while_paused_does_not_report_the_paused_period(state):
    store, service, clock = state
    task_id = str(service.create(command(effective_at=at(10, 9))).task_id)
    clock.value = at(10, 11)
    service.pause(task_id)
    run_count = len(store.list_runs(task_id))
    clock.value = at(11, 11)
    ended = service.end(task_id)
    assert ended.status == "ended"
    assert len(store.list_runs(task_id)) == run_count
    assert store.list_boundaries(task_id)[-1].boundary_type == BoundaryType.PAUSE


def test_future_task_cannot_end_before_effective_time(tmp_path):
    clock = Clock(at(10, 8))
    store = MonitorStore(tmp_path / "future.sqlite3")
    service = MonitorTaskService(store, clock=clock)
    task_id = str(service.create(command(effective_at=at(10, 9))).task_id)
    with pytest.raises(ValueError, match="before its effective"):
        service.end(task_id)


def test_resume_after_configured_end_stays_ended_without_pause_backfill(tmp_path):
    clock = Clock(at(10, 8))
    store = MonitorStore(tmp_path / "paused-end.sqlite3")
    service = MonitorTaskService(store, clock=clock)
    task_id = str(
        service.create(
            command(effective_at=at(10, 7), end_at=at(11, 10))
        ).task_id
    )
    clock.value = at(10, 9)
    service.pause(task_id)
    run_count = len(store.list_runs(task_id))
    clock.value = at(12, 10)
    resumed = service.resume(task_id)
    assert resumed.status == "ended"
    assert resumed.ended_at == at(11, 10)
    assert len(store.list_runs(task_id)) == run_count


def test_multiple_missed_cutoffs_are_ordered_and_runs_are_logically_unique(state):
    store, service, clock = state
    clock.value = at(10, 9)
    task_id = str(service.create(command(effective_at=at(7, 9))).task_id)
    clock.value = at(10, 11)
    service.materialize_due(task_id)
    service.materialize_due(task_id)
    runs = store.list_runs(task_id)
    assert [run.end_at for run in runs] == [at(7, 10), at(8, 10), at(9, 10), at(10, 10)]
    assert len({(run.task_id, run.end_at) for run in runs}) == len(runs)
    assert [run.start_at for run in runs[1:]] == [run.end_at for run in runs[:-1]]


def test_stale_generation_cannot_append_a_boundary(state):
    store, service, clock = state
    task_id = str(service.create(command(effective_at=at(10, 9))).task_id)
    task = store.get_task(task_id)
    store.update_task(
        task_id,
        {"generation": task.generation + 1},
        clock.value,
        expected_generation=task.generation,
        expected_lifecycle="active",
    )
    with pytest.raises(MonitorStateConflict):
        store.append_boundaries(
            task_id,
            [BoundarySpec(at(10, 11), BoundaryType.SCHEDULED, task.generation, "stale")],
            clock.value,
            expected_generation=task.generation,
            expected_lifecycle="active",
        )
    assert all(run.end_at != at(10, 11) for run in store.list_runs(task_id))


def test_creation_and_missed_recovery_enforce_30_day_limit(state):
    _, service, clock = state
    with pytest.raises(MonitorScheduleError, match="30 days"):
        service.create(command(effective_at=clock.value - timedelta(days=30, seconds=1)))
    with pytest.raises(MonitorScheduleError, match="30 day"):
        scheduled_boundaries(
            after=clock.value - timedelta(days=31), due_at=clock.value,
            trigger=time(18), generation=1,
        )


def test_configured_end_state_conflict_is_an_idempotent_noop(state, monkeypatch):
    store, service, clock = state
    clock.value = at(10, 9)
    task_id = str(
        service.create(
            command(effective_at=at(10, 8), end_at=at(10, 10))
        ).task_id
    )
    clock.value = at(10, 10)

    def conflict(*args, **kwargs):
        raise MonitorStateConflict("concurrent runner won")

    monkeypatch.setattr(store, "transition_task", conflict)
    assert service.materialize_due(task_id) == []


def test_failure_does_not_move_following_interval(state):
    store, service, clock = state
    task_id = str(service.create(command(effective_at=at(10, 9))).task_id)
    first = store.list_runs(task_id)[0]
    claim = store.claim_run(
        first.run_id, now=clock.value, lease_for=timedelta(minutes=5), trigger="scheduled"
    )
    error = MonitorPublicErrorPayload(
        code="MONITOR_SVN_TIMEOUT", stage="history", message="temporary", retryable=True
    )
    store.finish_run(first.run_id, claim.lease_token, now=clock.value, status="failed", errors=[error])
    clock.value = at(11, 10)
    service.materialize_due(task_id)
    second = store.list_runs(task_id)[1]
    assert (second.start_at, second.end_at) == (at(10, 10), at(11, 10))


@pytest.mark.parametrize(
    "metadata",
    [
        {"start_revision": 100},
        {"summary": {"workbook_count": 1, "changed_workbook_count": 0, "change_count": 0, "error_count": 1}},
        {"report_ref": "m3r_abcdefghijklmnopqrstuv"},
    ],
)
def test_failed_run_rejects_every_partial_report_metadata_shape(state, metadata):
    store, service, clock = state
    task_id = str(service.create(command(effective_at=at(10, 9))).task_id)
    run_id = store.list_runs(task_id)[0].run_id
    claim = store.claim_run(
        run_id, now=clock.value, lease_for=timedelta(minutes=5), trigger="scheduled"
    )
    error = MonitorPublicErrorPayload(
        code="MONITOR_SVN_TIMEOUT", stage="history", message="temporary", retryable=True
    )
    with pytest.raises(ValueError, match="unpublished"):
        store.finish_run(
            run_id, claim.lease_token, now=clock.value,
            status="failed", errors=[error], **metadata,
        )
    assert store.get_run(run_id).status == "running"


def test_store_terminal_shapes_match_the_frozen_run_contract(state):
    store, service, clock = state
    task_id = str(service.create(command(effective_at=at(10, 9))).task_id)
    run_id = store.list_runs(task_id)[0].run_id
    claim = store.claim_run(
        run_id, now=clock.value, lease_for=timedelta(minutes=5), trigger="scheduled"
    )
    error = MonitorPublicErrorPayload(
        code="MONITOR_PARSE_FAILED", stage="csv_parse", message="partial", retryable=False
    )
    metadata = {
        "start_revision": 100,
        "end_revision": 101,
        "summary": {
            "workbook_count": 1,
            "changed_workbook_count": 0,
            "change_count": 0,
            "error_count": 1,
        },
        "report_ref": "m3r_abcdefghijklmnopqrstuv",
        "report_sha256": "a" * 64,
        "report_expires_at": clock.value + timedelta(days=30),
    }
    with pytest.raises(ValueError, match="succeeded"):
        store.finish_run(
            run_id, claim.lease_token, now=clock.value,
            status="succeeded", errors=[error], **metadata,
        )
    with pytest.raises(ValueError, match="requires public errors"):
        store.finish_run(
            run_id, claim.lease_token, now=clock.value,
            status="failed", errors=[],
        )
    mismatched = {
        **metadata,
        "summary": {**metadata["summary"], "error_count": 0},
    }
    with pytest.raises(ValueError, match="error_count"):
        store.finish_run(
            run_id, claim.lease_token, now=clock.value,
            status="partial", errors=[error], **mismatched,
        )
    store.finish_run(
        run_id, claim.lease_token, now=clock.value,
        status="partial", errors=[error], **metadata,
    )
    assert service.public_run(run_id).status == "partial"


def test_store_allows_unresolved_only_partial_without_public_errors(state):
    store, service, clock = state
    task_id = str(service.create(command(effective_at=at(10, 9))).task_id)
    run_id = store.list_runs(task_id)[0].run_id
    claim = store.claim_run(
        run_id, now=clock.value, lease_for=timedelta(minutes=5), trigger="scheduled"
    )
    store.finish_run(
        run_id,
        claim.lease_token,
        now=clock.value,
        status="partial",
        errors=[],
        start_revision=100,
        end_revision=101,
        summary={
            "workbook_count": 1,
            "changed_workbook_count": 1,
            "change_count": 1,
            "error_count": 0,
        },
        report_ref="m3r_abcdefghijklmnopqrstuv",
        report_sha256="a" * 64,
        report_expires_at=clock.value + timedelta(days=30),
    )

    run = service.public_run(run_id)
    assert run.status.value == "partial"
    assert run.errors == []
    assert run.summary.error_count == 0


def test_concurrent_lease_has_one_winner_and_expired_attempt_recovers(state):
    store, service, clock = state
    task_id = str(service.create(command(effective_at=at(10, 9))).task_id)
    run_id = store.list_runs(task_id)[0].run_id

    def claim():
        return store.claim_run(
            run_id, now=clock.value, lease_for=timedelta(minutes=5), trigger="scheduled"
        )

    with ThreadPoolExecutor(max_workers=2) as executor:
        claims = list(executor.map(lambda _: claim(), range(2)))
    assert sum(item is not None for item in claims) == 1
    clock.value += timedelta(minutes=6)
    recovered = store.claim_run(
        run_id, now=clock.value, lease_for=timedelta(minutes=5), trigger="scheduled"
    )
    assert recovered is not None
    assert recovered.attempt == 2
    assert recovered.trigger == "automatic_retry"
    attempts = store.attempts(run_id)
    assert [item["attempt"] for item in attempts] == [1, 2]
    assert attempts[0]["status"] == "failed"
    winner = next(item for item in claims if item is not None)
    with pytest.raises(MonitorLeaseLost):
        store.finish_run(
            run_id,
            winner.lease_token,
            now=clock.value,
            status="failed",
            errors=[
                MonitorPublicErrorPayload(
                    code="MONITOR_INTERNAL_ERROR",
                    stage="report_publish",
                    message="stale worker",
                    retryable=True,
                )
            ],
        )


def test_automatic_retry_limit_excludes_manual_attempts(state):
    store, service, clock = state
    task_id = str(service.create(command(effective_at=at(10, 9))).task_id)
    run_id = store.list_runs(task_id)[0].run_id
    error = MonitorPublicErrorPayload(
        code="MONITOR_SVN_TIMEOUT", stage="history", message="temporary", retryable=True
    )
    triggers = (
        "scheduled", "manual_retry",
        "automatic_retry", "automatic_retry", "automatic_retry",
    )
    for number, trigger in enumerate(triggers, 1):
        claim = store.claim_run(
            run_id, now=clock.value, lease_for=timedelta(minutes=5), trigger=trigger
        )
        assert claim.attempt == number
        store.finish_run(run_id, claim.lease_token, now=clock.value, status="failed", errors=[error])
    run = store.get_run(run_id)
    assert run.run_id == run_id
    assert run.attempt_count == 5
    assert [item["trigger"] for item in store.attempts(run_id)] == list(triggers)
    assert store.automatic_retry_count(run_id) == 3
    assert store.claim_run(
        run_id, now=clock.value, lease_for=timedelta(minutes=5),
        trigger="automatic_retry",
    ) is None


def test_expired_third_automatic_attempt_becomes_a_valid_terminal_failure(state):
    store, service, clock = state
    task_id = str(service.create(command(effective_at=at(10, 9))).task_id)
    run_id = store.list_runs(task_id)[0].run_id
    error = MonitorPublicErrorPayload(
        code="MONITOR_SVN_TIMEOUT", stage="history", message="temporary", retryable=True
    )
    for trigger in ("scheduled", "automatic_retry", "automatic_retry"):
        claim = store.claim_run(
            run_id, now=clock.value, lease_for=timedelta(minutes=5), trigger=trigger
        )
        store.finish_run(
            run_id, claim.lease_token, now=clock.value, status="failed", errors=[error]
        )
    running = store.claim_run(
        run_id, now=clock.value, lease_for=timedelta(minutes=5),
        trigger="automatic_retry",
    )
    clock.value += timedelta(minutes=6)
    assert store.claim_run(
        run_id, now=clock.value, lease_for=timedelta(minutes=5), trigger="scheduled"
    ) is None
    public = service.public_run(run_id)
    assert public.status == "failed"
    assert public.attempt_count == 4
    assert public.attempts[-1].status == "failed"
    assert running.attempt == 4
