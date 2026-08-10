"""M3 snapshot loading and final TableCsv net-value calculation."""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import PurePosixPath
import posixpath
from typing import Protocol

from app.schemas.monitor import (
    MonitorAttributionPayload,
    MonitorChangePayload,
    MonitorChangeSidePayload,
    MonitorChangeType,
    MonitorErrorCode,
    MonitorErrorStage,
    MonitorFieldDefinitionValuePayload,
    MonitorPublicErrorPayload,
)
from app.services.branch_history_service import BranchHistoryService
from app.services.workbook_diff_service import DatasetLayout
from core.m2_errors import M2ProcessingError
from core.semantic_diff import diff_table_csv, row_values_in_field_order
from core.svn_history import BranchIdentity
from core.svn_provider import SVNProviderError, normalize_relative_path
from core.table_csv_parser import CsvField, ParsedTableCsv, parse_table_csv
from core.workbook_manifest_parser import parse_workbook_manifest


EXCEL_EXTENSIONS = {".xlsx", ".xlsm", ".xls"}


@dataclass(frozen=True)
class MonitorWorkbookSnapshot:
    sheets: dict[str, ParsedTableCsv] = field(default_factory=dict)


@dataclass(frozen=True)
class MonitorSnapshot:
    revision: int
    workbooks: dict[str, MonitorWorkbookSnapshot] = field(default_factory=dict)
    errors: tuple[MonitorPublicErrorPayload, ...] = ()


@dataclass(frozen=True)
class MonitorNetDiff:
    workbook_count: int
    changes: tuple[MonitorChangePayload, ...]
    errors: tuple[MonitorPublicErrorPayload, ...]


class MonitorSnapshotReader(Protocol):
    def load_snapshot(self, revision: int) -> MonitorSnapshot: ...


def _unresolved() -> MonitorAttributionPayload:
    return MonitorAttributionPayload(
        status="unresolved",
        author="未知",
        revision=None,
        changed_at=None,
        commit_message=None,
    )


def _public_parse_error(
    *,
    stage: MonitorErrorStage,
    message: str,
    workbook: str | None = None,
    sheet_name: str | None = None,
) -> MonitorPublicErrorPayload:
    return MonitorPublicErrorPayload(
        code=MonitorErrorCode.PARSE_FAILED,
        stage=stage,
        message=message,
        retryable=False,
        workbook=workbook,
        sheet_name=sheet_name,
    )


def _public_svn_error(
    error: SVNProviderError,
    *,
    workbook: str | None = None,
    sheet_name: str | None = None,
) -> MonitorPublicErrorPayload:
    if error.code == "SVN_TIMEOUT":
        code = MonitorErrorCode.SVN_TIMEOUT
        message = "固定 Revision 快照读取超时"
        retryable = True
    elif error.code == "SVN_AUTH_FAILED":
        code = MonitorErrorCode.SVN_AUTH_FAILED
        message = "固定 Revision 快照认证失败"
        retryable = False
    else:
        code = MonitorErrorCode.PARSE_FAILED
        message = "固定 Revision 快照路径无法读取"
        retryable = False
    return MonitorPublicErrorPayload(
        code=code,
        stage=MonitorErrorStage.SNAPSHOT,
        message=message,
        retryable=retryable,
        workbook=workbook,
        sheet_name=sheet_name,
    )


