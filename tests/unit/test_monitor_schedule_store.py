from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, time, timedelta, timezone
from uuid import uuid4

import pytest

from app.schemas.monitor import MonitorPublicErrorPayload
from app.services.monitor_schedule import (
    BoundarySpec,
    BoundaryType,
    MonitorScheduleError,
    scheduled_boundaries,
)
from app.services.monitor_store import MonitorLeaseLost, MonitorStateConflict, MonitorStore
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
            "SELECT version FROM monitor_schema_migrations"
        ).fetchall()] == [1]


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


def test_automatic_and_manual_retry_reuse_run_and_continue_attempts(state):
    store, service, clock = state
    task_id = str(service.create(command(effective_at=at(10, 9))).task_id)
    run_id = store.list_runs(task_id)[0].run_id
    error = MonitorPublicErrorPayload(
        code="MONITOR_SVN_TIMEOUT", stage="history", message="temporary", retryable=True
    )
    for number, trigger in enumerate(("scheduled", "automatic_retry", "manual_retry"), 1):
        claim = store.claim_run(
            run_id, now=clock.value, lease_for=timedelta(minutes=5), trigger=trigger
        )
        assert claim.attempt == number
        store.finish_run(run_id, claim.lease_token, now=clock.value, status="failed", errors=[error])
    run = store.get_run(run_id)
    assert run.run_id == run_id
    assert run.attempt_count == 3
    assert [item["trigger"] for item in store.attempts(run_id)] == [
        "scheduled", "automatic_retry", "manual_retry",
    ]
