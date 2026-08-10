"""Transactional task lifecycle orchestration for M3 monitoring."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, time, timezone
from typing import Callable
import urllib.parse
from uuid import UUID, uuid4

from app.schemas.monitor import (
    MonitorBranchPayload,
    MonitorRunAttemptPayload,
    MonitorRunDigestPayload,
    MonitorRunPayload,
    MonitorRunSummaryPayload,
    MonitorSchedulePayload,
    MonitorSchedulerPayload,
    MonitorTaskPayload,
    MonitorTimeIntervalPayload,
)
from app.services.monitor_schedule import (
    BoundarySpec,
    BoundaryType,
    next_scheduled_cutoff,
    require_utc,
    scheduled_boundaries,
    validate_creation_window,
)
from app.services.monitor_store import (
    MonitorStateConflict,
    MonitorStore,
    RunRecord,
    TaskRecord,
)
from core.svn_history import canonicalize_svn_url


Clock = Callable[[], datetime]


@dataclass(frozen=True)
class CreateMonitorTask:
    name: str
    endpoint_id: str
    branch_label: str
    repository_uuid: str
    canonical_url: str
    repository_relative_path: str
    bound_revision: int
    copy_boundary_revision: int
    effective_at: datetime
    daily_trigger_time: time
    end_at: datetime | None = None
    task_id: str | None = None


class MonitorTaskService:
    def __init__(self, store: MonitorStore, *, clock: Clock | None = None):
        self.store = store
        self.clock = clock or (lambda: datetime.now(timezone.utc))

    def _now(self) -> datetime:
        return require_utc(self.clock())

    @staticmethod
    def _parse_trigger(value: str) -> time:
        return time.fromisoformat(value)

    def create(self, command: CreateMonitorTask) -> MonitorTaskPayload:
        now = self._now()
        effective, end, _ = validate_creation_window(command.effective_at, command.end_at, now)
        if not isinstance(command.name, str) or not 1 <= len(command.name) <= 128:
            raise ValueError("monitor task name must contain 1 to 128 characters")
        canonical_url = canonicalize_svn_url(command.canonical_url)
        parsed_url = urllib.parse.urlsplit(canonical_url)
        if parsed_url.username is not None or parsed_url.password is not None:
            raise ValueError("canonical SVN URL cannot contain credentials")
        branch = MonitorBranchPayload(
            endpoint_id=command.endpoint_id,
            label=command.branch_label,
            repository_uuid=command.repository_uuid,
            repository_relative_path=command.repository_relative_path,
            bound_revision=command.bound_revision,
            copy_boundary_revision=command.copy_boundary_revision,
        )
        trigger = command.daily_trigger_time
        if trigger.tzinfo is not None or trigger.microsecond:
            raise ValueError("daily_trigger_time must be a whole-second wall time")
        task_id = str(UUID(command.task_id)) if command.task_id else str(uuid4())
        existing = self.store.get_task(task_id)
        if existing is not None:
            requested_identity = (
                command.name,
                command.endpoint_id,
                branch.label,
                str(branch.repository_uuid),
                canonical_url,
                branch.repository_relative_path,
                branch.bound_revision,
                branch.copy_boundary_revision,
                effective,
                end,
                trigger.isoformat(),
            )
            stored_identity = (
                existing.name,
                existing.endpoint_id,
                existing.branch_label,
                existing.repository_uuid,
                existing.canonical_url,
                existing.repository_relative_path,
                existing.bound_revision,
                existing.copy_boundary_revision,
                existing.effective_at,
                existing.end_at,
                existing.daily_trigger_time,
            )
            if requested_identity != stored_identity:
                raise ValueError("task_id already belongs to a different monitor task")
            return self.to_public_task(existing)
        task = self.store.create_task(
            {
                "task_id": task_id,
                "name": command.name,
                "endpoint_id": branch.endpoint_id,
                "branch_label": branch.label,
                "repository_uuid": str(branch.repository_uuid),
                "canonical_url": canonical_url,
                "repository_relative_path": branch.repository_relative_path,
                "bound_revision": branch.bound_revision,
                "copy_boundary_revision": branch.copy_boundary_revision,
                "effective_at": effective,
                "end_at": end,
                "daily_trigger_time": trigger.isoformat(),
                "created_at": now,
            },
            BoundarySpec(effective, BoundaryType.START, 1, "task_effective"),
        )
        self.materialize_due(task.task_id, now=now)
        return self.to_public_task(self.store.get_task(task.task_id))

    def materialize_due(self, task_id: str, *, now: datetime | None = None) -> list[RunRecord]:
        current = require_utc(now) if now is not None else self._now()
        task = self._require_task(task_id)
        if task.lifecycle != "active":
            return []
        anchor = max(
            self.store.latest_boundary(task_id).boundary_at,
            task.schedule_effective_at,
        )
        specs = scheduled_boundaries(
            after=anchor,
            due_at=current,
            trigger=self._parse_trigger(task.daily_trigger_time),
            generation=task.generation,
            end_at=task.end_at,
        )
        before = {run.run_id for run in self.store.list_runs(task_id)}
        if task.end_at is not None and task.end_at <= current:
            try:
                self.store.transition_task(
                    task_id,
                    boundaries=specs,
                    updates={
                        "lifecycle": "ended",
                        "generation": task.generation + 1,
                        "scheduler_desired_state": "disabled",
                        "scheduler_sync_status": "pending",
                        "scheduler_error": None,
                        "ended_at": task.end_at,
                        "ended_reason": "configured",
                    },
                    now=current,
                    expected_generation=task.generation,
                    expected_lifecycle="active",
                )
            except MonitorStateConflict:
                return []
        else:
            try:
                self.store.append_boundaries(
                    task_id,
                    specs,
                    current,
                    expected_generation=task.generation,
                    expected_lifecycle="active",
                )
            except MonitorStateConflict:
                return []
        return [run for run in self.store.list_runs(task_id) if run.run_id not in before]

    def modify_schedule(
        self,
        task_id: str,
        *,
        daily_trigger_time: time,
        end_at: datetime | None,
    ) -> MonitorTaskPayload:
        now = self._now()
        task = self._require_task(task_id)
        if task.lifecycle != "active":
            raise ValueError("only an active monitor task can change its schedule")
        self.materialize_due(task_id, now=now)
        task = self._require_task(task_id)
        if task.lifecycle != "active":
            return self.to_public_task(task)
        end = require_utc(end_at) if end_at is not None else None
        if end is not None and end <= task.effective_at:
            raise ValueError("end_at must be later than effective_at")
        if end is not None and end <= now:
            raise ValueError("a modified end_at must be in the future")
        trigger_text = daily_trigger_time.isoformat()
        if daily_trigger_time.tzinfo is not None or daily_trigger_time.microsecond:
            raise ValueError("daily_trigger_time must be a whole-second wall time")
        if task.daily_trigger_time == trigger_text and task.end_at == end:
            return self.to_public_task(task)
        updated = self.store.update_task(
            task_id,
            {
                "daily_trigger_time": trigger_text,
                "schedule_effective_at": now,
                "end_at": end,
                "generation": task.generation + 1,
                "scheduler_sync_status": "pending",
                "scheduler_error": None,
            },
            now,
            expected_generation=task.generation,
            expected_lifecycle="active",
        )
        return self.to_public_task(updated)

    def pause(self, task_id: str) -> MonitorTaskPayload:
        now = self._now()
        task = self._require_task(task_id)
        if task.lifecycle == "paused":
            return self.to_public_task(task)
        if task.lifecycle != "active":
            raise ValueError("only an active monitor task can be paused")
        self.materialize_due(task_id, now=now)
        task = self._require_task(task_id)
        if task.lifecycle != "active":
            return self.to_public_task(task)
        anchor = self.store.latest_boundary(task_id).boundary_at
        generation = task.generation + 1
        boundaries = (
            [BoundarySpec(now, BoundaryType.PAUSE, generation, "user_pause")]
            if now > anchor else []
        )
        updated = self.store.transition_task(
            task_id,
            boundaries=boundaries,
            updates={
                "lifecycle": "paused",
                "generation": generation,
                "scheduler_desired_state": "disabled",
                "scheduler_sync_status": "pending",
                "scheduler_error": None,
                "paused_at": now,
            },
            now=now,
            expected_generation=task.generation,
            expected_lifecycle="active",
        )
        return self.to_public_task(updated)

    def resume(self, task_id: str) -> MonitorTaskPayload:
        now = self._now()
        task = self._require_task(task_id)
        if task.lifecycle == "active":
            return self.to_public_task(task)
        if task.lifecycle != "paused":
            raise ValueError("only a paused monitor task can resume")
        generation = task.generation + 1
        if task.end_at is not None and task.end_at <= now:
            updated = self.store.transition_task(
                task_id,
                boundaries=[],
                updates={
                    "lifecycle": "ended",
                    "generation": generation,
                    "scheduler_desired_state": "disabled",
                    "scheduler_sync_status": "pending",
                    "scheduler_error": None,
                    "paused_at": None,
                    "ended_at": task.end_at,
                    "ended_reason": "configured",
                },
                now=now,
                expected_generation=task.generation,
                expected_lifecycle="paused",
            )
            return self.to_public_task(updated)
        updated = self.store.transition_task(
            task_id,
            boundaries=[BoundarySpec(now, BoundaryType.RESUME, generation, "user_resume")],
            updates={
                "lifecycle": "active",
                "schedule_effective_at": now,
                "generation": generation,
                "scheduler_desired_state": "enabled",
                "scheduler_sync_status": "pending",
                "scheduler_error": None,
                "paused_at": None,
            },
            now=now,
            expected_generation=task.generation,
            expected_lifecycle="paused",
        )
        return self.to_public_task(updated)

    def end(self, task_id: str) -> MonitorTaskPayload:
        now = self._now()
        task = self._require_task(task_id)
        if task.lifecycle == "ended":
            return self.to_public_task(task)
        if task.lifecycle not in {"active", "paused"}:
            raise ValueError("monitor task cannot be ended from its current state")
        if now <= task.effective_at:
            raise ValueError("monitor task cannot end before its effective time")
        if task.lifecycle == "active":
            self.materialize_due(task_id, now=now)
            task = self._require_task(task_id)
            if task.lifecycle == "ended":
                return self.to_public_task(task)
        generation = task.generation + 1
        anchor = self.store.latest_boundary(task_id).boundary_at
        boundaries = (
            [BoundarySpec(now, BoundaryType.END, generation, "user_end")]
            if task.lifecycle == "active" and now > anchor else []
        )
        updated = self.store.transition_task(
            task_id,
            boundaries=boundaries,
            updates={
                "lifecycle": "ended",
                "end_at": now,
                "generation": generation,
                "scheduler_desired_state": "disabled",
                "scheduler_sync_status": "pending",
                "scheduler_error": None,
                "paused_at": None,
                "ended_at": now,
                "ended_reason": "user",
            },
            now=now,
            expected_generation=task.generation,
            expected_lifecycle=task.lifecycle,
        )
        return self.to_public_task(updated)

    def _require_task(self, task_id: str) -> TaskRecord:
        task = self.store.get_task(task_id)
        if task is None:
            raise KeyError(task_id)
        return task

    def to_public_task(self, task: TaskRecord) -> MonitorTaskPayload:
        latest = self.store.list_runs(task.task_id)
        latest_run = self._run_digest(latest[-1]) if latest else None
        trigger = self._parse_trigger(task.daily_trigger_time)
        next_cutoff = None
        if task.lifecycle == "active":
            next_cutoff = next_scheduled_cutoff(
                after=max(
                    self.store.latest_boundary(task.task_id).boundary_at,
                    task.schedule_effective_at,
                ),
                trigger=trigger,
                end_at=task.end_at,
            )
        if task.lifecycle == "active":
            public_status = {
                "pending": "syncing",
                "synced": "active",
                "drifted": "scheduler_error",
                "error": "scheduler_error",
                "not_present": "scheduler_error",
            }[task.scheduler_sync_status]
        else:
            public_status = {
                "paused": "paused",
                "ended": "ended",
                "archived": "archived",
            }[task.lifecycle]
        return MonitorTaskPayload(
            task_id=task.task_id,
            name=task.name,
            status=public_status,
            branch=MonitorBranchPayload(
                endpoint_id=task.endpoint_id,
                label=task.branch_label,
                repository_uuid=task.repository_uuid,
                repository_relative_path=task.repository_relative_path,
                bound_revision=task.bound_revision,
                copy_boundary_revision=task.copy_boundary_revision,
            ),
            schedule=MonitorSchedulePayload(
                effective_at=task.effective_at,
                end_at=task.end_at,
                daily_trigger_time=trigger,
                next_logical_cutoff_at=next_cutoff,
            ),
            scheduler=MonitorSchedulerPayload(
                generation=task.generation,
                desired_state=task.scheduler_desired_state,
                sync_status=task.scheduler_sync_status,
                last_synced_at=task.scheduler_last_synced_at,
                last_error=task.scheduler_error,
            ),
            latest_run=latest_run,
            last_runner_heartbeat_at=task.last_runner_heartbeat_at,
            created_at=task.created_at,
            updated_at=task.updated_at,
            paused_at=task.paused_at,
            ended_at=task.ended_at,
            archived_at=task.archived_at,
        )

    @staticmethod
    def _run_digest(run: RunRecord) -> MonitorRunDigestPayload:
        published = run.status in {"succeeded", "partial"}
        return MonitorRunDigestPayload(
            run_id=run.run_id,
            status=run.status,
            interval=MonitorTimeIntervalPayload(
                start_at=run.start_at,
                end_at=run.end_at,
                logical_cutoff_at=run.end_at,
                boundary_kind=run.boundary_type.value,
            ),
            summary=MonitorRunSummaryPayload.model_validate(run.summary) if published else None,
            report_ref=run.report_ref if published else None,
        )

    def public_run(self, run_id: str) -> MonitorRunPayload:
        run = self.store.get_run(run_id)
        if run is None:
            raise KeyError(run_id)
        attempts = [MonitorRunAttemptPayload(**attempt) for attempt in self.store.attempts(run_id)]
        published = run.status in {"succeeded", "partial"}
        return MonitorRunPayload(
            run_id=run.run_id,
            task_id=run.task_id,
            status=run.status,
            interval=MonitorTimeIntervalPayload(
                start_at=run.start_at,
                end_at=run.end_at,
                logical_cutoff_at=run.end_at,
                boundary_kind=run.boundary_type.value,
            ),
            start_revision=run.start_revision if published else None,
            end_revision=run.end_revision if published else None,
            attempt_count=run.attempt_count,
            attempts=attempts,
            summary=MonitorRunSummaryPayload.model_validate(run.summary) if published else None,
            report_ref=run.report_ref if published else None,
            report_sha256=run.report_sha256 if published else None,
            report_expires_at=run.report_expires_at if published else None,
            errors=run.errors,
            created_at=run.created_at,
            started_at=run.started_at,
            finished_at=run.finished_at,
            updated_at=run.updated_at,
        )
