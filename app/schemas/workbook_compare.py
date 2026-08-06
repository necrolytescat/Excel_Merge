"""M2 单工作簿 Web 编排请求契约。"""
from __future__ import annotations

from pathlib import PurePosixPath
import re
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator


REQUEST_SCHEMA_VERSION = "m2.workbook-compare.request.v1"
_ENDPOINT_ID_PATTERN = r"^[A-Za-z0-9._-]+$"
_WINDOWS_DRIVE = re.compile(r"^[A-Za-z]:")


class StrictRequestPayload(BaseModel):
    model_config = ConfigDict(extra="forbid")


class WorkbookCompareEndpointPayload(StrictRequestPayload):
    endpoint_id: str = Field(
        ...,
        min_length=1,
        max_length=128,
        pattern=_ENDPOINT_ID_PATTERN,
        strict=True,
    )
    revision: int = Field(..., gt=0, strict=True)


class WorkbookCompareRequestPayload(StrictRequestPayload):
    schema_version: Literal[REQUEST_SCHEMA_VERSION]
    request_id: UUID
    source: WorkbookCompareEndpointPayload
    target: WorkbookCompareEndpointPayload
    workbook_path: str = Field(..., min_length=1, max_length=1024, strict=True)

    @field_validator("workbook_path")
    @classmethod
    def validate_workbook_path(cls, value: str) -> str:
        if value != value.strip() or chr(92) in value or "\x00" in value:
            raise ValueError("workbook_path 必须是使用 / 的逻辑相对路径")
        if any(ord(character) < 32 for character in value):
            raise ValueError("workbook_path 不能包含控制字符")
        if value.startswith(("/", "//")) or _WINDOWS_DRIVE.match(value):
            raise ValueError("workbook_path 不能是绝对路径")
        if "://" in value:
            raise ValueError("workbook_path 不能是 URL")

        parts = value.split("/")
        if any(part in {"", ".", ".."} for part in parts):
            raise ValueError("workbook_path 不能包含空段、. 或 ..")
        if PurePosixPath(value).name in {"", ".", ".."}:
            raise ValueError("workbook_path 必须指向工作簿")
        return value