class SvnMonitorSnapshotReader:
    """Read one fixed branch revision without materializing a working copy."""

    def __init__(
        self,
        history: BranchHistoryService,
        identity: BranchIdentity,
        layout: DatasetLayout,
        *,
        table_directory: str,
        csv_directory_name: str = "TableCsv",
    ):
        self.history = history
        self.identity = identity
        self.layout = layout
        self.table_directory = normalize_relative_path(table_directory)
        parent = posixpath.dirname(self.table_directory)
        self.csv_directory = normalize_relative_path(
            posixpath.join(parent, csv_directory_name)
        )

    @staticmethod
    def _is_below(path: str, parent: str) -> bool:
        return path == parent or path.startswith(parent + "/")

    def _read(self, path: str, revision: int) -> bytes:
        return self.history.read_path_bytes_at_revision(
            self.identity,
            path,
            revision,
        )

    def load_snapshot(self, revision: int) -> MonitorSnapshot:
        try:
            entries = self.history.list_paths_at_revision(self.identity, revision)
        except SVNProviderError as error:
            return MonitorSnapshot(
                revision=revision,
                errors=(_public_svn_error(error),),
            )
        file_paths = sorted(
            {
                normalize_relative_path(entry.path)
                for entry in entries
                if entry.kind == "file"
            },
            key=str.casefold,
        )
        workbook_paths = [
            path
            for path in file_paths
            if self._is_below(path, self.table_directory)
            and PurePosixPath(path).suffix.casefold() in EXCEL_EXTENSIONS
        ]
        csv_paths = [
            path
            for path in file_paths
            if posixpath.dirname(path) == self.csv_directory
        ]
        csv_by_name: dict[str, list[str]] = {}
        for path in csv_paths:
            csv_by_name.setdefault(PurePosixPath(path).name.casefold(), []).append(path)

        workbooks: dict[str, MonitorWorkbookSnapshot] = {}
        errors: list[MonitorPublicErrorPayload] = []
        for workbook_path in workbook_paths:
            workbook = workbook_path[len(self.table_directory) :].lstrip("/")
            try:
                workbook_raw = self._read(workbook_path, revision)
            except SVNProviderError as error:
                errors.append(_public_svn_error(error, workbook=workbook))
                workbooks[workbook] = MonitorWorkbookSnapshot()
                continue
            try:
                manifest = parse_workbook_manifest(
                    workbook_raw,
                    sheet_name=self.layout.manifest_sheet_name,
                    sheet_field=self.layout.manifest_sheet_field,
                    csv_name_field=self.layout.manifest_csv_name_field,
                    export_flag_field=self.layout.manifest_export_flag_field,
                )
            except M2ProcessingError:
                errors.append(
                    _public_parse_error(
                        stage=MonitorErrorStage.MANIFEST_PARSE,
                        message="工作簿导出清单无法按冻结规则解析",
                        workbook=workbook,
                    )
                )
                workbooks[workbook] = MonitorWorkbookSnapshot()
                continue

            sheets: dict[str, ParsedTableCsv] = {}
            for manifest_entry in manifest.entries:
                csv_name = self.layout.filename_template.format(
                    tbxName=manifest_entry.tbx_name
                )
                matches = csv_by_name.get(csv_name.casefold(), [])
                exact = [path for path in matches if PurePosixPath(path).name == csv_name]
                selected = exact[0] if len(exact) == 1 else (matches[0] if len(matches) == 1 else None)
                if selected is None:
                    errors.append(
                        _public_parse_error(
                            stage=MonitorErrorStage.CSV_PARSE,
                            message="main 清单对应的 TableCsv 不存在或匹配不唯一",
                            workbook=workbook,
                            sheet_name=manifest_entry.sheet_name,
                        )
                    )
                    continue
                try:
                    raw = self._read(selected, revision)
                except SVNProviderError as error:
                    errors.append(
                        _public_svn_error(
                            error,
                            workbook=workbook,
                            sheet_name=manifest_entry.sheet_name,
                        )
                    )
                    continue
                try:
                    sheets[manifest_entry.sheet_name] = parse_table_csv(
                        raw,
                        csv_name,
                        field_name_row=self.layout.field_name_row,
                        field_type_row=self.layout.field_type_row,
                        field_scope_row=self.layout.field_scope_row,
                        data_start_row=self.layout.data_start_row,
                        primary_key_fields=self.layout.primary_key_fields,
                    )
                except M2ProcessingError:
                    errors.append(
                        _public_parse_error(
                            stage=MonitorErrorStage.CSV_PARSE,
                            message="TableCsv 无法按冻结规则解析",
                            workbook=workbook,
                            sheet_name=manifest_entry.sheet_name,
                        )
                    )
            workbooks[workbook] = MonitorWorkbookSnapshot(sheets=sheets)
        return MonitorSnapshot(
            revision=revision,
            workbooks=workbooks,
            errors=tuple(errors),
        )


