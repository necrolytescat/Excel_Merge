from __future__ import annotations

import csv
from io import BytesIO, StringIO

from openpyxl import Workbook

from app.services.monitor_diff_service import MonitorDiffService, SvnMonitorSnapshotReader
from app.services.workbook_diff_service import DatasetLayout
from core.models import TreeEntry
from core.svn_history import BranchIdentity
from core.svn_provider import SVNProviderError


def _workbook_bytes(*, include_broken=False):
    workbook = Workbook()
    sheet = workbook.active
    sheet.title = "main"
    sheet.append(["sheetName", "tbxName", "isExport"])
    sheet.append(["Role", "role", 1])
    sheet.append(["NotExported", "Missing", 0])
    if include_broken:
        sheet.append(["Broken", "Broken", 1])
    output = BytesIO()
    workbook.save(output)
    workbook.close()
    return output.getvalue()


def _csv_bytes(rows, *, hp_type="uint32", note="note"):
    output = StringIO(newline="")
    writer = csv.writer(output, lineterminator="\n")
    writer.writerows(
        [
            ["ID", "生命值", "策划备注"],
            ["Id", "Hp", "Note"],
            ["uint32", hp_type, "string"],
            ["All", "Client", "None"],
            ["meta", "meta", "meta"],
            ["meta", "meta", "meta"],
            ["meta", "meta", "meta"],
            *[[row[0], row[1], note] for row in rows],
        ]
    )
    return output.getvalue().encode("utf-8")


class History:
    def __init__(self, files):
        self.files = files
        self.reads = []

    def list_paths_at_revision(self, identity, revision):
        return [
            TreeEntry(path=path, kind="file")
            for path in self.files[revision]
        ]

    def read_path_bytes_at_revision(self, identity, path, revision):
        self.reads.append((path, revision))
        try:
            return self.files[revision][path]
        except KeyError as exc:
            raise SVNProviderError("SVN_PATH_NOT_FOUND", "missing") from exc


class FailingHistory(History):
    def list_paths_at_revision(self, identity, revision):
        raise SVNProviderError("SVN_TIMEOUT", "private timeout details")


def _layout():
    return DatasetLayout(
        csv_extension=".csv",
        filename_template="{tbxName}.csv",
        field_name_row=2,
        field_type_row=3,
        field_scope_row=4,
        data_start_row=8,
        primary_key_fields=("Id", "id"),
        manifest_sheet_name="main",
        manifest_sheet_field="sheetName",
        manifest_csv_name_field="tbxName",
        manifest_export_flag_field="isExport",
    )


def test_svn_snapshot_reader_reuses_m2_pairing_and_isolates_parse_failures():
    files = {
        100: {
            "Source/Table/Combat.xlsm": _workbook_bytes(),
            "Source/Table/Broken.xlsm": b"not-an-excel-file",
            "Source/TableCsv/Role.csv": _csv_bytes([("001", "01")], note="old"),
        },
        110: {
            "Source/Table/Combat.xlsm": _workbook_bytes(),
            "Source/Table/Broken.xlsm": b"still-not-an-excel-file",
            "Source/TableCsv/Role.csv": _csv_bytes(
                [("001", "1"), ("2", "80")],
                hp_type="uint64",
                note="changed but scope none",
            ),
        },
    }
    history = History(files)
    identity = BranchIdentity(
        canonical_url="https://svn.example/repo/branches/foo",
        repository_root="https://svn.example/repo",
        repository_uuid="20000000-0000-4000-8000-000000000001",
        repository_relative_path="branches/foo",
        bound_revision=110,
    )
    reader = SvnMonitorSnapshotReader(
        history,
        identity,
        _layout(),
        table_directory="Source/Table",
    )

    result = MonitorDiffService(reader).compare_revisions(100, 110)

    assert result.workbook_count == 2
    assert [(change.change_type.value, change.row_key, change.field_name) for change in result.changes] == [
        ("field_definition_modified", None, "Hp"),
        ("row_added", "2", None),
    ]
    assert result.changes[1].primary_key_field == "Id"
    assert result.changes[1].target.row_values == {"Id": "2", "Hp": "80"}
    assert [(error.workbook, error.stage.value) for error in result.errors] == [
        ("Broken.xlsm", "manifest_parse"),
    ]
    assert all(revision in {100, 110} for _, revision in history.reads)
    assert not any("Missing.csv" in path for path, _ in history.reads)


def test_svn_snapshot_reader_preserves_retryable_transport_error_classification():
    identity = BranchIdentity(
        canonical_url="https://svn.example/repo/branches/foo",
        repository_root="https://svn.example/repo",
        repository_uuid="20000000-0000-4000-8000-000000000001",
        repository_relative_path="branches/foo",
        bound_revision=110,
    )
    reader = SvnMonitorSnapshotReader(
        FailingHistory({}),
        identity,
        _layout(),
        table_directory="Source/Table",
    )

    snapshot = reader.load_snapshot(110)

    assert len(snapshot.errors) == 1
    assert snapshot.errors[0].code.value == "MONITOR_SVN_TIMEOUT"
    assert snapshot.errors[0].stage.value == "snapshot"
    assert snapshot.errors[0].retryable is True
