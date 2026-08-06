"""按 TableCsv 固定布局严格解析单个业务 CSV。"""
from __future__ import annotations

import csv
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from io import StringIO
import re

from core.m2_errors import M2ProcessingError


_INTEGER_TYPE = re.compile(r"^(?:u?int(?:8|16|32|64)?|long|short|byte|sbyte)$", re.IGNORECASE)
_DECIMAL_TYPE = re.compile(r"^(?:float|double|decimal|number)$", re.IGNORECASE)


@dataclass(frozen=True)
class CsvField:
    name: str
    declared_type: str
    scope: str
    position: int


@dataclass(frozen=True)
class CsvDataRow:
    row_number: int
    key: str
    values: dict[str, str]
    normalized_values: dict[str, str]


@dataclass(frozen=True)
class ParsedTableCsv:
    name: str
    fields: tuple[CsvField, ...]
    primary_key: str
    rows: tuple[CsvDataRow, ...]


def _normalize_value(value: str, declared_type: str) -> str:
    if value == "":
        return ""
    type_name = declared_type.strip()
    try:
        if _INTEGER_TYPE.fullmatch(type_name):
            return str(int(value, 10))
        if _DECIMAL_TYPE.fullmatch(type_name):
            number = Decimal(value)
            if not number.is_finite():
                return value
            normalized = number.normalize()
            return format(normalized, "f")
        if type_name.casefold() in {"bool", "boolean"}:
            folded = value.casefold()
            if folded in {"1", "true"}:
                return "true"
            if folded in {"0", "false"}:
                return "false"
        if type_name.casefold() == "date":
            return date.fromisoformat(value).isoformat()
        if type_name.casefold() in {"datetime", "timestamp"}:
            return datetime.fromisoformat(value).isoformat()
    except (InvalidOperation, ValueError):
        return value
    return value


def _row_at(records: list[list[str]], row_number: int, file_name: str) -> list[str]:
    if row_number < 1 or row_number > len(records):
        raise M2ProcessingError(
            "M2_CSV_STRUCTURE_INVALID",
            "csv_parse",
            f"CSV 缺少第 {row_number} 条逻辑记录",
            file_name=file_name,
            details={"required_row": row_number, "record_count": len(records)},
        )
    return records[row_number - 1]


