import json
from pathlib import Path

import pytest


PROJECT_ROOT = Path(__file__).resolve().parents[2]
CONFIG_PATHS = (
    PROJECT_ROOT / "config" / "settings.json",
    PROJECT_ROOT / "config" / "settings.m0.example.json",
)


@pytest.mark.parametrize("config_path", CONFIG_PATHS, ids=lambda path: path.name)
def test_dataset_layout_binds_table_and_tablecsv_to_same_snapshot(config_path):
    config = json.loads(config_path.read_text(encoding="utf-8"))
    layout = config["dataset_layout"]

    assert layout["binding_policy"] == {
        "same_endpoint": True,
        "same_frozen_revision": True,
    }
    assert layout["workbook_source"] == {
        "logical_scope": "TABLE",
        "directory_name": "Table",
    }

    csv_export = layout["csv_export"]
    assert csv_export["logical_scope"] == "TABLECSV"
    assert csv_export["directory_name"] == "TableCsv"
    assert csv_export["extension"] == ".csv"
    assert csv_export["filename_template"] == "{tbxName}.csv"
    assert csv_export["field_name_row"] == 2
    assert csv_export["field_type_row"] == 3
    assert csv_export["field_scope_row"] == 4
    assert csv_export["data_start_row"] == 8
    assert csv_export["primary_key_fields"] == ["Id", "id"]

    assert layout["manifest"] == {
        "sheet_name": "main",
        "sheet_field": "sheetName",
        "csv_name_field": "tbxName",
        "export_flag_field": "isExport",
    }
