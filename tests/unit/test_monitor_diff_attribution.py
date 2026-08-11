from __future__ import annotations

from copy import deepcopy
import csv
from datetime import datetime
from io import StringIO
import json
from pathlib import Path

from app.schemas.monitor import (
    MonitorErrorCode,
    MonitorErrorStage,
    MonitorPublicErrorPayload,
)
from app.services.monitor_attribution_service import MonitorAttributionService
from app.services.monitor_diff_service import (
    MonitorDiffService,
    MonitorSnapshot,
    MonitorWorkbookSnapshot,
)
from core.svn_history import BranchCommit
from core.table_csv_parser import parse_table_csv


FIXTURE = (
    Path(__file__).parents[1] / "fixtures" / "m3_monitor" / "mock_svn_history.json"
)


def _table(
    rows: dict[str, dict[str, str]],
    *,
    definitions: dict[str, dict[str, str]] | None = None,
    note_values: dict[str, str] | None = None,
):
    definitions = definitions or {
        "Id": {"display_name": "ID", "declared_type": "uint32", "scope": "All"},
        "Hp": {"display_name": "生命值", "declared_type": "uint32", "scope": "Client"},
    }
    fields = list(definitions)
    if note_values is not None:
        fields.append("Note")
        definitions = {
            **definitions,
            "Note": {"display_name": "备注", "declared_type": "string", "scope": "None"},
        }
    buffer = StringIO(newline="")
    writer = csv.writer(buffer, lineterminator="\n")
    writer.writerows(
        [
            [definitions[field]["display_name"] for field in fields],
            fields,
            [definitions[field]["declared_type"] for field in fields],
            [definitions[field]["scope"] for field in fields],
            ["meta"] * len(fields),
            ["meta"] * len(fields),
            ["meta"] * len(fields),
            *[
                [
                    values.get(field, note_values.get(key, "") if note_values else "")
                    for field in fields
                ]
                for key, values in rows.items()
            ],
        ]
    )
    return parse_table_csv(buffer.getvalue().encode("utf-8"), "Role.csv")


def _snapshot(
    revision: int,
    rows: dict[str, dict[str, str]],
    *,
    definitions: dict[str, dict[str, str]] | None = None,
    errors=(),
    note_values: dict[str, str] | None = None,
):
    return MonitorSnapshot(
        revision=revision,
        workbooks={
            "CombatConfig.xlsm": MonitorWorkbookSnapshot(
                sheets={"Role": _table(rows, definitions=definitions, note_values=note_values)}
            )
        },
        errors=tuple(errors),
    )


class Reader:
    def __init__(self, snapshots):
        self.snapshots = snapshots

    def load_snapshot(self, revision):
        return self.snapshots[revision]


def _commit(revision, author, hour):
    return BranchCommit(
        revision=revision,
        author=author,
        changed_at=datetime.fromisoformat(f"2026-08-10T{hour:02d}:00:00+00:00"),
        message=f"r{revision}",
    )


def test_field_catalog_prefers_target_display_names_and_falls_back_to_source():
    source_definitions = {
        "Id": {"display_name": "旧编号", "declared_type": "uint32", "scope": "All"},
        "Legacy": {"display_name": "旧字段", "declared_type": "string", "scope": "Client"},
    }
    target_definitions = {
        "Id": {"display_name": "新编号", "declared_type": "uint32", "scope": "All"},
        "Current": {"display_name": "新字段", "declared_type": "string", "scope": "Client"},
    }
    snapshots = {
        100: _snapshot(
            100,
            {"1": {"Id": "1", "Legacy": "old"}},
            definitions=source_definitions,
        ),
        101: _snapshot(
            101,
            {"2": {"Id": "2", "Current": "new"}},
            definitions=target_definitions,
        ),
    }
    service = MonitorDiffService(Reader(snapshots))
    net = service.compare_revisions(100, 101)

    catalog = net.field_catalog[0]
    assert (catalog.workbook, catalog.sheet_name) == ("CombatConfig.xlsm", "Role")
    assert [
        (field.field_name, field.display_name) for field in catalog.fields
    ] == [
        ("Id", "新编号"),
        ("Current", "新字段"),
        ("Legacy", "旧字段"),
    ]

    attributed = MonitorAttributionService(service).attribute(
        net,
        start_revision=100,
        commits=[_commit(101, "author", 1)],
    )
    assert attributed.field_catalog == net.field_catalog

