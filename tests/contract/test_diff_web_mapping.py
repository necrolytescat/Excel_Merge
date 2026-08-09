from __future__ import annotations

import json
from pathlib import Path
import subprocess


PROJECT_ROOT = Path(__file__).resolve().parents[2]
NODE_MAPPER_SCRIPT = r"""
const fs = require("node:fs");
require("./app/static/m2_diff_mapper.js");
const input = JSON.parse(fs.readFileSync(0, "utf8"));
const result = globalThis.M2DiffMapper.mapDiffPayload(input.payload, input.candidate);
process.stdout.write(JSON.stringify(result));
"""


def map_payload(payload: dict) -> dict:
    completed = subprocess.run(
        ["node", "-e", NODE_MAPPER_SCRIPT],
        cwd=PROJECT_ROOT,
        input=json.dumps(
            {
                "payload": payload,
                "candidate": {
                    "path": "AtlasConfig.xlsm",
                    "status": "modified",
                    "sourceFile": {"path": "AtlasConfig.xlsm"},
                    "targetFile": {"path": "AtlasConfig.xlsm"},
                },
            }
        ),
        text=True,
        encoding="utf-8",
        capture_output=True,
        check=True,
    )
    return json.loads(completed.stdout)


def payload(status: str = "modified") -> dict:
    return {
        "schema_version": "m2.diff.v1",
        "direction": {"source": "left", "target": "right"},
        "workbook": {
            "name": "AtlasConfig.xlsm",
            "status": status,
            "source_sha256": "a" * 64,
            "target_sha256": "b" * 64,
        },
        "summary": {
            "total_sheets": 1,
            "unchanged_sheets": 0,
            "modified_sheets": 1,
            "source_only_sheets": 0,
            "target_only_sheets": 0,
            "failed_sheets": 0,
            "source_only_rows": 1,
            "target_only_rows": 0,
            "modified_rows": 1,
            "modified_fields": 1,
            "error_count": 0,
        },
        "sheets": [
            {
                "sheet_name": "Base",
                "status": "modified",
                "primary_key": "Id",
                "source_csv": None,
                "target_csv": None,
                "summary": {
                    "source_only_rows": 1,
                    "target_only_rows": 0,
                    "modified_rows": 1,
                    "modified_fields": 1,
                },
                "fields": [
                    {
                        "name": "Id",
                        "status": "common",
                        "source_display_name": "流水ID",
                        "target_display_name": "流水编号",
                        "source_type": "int",
                        "target_type": "int",
                        "source_scope": "all",
                        "target_scope": "all",
                    },
                    {
                        "name": "Name",
                        "status": "common",
                        "source_display_name": "源名称",
                        "target_display_name": "目标名称",
                        "source_type": "string",
                        "target_type": "string",
                        "source_scope": "client",
                        "target_scope": "client",
                    },
                ],
                "rows": [
                    {
                        "key": "1",
                        "status": "modified",
                        "source": {
                            "row_number": 8,
                            "values": {"Id": "1", "Name": "左值"},
                        },
                        "target": {
                            "row_number": 9,
                            "values": {"Id": "1", "Name": "右值"},
                        },
                        "changes": [
                            {
                                "field": "Name",
                                "status": "modified",
                                "source": "左值",
                                "target": "右值",
                            }
                        ],
                    },
                    {
                        "key": "2",
                        "status": "source_only",
                        "source": {
                            "row_number": 10,
                            "values": {"Id": "2", "Name": "仅左侧"},
                        },
                        "target": None,
                        "changes": [],
                    },
                ],
                "errors": [],
            }
        ],
        "errors": [],
    }


def test_mapper_uses_server_summary_and_real_one_sided_row_values():
    mapped = map_payload(payload())

    assert mapped["state"] == "diff_ready"
    assert mapped["summary"]["modified_fields"] == 1
    assert mapped["sheets"][0]["primaryKey"] == "Id"
    assert mapped["sheets"][0]["fieldDefinitions"][0]["source_display_name"] == "流水ID"
    assert mapped["sheets"][0]["fieldDefinitions"][1]["target_display_name"] == "目标名称"
    modified = mapped["sheets"][0]["rows"][0]
    assert modified["sourceRowNumber"] == 8
    assert modified["targetRowNumber"] == 9
    assert modified["sourceValues"] == {"Id": "1", "Name": "左值"}
    assert modified["targetValues"] == {"Id": "1", "Name": "右值"}
    assert mapped["sheets"][0]["rows"][0]["fields"][0] == {
        "name": "Name",
        "status": "modified",
        "sourceValue": "左值",
        "targetValue": "右值",
        "sourceRowNumber": 8,
        "targetRowNumber": 9,
        "location": "Base · Name · 左侧第 8 行 / 右侧第 9 行",
        "definition": {
            "name": "Name",
            "status": "common",
            "source_display_name": "源名称",
            "target_display_name": "目标名称",
            "source_type": "string",
            "target_type": "string",
            "source_scope": "client",
            "target_scope": "client",
        },
    }
    source_only = mapped["sheets"][0]["rows"][1]
    assert source_only["change"] == "deleted"
    assert source_only["sourceRowNumber"] == 10
    assert source_only["targetRowNumber"] is None
    assert source_only["sourceValues"] == {"Id": "2", "Name": "仅左侧"}
    assert source_only["targetValues"] is None
    assert [field["name"] for field in source_only["fields"]] == ["Id", "Name"]
    assert source_only["fields"][1]["sourceValue"] == "仅左侧"
    assert source_only["fields"][1]["targetValue"] == "—"
    assert "整行" not in json.dumps(mapped, ensure_ascii=False)
    assert "!A" not in json.dumps(mapped, ensure_ascii=False)


