"""从 TableCsv 前两条逻辑记录生成稳定的 AI 字段目录。"""
from __future__ import annotations

import csv
from dataclasses import dataclass, replace
import hashlib
from io import StringIO
import json
from pathlib import Path
from typing import Any


CATALOG_SCHEMA_VERSION = "ai.field_catalog.v1"
MANIFEST_SCHEMA_VERSION = "ai.field_scan_manifest.v1"


@dataclass(frozen=True)
class FieldCatalogEntry:
    csv_name: str
    column_index: int
    display_name: str
    field_name: str
    quality_issues: tuple[str, ...] = ()

    def as_dict(self) -> dict[str, Any]:
        return {
            "csv_name": self.csv_name,
            "column_index": self.column_index,
            "display_name": self.display_name,
            "field_name": self.field_name,
            "quality_issues": list(self.quality_issues),
        }


@dataclass(frozen=True)
class FileScanResult:
    csv_name: str
    size_bytes: int
    encoding: str | None
    schema_sha256: str | None
    fields: tuple[FieldCatalogEntry, ...]
    issues: tuple[dict[str, Any], ...]

    def manifest_dict(self) -> dict[str, Any]:
        return {
            "csv_name": self.csv_name,
            "size_bytes": self.size_bytes,
            "encoding": self.encoding,
            "schema_sha256": self.schema_sha256,
            "field_count": len(self.fields),
            "issue_count": len(self.issues),
            "issues": list(self.issues),
        }


@dataclass(frozen=True)
class CatalogScanResult:
    source_root: Path
    files: tuple[FileScanResult, ...]

    @property
    def fields(self) -> tuple[FieldCatalogEntry, ...]:
        return tuple(field for file in self.files for field in file.fields)


def _issue(code: str, message: str, **details: Any) -> dict[str, Any]:
    payload: dict[str, Any] = {"code": code, "message": message}
    if details:
        payload["details"] = details
    return payload


def _decode(raw: bytes) -> tuple[str, str]:
    if raw.startswith(b"\xef\xbb\xbf"):
        return raw.decode("utf-8-sig"), "utf-8-sig"
    try:
        return raw.decode("utf-8"), "utf-8"
    except UnicodeDecodeError:
        return raw.decode("gb18030"), "gb18030"


