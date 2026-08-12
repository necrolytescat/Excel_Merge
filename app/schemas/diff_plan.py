"""M4 DiffPlan 计划管理契约。"""
from __future__ import annotations

from datetime import datetime
from pathlib import PurePosixPath
from typing import Annotated, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


StrictRevision = Annotated[int, Field(gt=0, strict=True)] | Literal["HEAD"]


class StrictPayload(BaseModel):
    model_config = ConfigDict(extra="forbid")


class DiffPlanDefinitionPayload(StrictPayload):
    name: str = Field(..., min_length=1, max_length=128)
    source_endpoint_id: str = Field(..., min_length=1, max_length=128)
    target_endpoint_ids: list[str] = Field(..., min_length=1, max_length=4)
    workbook_paths: list[str] = Field(..., min_length=1, max_length=10)

    @field_validator("name")
    @classmethod
    def normalize_name(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized:
            raise ValueError("计划名称不能为空")
        return normalized

    @field_validator("source_endpoint_id")
    @classmethod
    def normalize_source_endpoint(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized:
            raise ValueError("基准分支不能为空")
        return normalized

    @field_validator("target_endpoint_ids")
    @classmethod
    def validate_targets(cls, value: list[str]) -> list[str]:
        normalized = [item.strip() for item in value]
        if any(not item for item in normalized):
            raise ValueError("目标分支不能为空")
        if len(set(normalized)) != len(normalized):
            raise ValueError("目标分支不能重复")
        return normalized

    @field_validator("workbook_paths")
    @classmethod
    def validate_workbooks(cls, value: list[str]) -> list[str]:
        normalized = [item.replace("\\", "/").strip("/") for item in value]
        if any(not item for item in normalized):
            raise ValueError("工作簿路径不能为空")
        if any(PurePosixPath(item).is_absolute() or ".." in PurePosixPath(item).parts for item in normalized):
            raise ValueError("工作簿路径不合法")
        if len({item.casefold() for item in normalized}) != len(normalized):
            raise ValueError("工作簿路径不能重复")
        return normalized

    @model_validator(mode="after")
    def validate_endpoint_roles(self):
        if self.source_endpoint_id in self.target_endpoint_ids:
            raise ValueError("基准分支不能同时作为目标分支")
        return self


class DiffPlanCreateRequestPayload(DiffPlanDefinitionPayload):
    schema_version: Literal["m4.diff-plan-create.request.v1"]
    request_id: UUID


class DiffPlanUpdateRequestPayload(DiffPlanDefinitionPayload):
    schema_version: Literal["m4.diff-plan-update.request.v1"]
    request_id: UUID
    expected_version: int = Field(..., ge=1, strict=True)


class DiffPlanCommandRequestPayload(StrictPayload):
    schema_version: Literal["m4.diff-plan-command.request.v1"]
    request_id: UUID
    expected_version: int = Field(..., ge=1, strict=True)


class DiffPlanPayload(DiffPlanDefinitionPayload):
    schema_version: Literal["m4.diff-plan.v1"] = "m4.diff-plan.v1"
    plan_id: UUID
    version: int = Field(..., ge=1)
    archived: bool
    created_at: datetime
    updated_at: datetime
    archived_at: datetime | None = None
    recent_run: "DiffPlanRunSummaryPayload | None" = None


class DiffPlanListPayload(StrictPayload):
    schema_version: Literal["m4.diff-plan-list.v1"] = "m4.diff-plan-list.v1"
    plans: list[DiffPlanPayload]
    total: int = Field(..., ge=0)


class WorkbookCatalogRequestPayload(StrictPayload):
    schema_version: Literal["m4.workbook-catalog.request.v1"]
    endpoint_id: str = Field(..., min_length=1, max_length=128)
    revision: StrictRevision = "HEAD"


class WorkbookCatalogItemPayload(StrictPayload):
    path: str
    size_bytes: int | None = Field(None, ge=0)
    svn_revision: int | str | None = None


class WorkbookCatalogPayload(StrictPayload):
    schema_version: Literal["m4.workbook-catalog.v1"] = "m4.workbook-catalog.v1"
    endpoint_id: str
    endpoint_label: str
    resolved_revision: int = Field(..., gt=0)
    table_path: str
    workbooks: list[WorkbookCatalogItemPayload]
    total: int = Field(..., ge=0)


DiffPlanRunStatus = Literal[
    "queued", "preparing", "running", "cancelling",
    "completed", "completed_with_failures", "cancelled", "failed",
]
DiffPlanItemStatus = Literal[
    "queued", "running", "identical", "semantic_equal", "changed",
    "source_missing", "target_missing", "both_missing", "read_failed",
    "business_failed", "orchestration_failed", "cancelled",
]


class DiffPlanRunStartRequestPayload(StrictPayload):
    schema_version: Literal["m4.diff-plan-run-start.request.v1"]
    request_id: UUID
    revisions: dict[str, StrictRevision] = Field(default_factory=dict, max_length=5)


class DiffPlanRunCommandRequestPayload(StrictPayload):
    schema_version: Literal["m4.diff-plan-run-command.request.v1"]
    request_id: UUID


class DiffPlanRunRetryRequestPayload(StrictPayload):
    schema_version: Literal["m4.diff-plan-run-retry.request.v1"]
    request_id: UUID
    item_ids: list[UUID] | None = Field(None, min_length=1, max_length=40)


class DiffPlanRunProgressPayload(StrictPayload):
    total_items: int = Field(..., ge=1, le=40)
    processed_items: int = Field(..., ge=0, le=40)
    identical_items: int = Field(0, ge=0)
    semantic_equal_items: int = Field(0, ge=0)
    changed_items: int = Field(0, ge=0)
    missing_items: int = Field(0, ge=0)
    failed_items: int = Field(0, ge=0)
    cancelled_items: int = Field(0, ge=0)
    ratio: float = Field(..., ge=0, le=1)


class DiffPlanRunErrorPayload(StrictPayload):
    code: str
    message: str
    retryable: bool


class DiffPlanRunItemPayload(StrictPayload):
    item_id: UUID
    retry_of_item_id: UUID | None = None
    ordinal: int = Field(..., ge=0, le=39)
    workbook_path: str
    target_endpoint_id: str
    status: DiffPlanItemStatus
    candidate_status: Literal["identical", "modified", "left_only", "right_only", "both_missing", "read_error"] | None = None
    source_exists: bool | None = None
    target_exists: bool | None = None
    source_sha256: str | None = None
    target_sha256: str | None = None
    diff_status: Literal["unchanged", "modified", "partial", "failed"] | None = None
    diff_error_count: int = Field(0, ge=0)
    result_ref: str | None = None
    error: DiffPlanRunErrorPayload | None = None
    started_at: datetime | None = None
    finished_at: datetime | None = None


class DiffPlanRunSummaryPayload(StrictPayload):
    schema_version: Literal["m4.diff-plan-run-summary.v1"] = "m4.diff-plan-run-summary.v1"
    run_id: UUID
    plan_id: UUID
    retry_of_run_id: UUID | None = None
    status: DiffPlanRunStatus
    source_endpoint_id: str
    target_endpoint_ids: list[str]
    source_revision: int = Field(..., gt=0)
    target_revisions: dict[str, int]
    progress: DiffPlanRunProgressPayload
    created_at: datetime
    started_at: datetime | None = None
    finished_at: datetime | None = None


class DiffPlanRunPayload(DiffPlanRunSummaryPayload):
    schema_version: Literal["m4.diff-plan-run.v1"] = "m4.diff-plan-run.v1"
    plan_version: int = Field(..., ge=1)
    plan_name: str
    workbook_paths: list[str]
    items: list[DiffPlanRunItemPayload]
    cancel_requested_at: datetime | None = None
    details_expires_at: datetime | None = None
    details_expired: bool = False
    errors: list[DiffPlanRunErrorPayload] = Field(default_factory=list)


class DiffPlanRunListPayload(StrictPayload):
    schema_version: Literal["m4.diff-plan-run-list.v1"] = "m4.diff-plan-run-list.v1"
    runs: list[DiffPlanRunSummaryPayload]
    total: int = Field(..., ge=0)