def test_last_commit_that_forms_final_value_gets_attribution():
    snapshots = {
        100: _snapshot(100, {"100": {"Id": "100", "Hp": "100"}}),
        101: _snapshot(101, {"100": {"Id": "100", "Hp": "120"}}),
        103: _snapshot(103, {"100": {"Id": "100", "Hp": "110"}}),
    }
    service = MonitorDiffService(Reader(snapshots))
    net = service.compare_revisions(100, 103)

    result = MonitorAttributionService(service).attribute(
        net,
        start_revision=100,
        commits=[_commit(101, "alice", 1), _commit(103, "bob", 2)],
    )

    assert len(result.changes) == 1
    change = result.changes[0]
    assert (change.source.display_value, change.target.display_value) == ("100", "110")
    assert (change.attribution.revision, change.attribution.author) == (103, "bob")


def test_reverted_value_has_no_final_net_change():
    snapshots = {
        100: _snapshot(100, {"400": {"Id": "400", "Hp": "100"}}),
        104: _snapshot(104, {"400": {"Id": "400", "Hp": "120"}}),
        106: _snapshot(106, {"400": {"Id": "400", "Hp": "100"}}),
    }
    service = MonitorDiffService(Reader(snapshots))

    net = service.compare_revisions(100, 106)
    result = MonitorAttributionService(service).attribute(
        net,
        start_revision=100,
        commits=[_commit(104, "alice", 3), _commit(106, "alice", 4)],
    )

    assert result.changes == ()
    assert result.errors == ()


def test_missing_event_is_stably_unresolved_and_missing_author_is_unknown():
    snapshots = {
        100: _snapshot(100, {"100": {"Id": "100", "Hp": "100"}}),
        101: _snapshot(101, {"100": {"Id": "100", "Hp": "110"}}),
    }
    service = MonitorDiffService(Reader(snapshots))
    net = service.compare_revisions(100, 101)

    unresolved = MonitorAttributionService(service).attribute(
        net,
        start_revision=100,
        commits=[],
    )
    assert unresolved.changes[0].attribution.status == "unresolved"
    assert unresolved.changes[0].attribution.revision is None
    assert [error.code.value for error in unresolved.errors] == [
        "MONITOR_ATTRIBUTION_INCOMPLETE"
    ]

    unknown = MonitorAttributionService(service).attribute(
        net,
        start_revision=100,
        commits=[_commit(101, None, 1)],
    )
    assert unknown.changes[0].attribution.status == "unknown_author"
    assert unknown.changes[0].attribution.revision == 101
    assert unknown.errors == ()


