"""严格按业务主键和字段名计算 TableCsv 语义差异。"""
from __future__ import annotations

from dataclasses import dataclass

from core.table_csv_parser import CsvDataRow, CsvField, ParsedTableCsv


@dataclass(frozen=True)
class SemanticField:
    name: str
    status: str
    source: CsvField | None
    target: CsvField | None


@dataclass(frozen=True)
class SemanticFieldChange:
    field: str
    source: str
    target: str


@dataclass(frozen=True)
class SemanticRow:
    key: str
    status: str
    source: CsvDataRow | None
    target: CsvDataRow | None
    changes: tuple[SemanticFieldChange, ...]


@dataclass(frozen=True)
class SemanticSummary:
    source_only_rows: int
    target_only_rows: int
    modified_rows: int
    modified_fields: int


@dataclass(frozen=True)
class SemanticTableDiff:
    status: str
    primary_key: str
    fields: tuple[SemanticField, ...]
    rows: tuple[SemanticRow, ...]
    summary: SemanticSummary


def _field_map(table: ParsedTableCsv | None) -> dict[str, CsvField]:
    if table is None:
        return {}
    return {field.name: field for field in table.fields}


def _ordered_values(row: CsvDataRow, fields: tuple[CsvField, ...]) -> dict[str, str]:
    return {field.name: row.values[field.name] for field in fields}


def diff_table_csv(
    source: ParsedTableCsv | None,
    target: ParsedTableCsv | None,
) -> SemanticTableDiff:
    """比较一个逻辑 Sheet 的两侧 CSV；不使用行号或模糊匹配。"""
    source_fields = source.fields if source is not None else ()
    target_fields = target.fields if target is not None else ()
    source_field_map = _field_map(source)
    target_field_map = _field_map(target)

    fields: list[SemanticField] = []
    for field in source_fields:
        target_field = target_field_map.get(field.name)
        if target_field is None:
            status = "source_only"
        elif (
            field.declared_type != target_field.declared_type
            or field.scope != target_field.scope
        ):
            status = "modified"
        else:
            status = "common"
        fields.append(
            SemanticField(
                name=field.name,
                status=status,
                source=field,
                target=target_field,
            )
        )
    for field in target_fields:
        if field.name not in source_field_map:
            fields.append(
                SemanticField(
                    name=field.name,
                    status="target_only",
                    source=None,
                    target=field,
                )
            )

    source_rows = source.rows if source is not None else ()
    target_rows = target.rows if target is not None else ()
    target_by_key = {row.key: row for row in target_rows}
    source_keys = {row.key for row in source_rows}
    shared_fields = [
        field.name
        for field in source_fields
        if field.name in target_field_map
    ]

    rows: list[SemanticRow] = []
    modified_fields = 0
    for source_row in source_rows:
        target_row = target_by_key.get(source_row.key)
        if target_row is None:
            rows.append(
                SemanticRow(
                    key=source_row.key,
                    status="source_only",
                    source=source_row,
                    target=None,
                    changes=(),
                )
            )
            continue
        changes = tuple(
            SemanticFieldChange(
                field=field_name,
                source=source_row.values[field_name],
                target=target_row.values[field_name],
            )
            for field_name in shared_fields
            if source_row.normalized_values[field_name]
            != target_row.normalized_values[field_name]
        )
        if changes:
            modified_fields += len(changes)
            rows.append(
                SemanticRow(
                    key=source_row.key,
                    status="modified",
                    source=source_row,
                    target=target_row,
                    changes=changes,
                )
            )

    for target_row in target_rows:
        if target_row.key not in source_keys:
            rows.append(
                SemanticRow(
                    key=target_row.key,
                    status="target_only",
                    source=None,
                    target=target_row,
                    changes=(),
                )
            )

    summary = SemanticSummary(
        source_only_rows=sum(row.status == "source_only" for row in rows),
        target_only_rows=sum(row.status == "target_only" for row in rows),
        modified_rows=sum(row.status == "modified" for row in rows),
        modified_fields=modified_fields,
    )
    has_field_changes = any(field.status != "common" for field in fields)
    status = "modified" if rows or has_field_changes else "unchanged"
    if source is None and target is not None:
        status = "target_only"
    elif target is None and source is not None:
        status = "source_only"

    if source is not None and target is not None:
        primary_key = (
            source.primary_key
            if source.primary_key == target.primary_key
            else f"{source.primary_key}/{target.primary_key}"
        )
    elif source is not None:
        primary_key = source.primary_key
    elif target is not None:
        primary_key = target.primary_key
    else:
        primary_key = ""

    return SemanticTableDiff(
        status=status,
        primary_key=primary_key,
        fields=tuple(fields),
        rows=tuple(rows),
        summary=summary,
    )


def row_values_in_field_order(
    row: CsvDataRow,
    table: ParsedTableCsv,
) -> dict[str, str]:
    return _ordered_values(row, table.fields)
