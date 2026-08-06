"""M2-07 批量 Diff 请求与响应契约。"""
from __future__ import annotations

from datetime import datetime
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


BATCH_SCHEMA_VERSION = "m2.batch.v1"
BATCH_CREATE_SCHEMA_VERSION = "m2.batch-create.request.v1"
BATCH_CANCEL_SCHEMA_VERSION = "m2.batch-cancel.request.v1"
BATCH_RETRY_SCHEMA_VERSION = "m2.batch-retry.request.v1"

TaskStatus = Literal[
    "queued",
    "preparing",
    "running",
    "cancelling",
    "completed",
    "completed_with_failures",
    "cancelled",
    "failed",
]
ItemStatus = Literal[
    "queued",
    "running",
    "succeeded",
    "business_failed",
    "orchestration_failed",
    "skipped",
    "cancelled",
]
CandidateStatus = Literal["modified", "left_only", "right_only", "read_error"]
DiffStatus = Literal["unchanged", "modified", "partial", "failed"]
CandidateSourceStatus = Literal["pending", "preparing", "ready", "failed"]
CandidateScope = Literal["all", "retry_subset"]
ReasonCode = Literal[
    "BATCH_CANDIDATE_LEFT_ONLY",
    "BATCH_CANDIDATE_RIGHT_ONLY",
    "BATCH_CANDIDATE_READ_ERROR",
    "BATCH_CANCELLED_BEFORE_START",
]


class StrictBatchPayload(BaseModel):
    model_config = ConfigDict(extra="forbid")


class BatchEndpointPayload(StrictBatchPayload):
    endpoint_id: str = Field(
        ...,
        min_length=1,
        max_length=128,
        pattern=r"^[A-Za-z0-9._-]+$",
        strict=True,
    )
    revision: int = Field(..., gt=0, strict=True)


class BatchCreateRequestPayload(StrictBatchPayload):
    schema_version: Literal[BATCH_CREATE_SCHEMA_VERSION]
    request_id: UUID
    source: BatchEndpointPayload
    target: BatchEndpointPayload


class BatchCancelRequestPayload(StrictBatchPayload):
    schema_version: Literal[BATCH_CANCEL_SCHEMA_VERSION]
    request_id: UUID
    reason: str | None = Field(default=None, max_length=256, strict=True)

    @field_validator("reason")
    @classmethod
    def validate_reason(cls, value: str | None) -> str | None:
        if value is None:
            return None
        stripped = value.strip()
        if not stripped:
            return None
        if any(ord(character) < 32 for character in stripped):
            raise ValueError("reason 不能包含控制字符")
        return stripped


class BatchRetryRequestPayload(StrictBatchPayload):
    schema_version: Literal[BATCH_RETRY_SCHEMA_VERSION]
    request_id: UUID
    item_ids: list[UUID] | None = Field(default=None, max_length=500)

    @field_validator("item_ids")
    @classmethod
    def validate_item_ids(cls, value: list[UUID] | None) -> list[UUID] | None:
        if value is None:
            return None
        if not value:
            raise ValueError("item_ids 不能为空数组")
        if len(set(value)) != len(value):
            raise ValueError("item_ids 不能重复")
        return value


class BatchReadErrorPayload(StrictBatchPayload):
    code: str = Field(..., min_length=1, max_length=128, strict=True)
    message: str = Field(..., min_length=1, max_length=1024, strict=True)


class BatchCandidateSidePayload(StrictBatchPayload):
    exists: bool
    size_bytes: int | None = Field(default=None, ge=0, strict=True)
    content_sha256: str | None = Field(
        default=None,
        pattern=r"^[0-9a-f]{64}$",
        strict=True,
    )
    read_error: BatchReadErrorPayload | None = None

    @model_validator(mode="after")
    def validate_presence(self) -> "BatchCandidateSidePayload":
        if not self.exists and any(
            value is not None
            for value in (self.size_bytes, self.content_sha256, self.read_error)
        ):
            raise ValueError("不存在的一侧不能包含文件信息")
        if self.read_error is not None and self.content_sha256 is not None:
            raise ValueError("读取失败的一侧不能同时包含 content_sha256")
        return self