def test_rows_structure_scope_type_and_primary_key_follow_m2_semantics():
    source_definitions = {
        "Id": {"display_name": "ID", "declared_type": "uint32", "scope": "All"},
        "Hp": {"display_name": "生命值", "declared_type": "uint32", "scope": "Client"},
    }
    target_definitions = deepcopy(source_definitions)
    target_definitions["Hp"]["declared_type"] = "uint64"
    display_changed_definitions = deepcopy(target_definitions)
    display_changed_definitions["Hp"]["display_name"] = "最终生命值"
    snapshots = {
        100: _snapshot(
            100,
            {
                "100": {"Id": "100", "Hp": "01"},
                "200": {"Id": "200", "Hp": "90"},
            },
            definitions=source_definitions,
            note_values={"100": "old"},
        ),
        101: _snapshot(
            101,
            {
                "100": {"Id": "100", "Hp": "01"},
                "200": {"Id": "200", "Hp": "90"},
            },
            definitions=display_changed_definitions,
            note_values={"100": "ignored"},
        ),
        102: _snapshot(
            102,
            {
                "100": {"Id": "100", "Hp": "1"},
                "200": {"Id": "200", "Hp": "90"},
                "300": {"Id": "300", "Hp": "80"},
            },
            definitions=display_changed_definitions,
            note_values={"100": "still ignored"},
        ),
        103: _snapshot(
            103,
            {
                "100": {"Id": "100", "Hp": "1"},
                "300": {"Id": "300", "Hp": "80"},
            },
            definitions=target_definitions,
            note_values={"100": "ignored again"},
        ),
    }
    service = MonitorDiffService(Reader(snapshots))
    net = service.compare_revisions(100, 103)
    result = MonitorAttributionService(service).attribute(
        net,
        start_revision=100,
        commits=[_commit(101, "schema", 1), _commit(102, "creator", 2), _commit(103, "deleter", 3)],
    )

    assert [(change.change_type.value, change.row_key) for change in result.changes] == [
        ("field_definition_modified", None),
        ("row_deleted", "200"),
        ("row_added", "300"),
    ]
    assert [change.primary_key_field for change in result.changes] == ["Id", "Id", "Id"]
    assert [change.attribution.author for change in result.changes] == [
        "schema",
        "deleter",
        "creator",
    ]


def test_phase_zero_mock_drives_real_net_diff_and_event_ledger():
    fixture = json.loads(FIXTURE.read_text(encoding="utf-8"))
    rows = deepcopy(fixture["start_rows"])
    definitions = deepcopy(fixture["start_field_definitions"])
    snapshots = {
        fixture["start_revision"]: _snapshot(
            fixture["start_revision"], rows, definitions=definitions
        )
    }
    commits = []
    for item in fixture["commits"]:
        errors = []
        for event in item["target_events"]:
            if event["event"] == "field_set":
                rows[event["row_key"]][event["field_name"]] = event["value"]
            elif event["event"] == "field_definition_modified":
                definitions[event["field_name"]]["declared_type"] = event["declared_type"]
                definitions[event["field_name"]]["scope"] = event["scope"]
            elif event["event"] == "row_added":
                rows[event["row_key"]] = event["values"]
            elif event["event"] == "row_deleted":
                rows.pop(event["row_key"])
            elif event["event"] == "parse_failed":
                errors.append(
                    MonitorPublicErrorPayload(
                        code=MonitorErrorCode.PARSE_FAILED,
                        stage=MonitorErrorStage.CSV_PARSE,
                        message="TableCsv 无法按冻结规则解析",
                        retryable=False,
                        workbook=event["workbook"],
                        sheet_name=event["sheet_name"],
                    )
                )
        snapshots[item["revision"]] = _snapshot(
            item["revision"],
            deepcopy(rows),
            definitions=deepcopy(definitions),
            errors=errors,
        )
        commits.append(
            BranchCommit(
                revision=item["revision"],
                author=item["author"],
                changed_at=datetime.fromisoformat(item["changed_at"].replace("Z", "+00:00")),
                message=item["message"],
            )
        )
    service = MonitorDiffService(Reader(snapshots))
    net = service.compare_revisions(fixture["start_revision"], fixture["end_revision"])
    result = MonitorAttributionService(service).attribute(
        net,
        start_revision=fixture["start_revision"],
        commits=commits,
    )

    actual = [
        {
            "change_type": change.change_type.value,
            "row_key": change.row_key,
            "field_name": change.field_name,
            "final_revision": change.attribution.revision,
            "final_author": change.attribution.author,
        }
        for change in result.changes
    ]
    expected = [
        {
            "change_type": change["change_type"],
            "row_key": change["row_key"],
            "field_name": change.get("field_name"),
            "final_revision": change["final_revision"],
            "final_author": change["final_author"],
        }
        for change in fixture["expected"]["net_changes"]
    ]
    identity = lambda change: (
        change["change_type"],
        change["row_key"] or "",
        change["field_name"] or "",
    )
    assert sorted(actual, key=identity) == sorted(expected, key=identity)
    assert [error.code.value for error in result.errors] == ["MONITOR_PARSE_FAILED"]
    assert "400" not in {change.row_key for change in result.changes}
