import csv
from io import StringIO

import pytest

from core.m2_errors import M2ProcessingError
from core.table_csv_parser import parse_table_csv


def _csv_bytes(data_rows):
    buffer = StringIO(newline="")
    writer = csv.writer(buffer, lineterminator="\n")
    writer.writerows(
        [
            ["显示名", "描述"],
            ["Id", "Name"],
            ["uint32", "string"],
            ["All", "Client"],
            ["meta", "line one\nline two"],
            ["meta", ""],
            ["meta", ""],
            *data_rows,
        ]
    )
    return b"\xef\xbb\xbf" + buffer.getvalue().encode("utf-8")


def _layout_csv(display_names, headers, types, scopes, data_rows):
    width = max(len(display_names), len(headers), len(types), len(scopes))
    buffer = StringIO(newline="")
    writer = csv.writer(buffer, lineterminator="\n")
    writer.writerows(
        [
            display_names,
            headers,
            types,
            scopes,
            ["meta"] * width,
            ["meta"] * width,
            ["meta"] * width,
            *data_rows,
        ]
    )
    return b"\xef\xbb\xbf" + buffer.getvalue().encode("utf-8")


def test_table_csv_parser_uses_logical_record_numbers():
    parsed = parse_table_csv(
        _csv_bytes([["001", " Alpha "], ["2", "Beta"]]),
        "AtlasConfig_Base.csv",
    )

    assert parsed.primary_key == "Id"
    assert [(field.display_name, field.name) for field in parsed.fields] == [
        ("显示名", "Id"),
        ("描述", "Name"),
    ]
    assert [row.row_number for row in parsed.rows] == [8, 9]
    assert parsed.rows[0].key == "001"
    assert parsed.rows[0].values["Name"] == " Alpha "
    assert parsed.rows[0].normalized_values["Id"] == "1"


def test_table_csv_parser_rejects_duplicate_keys():
    with pytest.raises(M2ProcessingError) as captured:
        parse_table_csv(
            _csv_bytes([["1", "Alpha"], ["1", "Beta"]]),
            "AtlasConfig_Base.csv",
        )

    assert captured.value.code == "M2_CSV_DUPLICATE_KEY"
    assert captured.value.details == {"key": "1", "rows": [8, 9]}


@pytest.mark.parametrize(
    ("field_name", "declared_type", "scope", "key"),
    [
        ("Old_level", "uint32", "Server", "1"),
        ("EntryQuality", "uint32", "Client", "2"),
        ("ShopId", "uint32", "All", "510000"),
    ],
)
def test_table_csv_parser_falls_back_to_first_column(
    field_name,
    declared_type,
    scope,
    key,
):
    parsed = parse_table_csv(
        _layout_csv(
            ["业务键", "名称"],
            [field_name, "Name"],
            [declared_type, "string"],
            [scope, "Client"],
            [[key, "Alpha"]],
        ),
        "first_column_primary_key.csv",
    )

    assert parsed.primary_key == field_name
    assert [row.key for row in parsed.rows] == [key]


def test_parser_does_not_fall_back_past_scope_none_first_column():
    with pytest.raises(M2ProcessingError) as captured:
        parse_table_csv(
            _layout_csv(
                ["忽略字段", "业务编码"],
                ["Ignored", "Code"],
                ["uint32", "uint32"],
                ["None", "All"],
                [["99", "1"]],
            ),
            "scope_none_first_column.csv",
        )

    assert captured.value.code == "M2_CSV_PRIMARY_KEY_MISSING"


def test_parser_prefers_configured_primary_key_over_first_column():
    parsed = parse_table_csv(
        _layout_csv(
            ["排序", "ID", "名称"],
            ["Order", "Id", "Name"],
            ["uint32", "uint32", "string"],
            ["All", "All", "Client"],
            [["10", "1", "Alpha"]],
        ),
        "id_after_first_column.csv",
    )

    assert parsed.primary_key == "Id"
    assert [row.key for row in parsed.rows] == ["1"]


def test_parser_rejects_duplicate_first_column_fallback_keys():
    with pytest.raises(M2ProcessingError) as captured:
        parse_table_csv(
            _layout_csv(
                ["旧等级", "新等级"],
                ["Old_level", "Reborn_level"],
                ["uint32", "uint32"],
                ["Server", "Server"],
                [["1", "2"], ["1", "3"]],
            ),
            "HeroConfig_Reborn_TransferLevel.csv",
        )

    assert captured.value.code == "M2_CSV_DUPLICATE_KEY"
    assert captured.value.details == {"key": "1", "rows": [8, 9]}