def parse_table_csv(
    raw: bytes,
    file_name: str,
    *,
    display_name_row: int = 1,
    field_name_row: int = 2,
    field_type_row: int = 3,
    field_scope_row: int = 4,
    data_start_row: int = 8,
    primary_key_fields: tuple[str, ...] = ("Id", "id"),
) -> ParsedTableCsv:
    try:
        text = raw.decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        raise M2ProcessingError(
            "M2_CSV_DECODE_FAILED",
            "csv_parse",
            "CSV 不是有效的 UTF-8 文本",
            file_name=file_name,
        ) from exc

    try:
        records = list(csv.reader(StringIO(text, newline="")))
    except csv.Error as exc:
        raise M2ProcessingError(
            "M2_CSV_STRUCTURE_INVALID",
            "csv_parse",
            "CSV 结构无法解析",
            file_name=file_name,
        ) from exc

    display_values = _row_at(records, display_name_row, file_name)
    raw_headers = _row_at(records, field_name_row, file_name)
    type_values = _row_at(records, field_type_row, file_name)
    scope_values = _row_at(records, field_scope_row, file_name)
    metadata_rows = (display_values, raw_headers, type_values, scope_values)
    layout_width = max(len(values) for values in metadata_rows)
    while layout_width > 0 and all(
        layout_width - 1 >= len(values) or values[layout_width - 1] == ""
        for values in metadata_rows
    ):
        layout_width -= 1
    if not raw_headers or all(
        index >= len(raw_headers) or raw_headers[index] == ""
        for index in range(layout_width)
    ):
        raise M2ProcessingError(
            "M2_CSV_STRUCTURE_INVALID",
            "csv_parse",
            "CSV 字段名行为空",
            file_name=file_name,
        )

    seen_fields: dict[str, int] = {}
    fields: list[CsvField] = []
    for index in range(layout_width):
        display_name = display_values[index] if index < len(display_values) else ""
        name = raw_headers[index] if index < len(raw_headers) else ""
        declared_type = type_values[index] if index < len(type_values) else ""
        scope = scope_values[index] if index < len(scope_values) else ""
        if scope.casefold() == "none":
            continue
        if name == "":
            if display_name != "" and declared_type == "" and scope == "":
                continue
            raise M2ProcessingError(
                "M2_CSV_STRUCTURE_INVALID",
                "csv_parse",
                "CSV 业务字段之间存在无法识别的空字段名",
                file_name=file_name,
                details={"column": index + 1},
            )
        if name in seen_fields:
            raise M2ProcessingError(
                "M2_CSV_DUPLICATE_FIELD",
                "csv_parse",
                f"CSV 存在重复字段名：{name}",
                file_name=file_name,
                details={"field": name, "columns": [seen_fields[name], index + 1]},
            )
        seen_fields[name] = index + 1
        fields.append(
            CsvField(
                name=name,
                declared_type=declared_type,
                scope=scope,
                position=index,
            )
        )

    candidate_names = {candidate.casefold() for candidate in primary_key_fields}
    primary_key_matches = [
        field.name for field in fields if field.name.casefold() in candidate_names
    ]
    if len(primary_key_matches) > 1:
        raise M2ProcessingError(
            "M2_CSV_PRIMARY_KEY_MISSING",
            "csv_parse",
            "CSV 业务主键字段大小写匹配不唯一",
            file_name=file_name,
            details={
                "candidates": list(primary_key_fields),
                "matches": primary_key_matches,
            },
        )
    if primary_key_matches:
        primary_key = primary_key_matches[0]
    else:
        first_column = next(
            (field for field in fields if field.position == 0),
            None,
        )
        if first_column is None:
            raise M2ProcessingError(
                "M2_CSV_PRIMARY_KEY_MISSING",
                "csv_parse",
                "CSV 缺少业务主键字段，且第一列不是可比较业务字段",
                file_name=file_name,
                details={
                    "candidates": list(primary_key_fields),
                    "matches": [],
                },
            )
        primary_key = first_column.name

    parsed_rows: list[CsvDataRow] = []
    seen_keys: dict[str, int] = {}
    for row_number, record in enumerate(records[data_start_row - 1 :], start=data_start_row):
        if len(record) > layout_width:
            raise M2ProcessingError(
                "M2_CSV_STRUCTURE_INVALID",
                "csv_parse",
                "CSV 数据列数超过字段定义",
                file_name=file_name,
                details={
                    "row": row_number,
                    "field_count": layout_width,
                    "value_count": len(record),
                },
            )
        values = {
            field.name: record[field.position] if field.position < len(record) else ""
            for field in fields
        }
        if all(value == "" for value in values.values()):
            continue
        key = values[primary_key]
        if key == "":
            raise M2ProcessingError(
                "M2_CSV_PRIMARY_KEY_MISSING",
                "csv_parse",
                f"CSV 数据行缺少主键 {primary_key}",
                file_name=file_name,
                details={"row": row_number, "field": primary_key},
            )
        if key in seen_keys:
            raise M2ProcessingError(
                "M2_CSV_DUPLICATE_KEY",
                "csv_parse",
                f"主键 {primary_key} 存在重复值",
                file_name=file_name,
                details={"key": key, "rows": [seen_keys[key], row_number]},
            )
        seen_keys[key] = row_number
        parsed_rows.append(
            CsvDataRow(
                row_number=row_number,
                key=key,
                values=values,
                normalized_values={
                    field.name: _normalize_value(values[field.name], field.declared_type)
                    for field in fields
                },
            )
        )

    return ParsedTableCsv(
        name=file_name,
        fields=tuple(fields),
        primary_key=primary_key,
        rows=tuple(parsed_rows),
    )
