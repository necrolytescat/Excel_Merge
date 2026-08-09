import csv
from io import StringIO
from pathlib import Path

from app.schemas.diff import SheetStatus, WorkbookStatus
from app.services.workbook_diff_service import DatasetLayout, WorkbookDiffService
from core.workbook_manifest_parser import ManifestEntry, WorkbookManifest


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


def test_missing_csv_is_a_structured_failure(tmp_path, monkeypatch):
    source = tmp_path / "left"
    target = tmp_path / "right"
    source.mkdir()
    target.mkdir()
    (source / "AtlasConfig.xlsm").write_bytes(b"source")
    (target / "AtlasConfig.xlsm").write_bytes(b"target")
    manifest = WorkbookManifest(
        entries=(
            ManifestEntry(
                sheet_name="Base",
                tbx_name="AtlasConfig_Base",
                is_export="1",
                row_number=2,
            ),
        ),
        parser="test",
    )
    service = WorkbookDiffService(_layout())
    monkeypatch.setattr(service, "_manifest", lambda raw: manifest)

    result = service.compare_local(source, target, "AtlasConfig.xlsm")

    assert result.workbook.status == WorkbookStatus.FAILED
    assert result.summary.error_count == 2
    assert result.sheets[0].status == SheetStatus.FAILED
    assert [error.code for error in result.sheets[0].errors] == [
        "M2_CSV_MISSING",
        "M2_CSV_MISSING",
    ]
    assert {error.side for error in result.errors} == {"source", "target"}
    assert all(str(tmp_path) not in error.message for error in result.errors)


def _table_csv(display_names, field_names, values):
    buffer = StringIO(newline="")
    writer = csv.writer(buffer, lineterminator="\n")
    writer.writerows(
        [
            display_names,
            field_names,
            ["uint32", "string"],
            ["All", "Client"],
            ["meta", "meta"],
            ["meta", "meta"],
            ["meta", "meta"],
            values,
        ]
    )
    return buffer.getvalue().encode("utf-8")


def test_field_payload_preserves_both_display_names_without_changing_diff_status(tmp_path):
    source = tmp_path / "left"
    target = tmp_path / "right"
    source.mkdir()
    target.mkdir()
    (source / "Base.csv").write_bytes(
        _table_csv(["流水ID", "源名称"], ["Id", "Name"], ["1", "Alpha"])
    )
    (target / "Base.csv").write_bytes(
        _table_csv(["流水编号", "目标名称"], ["Id", "Name"], ["1", "Alpha"])
    )
    entry = ManifestEntry(sheet_name="Base", tbx_name="Base", is_export="1", row_number=2)

    sheet = WorkbookDiffService(_layout())._sheet_payload(
        sheet_name="Base",
        source_entry=entry,
        target_entry=entry,
        source_directory=source,
        target_directory=target,
        workbook_name="AtlasConfig.xlsm",
    )

    assert sheet.status == SheetStatus.UNCHANGED
    assert [field.status.value for field in sheet.fields] == ["common", "common"]
    assert [
        (field.source_display_name, field.target_display_name) for field in sheet.fields
    ] == [("流水ID", "流水编号"), ("源名称", "目标名称")]