class BatchCandidatePayload(StrictBatchPayload):
    path: str = Field(..., min_length=1, max_length=1024, strict=True)
    status: CandidateStatus
    source: BatchCandidateSidePayload
    target: BatchCandidateSidePayload
    fingerprint_sha256: str = Field(..., pattern=r"^[0-9a-f]{64}$", strict=True)

    @model_validator(mode="after")
    def validate_status(self) -> "BatchCandidatePayload":
        if self.status == "left_only" and not (self.source.exists and not self.target.exists):
            raise ValueError("left_only 的两侧存在性不一致")
        if self.status == "right_only" and not (not self.source.exists and self.target.exists):
            raise ValueError("right_only 的两侧存在性不一致")
        if self.status in {"modified", "read_error"} and not (
            self.source.exists and self.target.exists
        ):
            raise ValueError("双侧候选必须在两侧存在")
        if self.status == "read_error" and not (
            self.source.read_error or self.target.read_error
        ):
            raise ValueError("read_error 候选必须包含侧别读取错误")
        return self


class BatchCandidateSourcePayload(StrictBatchPayload):
    kind: Literal["m1_server_revalidated"] = "m1_server_revalidated"
    ruleset: Literal["m1.table-excel-candidates.v1"] = (
        "m1.table-excel-candidates.v1"
    )
    scope: CandidateScope
    status: CandidateSourceStatus
    manifest_sha256: str | None = Field(
        default=None,
        pattern=r"^[0-9a-f]{64}$",
        strict=True,
    )

    @model_validator(mode="after")
    def validate_manifest(self) -> "BatchCandidateSourcePayload":
        if (self.status == "ready") != (self.manifest_sha256 is not None):
            raise ValueError("ready 与 manifest_sha256 必须同时出现")
        return self


class BatchExecutionPolicyPayload(StrictBatchPayload):
    global_concurrency: Literal[2] = 2
    per_task_concurrency: Literal[1] = 1
    item_timeout_seconds: Literal[600] = 600
    automatic_retry_attempts: Literal[0] = 0
    max_recovery_attempts: Literal[1] = 1


class BatchProgressPayload(StrictBatchPayload):
    total_items: int | None = Field(default=None, ge=0, strict=True)
    queued_items: int = Field(default=0, ge=0, strict=True)
    running_items: int = Field(default=0, ge=0, strict=True)
    succeeded_items: int = Field(default=0, ge=0, strict=True)
    business_failed_items: int = Field(default=0, ge=0, strict=True)
    orchestration_failed_items: int = Field(default=0, ge=0, strict=True)
    skipped_items: int = Field(default=0, ge=0, strict=True)
    cancelled_items: int = Field(default=0, ge=0, strict=True)
    processed_items: int = Field(default=0, ge=0, strict=True)
    ratio: float | None = Field(default=None, ge=0, le=1)

    @model_validator(mode="after")
    def validate_counts(self) -> "BatchProgressPayload":
        terminal = (
            self.succeeded_items
            + self.business_failed_items
            + self.orchestration_failed_items
            + self.skipped_items
            + self.cancelled_items
        )
        if self.processed_items != terminal:
            raise ValueError("processed_items 与终态计数不一致")
        if self.total_items is None:
            if self.ratio is not None or any(
                (
                    self.queued_items,
                    self.running_items,
                    terminal,
                )
            ):
                raise ValueError("候选准备前不能包含进度")
            return self
        total = self.queued_items + self.running_items + terminal
        if total != self.total_items:
            raise ValueError("total_items 与状态计数不一致")
        expected = 1.0 if self.total_items == 0 else terminal / self.total_items
        if self.ratio is None or abs(self.ratio - expected) > 1e-9:
            raise ValueError("ratio 与终态计数不一致")
        return self


class BatchOrchestrationErrorPayload(StrictBatchPayload):
    code: str = Field(..., min_length=1, max_length=128, strict=True)
    message: str = Field(..., min_length=1, max_length=1024, strict=True)
    retryable: bool


