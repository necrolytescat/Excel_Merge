import hashlib
import json
from pathlib import Path

import pytest
from pydantic import ValidationError

from app.schemas.diff import (
    DiffDirectionPayload,
    DiffResultPayload,
    FieldChangePayload,
    FieldDefinitionPayload,
    FieldStatus,
    RowDiffPayload,
    RowSidePayload,
    RowStatus,
    SheetDiffPayload,
    SheetStatus,
    WorkbookDiffPayload,
    WorkbookStatus,
    WorkbookSummaryPayload,
    serialize_diff_json,
)


def _sample_result() -> DiffResultPayload:
    return DiffResultPayload(
        direction=DiffDirectionPayload(source="left", target="right"),
        workbook=WorkbookDiffPayload(
            name="AtlasConfig.xlsm",
            status=WorkbookStatus.MODIFIED,
            source_sha256="a" * 64,
            target_sha256="b" * 64,
        ),
        summary=WorkbookSummaryPayload(
            total_sheets=1,
            modified_sheets=1,
            modified_rows=1,
            modified_fields=1,
        ),
        sheets=[
            SheetDiffPayload(
                sheet_name="Base",
                status=SheetStatus.MODIFIED,
                primary_key="Id",
                fields=[
                    FieldDefinitionPayload(
                        name="Name",
                        status=FieldStatus.COMMON,
                        source_display_name="源名称",
                        target_display_name="目标名称",
                    )
                ],
                rows=[
                    RowDiffPayload(
                        key="1",
                        status=RowStatus.MODIFIED,
                        source=RowSidePayload(row_number=8, values={"Id": "1", "Name": "左"}),
                        target=RowSidePayload(row_number=9, values={"Id": "1", "Name": "右"}),
                        changes=[
                            FieldChangePayload(
                                field="Name",
                                status=FieldStatus.MODIFIED,
                                source="左",
                                target="右",
                            )
                        ],
                    )
                ],
            )
        ],
    )


def test_diff_json_serialization_is_stable_utf8():
    payload = _sample_result()

    first = serialize_diff_json(payload)
    second = serialize_diff_json(payload)

    assert first == second
    assert hashlib.sha256(first).hexdigest() == hashlib.sha256(second).hexdigest()
    assert first.endswith(b"\n")
    assert b"\\u5de6" not in first
    data = json.loads(first)
    assert data["schema_version"] == "m2.diff.v1"
    assert data["sheets"][0]["fields"][0]["source_display_name"] == "源名称"
    assert data["sheets"][0]["fields"][0]["target_display_name"] == "目标名称"


def test_diff_contract_rejects_unknown_fields_and_statuses():
    data = _sample_result().model_dump(mode="json")
    data["generated_at"] = "unstable"
    with pytest.raises(ValidationError):
        DiffResultPayload.model_validate(data)

    data = _sample_result().model_dump(mode="json")
    data["workbook"]["status"] = "old"
    with pytest.raises(ValidationError):
        DiffResultPayload.model_validate(data)


def test_diff_contract_uses_source_target_vocabulary():
    text = serialize_diff_json(_sample_result()).decode("utf-8")

    assert '"source"' in text
    assert '"target"' in text
    assert '"old"' not in text
    assert '"new"' not in text


def test_documented_diff_example_matches_contract():
    root = Path(__file__).resolve().parents[2]
    data = json.loads(
        (root / "docs" / "contracts" / "m2.diff.v1.example.json").read_text(
            encoding="utf-8"
        )
    )

    payload = DiffResultPayload.model_validate(data)

    assert json.loads(serialize_diff_json(payload)) == data
