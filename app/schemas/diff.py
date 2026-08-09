"""M2 单工作簿语义 Diff 的稳定 JSON 契约。"""
from __future__ import annotations

from enum import Enum
import json
from typing import Any

from pydantic import BaseModel, ConfigDict, Field


SCHEMA_VERSION = "m2.diff.v1"


class StrictPayload(BaseModel):
    model_config = ConfigDict(extra="forbid")


class WorkbookStatus(str, Enum):
    UNCHANGED = "unchanged"
    MODIFIED = "modified"
    PARTIAL = "partial"
    FAILED = "failed"


class SheetStatus(str, Enum):
    UNCHANGED = "unchanged"
    MODIFIED = "modified"
    SOURCE_ONLY = "source_only"
    TARGET_ONLY = "target_only"
    FAILED = "failed"


class RowStatus(str, Enum):
    MODIFIED = "modified"
    SOURCE_ONLY = "source_only"
    TARGET_ONLY = "target_only"


class FieldStatus(str, Enum):
    COMMON = "common"
    MODIFIED = "modified"
    SOURCE_ONLY = "source_only"
    TARGET_ONLY = "target_only"


class ErrorStage(str, Enum):
    WORKBOOK_PARSE = "workbook_parse"
    MANIFEST_PARSE = "manifest_parse"
    CSV_READ = "csv_read"
    CSV_PARSE = "csv_parse"
    DIFF = "diff"


class DiffDirectionPayload(StrictPayload):
    source: str
    target: str


class WorkbookDiffPayload(StrictPayload):
    name: str
    status: WorkbookStatus
    source_sha256: str
    target_sha256: str


class WorkbookSummaryPayload(StrictPayload):
    total_sheets: int = 0
    unchanged_sheets: int = 0
    modified_sheets: int = 0
    source_only_sheets: int = 0
    target_only_sheets: int = 0
    failed_sheets: int = 0
    source_only_rows: int = 0
    target_only_rows: int = 0
    modified_rows: int = 0
    modified_fields: int = 0
    error_count: int = 0


class CsvFilePayload(StrictPayload):
    name: str
    sha256: str


class SheetSummaryPayload(StrictPayload):
    source_only_rows: int = 0
    target_only_rows: int = 0
    modified_rows: int = 0
    modified_fields: int = 0


class FieldDefinitionPayload(StrictPayload):
    name: str
    status: FieldStatus
    source_display_name: str | None = None
    target_display_name: str | None = None
    source_type: str | None = None
    target_type: str | None = None
    source_scope: str | None = None
    target_scope: str | None = None


class RowSidePayload(StrictPayload):
    row_number: int
    values: dict[str, str] = Field(default_factory=dict)


class FieldChangePayload(StrictPayload):
    field: str
    status: FieldStatus
    source: str | None = None
    target: str | None = None


class RowDiffPayload(StrictPayload):
    key: str
    status: RowStatus
    source: RowSidePayload | None = None
    target: RowSidePayload | None = None
    changes: list[FieldChangePayload] = Field(default_factory=list)


class DiffErrorPayload(StrictPayload):
    code: str
    stage: ErrorStage
    side: str | None = None
    workbook: str
    sheet_name: str | None = None
    file: str | None = None
    message: str
    details: dict[str, Any] = Field(default_factory=dict)


class SheetDiffPayload(StrictPayload):
    sheet_name: str
    status: SheetStatus
    primary_key: str | None = None
    source_csv: CsvFilePayload | None = None
    target_csv: CsvFilePayload | None = None
    summary: SheetSummaryPayload = Field(default_factory=SheetSummaryPayload)
    fields: list[FieldDefinitionPayload] = Field(default_factory=list)
    rows: list[RowDiffPayload] = Field(default_factory=list)
    errors: list[DiffErrorPayload] = Field(default_factory=list)


class DiffResultPayload(StrictPayload):
    schema_version: str = SCHEMA_VERSION
    direction: DiffDirectionPayload
    workbook: WorkbookDiffPayload
    summary: WorkbookSummaryPayload
    sheets: list[SheetDiffPayload] = Field(default_factory=list)
    errors: list[DiffErrorPayload] = Field(default_factory=list)


def serialize_diff_json(payload: DiffResultPayload) -> bytes:
    """使用唯一格式生成可做 SHA-256 回归的 UTF-8 JSON。"""
    data = payload.model_dump(mode="json")
    text = json.dumps(data, ensure_ascii=False, indent=2, separators=(",", ": "))
    return (text + "\n").encode("utf-8")