def _schema_sha256(display_row: list[str], field_row: list[str]) -> str:
    raw = json.dumps(
        [display_row, field_row],
        ensure_ascii=False,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()


def scan_csv_header(path: Path) -> FileScanResult:
    try:
        raw = path.read_bytes()
    except OSError:
        return FileScanResult(
            csv_name=path.name,
            size_bytes=0,
            encoding=None,
            schema_sha256=None,
            fields=(),
            issues=(_issue("CSV_READ_FAILED", "CSV 无法读取"),),
        )

    try:
        text, encoding = _decode(raw)
    except UnicodeDecodeError:
        return FileScanResult(
            csv_name=path.name,
            size_bytes=len(raw),
            encoding=None,
            schema_sha256=None,
            fields=(),
            issues=(_issue("CSV_DECODE_FAILED", "CSV 不是有效的 UTF-8 或 GB18030 文本"),),
        )

    reader = csv.reader(StringIO(text, newline=""))
    try:
        display_row = next(reader)
        field_row = next(reader)
    except (StopIteration, csv.Error):
        return FileScanResult(
            csv_name=path.name,
            size_bytes=len(raw),
            encoding=encoding,
            schema_sha256=None,
            fields=(),
            issues=(_issue("CSV_HEADER_INCOMPLETE", "CSV 缺少前两条逻辑记录"),),
        )

    file_issues: list[dict[str, Any]] = []
    if len(display_row) != len(field_row):
        file_issues.append(
            _issue(
                "HEADER_WIDTH_MISMATCH",
                "第一行和第二行列数不一致",
                display_columns=len(display_row),
                field_columns=len(field_row),
            )
        )

    fields: list[FieldCatalogEntry] = []
    named_columns: dict[str, list[int]] = {}
    width = max(len(display_row), len(field_row))
    for index in range(width):
        display_name = display_row[index] if index < len(display_row) else ""
        field_name = field_row[index] if index < len(field_row) else ""
        if field_name.strip() == "":
            if display_name.strip() != "":
                file_issues.append(
                    _issue(
                        "DISPLAY_WITHOUT_FIELD_NAME",
                        "第一行存在中文说明，但第二行字段名为空",
                        column_index=index + 1,
                        display_name=display_name,
                    )
                )
            continue

        quality_issues: list[str] = []
        if display_name.strip() == "":
            quality_issues.append("MISSING_DISPLAY_NAME")
        if field_name != field_name.strip():
            quality_issues.append("FIELD_NAME_SURROUNDING_WHITESPACE")
        if display_name != display_name.strip():
            quality_issues.append("DISPLAY_NAME_SURROUNDING_WHITESPACE")

        fields.append(
            FieldCatalogEntry(
                csv_name=path.name,
                column_index=index + 1,
                display_name=display_name,
                field_name=field_name,
                quality_issues=tuple(quality_issues),
            )
        )
        named_columns.setdefault(field_name, []).append(index + 1)

    duplicate_field_names: set[str] = set()
    for field_name, columns in named_columns.items():
        if len(columns) > 1:
            duplicate_field_names.add(field_name)
            file_issues.append(
                _issue(
                    "DUPLICATE_FIELD_NAME",
                    "同一 CSV 的第二行存在重复字段名",
                    field_name=field_name,
                    columns=columns,
                )
            )
    if duplicate_field_names:
        fields = [
            replace(
                field,
                quality_issues=tuple(
                    dict.fromkeys((*field.quality_issues, "DUPLICATE_FIELD_NAME"))
                ),
            )
            if field.field_name in duplicate_field_names
            else field
            for field in fields
        ]
    if not fields:
        file_issues.append(_issue("NO_NAMED_FIELDS", "第二行不存在有效字段名"))

    return FileScanResult(
        csv_name=path.name,
        size_bytes=len(raw),
        encoding=encoding,
        schema_sha256=_schema_sha256(display_row, field_row),
        fields=tuple(fields),
        issues=tuple(file_issues),
    )


def scan_catalog(source_root: Path) -> CatalogScanResult:
    if not source_root.is_dir():
        raise ValueError(f"CSV 目录不存在：{source_root}")
    paths = sorted(source_root.glob("*.csv"), key=lambda path: (path.name.casefold(), path.name))
    return CatalogScanResult(
        source_root=source_root.resolve(),
        files=tuple(scan_csv_header(path) for path in paths),
    )


def serialize_manifest_json(result: CatalogScanResult) -> bytes:
    inventory = json.dumps(
        [field.as_dict() for field in result.fields],
        ensure_ascii=False,
        separators=(",", ":"),
    ).encode("utf-8")
    files_with_issues = sum(bool(file.issues) for file in result.files)
    manifest = {
        "schema_version": MANIFEST_SCHEMA_VERSION,
        "catalog_schema_version": CATALOG_SCHEMA_VERSION,
        "source_root": str(result.source_root),
        "header_inventory_sha256": hashlib.sha256(inventory).hexdigest(),
        "summary": {
            "csv_count": len(result.files),
            "field_count": len(result.fields),
            "files_with_issues": files_with_issues,
            "issue_count": sum(len(file.issues) for file in result.files),
            "missing_display_name_fields": sum(
                "MISSING_DISPLAY_NAME" in field.quality_issues for field in result.fields
            ),
        },
        "files": [file.manifest_dict() for file in result.files],
    }
    text = json.dumps(manifest, ensure_ascii=False, indent=2, separators=(",", ": "))
    return (text + "\n").encode("utf-8")
