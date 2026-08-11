"""M3 version monitoring public contracts.

The models in this module are deliberately independent from the M2 batch and
diff contracts. They describe public API/report data only; storage paths,
credentials, command output, and exception details are not contract fields.
"""
from __future__ import annotations

from datetime import datetime, time, timezone
from enum import Enum
import json
from typing import Annotated, Literal
from uuid import UUID

from pydantic import AfterValidator, BaseModel, ConfigDict, Field, field_validator, model_validator


MONITOR_TASK_SCHEMA_VERSION = "m3.monitor-task.v1"
MONITOR_TASK_LIST_SCHEMA_VERSION = "m3.monitor-task-list.v1"
MONITOR_RUN_SCHEMA_VERSION = "m3.monitor-run.v1"
MONITOR_REPORT_SCHEMA_VERSION = "m3.monitor-report.v1"
MONITOR_RUN_LIST_SCHEMA_VERSION = "m3.monitor-run-list.v1"
MONITOR_ENDPOINT_OPTIONS_SCHEMA_VERSION = "m3.monitor-endpoint-options.v1"
MONITOR_RETRY_ACCEPTED_SCHEMA_VERSION = "m3.monitor-run-retry.accepted.v1"


def _utc_datetime(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("datetime must include a UTC offset")
    return value.astimezone(timezone.utc)


UtcDateTime = Annotated[datetime, AfterValidator(_utc_datetime)]


class StrictMonitorPayload(BaseModel):
    model_config = ConfigDict(extra="forbid")


class MonitorTaskCreateRequestPayload(StrictMonitorPayload):
    schema_version: Literal["m3.monitor-task-create.request.v1"]
    request_id: UUID
    name: str = Field(..., min_length=1, max_length=128, strict=True)
    endpoint_id: str = Field(
        ..., min_length=1, max_length=128, pattern=r"^[A-Za-z0-9._-]+$", strict=True
    )
    effective_at: UtcDateTime
    end_at: UtcDateTime | None = None
    daily_trigger_time: time

    @field_validator("daily_trigger_time")
    @classmethod
    def validate_trigger(cls, value: time) -> time:
        if value.tzinfo is not None or value.microsecond:
            raise ValueError("daily_trigger_time must be a whole-second wall time")
        return value

    @model_validator(mode="after")
    def validate_range(self) -> "MonitorTaskCreateRequestPayload":
        if self.end_at is not None and self.end_at <= self.effective_at:
            raise ValueError("end_at must be later than effective_at")
        return self


class MonitorTaskPatchRequestPayload(StrictMonitorPayload):
    schema_version: Literal["m3.monitor-task-patch.request.v1"]
    request_id: UUID
    daily_trigger_time: time
    end_at: UtcDateTime | None

    @field_validator("daily_trigger_time")
    @classmethod
    def validate_trigger(cls, value: time) -> time:
        if value.tzinfo is not None or value.microsecond:
            raise ValueError("daily_trigger_time must be a whole-second wall time")
        return value


class MonitorCommandRequestPayload(StrictMonitorPayload):
    schema_version: Literal["m3.monitor-command.request.v1"]
    request_id: UUID


class MonitorRunRetryRequestPayload(StrictMonitorPayload):
    schema_version: Literal["m3.monitor-run-retry.request.v1"]
    request_id: UUID


class MonitorApiErrorCode(str, Enum):
    INVALID_REQUEST = "MONITOR_INVALID_REQUEST"
    INVALID_CURSOR = "MONITOR_INVALID_CURSOR"
    ENDPOINT_NOT_FOUND = "MONITOR_ENDPOINT_NOT_FOUND"
    ENDPOINT_DISABLED = "MONITOR_ENDPOINT_DISABLED"
    BRANCH_CONFIGURATION_INVALID = "MONITOR_BRANCH_CONFIGURATION_INVALID"
    DATASET_CONFIGURATION_INVALID = "MONITOR_DATASET_CONFIGURATION_INVALID"
    TASK_NOT_FOUND = "MONITOR_TASK_NOT_FOUND"
    RUN_NOT_FOUND = "MONITOR_RUN_NOT_FOUND"
    REPORT_NOT_FOUND = "MONITOR_REPORT_NOT_FOUND"
    REPORT_EXPIRED = "MONITOR_REPORT_EXPIRED"
    STATE_CONFLICT = "MONITOR_STATE_CONFLICT"
    IDEMPOTENCY_CONFLICT = "MONITOR_IDEMPOTENCY_CONFLICT"
    SERVICE_UNAVAILABLE = "MONITOR_SERVICE_UNAVAILABLE"
    INTERNAL_ERROR = "MONITOR_API_INTERNAL_ERROR"


class MonitorApiErrorPayload(StrictMonitorPayload):
    code: MonitorApiErrorCode
    message: str = Field(..., min_length=1, max_length=256, strict=True)

    @field_validator("message")
    @classmethod
    def reject_control_characters(cls, value: str) -> str:
        if any(ord(character) < 32 for character in value):
            raise ValueError("public text cannot contain control characters")
        return value


class MonitorApiErrorEnvelope(StrictMonitorPayload):
    error: MonitorApiErrorPayload


class MonitorEndpointOptionPayload(StrictMonitorPayload):
    endpoint_id: str = Field(
        ..., min_length=1, max_length=128, pattern=r"^[A-Za-z0-9._-]+$", strict=True
    )
    label: str = Field(..., min_length=1, max_length=256, strict=True)


class MonitorEndpointOptionsPayload(StrictMonitorPayload):
    schema_version: Literal[MONITOR_ENDPOINT_OPTIONS_SCHEMA_VERSION] = (
        MONITOR_ENDPOINT_OPTIONS_SCHEMA_VERSION
    )
    items: list[MonitorEndpointOptionPayload] = Field(default_factory=list)


class MonitorRetryAcceptedPayload(StrictMonitorPayload):
    schema_version: Literal[MONITOR_RETRY_ACCEPTED_SCHEMA_VERSION] = (
        MONITOR_RETRY_ACCEPTED_SCHEMA_VERSION
    )
    request_id: UUID
    task_id: UUID
    run_id: UUID
    status: Literal["accepted"] = "accepted"
    dispatch_state: Literal["pending"] = "pending"


class MonitorTaskStatus(str, Enum):
    SYNCING = "syncing"
    ACTIVE = "active"
    PAUSED = "paused"
    SCHEDULER_ERROR = "scheduler_error"
    ENDED = "ended"
    ARCHIVED = "archived"


class MonitorSchedulerSyncStatus(str, Enum):
    PENDING = "pending"
    SYNCED = "synced"
    DRIFTED = "drifted"
    ERROR = "error"
    NOT_PRESENT = "not_present"


class MonitorRunStatus(str, Enum):
    QUEUED = "queued"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    PARTIAL = "partial"
    FAILED = "failed"


class MonitorAttemptStatus(str, Enum):
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    PARTIAL = "partial"
    FAILED = "failed"


class MonitorBoundaryKind(str, Enum):
    SCHEDULED = "scheduled"
    PAUSE = "pause"
    END = "end"


class MonitorChangeType(str, Enum):
    FIELD_MODIFIED = "field_modified"
    ROW_ADDED = "row_added"
    ROW_DELETED = "row_deleted"
    FIELD_ADDED = "field_added"
    FIELD_REMOVED = "field_removed"
    FIELD_DEFINITION_MODIFIED = "field_definition_modified"


class MonitorErrorCode(str, Enum):
    SVN_TIMEOUT = "MONITOR_SVN_TIMEOUT"
    SVN_AUTH_FAILED = "MONITOR_SVN_AUTH_FAILED"
    BRANCH_BINDING_INVALID = "MONITOR_BRANCH_BINDING_INVALID"
    CONFIGURATION_INVALID = "MONITOR_CONFIGURATION_INVALID"
    PARSE_FAILED = "MONITOR_PARSE_FAILED"
    ATTRIBUTION_INCOMPLETE = "MONITOR_ATTRIBUTION_INCOMPLETE"
    REPORT_PUBLISH_FAILED = "MONITOR_REPORT_PUBLISH_FAILED"
    SCHEDULER_SYNC_FAILED = "MONITOR_SCHEDULER_SYNC_FAILED"
    INTERNAL_ERROR = "MONITOR_INTERNAL_ERROR"


class MonitorErrorStage(str, Enum):
    SCHEDULER = "scheduler"
    BRANCH_IDENTITY = "branch_identity"
    HISTORY = "history"
    SNAPSHOT = "snapshot"
    MANIFEST_PARSE = "manifest_parse"
    CSV_PARSE = "csv_parse"
    DIFF = "diff"
    ATTRIBUTION = "attribution"
    REPORT_PUBLISH = "report_publish"


class MonitorPublicErrorPayload(StrictMonitorPayload):
    code: MonitorErrorCode
    stage: MonitorErrorStage
    message: str = Field(..., min_length=1, max_length=512, strict=True)
    retryable: bool = Field(..., strict=True)
    workbook: str | None = Field(default=None, min_length=1, max_length=256, strict=True)
    sheet_name: str | None = Field(default=None, min_length=1, max_length=256, strict=True)

    @field_validator("message", "workbook", "sheet_name")
    @classmethod
    def reject_control_characters(cls, value: str | None) -> str | None:
        if value is not None and any(ord(character) < 32 for character in value):
            raise ValueError("public text cannot contain control characters")
        return value


class MonitorBranchPayload(StrictMonitorPayload):
    endpoint_id: str = Field(
        ...,
        min_length=1,
        max_length=128,
        pattern=r"^[A-Za-z0-9._-]+$",
        strict=True,
    )
    label: str = Field(..., min_length=1, max_length=256, strict=True)
    repository_uuid: UUID
    repository_relative_path: str = Field(..., min_length=1, max_length=1024, strict=True)
    bound_revision: int = Field(..., gt=0, strict=True)
    copy_boundary_revision: int = Field(..., gt=0, strict=True)

    @field_validator("repository_relative_path")
    @classmethod
    def validate_repository_relative_path(cls, value: str) -> str:
        if value.startswith("/") or value.endswith("/") or "\\" in value:
            raise ValueError("repository_relative_path must be a normalized relative path")
        segments = value.split("/")
        if any(segment in {"", ".", ".."} for segment in segments):
            raise ValueError("repository_relative_path contains an invalid segment")
        if any(ord(character) < 32 for character in value):
            raise ValueError("repository_relative_path cannot contain control characters")
        return value

    @model_validator(mode="after")
    def validate_copy_boundary(self) -> "MonitorBranchPayload":
        if self.copy_boundary_revision > self.bound_revision:
            raise ValueError("copy_boundary_revision cannot exceed bound_revision")
        return self


class MonitorSchedulePayload(StrictMonitorPayload):
    timezone: Literal["Asia/Shanghai"] = "Asia/Shanghai"
    effective_at: UtcDateTime
    end_at: UtcDateTime | None = None
    daily_trigger_time: time
    next_logical_cutoff_at: UtcDateTime | None = None

    @field_validator("daily_trigger_time")
    @classmethod
    def validate_daily_trigger_time(cls, value: time) -> time:
        if value.tzinfo is not None or value.microsecond != 0:
            raise ValueError("daily_trigger_time must be a whole-second local wall time")
        return value

    @model_validator(mode="after")
    def validate_range(self) -> "MonitorSchedulePayload":
        if self.end_at is not None and self.end_at <= self.effective_at:
            raise ValueError("end_at must be later than effective_at")
        if (
            self.next_logical_cutoff_at is not None
            and self.next_logical_cutoff_at <= self.effective_at
        ):
            raise ValueError("next_logical_cutoff_at must be later than effective_at")
        return self


class MonitorSchedulerPayload(StrictMonitorPayload):
    generation: int = Field(..., gt=0, strict=True)
    desired_state: Literal["enabled", "disabled", "removed"]
    sync_status: MonitorSchedulerSyncStatus
    last_synced_at: UtcDateTime | None = None
    last_error: MonitorPublicErrorPayload | None = None

    @model_validator(mode="after")
    def validate_error(self) -> "MonitorSchedulerPayload":
        has_sync_failure = self.sync_status in {
            MonitorSchedulerSyncStatus.DRIFTED,
            MonitorSchedulerSyncStatus.ERROR,
        }
        if has_sync_failure != (self.last_error is not None):
            raise ValueError("scheduler drift/error and last_error must appear together")
        if self.last_error is not None and self.last_error.stage != MonitorErrorStage.SCHEDULER:
            raise ValueError("scheduler last_error must use the scheduler stage")
        return self


def _validate_task_lifecycle(
    status: MonitorTaskStatus,
    schedule: MonitorSchedulePayload,
    scheduler: MonitorSchedulerPayload,
) -> None:
    if status == MonitorTaskStatus.SYNCING:
        if scheduler.sync_status != MonitorSchedulerSyncStatus.PENDING:
            raise ValueError("syncing task must have pending scheduler state")
    elif status == MonitorTaskStatus.ACTIVE:
        if (
            scheduler.sync_status != MonitorSchedulerSyncStatus.SYNCED
            or scheduler.desired_state != "enabled"
        ):
            raise ValueError("active task requires a synced enabled scheduler")
    elif status == MonitorTaskStatus.PAUSED:
        if scheduler.desired_state != "disabled":
            raise ValueError("paused task requires a disabled scheduler")
    elif status == MonitorTaskStatus.SCHEDULER_ERROR:
        if scheduler.sync_status not in {
            MonitorSchedulerSyncStatus.DRIFTED,
            MonitorSchedulerSyncStatus.ERROR,
        }:
            raise ValueError("scheduler_error task requires scheduler drift/error")
    elif scheduler.desired_state == "enabled":
        raise ValueError("ended or archived task cannot keep an enabled scheduler")

    if status in {
        MonitorTaskStatus.PAUSED,
        MonitorTaskStatus.ENDED,
        MonitorTaskStatus.ARCHIVED,
    } and schedule.next_logical_cutoff_at is not None:
        raise ValueError("inactive task cannot expose a next logical cutoff")


class MonitorTimeIntervalPayload(StrictMonitorPayload):
    start_at: UtcDateTime
    end_at: UtcDateTime
    start_inclusive: Literal[False] = False
    end_inclusive: Literal[True] = True
    logical_cutoff_at: UtcDateTime
    boundary_kind: MonitorBoundaryKind

    @model_validator(mode="after")
    def validate_interval(self) -> "MonitorTimeIntervalPayload":
        if self.start_at >= self.end_at:
            raise ValueError("monitor interval must have positive duration")
        if self.logical_cutoff_at != self.end_at:
            raise ValueError("logical_cutoff_at must equal the right-closed interval end")
        return self


class MonitorRunSummaryPayload(StrictMonitorPayload):
    workbook_count: int = Field(..., ge=0, strict=True)
    changed_workbook_count: int = Field(..., ge=0, strict=True)
    change_count: int = Field(..., ge=0, strict=True)
    error_count: int = Field(..., ge=0, strict=True)

    @model_validator(mode="after")
    def validate_counts(self) -> "MonitorRunSummaryPayload":
        if self.changed_workbook_count > self.workbook_count:
            raise ValueError("changed_workbook_count cannot exceed workbook_count")
        return self


class MonitorRunDigestPayload(StrictMonitorPayload):
    run_id: UUID
    status: MonitorRunStatus
    interval: MonitorTimeIntervalPayload
    summary: MonitorRunSummaryPayload | None = None
    report_ref: str | None = Field(
        default=None,
        pattern=r"^m3r_[A-Za-z0-9_-]{22}$",
        strict=True,
    )

    @model_validator(mode="after")
    def validate_terminal_result(self) -> "MonitorRunDigestPayload":
        published = self.status in {MonitorRunStatus.SUCCEEDED, MonitorRunStatus.PARTIAL}
        has_summary = self.summary is not None
        has_report_ref = self.report_ref is not None
        if has_summary != published or has_report_ref != published:
            raise ValueError("published run digest requires summary and report_ref")
        return self


class MonitorLatestReportPayload(StrictMonitorPayload):
    run_id: UUID
    status: Literal["succeeded", "partial"]
    interval: MonitorTimeIntervalPayload
    summary: MonitorRunSummaryPayload


class MonitorTaskPayload(StrictMonitorPayload):
    schema_version: Literal[MONITOR_TASK_SCHEMA_VERSION] = MONITOR_TASK_SCHEMA_VERSION
    task_id: UUID
    name: str = Field(..., min_length=1, max_length=128, strict=True)
    status: MonitorTaskStatus
    branch: MonitorBranchPayload
    schedule: MonitorSchedulePayload
    scheduler: MonitorSchedulerPayload
    latest_run: MonitorRunDigestPayload | None = None
    latest_report: MonitorLatestReportPayload | None = None
    pending_run_count: int = Field(..., ge=0, strict=True)
    last_runner_heartbeat_at: UtcDateTime | None = None
    created_at: UtcDateTime
    updated_at: UtcDateTime
    paused_at: UtcDateTime | None = None
    ended_at: UtcDateTime | None = None
    archived_at: UtcDateTime | None = None

    @model_validator(mode="after")
    def validate_state(self) -> "MonitorTaskPayload":
        if self.updated_at < self.created_at:
            raise ValueError("updated_at cannot precede created_at")
        _validate_task_lifecycle(self.status, self.schedule, self.scheduler)
        if (self.status == MonitorTaskStatus.PAUSED) != (self.paused_at is not None):
            raise ValueError("paused status and paused_at must appear together")
        if self.status == MonitorTaskStatus.ENDED and self.ended_at is None:
            raise ValueError("ended task requires ended_at")
        if self.status == MonitorTaskStatus.ARCHIVED and self.archived_at is None:
            raise ValueError("archived task requires archived_at")
        if (
            self.latest_run is not None
            and self.latest_run.status in {MonitorRunStatus.QUEUED, MonitorRunStatus.RUNNING}
            and self.pending_run_count < 1
        ):
            raise ValueError("queued/running latest run requires pending_run_count")
        if self.latest_report is not None and self.latest_run is None:
            raise ValueError("latest report requires latest run")
        if self.latest_run is not None:
            latest_published = self.latest_run.status in {
                MonitorRunStatus.SUCCEEDED,
                MonitorRunStatus.PARTIAL,
            }
            if latest_published:
                if self.latest_report is None or (
                    self.latest_report.run_id != self.latest_run.run_id
                    or self.latest_report.status != self.latest_run.status.value
                    or self.latest_report.interval != self.latest_run.interval
                    or self.latest_report.summary != self.latest_run.summary
                ):
                    raise ValueError("published latest run must equal latest report")
            elif self.latest_report is not None and (
                self.latest_report.interval.logical_cutoff_at
                >= self.latest_run.interval.logical_cutoff_at
            ):
                raise ValueError("retained latest report must precede unpublished latest run")
        return self


class MonitorTaskListItemPayload(StrictMonitorPayload):
    task_id: UUID
    name: str = Field(..., min_length=1, max_length=128, strict=True)
    status: MonitorTaskStatus
    branch: MonitorBranchPayload
    schedule: MonitorSchedulePayload
    scheduler: MonitorSchedulerPayload
    latest_run: MonitorRunDigestPayload | None = None
    latest_report: MonitorLatestReportPayload | None = None
    pending_run_count: int = Field(..., ge=0, strict=True)
    last_runner_heartbeat_at: UtcDateTime | None = None
    created_at: UtcDateTime
    updated_at: UtcDateTime

    @model_validator(mode="after")
    def validate_state(self) -> "MonitorTaskListItemPayload":
        if self.updated_at < self.created_at:
            raise ValueError("updated_at cannot precede created_at")
        _validate_task_lifecycle(self.status, self.schedule, self.scheduler)
        if (
            self.latest_run is not None
            and self.latest_run.status in {MonitorRunStatus.QUEUED, MonitorRunStatus.RUNNING}
            and self.pending_run_count < 1
        ):
            raise ValueError("queued/running latest run requires pending_run_count")
        if self.latest_report is not None and self.latest_run is None:
            raise ValueError("latest report requires latest run")
        if self.latest_run is not None:
            latest_published = self.latest_run.status in {
                MonitorRunStatus.SUCCEEDED,
                MonitorRunStatus.PARTIAL,
            }
            if latest_published:
                if self.latest_report is None or (
                    self.latest_report.run_id != self.latest_run.run_id
                    or self.latest_report.status != self.latest_run.status.value
                    or self.latest_report.interval != self.latest_run.interval
                    or self.latest_report.summary != self.latest_run.summary
                ):
                    raise ValueError("published latest run must equal latest report")
            elif self.latest_report is not None and (
                self.latest_report.interval.logical_cutoff_at
                >= self.latest_run.interval.logical_cutoff_at
            ):
                raise ValueError("retained latest report must precede unpublished latest run")
        return self


class MonitorTaskListPayload(StrictMonitorPayload):
    schema_version: Literal[MONITOR_TASK_LIST_SCHEMA_VERSION] = (
        MONITOR_TASK_LIST_SCHEMA_VERSION
    )
    items: list[MonitorTaskListItemPayload] = Field(default_factory=list)
    next_cursor: str | None = Field(
        default=None,
        max_length=512,
        pattern=r"^[A-Za-z0-9_-]+$",
        strict=True,
    )
    has_more: bool = Field(..., strict=True)
    as_of: UtcDateTime


class MonitorRunAttemptPayload(StrictMonitorPayload):
    attempt: int = Field(..., gt=0, strict=True)
    trigger: Literal["scheduled", "automatic_retry", "manual_retry"]
    status: MonitorAttemptStatus
    started_at: UtcDateTime
    finished_at: UtcDateTime | None = None
    errors: list[MonitorPublicErrorPayload] = Field(default_factory=list)

    @model_validator(mode="after")
    def validate_status(self) -> "MonitorRunAttemptPayload":
        running = self.status == MonitorAttemptStatus.RUNNING
        if running == (self.finished_at is not None):
            raise ValueError("only terminal attempts have finished_at")
        if self.finished_at is not None and self.finished_at < self.started_at:
            raise ValueError("attempt finished_at cannot precede started_at")
        if self.status in {
            MonitorAttemptStatus.RUNNING,
            MonitorAttemptStatus.SUCCEEDED,
        } and self.errors:
            raise ValueError("running or succeeded attempts cannot contain public errors")
        if self.status == MonitorAttemptStatus.FAILED and not self.errors:
            raise ValueError("failed attempts require public errors")
        return self


class MonitorRunPayload(StrictMonitorPayload):
    schema_version: Literal[MONITOR_RUN_SCHEMA_VERSION] = MONITOR_RUN_SCHEMA_VERSION
    run_id: UUID
    task_id: UUID
    status: MonitorRunStatus
    interval: MonitorTimeIntervalPayload
    start_revision: int | None = Field(default=None, gt=0, strict=True)
    end_revision: int | None = Field(default=None, gt=0, strict=True)
    attempt_count: int = Field(..., ge=0, strict=True)
    attempts: list[MonitorRunAttemptPayload] = Field(default_factory=list)
    summary: MonitorRunSummaryPayload | None = None
    report_ref: str | None = Field(
        default=None,
        pattern=r"^m3r_[A-Za-z0-9_-]{22}$",
        strict=True,
    )
    report_sha256: str | None = Field(
        default=None,
        pattern=r"^[0-9a-f]{64}$",
        strict=True,
    )
    report_expires_at: UtcDateTime | None = None
    errors: list[MonitorPublicErrorPayload] = Field(default_factory=list)
    created_at: UtcDateTime
    started_at: UtcDateTime | None = None
    finished_at: UtcDateTime | None = None
    updated_at: UtcDateTime

    @model_validator(mode="after")
    def validate_state(self) -> "MonitorRunPayload":
        if self.attempt_count != len(self.attempts):
            raise ValueError("attempt_count must equal the number of public attempts")
        if [attempt.attempt for attempt in self.attempts] != list(
            range(1, self.attempt_count + 1)
        ):
            raise ValueError("attempt numbers must be contiguous and start at one")
        if (self.start_revision is None) != (self.end_revision is None):
            raise ValueError("start_revision and end_revision must appear together")
        if (
            self.start_revision is not None
            and self.end_revision is not None
            and self.end_revision < self.start_revision
        ):
            raise ValueError("end_revision cannot precede start_revision")
        if self.updated_at < self.created_at:
            raise ValueError("updated_at cannot precede created_at")

        report_fields = (self.report_ref, self.report_sha256, self.report_expires_at)
        published = self.status in {MonitorRunStatus.SUCCEEDED, MonitorRunStatus.PARTIAL}
        if published:
            if (
                self.finished_at is None
                or self.summary is None
                or self.start_revision is None
                or any(value is None for value in report_fields)
            ):
                raise ValueError("published run requires revisions, summary, and report metadata")
            if self.summary.error_count != len(self.errors):
                raise ValueError("summary error_count must equal run errors length")
        elif (
            self.start_revision is not None
            or any(value is not None for value in report_fields)
            or self.summary is not None
        ):
            raise ValueError("unpublished run cannot contain revisions or report metadata")

        if self.status == MonitorRunStatus.QUEUED:
            if (
                self.attempts
                or self.errors
                or self.started_at is not None
                or self.finished_at is not None
            ):
                raise ValueError(
                    "queued run cannot have attempts, errors, or execution timestamps"
                )
        elif self.status == MonitorRunStatus.RUNNING:
            if (
                self.started_at is None
                or self.finished_at is not None
                or not self.attempts
                or self.attempts[-1].status != MonitorAttemptStatus.RUNNING
            ):
                raise ValueError("running run requires started_at and no finished_at")
        elif (
            self.started_at is None
            or self.finished_at is None
            or not self.attempts
            or self.attempts[-1].status.value != self.status.value
        ):
            raise ValueError("terminal run must match its final completed attempt")

        if self.attempts and self.errors != self.attempts[-1].errors:
            raise ValueError("run errors must match the final attempt errors")
        return self


class MonitorRunListPayload(StrictMonitorPayload):
    schema_version: Literal[MONITOR_RUN_LIST_SCHEMA_VERSION] = (
        MONITOR_RUN_LIST_SCHEMA_VERSION
    )
    items: list[MonitorRunPayload] = Field(default_factory=list)
    next_cursor: str | None = Field(
        default=None,
        max_length=512,
        pattern=r"^[A-Za-z0-9_-]+$",
        strict=True,
    )
    has_more: bool = Field(..., strict=True)
    as_of: UtcDateTime


class MonitorFieldDefinitionValuePayload(StrictMonitorPayload):
    display_name: str | None = Field(default=None, max_length=256, strict=True)
    declared_type: str = Field(..., min_length=1, max_length=128, strict=True)
    scope: str = Field(..., min_length=1, max_length=128, strict=True)


class MonitorChangeSidePayload(StrictMonitorPayload):
    display_value: str | None = Field(default=None, max_length=8192, strict=True)
    normalized_value: str | None = Field(default=None, max_length=8192, strict=True)
    field_definition: MonitorFieldDefinitionValuePayload | None = None
    row_values: dict[str, str] | None = None

    @model_validator(mode="after")
    def validate_shape(self) -> "MonitorChangeSidePayload":
        scalar = self.display_value is not None or self.normalized_value is not None
        shapes = int(scalar) + int(self.field_definition is not None) + int(self.row_values is not None)
        if shapes != 1:
            raise ValueError("change side must contain exactly one value shape")
        if scalar and (self.display_value is None or self.normalized_value is None):
            raise ValueError("scalar side requires display_value and normalized_value")
        if self.row_values is not None and not self.row_values:
            raise ValueError("row_values cannot be empty")
        return self


class MonitorAttributionPayload(StrictMonitorPayload):
    status: Literal["attributed", "unknown_author", "unresolved"]
    author: str = Field(..., min_length=1, max_length=256, strict=True)
    revision: int | None = Field(default=None, gt=0, strict=True)
    changed_at: UtcDateTime | None = None
    commit_message: str | None = Field(default=None, max_length=512, strict=True)

    @model_validator(mode="after")
    def validate_status(self) -> "MonitorAttributionPayload":
        if self.status == "attributed":
            if self.author == "未知" or self.revision is None or self.changed_at is None:
                raise ValueError("attributed change requires author, revision, and changed_at")
        elif self.status == "unknown_author":
            if self.author != "未知" or self.revision is None or self.changed_at is None:
                raise ValueError("unknown_author keeps revision/time and displays 未知")
        elif self.author != "未知" or any(
            value is not None
            for value in (self.revision, self.changed_at, self.commit_message)
        ):
            raise ValueError("unresolved attribution exposes no invented commit facts")
        return self


class MonitorChangePayload(StrictMonitorPayload):
    change_type: MonitorChangeType
    workbook: str = Field(..., min_length=1, max_length=256, strict=True)
    sheet_name: str = Field(..., min_length=1, max_length=256, strict=True)
    primary_key_field: str = Field(..., min_length=1, max_length=256, strict=True)
    row_key: str | None = Field(..., min_length=1, max_length=1024, strict=True)
    field_name: str | None = Field(default=None, min_length=1, max_length=256, strict=True)
    display_name: str | None = Field(default=None, max_length=256, strict=True)
    source: MonitorChangeSidePayload | None = None
    target: MonitorChangeSidePayload | None = None
    attribution: MonitorAttributionPayload

    @model_validator(mode="after")
    def validate_change_shape(self) -> "MonitorChangePayload":
        structural_change = self.change_type in {
            MonitorChangeType.FIELD_ADDED,
            MonitorChangeType.FIELD_REMOVED,
            MonitorChangeType.FIELD_DEFINITION_MODIFIED,
        }
        field_change = self.change_type in {
            MonitorChangeType.FIELD_MODIFIED,
            MonitorChangeType.FIELD_ADDED,
            MonitorChangeType.FIELD_REMOVED,
            MonitorChangeType.FIELD_DEFINITION_MODIFIED,
        }
        if field_change != (self.field_name is not None):
            raise ValueError("field identity is required only for field changes")
        if structural_change != (self.row_key is None):
            raise ValueError("only structural field changes require a null row_key")

        source_kind = self._side_kind(self.source)
        target_kind = self._side_kind(self.target)
        expected = {
            MonitorChangeType.FIELD_MODIFIED: ("scalar", "scalar"),
            MonitorChangeType.ROW_ADDED: (None, "row"),
            MonitorChangeType.ROW_DELETED: ("row", None),
            MonitorChangeType.FIELD_ADDED: (None, "definition"),
            MonitorChangeType.FIELD_REMOVED: ("definition", None),
            MonitorChangeType.FIELD_DEFINITION_MODIFIED: ("definition", "definition"),
        }[self.change_type]
        if (source_kind, target_kind) != expected:
            raise ValueError("change_type does not match source/target value shapes")
        return self

    @staticmethod
    def _side_kind(side: MonitorChangeSidePayload | None) -> str | None:
        if side is None:
            return None
        if side.row_values is not None:
            return "row"
        if side.field_definition is not None:
            return "definition"
        return "scalar"


class MonitorChangeTypeCountsPayload(StrictMonitorPayload):
    field_modified: int = Field(..., ge=0, strict=True)
    row_added: int = Field(..., ge=0, strict=True)
    row_deleted: int = Field(..., ge=0, strict=True)
    field_added: int = Field(..., ge=0, strict=True)
    field_removed: int = Field(..., ge=0, strict=True)
    field_definition_modified: int = Field(..., ge=0, strict=True)

    def total(self) -> int:
        return sum(self.model_dump().values())


class MonitorReportSummaryPayload(StrictMonitorPayload):
    workbook_count: int = Field(..., ge=0, strict=True)
    changed_workbook_count: int = Field(..., ge=0, strict=True)
    sheet_count: int = Field(..., ge=0, strict=True)
    changed_row_count: int = Field(..., ge=0, strict=True)
    changed_field_count: int = Field(..., ge=0, strict=True)
    author_count: int = Field(..., ge=0, strict=True)
    change_count: int = Field(..., ge=0, strict=True)
    error_count: int = Field(..., ge=0, strict=True)
    by_change_type: MonitorChangeTypeCountsPayload

    @model_validator(mode="after")
    def validate_counts(self) -> "MonitorReportSummaryPayload":
        if self.changed_workbook_count > self.workbook_count:
            raise ValueError("changed_workbook_count cannot exceed workbook_count")
        if self.change_count != self.by_change_type.total():
            raise ValueError("change_count must equal by_change_type total")
        return self


CoverageExclusion = Literal[
    "scope_none_fields",
    "unexported_fields",
    "excel_notes",
    "formulas",
    "styles",
    "macros",
]


class MonitorCoveragePayload(StrictMonitorPayload):
    data_source: Literal["reliable_table_csv"] = "reliable_table_csv"
    compared_scope: Literal["exported_business_fields"] = "exported_business_fields"
    interval_semantics: Literal["left_open_right_closed"] = "left_open_right_closed"
    excluded_content: list[CoverageExclusion]
    unknown_author_count: int = Field(..., ge=0, strict=True)
    unattributed_change_count: int = Field(..., ge=0, strict=True)
    failed_workbook_count: int = Field(..., ge=0, strict=True)

    @field_validator("excluded_content")
    @classmethod
    def validate_exclusions(cls, value: list[CoverageExclusion]) -> list[CoverageExclusion]:
        expected = {
            "scope_none_fields",
            "unexported_fields",
            "excel_notes",
            "formulas",
            "styles",
            "macros",
        }
        if len(value) != len(set(value)) or set(value) != expected:
            raise ValueError("excluded_content must list each frozen exclusion exactly once")
        return value


class MonitorRevisionRangePayload(StrictMonitorPayload):
    start_revision: int = Field(..., gt=0, strict=True)
    end_revision: int = Field(..., gt=0, strict=True)

    @model_validator(mode="after")
    def validate_range(self) -> "MonitorRevisionRangePayload":
        if self.end_revision < self.start_revision:
            raise ValueError("end_revision cannot precede start_revision")
        return self


class MonitorReportFieldPayload(StrictMonitorPayload):
    field_name: str = Field(..., min_length=1, max_length=256, strict=True)
    display_name: str | None = Field(default=None, max_length=256, strict=True)


class MonitorReportSheetFieldsPayload(StrictMonitorPayload):
    workbook: str = Field(..., min_length=1, max_length=256, strict=True)
    sheet_name: str = Field(..., min_length=1, max_length=256, strict=True)
    fields: list[MonitorReportFieldPayload]

    @model_validator(mode="after")
    def validate_fields(self) -> "MonitorReportSheetFieldsPayload":
        names = [field.field_name for field in self.fields]
        if not names or len(names) != len(set(names)):
            raise ValueError("field catalog requires unique non-empty field names")
        return self


class MonitorReportPayload(StrictMonitorPayload):
    schema_version: Literal[MONITOR_REPORT_SCHEMA_VERSION] = MONITOR_REPORT_SCHEMA_VERSION
    report_id: UUID
    run_id: UUID
    task_id: UUID
    task_name: str = Field(..., min_length=1, max_length=128, strict=True)
    status: Literal["succeeded", "partial"]
    branch: MonitorBranchPayload
    interval: MonitorTimeIntervalPayload
    revisions: MonitorRevisionRangePayload
    generated_at: UtcDateTime
    summary: MonitorReportSummaryPayload
    coverage: MonitorCoveragePayload
    field_catalog: list[MonitorReportSheetFieldsPayload] = Field(default_factory=list)
    changes: list[MonitorChangePayload] = Field(default_factory=list)
    errors: list[MonitorPublicErrorPayload] = Field(default_factory=list)

    @model_validator(mode="after")
    def validate_report(self) -> "MonitorReportPayload":
        if self.generated_at < self.interval.end_at:
            raise ValueError("generated_at cannot precede the logical cutoff")
        if self.summary.change_count != len(self.changes):
            raise ValueError("summary change_count must equal changes length")
        if self.summary.error_count != len(self.errors):
            raise ValueError("summary error_count must equal errors length")
        actual_counts = {change_type.value: 0 for change_type in MonitorChangeType}
        for change in self.changes:
            actual_counts[change.change_type.value] += 1
        if self.summary.by_change_type.model_dump() != actual_counts:
            raise ValueError("by_change_type must match changes")

        changed_workbooks = {change.workbook for change in self.changes}
        changed_sheets = {
            (change.workbook, change.sheet_name) for change in self.changes
        }
        catalog_sheets = [
            (catalog.workbook, catalog.sheet_name) for catalog in self.field_catalog
        ]
        if len(catalog_sheets) != len(set(catalog_sheets)):
            raise ValueError("field catalog sheet identities must be unique")
        if not set(catalog_sheets).issubset(changed_sheets):
            raise ValueError("field catalog can only describe changed sheets")
        row_change_sheets = {
            (change.workbook, change.sheet_name)
            for change in self.changes
            if change.change_type in {
                MonitorChangeType.ROW_ADDED,
                MonitorChangeType.ROW_DELETED,
            }
        }
        if self.field_catalog and not row_change_sheets.issubset(catalog_sheets):
            raise ValueError("field catalog must cover every changed row sheet")
        changed_rows = {
            (change.workbook, change.sheet_name, change.row_key)
            for change in self.changes
            if change.row_key is not None
        }
        changed_fields = {
            (change.workbook, change.sheet_name, change.row_key, change.field_name)
            for change in self.changes
            if change.field_name is not None
        }
        known_authors = {
            change.attribution.author
            for change in self.changes
            if change.attribution.status == "attributed"
        }
        unknown_author_count = sum(
            change.attribution.status == "unknown_author" for change in self.changes
        )
        unattributed_change_count = sum(
            change.attribution.status == "unresolved" for change in self.changes
        )
        failed_workbooks = {
            error.workbook for error in self.errors if error.workbook is not None
        }
        expected_summary_counts = {
            "changed_workbook_count": len(changed_workbooks),
            "sheet_count": len(changed_sheets),
            "changed_row_count": len(changed_rows),
            "changed_field_count": len(changed_fields),
            "author_count": len(known_authors),
        }
        for field_name, expected_count in expected_summary_counts.items():
            if getattr(self.summary, field_name) != expected_count:
                raise ValueError(f"summary {field_name} must match changes")
        if self.summary.workbook_count < len(changed_workbooks | failed_workbooks):
            raise ValueError("workbook_count cannot omit changed or failed workbooks")
        if self.coverage.unknown_author_count != unknown_author_count:
            raise ValueError("unknown_author_count must match changes")
        if self.coverage.unattributed_change_count != unattributed_change_count:
            raise ValueError("unattributed_change_count must match changes")
        if self.coverage.failed_workbook_count != len(failed_workbooks):
            raise ValueError("failed_workbook_count must match errors")
        if self.status == "succeeded" and (
            self.errors or self.coverage.unattributed_change_count > 0
        ):
            raise ValueError("incomplete report must use partial status")
        if self.status == "partial" and not (
            self.errors or self.coverage.unattributed_change_count > 0
        ):
            raise ValueError("partial report requires a public coverage gap")
        return self


MonitorContractPayload = (
    MonitorTaskPayload | MonitorTaskListPayload | MonitorRunPayload | MonitorReportPayload
)


def serialize_monitor_json(payload: MonitorContractPayload) -> bytes:
    """Serialize an M3 contract with the repository's canonical JSON format."""
    data = payload.model_dump(mode="json")
    text = json.dumps(data, ensure_ascii=False, indent=2, separators=(",", ": "))
    return (text + "\n").encode("utf-8")
