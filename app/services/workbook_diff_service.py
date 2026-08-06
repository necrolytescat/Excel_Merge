"""本地单工作簿 Excel 清单 + CSV 语义 Diff 编排。"""
from __future__ import annotations

from dataclasses import dataclass
import hashlib
from pathlib import Path
from typing import Any, Mapping

from app.schemas.diff import (
    CsvFilePayload,
    DiffDirectionPayload,
    DiffErrorPayload,
    DiffResultPayload,
    ErrorStage,
    FieldChangePayload,
    FieldDefinitionPayload,
    FieldStatus,
    RowDiffPayload,
    RowSidePayload,
    RowStatus,
    SheetDiffPayload,
    SheetStatus,
    SheetSummaryPayload,
    WorkbookDiffPayload,
    WorkbookStatus,
    WorkbookSummaryPayload,
)
from core.m2_errors import M2ProcessingError
from core.semantic_diff import diff_table_csv, row_values_in_field_order
from core.table_csv_parser import ParsedTableCsv, parse_table_csv
from core.workbook_manifest_parser import ManifestEntry, WorkbookManifest, parse_workbook_manifest


@dataclass(frozen=True)
class DatasetLayout:
    csv_extension: str
    filename_template: str
    field_name_row: int
    field_type_row: int
    field_scope_row: int
    data_start_row: int
    primary_key_fields: tuple[str, ...]
    manifest_sheet_name: str
    manifest_sheet_field: str
    manifest_csv_name_field: str
    manifest_export_flag_field: str

    @classmethod
    def from_config(cls, config: Mapping[str, Any]) -> "DatasetLayout":
        csv_export = dict(config["csv_export"])
        manifest = dict(config["manifest"])
        return cls(
            csv_extension=str(csv_export["extension"]),
            filename_template=str(csv_export["filename_template"]),
            field_name_row=int(csv_export["field_name_row"]),
            field_type_row=int(csv_export["field_type_row"]),
            field_scope_row=int(csv_export["field_scope_row"]),
            data_start_row=int(csv_export["data_start_row"]),
            primary_key_fields=tuple(str(value) for value in csv_export["primary_key_fields"]),
            manifest_sheet_name=str(manifest["sheet_name"]),
            manifest_sheet_field=str(manifest["sheet_field"]),
            manifest_csv_name_field=str(manifest["csv_name_field"]),
            manifest_export_flag_field=str(manifest["export_flag_field"]),
        )


