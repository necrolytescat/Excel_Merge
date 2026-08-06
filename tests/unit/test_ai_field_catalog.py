import csv
from io import StringIO
from pathlib import Path

from core.ai_field_catalog import scan_catalog, scan_csv_header


def _csv_bytes(rows, *, encoding="utf-8-sig"):
    buffer = StringIO(newline="")
    writer = csv.writer(buffer, lineterminator="\n")
    writer.writerows(rows)
    return buffer.getvalue().encode(encoding)


def test_scan_csv_header_only_uses_first_two_logical_records(tmp_path: Path):
    path = tmp_path / "Example.csv"
    path.write_bytes(
        _csv_bytes(
            [
                ["流水ID", "多行\n中文说明", "仅说明"],
                ["Id", "Name", ""],
                ["invalid-type", "invalid-type", "invalid-type"],
                ["invalid-scope", "invalid-scope", "invalid-scope"],
                ["invalid", "invalid", "invalid"],
                ["invalid", "invalid", "invalid"],
                ["1", "Alpha", "ignored"],
            ]
        )
    )

    result = scan_csv_header(path)

    assert result.encoding == "utf-8-sig"
    assert [(field.display_name, field.field_name) for field in result.fields] == [
        ("流水ID", "Id"),
        ("多行\n中文说明", "Name"),
    ]
    assert result.issues[0]["code"] == "DISPLAY_WITHOUT_FIELD_NAME"


def test_scan_csv_header_supports_gb18030_and_flags_missing_display_name(tmp_path: Path):
    path = tmp_path / "Legacy.csv"
    path.write_bytes(_csv_bytes([["", "名称"], ["Id", "Name"]], encoding="gb18030"))

    result = scan_csv_header(path)

    assert result.encoding == "gb18030"
    assert result.fields[0].quality_issues == ("MISSING_DISPLAY_NAME",)
    assert result.fields[1].display_name == "名称"


def test_duplicate_field_names_are_marked_on_review_rows(tmp_path: Path):
    path = tmp_path / "Duplicate.csv"
    path.write_bytes(_csv_bytes([["字段A", "字段B"], ["Value", "Value"]]))

    result = scan_csv_header(path)

    assert [field.quality_issues for field in result.fields] == [
        ("DUPLICATE_FIELD_NAME",),
        ("DUPLICATE_FIELD_NAME",),
    ]
    assert result.issues[0]["code"] == "DUPLICATE_FIELD_NAME"


def test_catalog_order_is_stable(tmp_path: Path):
    (tmp_path / "b.csv").write_bytes(_csv_bytes([["编号"], ["Id"]]))
    (tmp_path / "A.csv").write_bytes(_csv_bytes([["名称"], ["Name"]]))

    first = scan_catalog(tmp_path)
    second = scan_catalog(tmp_path)

    assert first == second
    assert first.files[0].csv_name == "A.csv"
    assert first.files[1].csv_name == "b.csv"
