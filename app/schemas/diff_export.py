"""Contracts for exporting selected rows from an M2 diff result."""
from __future__ import annotations

from enum import Enum
from typing import Literal

from pydantic import Field

from app.schemas.diff import StrictPayload


SCHEMA_VERSION = "m2.export.v1"
VALIDATION_SCHEMA_VERSION = "m2.export-validation.v1"


class ExportSide(str, Enum):
    SOURCE = "source"
    TARGET = "target"


class ExportAction(str, Enum):
    WRITE = "write"
    DELETE = "delete"


class ExportRowDecision(StrictPayload):
    key: str = Field(min_length=1)
    action: ExportAction
    value_side: ExportSide | None = None


class ExportSheetSelection(StrictPayload):
    sheet_name: str = Field(min_length=1)
    decisions: list[ExportRowDecision] = Field(min_length=1)


class DiffExportRequestPayload(StrictPayload):
    schema_version: Literal[SCHEMA_VERSION] = SCHEMA_VERSION
    target_layout: ExportSide
    sheets: list[ExportSheetSelection] = Field(min_length=1)


class ExportValidationIssue(StrictPayload):
    code: str
    message: str
    sheet_name: str | None = None
    key: str | None = None
    field: str | None = None
    details: dict[str, str | int | float | bool | None] = Field(default_factory=dict)


class DiffExportValidationPayload(StrictPayload):
    schema_version: Literal[VALIDATION_SCHEMA_VERSION] = VALIDATION_SCHEMA_VERSION
    issues: list[ExportValidationIssue] = Field(default_factory=list)


class ExportSheetSummary(StrictPayload):
    sheet_name: str
    output_sheet_name: str
    write_count: int = 0
    delete_count: int = 0
    fallback_count: int = 0
    omitted_field_count: int = 0


class DiffExportSummaryPayload(StrictPayload):
    target_layout: ExportSide
    workbook_name: str
    sheets: list[ExportSheetSummary] = Field(default_factory=list)

