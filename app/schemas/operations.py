from __future__ import annotations

from datetime import datetime
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field


OPERATIONS_LOG_LIST_SCHEMA_VERSION = "m2.operations-log-list.v1"
SVN_CACHE_STATUS_SCHEMA_VERSION = "m2.svn-cache-status.v1"
SVN_CACHE_CLEAR_REQUEST_SCHEMA_VERSION = "m2.svn-cache-clear.request.v1"
SVN_CACHE_CLEAR_RESULT_SCHEMA_VERSION = "m2.svn-cache-clear.result.v1"


class StrictOperationsPayload(BaseModel):
    model_config = ConfigDict(extra="forbid")


class OperationsLogEntryPayload(StrictOperationsPayload):
    event_id: UUID
    created_at: datetime
    level: Literal["debug", "info", "warning", "error"]
    logger: str = Field(..., min_length=1, max_length=128, strict=True)
    event: str = Field(
        ...,
        min_length=1,
        max_length=64,
        pattern=r"^[a-z0-9._-]+$",
        strict=True,
    )
    message: str = Field(..., min_length=1, max_length=1024, strict=True)
    request_id: UUID | None = None
    task_id: UUID | None = None
    process_id: int = Field(..., gt=0, strict=True)


class OperationsLogListPayload(StrictOperationsPayload):
    schema_version: Literal[OPERATIONS_LOG_LIST_SCHEMA_VERSION] = (
        OPERATIONS_LOG_LIST_SCHEMA_VERSION
    )
    items: list[OperationsLogEntryPayload] = Field(default_factory=list)
    next_cursor: str | None = Field(
        default=None,
        max_length=512,
        pattern=r"^[A-Za-z0-9_-]+$",
        strict=True,
    )
    has_more: bool
    as_of: datetime


class SVNCacheStatusPayload(StrictOperationsPayload):
    schema_version: Literal[SVN_CACHE_STATUS_SCHEMA_VERSION] = (
        SVN_CACHE_STATUS_SCHEMA_VERSION
    )
    scope: Literal["global_shared"] = "global_shared"
    reproducible: Literal[True] = True
    enabled: bool
    can_clear: bool
    file_count: int = Field(..., ge=0, strict=True)
    size_bytes: int = Field(..., ge=0, strict=True)
    ignored_file_count: int = Field(..., ge=0, strict=True)
    memory_entry_count: int = Field(..., ge=0, strict=True)
    session_memory_hits: int = Field(..., ge=0, strict=True)
    session_disk_hits: int = Field(..., ge=0, strict=True)
    session_misses: int = Field(..., ge=0, strict=True)
    session_writes: int = Field(..., ge=0, strict=True)
    session_hit_rate: float | None = Field(default=None, ge=0, le=1)
    last_modified_at: datetime | None = None


class SVNCacheClearRequestPayload(StrictOperationsPayload):
    schema_version: Literal[SVN_CACHE_CLEAR_REQUEST_SCHEMA_VERSION]
    request_id: UUID
    confirmation: Literal["清空全局 SVN 缓存"]


class SVNCacheClearResultPayload(StrictOperationsPayload):
    schema_version: Literal[SVN_CACHE_CLEAR_RESULT_SCHEMA_VERSION] = (
        SVN_CACHE_CLEAR_RESULT_SCHEMA_VERSION
    )
    request_id: UUID
    removed_file_count: int = Field(..., ge=0, strict=True)
    removed_size_bytes: int = Field(..., ge=0, strict=True)
    cleared_memory_entry_count: int = Field(..., ge=0, strict=True)
    completed_at: datetime