class BatchItemPayload(StrictBatchPayload):
    item_id: UUID
    retry_of_item_id: UUID | None = None
    ordinal: int = Field(..., ge=0, strict=True)
    candidate: BatchCandidatePayload
    status: ItemStatus
    diff_status: DiffStatus | None = None
    diff_error_count: int | None = Field(default=None, ge=0, strict=True)
    result_ref: str | None = Field(
        default=None,
        pattern=r"^m2r_[A-Za-z0-9_-]{22}$",
        strict=True,
    )
    result_sha256: str | None = Field(
        default=None,
        pattern=r"^[0-9a-f]{64}$",
        strict=True,
    )
    result_size_bytes: int | None = Field(default=None, ge=0, strict=True)
    result_expires_at: datetime | None = None
    orchestration_error: BatchOrchestrationErrorPayload | None = None
    reason_code: ReasonCode | None = None
    attempt_count: int = Field(default=0, ge=0, strict=True)
    recovery_count: int = Field(default=0, ge=0, le=1, strict=True)
    created_at: datetime
    started_at: datetime | None = None
    finished_at: datetime | None = None
    updated_at: datetime

    @model_validator(mode="after")
    def validate_state_fields(self) -> "BatchItemPayload":
        result_fields = (
            self.result_ref,
            self.result_sha256,
            self.result_size_bytes,
            self.result_expires_at,
        )
        if self.status == "succeeded":
            if self.diff_status not in {"unchanged", "modified"} or any(
                value is None for value in result_fields
            ):
                raise ValueError("succeeded 必须包含成功业务状态和完整结果引用")
        elif self.status == "business_failed":
            if self.diff_status not in {"partial", "failed"} or any(
                value is None for value in result_fields
            ):
                raise ValueError("business_failed 必须包含失败业务状态和完整结果引用")
        elif any(value is not None for value in result_fields) or self.diff_status is not None:
            raise ValueError("未生成合法业务结果的状态不能包含结果引用")
        if self.status == "orchestration_failed" and self.orchestration_error is None:
            raise ValueError("orchestration_failed 必须包含编排错误")
        if self.status != "orchestration_failed" and self.orchestration_error is not None:
            raise ValueError("仅 orchestration_failed 可包含编排错误")
        if self.status in {"skipped", "cancelled"} and self.reason_code is None:
            raise ValueError("跳过或取消必须包含 reason_code")
        if self.status not in {"skipped", "cancelled"} and self.reason_code is not None:
            raise ValueError("当前状态不能包含 reason_code")
        if self.status in {
            "succeeded",
            "business_failed",
            "orchestration_failed",
            "skipped",
            "cancelled",
        } and self.finished_at is None:
            raise ValueError("终态单项必须包含 finished_at")
        return self


class BatchTaskErrorPayload(StrictBatchPayload):
    code: str = Field(..., min_length=1, max_length=128, strict=True)
    message: str = Field(..., min_length=1, max_length=1024, strict=True)
    retryable: bool


class BatchTaskPayload(StrictBatchPayload):
    schema_version: Literal[BATCH_SCHEMA_VERSION] = BATCH_SCHEMA_VERSION
    task_id: UUID
    request_id: UUID
    retry_of_task_id: UUID | None = None
    status: TaskStatus
    source: BatchEndpointPayload
    target: BatchEndpointPayload
    candidate_source: BatchCandidateSourcePayload
    execution_policy: BatchExecutionPolicyPayload = Field(
        default_factory=BatchExecutionPolicyPayload
    )
    progress: BatchProgressPayload
    items: list[BatchItemPayload] = Field(default_factory=list)
    errors: list[BatchTaskErrorPayload] = Field(default_factory=list)
    created_at: datetime
    updated_at: datetime
    preparation_started_at: datetime | None = None
    prepared_at: datetime | None = None
    started_at: datetime | None = None
    cancel_requested_at: datetime | None = None
    finished_at: datetime | None = None
    expires_at: datetime | None = None

    @model_validator(mode="after")
    def validate_task(self) -> "BatchTaskPayload":
        terminal = self.status in {
            "completed",
            "completed_with_failures",
            "cancelled",
            "failed",
        }
        if terminal != (self.finished_at is not None and self.expires_at is not None):
            raise ValueError("任务终态与完成/过期时间不一致")
        if self.status == "failed" and not self.errors:
            raise ValueError("failed 任务必须包含任务级错误")
        if self.candidate_source.status == "ready":
            if self.progress.total_items != len(self.items):
                raise ValueError("任务 item 数量与 total_items 不一致")
        elif self.items:
            raise ValueError("候选清单 ready 前不能返回 items")
        return self
