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