def _sha256(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def _empty_summary(*, error_count: int = 0) -> WorkbookSummaryPayload:
    return WorkbookSummaryPayload(error_count=error_count)


class WorkbookDiffService:
    def __init__(self, layout: DatasetLayout):
        self.layout = layout

    def _manifest(self, raw: bytes) -> WorkbookManifest:
        return parse_workbook_manifest(
            raw,
            sheet_name=self.layout.manifest_sheet_name,
            sheet_field=self.layout.manifest_sheet_field,
            csv_name_field=self.layout.manifest_csv_name_field,
            export_flag_field=self.layout.manifest_export_flag_field,
        )

    def _csv_name(self, entry: ManifestEntry) -> str:
        return self.layout.filename_template.format(tbxName=entry.tbx_name)

    @staticmethod
    def _error(
        error: M2ProcessingError,
        *,
        side: str | None,
        workbook_name: str,
        sheet_name: str | None = None,
        file_name: str | None = None,
    ) -> DiffErrorPayload:
        try:
            stage = ErrorStage(error.stage)
        except ValueError:
            stage = ErrorStage.DIFF
        return DiffErrorPayload(
            code=error.code,
            stage=stage,
            side=side,
            workbook=workbook_name,
            sheet_name=error.sheet_name or sheet_name,
            file=error.file_name or file_name,
            message=error.message,
            details=error.details,
        )

    def _parse_csv(
        self,
        directory: Path,
        entry: ManifestEntry | None,
        *,
        side: str,
        workbook_name: str,
        sheet_name: str,
    ) -> tuple[ParsedTableCsv | None, CsvFilePayload | None, DiffErrorPayload | None]:
        if entry is None:
            return None, None, None
        csv_name = self._csv_name(entry)
        path = directory / csv_name
        try:
            raw = path.read_bytes()
        except OSError:
            error = M2ProcessingError(
                "M2_CSV_MISSING",
                "csv_read",
                "main 清单对应的 CSV 不存在或无法读取",
                sheet_name=sheet_name,
                file_name=csv_name,
            )
            return None, None, self._error(
                error,
                side=side,
                workbook_name=workbook_name,
            )

        reference = CsvFilePayload(name=csv_name, sha256=_sha256(raw))
        try:
            parsed = parse_table_csv(
                raw,
                csv_name,
                field_name_row=self.layout.field_name_row,
                field_type_row=self.layout.field_type_row,
                field_scope_row=self.layout.field_scope_row,
                data_start_row=self.layout.data_start_row,
                primary_key_fields=self.layout.primary_key_fields,
            )
            return parsed, reference, None
        except M2ProcessingError as error:
            return None, reference, self._error(
                error,
                side=side,
                workbook_name=workbook_name,
                sheet_name=sheet_name,
                file_name=csv_name,
            )

    @staticmethod
    def _row_side(row, table: ParsedTableCsv) -> RowSidePayload:
        return RowSidePayload(
            row_number=row.row_number,
            values=row_values_in_field_order(row, table),
        )

    def _sheet_payload(
        self,
        *,
        sheet_name: str,
        source_entry: ManifestEntry | None,
        target_entry: ManifestEntry | None,
        source_directory: Path,
        target_directory: Path,
        workbook_name: str,
    ) -> SheetDiffPayload:
        source, source_ref, source_error = self._parse_csv(
            source_directory,
            source_entry,
            side="source",
            workbook_name=workbook_name,
            sheet_name=sheet_name,
        )
        target, target_ref, target_error = self._parse_csv(
            target_directory,
            target_entry,
            side="target",
            workbook_name=workbook_name,
            sheet_name=sheet_name,
        )
        errors = [error for error in (source_error, target_error) if error is not None]
        if errors:
            return SheetDiffPayload(
                sheet_name=sheet_name,
                status=SheetStatus.FAILED,
                source_csv=source_ref,
                target_csv=target_ref,
                errors=errors,
            )

        semantic = diff_table_csv(source, target)
        field_status = {
            "common": FieldStatus.COMMON,
            "modified": FieldStatus.MODIFIED,
            "source_only": FieldStatus.SOURCE_ONLY,
            "target_only": FieldStatus.TARGET_ONLY,
        }
        row_status = {
            "modified": RowStatus.MODIFIED,
            "source_only": RowStatus.SOURCE_ONLY,
            "target_only": RowStatus.TARGET_ONLY,
        }
        status = {
            "unchanged": SheetStatus.UNCHANGED,
            "modified": SheetStatus.MODIFIED,
            "source_only": SheetStatus.SOURCE_ONLY,
            "target_only": SheetStatus.TARGET_ONLY,
        }[semantic.status]

        rows: list[RowDiffPayload] = []
        for row in semantic.rows:
            rows.append(
                RowDiffPayload(
                    key=row.key,
                    status=row_status[row.status],
                    source=(
                        self._row_side(row.source, source)
                        if row.source is not None and source is not None
                        else None
                    ),
                    target=(
                        self._row_side(row.target, target)
                        if row.target is not None and target is not None
                        else None
                    ),
                    changes=[
                        FieldChangePayload(
                            field=change.field,
                            status=FieldStatus.MODIFIED,
                            source=change.source,
                            target=change.target,
                        )
                        for change in row.changes
                    ],
                )
            )

        return SheetDiffPayload(
            sheet_name=sheet_name,
            status=status,
            primary_key=semantic.primary_key,
            source_csv=source_ref,
            target_csv=target_ref,
            summary=SheetSummaryPayload(
                source_only_rows=semantic.summary.source_only_rows,
                target_only_rows=semantic.summary.target_only_rows,
                modified_rows=semantic.summary.modified_rows,
                modified_fields=semantic.summary.modified_fields,
            ),
            fields=[
                FieldDefinitionPayload(
                    name=field.name,
                    status=field_status[field.status],
                    source_type=field.source.declared_type if field.source is not None else None,
                    target_type=field.target.declared_type if field.target is not None else None,
                    source_scope=field.source.scope if field.source is not None else None,
                    target_scope=field.target.scope if field.target is not None else None,
                )
                for field in semantic.fields
            ],
            rows=rows,
        )

    @staticmethod
    def _failed_result(
        *,
        workbook_name: str,
        source_raw: bytes,
        target_raw: bytes,
        errors: list[DiffErrorPayload],
    ) -> DiffResultPayload:
        return DiffResultPayload(
            direction=DiffDirectionPayload(source="left", target="right"),
            workbook=WorkbookDiffPayload(
                name=workbook_name,
                status=WorkbookStatus.FAILED,
                source_sha256=_sha256(source_raw) if source_raw else "",
                target_sha256=_sha256(target_raw) if target_raw else "",
            ),
            summary=_empty_summary(error_count=len(errors)),
            errors=errors,
        )

    def compare_local(
        self,
        source_directory: Path,
        target_directory: Path,
        workbook_name: str,
    ) -> DiffResultPayload:
        if Path(workbook_name).name != workbook_name:
            raise ValueError("workbook_name 必须是文件名，不能包含路径")
        source_path = source_directory / workbook_name
        target_path = target_directory / workbook_name
        source_raw = b""
        target_raw = b""
        root_errors: list[DiffErrorPayload] = []

        for side, path in (("source", source_path), ("target", target_path)):
            try:
                raw = path.read_bytes()
                if side == "source":
                    source_raw = raw
                else:
                    target_raw = raw
            except OSError:
                error = M2ProcessingError(
                    "M2_WORKBOOK_PARSE_FAILED",
                    "workbook_parse",
                    "工作簿不存在或无法读取",
                    file_name=workbook_name,
                )
                root_errors.append(
                    self._error(
                        error,
                        side=side,
                        workbook_name=workbook_name,
                    )
                )
        if root_errors:
            return self._failed_result(
                workbook_name=workbook_name,
                source_raw=source_raw,
                target_raw=target_raw,
                errors=root_errors,
            )

        manifests: dict[str, WorkbookManifest] = {}
        for side, raw in (("source", source_raw), ("target", target_raw)):
            try:
                manifests[side] = self._manifest(raw)
            except M2ProcessingError as error:
                root_errors.append(
                    self._error(
                        error,
                        side=side,
                        workbook_name=workbook_name,
                    )
                )
        if root_errors:
            return self._failed_result(
                workbook_name=workbook_name,
                source_raw=source_raw,
                target_raw=target_raw,
                errors=root_errors,
            )

        source_entries = {entry.sheet_name: entry for entry in manifests["source"].entries}
        target_entries = {entry.sheet_name: entry for entry in manifests["target"].entries}
        sheet_names = [entry.sheet_name for entry in manifests["source"].entries]
        sheet_names.extend(
            entry.sheet_name
            for entry in manifests["target"].entries
            if entry.sheet_name not in source_entries
        )
        sheets = [
            self._sheet_payload(
                sheet_name=sheet_name,
                source_entry=source_entries.get(sheet_name),
                target_entry=target_entries.get(sheet_name),
                source_directory=source_directory,
                target_directory=target_directory,
                workbook_name=workbook_name,
            )
            for sheet_name in sheet_names
        ]
        root_errors = [error for sheet in sheets for error in sheet.errors]
        summary = WorkbookSummaryPayload(
            total_sheets=len(sheets),
            unchanged_sheets=sum(sheet.status == SheetStatus.UNCHANGED for sheet in sheets),
            modified_sheets=sum(sheet.status == SheetStatus.MODIFIED for sheet in sheets),
            source_only_sheets=sum(sheet.status == SheetStatus.SOURCE_ONLY for sheet in sheets),
            target_only_sheets=sum(sheet.status == SheetStatus.TARGET_ONLY for sheet in sheets),
            failed_sheets=sum(sheet.status == SheetStatus.FAILED for sheet in sheets),
            source_only_rows=sum(sheet.summary.source_only_rows for sheet in sheets),
            target_only_rows=sum(sheet.summary.target_only_rows for sheet in sheets),
            modified_rows=sum(sheet.summary.modified_rows for sheet in sheets),
            modified_fields=sum(sheet.summary.modified_fields for sheet in sheets),
            error_count=len(root_errors),
        )

        if summary.failed_sheets:
            workbook_status = (
                WorkbookStatus.FAILED
                if summary.failed_sheets == summary.total_sheets
                else WorkbookStatus.PARTIAL
            )
        elif (
            summary.modified_sheets
            or summary.source_only_sheets
            or summary.target_only_sheets
        ):
            workbook_status = WorkbookStatus.MODIFIED
        else:
            workbook_status = WorkbookStatus.UNCHANGED

        return DiffResultPayload(
            direction=DiffDirectionPayload(source="left", target="right"),
            workbook=WorkbookDiffPayload(
                name=workbook_name,
                status=workbook_status,
                source_sha256=_sha256(source_raw),
                target_sha256=_sha256(target_raw),
            ),
            summary=summary,
            sheets=sheets,
            errors=root_errors,
        )