class MonitorDiffService:
    def __init__(self, snapshot_reader: MonitorSnapshotReader):
        self.snapshot_reader = snapshot_reader

    def compare_revisions(self, start_revision: int, end_revision: int) -> MonitorNetDiff:
        return self.compare_snapshots(
            self.snapshot_reader.load_snapshot(start_revision),
            self.snapshot_reader.load_snapshot(end_revision),
        )

    @staticmethod
    def _definition(field: CsvField) -> MonitorChangeSidePayload:
        return MonitorChangeSidePayload(
            field_definition=MonitorFieldDefinitionValuePayload(
                display_name=field.display_name or None,
                declared_type=field.declared_type,
                scope=field.scope,
            )
        )

    @staticmethod
    def _failed_workbooks(snapshot: MonitorSnapshot) -> set[str]:
        return {
            error.workbook
            for error in snapshot.errors
            if error.workbook is not None and error.sheet_name is None
        }

    @staticmethod
    def _failed_sheets(snapshot: MonitorSnapshot) -> set[tuple[str, str]]:
        return {
            (error.workbook, error.sheet_name)
            for error in snapshot.errors
            if error.workbook is not None and error.sheet_name is not None
        }

    @classmethod
    def compare_snapshots(
        cls,
        source: MonitorSnapshot,
        target: MonitorSnapshot,
    ) -> MonitorNetDiff:
        workbook_names = sorted(
            set(source.workbooks) | set(target.workbooks),
            key=str.casefold,
        )
        failed_workbooks = cls._failed_workbooks(source) | cls._failed_workbooks(target)
        failed_sheets = cls._failed_sheets(source) | cls._failed_sheets(target)
        changes: list[MonitorChangePayload] = []
        for workbook in workbook_names:
            if workbook in failed_workbooks:
                continue
            source_workbook = source.workbooks.get(workbook)
            target_workbook = target.workbooks.get(workbook)
            source_sheets = source_workbook.sheets if source_workbook is not None else {}
            target_sheets = target_workbook.sheets if target_workbook is not None else {}
            sheet_names = sorted(set(source_sheets) | set(target_sheets), key=str.casefold)
            for sheet_name in sheet_names:
                if (workbook, sheet_name) in failed_sheets:
                    continue
                source_table = source_sheets.get(sheet_name)
                target_table = target_sheets.get(sheet_name)
                semantic = diff_table_csv(source_table, target_table)
                if not semantic.primary_key:
                    continue
                for field_diff in semantic.fields:
                    change_type = {
                        "source_only": MonitorChangeType.FIELD_REMOVED,
                        "target_only": MonitorChangeType.FIELD_ADDED,
                        "modified": MonitorChangeType.FIELD_DEFINITION_MODIFIED,
                    }.get(field_diff.status)
                    if change_type is None:
                        continue
                    changes.append(
                        MonitorChangePayload(
                            change_type=change_type,
                            workbook=workbook,
                            sheet_name=sheet_name,
                            primary_key_field=semantic.primary_key,
                            row_key=None,
                            field_name=field_diff.name,
                            display_name=(
                                field_diff.target.display_name
                                if field_diff.target is not None
                                else field_diff.source.display_name
                                if field_diff.source is not None
                                else None
                            )
                            or None,
                            source=(
                                cls._definition(field_diff.source)
                                if field_diff.source is not None
                                else None
                            ),
                            target=(
                                cls._definition(field_diff.target)
                                if field_diff.target is not None
                                else None
                            ),
                            attribution=_unresolved(),
                        )
                    )
                for row in semantic.rows:
                    if row.status == "source_only":
                        changes.append(
                            MonitorChangePayload(
                                change_type=MonitorChangeType.ROW_DELETED,
                                workbook=workbook,
                                sheet_name=sheet_name,
                                primary_key_field=semantic.primary_key,
                                row_key=row.key,
                                source=MonitorChangeSidePayload(
                                    row_values=row_values_in_field_order(row.source, source_table)
                                ),
                                target=None,
                                attribution=_unresolved(),
                            )
                        )
                    elif row.status == "target_only":
                        changes.append(
                            MonitorChangePayload(
                                change_type=MonitorChangeType.ROW_ADDED,
                                workbook=workbook,
                                sheet_name=sheet_name,
                                primary_key_field=semantic.primary_key,
                                row_key=row.key,
                                source=None,
                                target=MonitorChangeSidePayload(
                                    row_values=row_values_in_field_order(row.target, target_table)
                                ),
                                attribution=_unresolved(),
                            )
                        )
                    else:
                        source_fields = {field.name: field for field in source_table.fields}
                        target_fields = {field.name: field for field in target_table.fields}
                        for field_change in row.changes:
                            source_field = source_fields[field_change.field]
                            target_field = target_fields[field_change.field]
                            changes.append(
                                MonitorChangePayload(
                                    change_type=MonitorChangeType.FIELD_MODIFIED,
                                    workbook=workbook,
                                    sheet_name=sheet_name,
                                    primary_key_field=semantic.primary_key,
                                    row_key=row.key,
                                    field_name=field_change.field,
                                    display_name=(target_field.display_name or source_field.display_name or None),
                                    source=MonitorChangeSidePayload(
                                        display_value=field_change.source,
                                        normalized_value=row.source.normalized_values[field_change.field],
                                    ),
                                    target=MonitorChangeSidePayload(
                                        display_value=field_change.target,
                                        normalized_value=row.target.normalized_values[field_change.field],
                                    ),
                                    attribution=_unresolved(),
                                )
                            )
        changes.sort(
            key=lambda change: (
                change.workbook.casefold(),
                change.sheet_name.casefold(),
                change.row_key or "",
                change.change_type.value,
                change.field_name or "",
            )
        )
        unique_errors = {
            (
                error.code,
                error.stage,
                error.message,
                error.retryable,
                error.workbook,
                error.sheet_name,
            ): error
            for error in source.errors + target.errors
        }
        errors = tuple(
            sorted(
                unique_errors.values(),
                key=lambda error: (
                    error.workbook or "",
                    error.sheet_name or "",
                    error.stage.value,
                    error.message,
                ),
            )
        )
        return MonitorNetDiff(
            workbook_count=len(workbook_names),
            changes=tuple(changes),
            errors=errors,
        )
