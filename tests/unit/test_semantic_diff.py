import csv
from io import StringIO

from core.semantic_diff import diff_table_csv
from core.table_csv_parser import parse_table_csv


def _table(headers, types, rows, name="sample.csv"):
    buffer = StringIO(newline="")
    writer = csv.writer(buffer, lineterminator="\n")
    writer.writerows(
        [
            headers,
            headers,
            types,
            ["All"] * len(headers),
            ["meta"] * len(headers),
            ["meta"] * len(headers),
            ["meta"] * len(headers),
            *rows,
        ]
    )
    return parse_table_csv(buffer.getvalue().encode("utf-8"), name)


def test_semantic_diff_ignores_row_and_field_reordering():
    source = _table(
        ["Id", "Name", "Score"],
        ["uint32", "string", "uint32"],
        [["1", "Alpha", "01"], ["2", "Beta", "2"]],
    )
    target = _table(
        ["Score", "Id", "Name"],
        ["uint32", "uint32", "string"],
        [["2", "2", "Beta"], ["1", "1", "Alpha"]],
    )

    result = diff_table_csv(source, target)

    assert result.status == "unchanged"
    assert result.rows == ()


def test_semantic_diff_never_pairs_changed_keys_by_row_number():
    source = _table(["Id", "Name"], ["uint32", "string"], [["1", "Alpha"]])
    target = _table(["Id", "Name"], ["uint32", "string"], [["2", "Beta"]])

    result = diff_table_csv(source, target)

    assert [(row.key, row.status) for row in result.rows] == [
        ("1", "source_only"),
        ("2", "target_only"),
    ]
    assert result.summary.modified_rows == 0


def test_semantic_diff_keeps_source_order_then_target_only_order():
    source = _table(
        ["Id", "Name"],
        ["uint32", "string"],
        [["2", "A"], ["1", "B"]],
    )
    target = _table(
        ["Id", "Name"],
        ["uint32", "string"],
        [["2", "AA"], ["3", "C"], ["4", "D"]],
    )

    result = diff_table_csv(source, target)

    assert [(row.key, row.status) for row in result.rows] == [
        ("2", "modified"),
        ("1", "source_only"),
        ("3", "target_only"),
        ("4", "target_only"),
    ]
    assert result.summary.modified_fields == 1