def test_partial_maps_to_error_state_and_preserves_all_sheets():
    partial = payload(status="partial")
    partial["summary"].update(failed_sheets=1, error_count=1, total_sheets=2)
    partial["sheets"].append(
        {
            "sheet_name": "Broken",
            "status": "failed",
            "primary_key": None,
            "source_csv": None,
            "target_csv": None,
            "summary": {
                "source_only_rows": 0,
                "target_only_rows": 0,
                "modified_rows": 0,
                "modified_fields": 0,
            },
            "fields": [],
            "rows": [],
            "errors": [],
        }
    )
    partial["errors"] = [
        {
            "code": "M2_CSV_MISSING",
            "stage": "csv_read",
            "side": "target",
            "workbook": "AtlasConfig.xlsm",
            "sheet_name": "Broken",
            "file": "AtlasConfig_Broken.csv",
            "message": "CSV 缺失",
            "details": {},
        }
    ]

    mapped = map_payload(partial)

    assert mapped["state"] == "diff_error"
    assert mapped["partial"] is True
    assert [sheet["id"] for sheet in mapped["sheets"]] == ["Base", "Broken"]
    assert mapped["errors"][0]["code"] == "M2_CSV_MISSING"


def test_failed_maps_to_error_instead_of_empty():
    failed = payload(status="failed")
    failed["summary"].update(
        total_sheets=0,
        modified_sheets=0,
        source_only_rows=0,
        modified_rows=0,
        modified_fields=0,
        error_count=1,
    )
    failed["sheets"] = []
    failed["errors"] = [
        {
            "code": "M2_MANIFEST_PARSE_FAILED",
            "stage": "manifest_parse",
            "side": "source",
            "workbook": "AtlasConfig.xlsm",
            "sheet_name": None,
            "file": "AtlasConfig.xlsm",
            "message": "main 清单解析失败",
            "details": {},
        }
    ]

    mapped = map_payload(failed)

    assert mapped["state"] == "diff_error"
    assert mapped["partial"] is False
    assert mapped["sheets"] == []
    assert "M2_MANIFEST_PARSE_FAILED" in mapped["error"]


def test_sheet_column_model_supports_original_view_without_changing_scheme_a():
    script = r"""
const fs = require("node:fs");
const source = fs.readFileSync("./app/static/compare_results.js", "utf8");
const start = source.indexOf("  function sheetColumnModel(sheet, rows, fieldViewMode)");
const end = source.indexOf("\n\n  function sideCellValue", start);
if (start < 0 || end < 0) throw new Error("sheetColumnModel source missing");
eval(source.slice(start, end));
const sheet = {
  primaryKey: "Id",
  fieldDefinitions: [
    { name: "Id", status: "common" },
    { name: "Name", status: "common" },
    { name: "Score", status: "common" },
  ],
};
const modifiedRows = [{
  status: "modified",
  sourceValues: { Id: "1", Name: "A", Score: "10" },
  targetValues: { Id: "1", Name: "B", Score: "10" },
  fields: [{ name: "Name", status: "modified" }],
  changedFields: new Map([["Name", {}]]),
}];
const oneSidedRows = [
  ...modifiedRows,
  {
    status: "target_only",
    sourceValues: null,
    targetValues: { Id: "2", Name: "C", Score: "20" },
    fields: [],
    changedFields: new Map(),
  },
];
const names = model => [model.primaryKey, ...model.fields.map(field => field.name)];
process.stdout.write(JSON.stringify({
  diff: names(sheetColumnModel(sheet, modifiedRows, "diff")),
  original: names(sheetColumnModel(sheet, modifiedRows, "original")),
  schemeA: names(sheetColumnModel(sheet, oneSidedRows, "diff")),
}));
"""
    completed = subprocess.run(
        ["node", "-e", script],
        cwd=PROJECT_ROOT,
        text=True,
        encoding="utf-8",
        capture_output=True,
        check=True,
    )

    assert json.loads(completed.stdout) == {
        "diff": ["Id", "Name"],
        "original": ["Id", "Name", "Score"],
        "schemeA": ["Id", "Name", "Score"],
    }


def test_header_display_name_prefers_current_side_and_falls_back_to_peer():
    script = r"""
const fs = require("node:fs");
const source = fs.readFileSync("./app/static/compare_results.js", "utf8");
const start = source.indexOf("  function fieldDisplayName(definition, side)");
const end = source.indexOf("\n\n  function createHeaderCell", start);
if (start < 0 || end < 0) throw new Error("fieldDisplayName source missing");
eval(source.slice(start, end));
const cases = [
  [{ source_display_name: "源名称", target_display_name: "目标名称" }, "source"],
  [{ source_display_name: "源名称", target_display_name: "目标名称" }, "target"],
  [{ source_display_name: "   ", target_display_name: "目标名称" }, "source"],
  [{ source_display_name: "源名称", target_display_name: null }, "target"],
  [{ source_display_name: null, target_display_name: null }, "source"],
];
process.stdout.write(JSON.stringify(cases.map(([definition, side]) => fieldDisplayName(definition, side))));
"""
    completed = subprocess.run(
        ["node", "-e", script],
        cwd=PROJECT_ROOT,
        text=True,
        encoding="utf-8",
        capture_output=True,
        check=True,
    )

    assert json.loads(completed.stdout) == [
        "源名称",
        "目标名称",
        "目标名称",
        "源名称",
        "",
    ]


def test_frontend_scripts_are_valid_javascript():
    for relative_path in (
        "app/static/m2_diff_mapper.js",
        "app/static/compare_results.js",
        "app/static/compare.js",
    ):
        subprocess.run(
            ["node", "--check", relative_path],
            cwd=PROJECT_ROOT,
            text=True,
            encoding="utf-8",
            capture_output=True,
            check=True,
        )