def test_table_csv_parser_ignores_trailing_unnamed_note_columns():
    buffer = StringIO(newline="")
    writer = csv.writer(buffer, lineterminator="\n")
    writer.writerows(
        [
            ["显示名", "备注"],
            ["Id", ""],
            ["uint32", ""],
            ["All", ""],
            ["meta", ""],
            ["meta", ""],
            ["meta", ""],
            ["1", "仅供策划阅读"],
        ]
    )

    parsed = parse_table_csv(
        b"\xef\xbb\xbf" + buffer.getvalue().encode("utf-8"),
        "AtlasConfig_Hero.csv",
    )

    assert [field.name for field in parsed.fields] == ["Id"]
    assert parsed.rows[0].values == {"Id": "1"}


def test_parser_filters_scope_none_before_duplicate_validation():
    parsed = parse_table_csv(
        _layout_csv(
            ["ID", "业务名称", "策划备注"],
            ["Id", "Name", "Name"],
            ["uint32", "string", "string"],
            ["All", "Client", "None"],
            [["1", "Alpha", "note"]],
        ),
        "ArenaPeak_Map.csv",
    )

    assert [(field.name, field.position) for field in parsed.fields] == [
        ("Id", 0),
        ("Name", 1),
    ]
    assert parsed.rows[0].values == {"Id": "1", "Name": "Alpha"}


def test_parser_keeps_active_duplicate_field_failure():
    with pytest.raises(M2ProcessingError) as captured:
        parse_table_csv(
            _layout_csv(
                ["ID", "名称一", "名称二"],
                ["Id", "Name", "Name"],
                ["uint32", "string", "string"],
                ["All", "Client", "All"],
                [["1", "Alpha", "Beta"]],
            ),
            "active_duplicate.csv",
        )

    assert captured.value.code == "M2_CSV_DUPLICATE_FIELD"
    assert captured.value.details == {"field": "Name", "columns": [2, 3]}


def test_parser_recognizes_labeled_middle_annotation_column():
    parsed = parse_table_csv(
        _layout_csv(
            ["ID", "备注", "值"],
            ["Id", "", "Value"],
            ["uint32", "", "string"],
            ["All", "", "Client"],
            [["1", "策划注释", "Alpha"]],
        ),
        "middle_annotation.csv",
    )

    assert [(field.name, field.position) for field in parsed.fields] == [
        ("Id", 0),
        ("Value", 2),
    ]
    assert parsed.rows[0].values == {"Id": "1", "Value": "Alpha"}


@pytest.mark.parametrize(
    ("declared_type", "scope"),
    [("uint32", ""), ("", "All")],
)
def test_parser_rejects_empty_code_with_active_scope_or_type(declared_type, scope):
    with pytest.raises(M2ProcessingError) as captured:
        parse_table_csv(
            _layout_csv(
                ["ID", "备注", "值"],
                ["Id", "", "Value"],
                ["uint32", declared_type, "string"],
                ["All", scope, "Client"],
                [["1", "invalid", "Alpha"]],
            ),
            "invalid_middle_field.csv",
        )

    assert captured.value.code == "M2_CSV_STRUCTURE_INVALID"
    assert captured.value.details == {"column": 2}


def test_parser_skips_row_with_only_scope_none_values():
    parsed = parse_table_csv(
        _layout_csv(
            ["ID", "名称", "说明"],
            ["Id", "Name", "Des"],
            ["uint32", "string", "string"],
            ["All", "Client", "None"],
            [["1", "Alpha", ""], ["", "", "-3"]],
        ),
        "ArenaTop64_Notice.csv",
    )

    assert [row.key for row in parsed.rows] == ["1"]


def test_parser_accepts_unique_casefold_primary_key():
    parsed = parse_table_csv(
        _layout_csv(
            ["ID", "名称"],
            ["ID", "Name"],
            ["uint32", "string"],
            ["All", "Client"],
            [["001", "Alpha"], ["2", "Beta"]],
        ),
        "ActivityBossConfigNew_Base.csv",
    )

    assert parsed.primary_key == "ID"
    assert [row.key for row in parsed.rows] == ["001", "2"]
    assert parsed.rows[0].normalized_values["ID"] == "1"


def test_parser_rejects_ambiguous_casefold_primary_keys():
    with pytest.raises(M2ProcessingError) as captured:
        parse_table_csv(
            _layout_csv(
                ["ID 1", "ID 2", "名称"],
                ["Id", "ID", "Name"],
                ["uint32", "uint32", "string"],
                ["All", "All", "Client"],
                [["1", "1", "Alpha"]],
            ),
            "ambiguous_case_id.csv",
        )

    assert captured.value.code == "M2_CSV_PRIMARY_KEY_MISSING"
    assert captured.value.details["matches"] == ["Id", "ID"]
