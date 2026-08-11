from __future__ import annotations

from app.services.monitor_diff_service import MonitorDiffService
from app.services.monitor_incremental_service import (
    MonitorIncrementalReplayService,
    compare_legacy_and_incremental,
)
from core.svn_history import BranchChangedPath

from tests.unit.test_monitor_incremental_service import (
    changed,
    commit,
    csv_bytes,
    files,
    reader,
    workbook_bytes,
)


def _shadow(revisions, commits):
    _, snapshot_reader = reader(revisions)
    return compare_legacy_and_incremental(
        MonitorDiffService(snapshot_reader),
        start_revision=min(revisions),
        end_revision=max(revisions),
        commits=commits,
    )


def test_deleted_csv_owner_survives_error_and_rebuild_matches_legacy():
    revisions = {100: files("100")}
    revisions[101] = dict(revisions[100])
    revisions[101].pop("Source/TableCsv/role.csv")
    revisions[102] = files("130")
    commits = [
        commit(101, "deleter", changed("Source/TableCsv/role.csv", "D")),
        commit(102, "rebuilder", changed("Source/TableCsv/role.csv", "A")),
    ]

    shadow = _shadow(revisions, commits)

    assert shadow.matches
    assert len(shadow.incremental.result.changes) == 1
    change_result = shadow.incremental.result.changes[0]
    assert change_result.attribution.status == "unresolved"
    assert [error.stage.value for error in shadow.incremental.result.errors] == [
        "attribution"
    ]
    assert shadow.incremental.plans[1].affected_sheets == (
        ("Combat.xlsm", "Role"),
    )


def test_manifest_rebind_uses_new_csv_but_keeps_sheet_identity():
    revisions = {100: files("100")}
    target = dict(revisions[100])
    target["Source/Table/Combat.xlsm"] = workbook_bytes([("Role", "role2")])
    target["Source/TableCsv/role2.csv"] = csv_bytes([("1", "120")])
    revisions[103] = target
    commits = [
        commit(
            103,
            "designer",
            changed("Source/Table/Combat.xlsm"),
            changed("Source/TableCsv/role2.csv", "A"),
        )
    ]

    shadow = _shadow(revisions, commits)

    assert shadow.matches
    changes = shadow.incremental.result.changes
    assert [(item.change_type.value, item.sheet_name) for item in changes] == [
        ("field_modified", "Role")
    ]
    assert changes[0].attribution.author == "designer"


def test_tbx_rename_with_same_business_value_has_no_net_change():
    revisions = {100: files("100")}
    target = dict(revisions[100])
    target["Source/Table/Combat.xlsm"] = workbook_bytes([("Role", "renamed")])
    target["Source/TableCsv/renamed.csv"] = target.pop(
        "Source/TableCsv/role.csv"
    )
    revisions[104] = target
    moved = BranchChangedPath(
        repository_path="branches/fixed/Source/TableCsv/renamed.csv",
        branch_relative_path="Source/TableCsv/renamed.csv",
        action="A",
        copied_from_path="branches/fixed/Source/TableCsv/role.csv",
        copied_from_revision=100,
    )
    commits = [
        commit(
            104,
            "designer",
            changed("Source/Table/Combat.xlsm"),
            changed("Source/TableCsv/role.csv", "D"),
            moved,
        )
    ]

    shadow = _shadow(revisions, commits)

    assert shadow.matches
    assert shadow.incremental.result.changes == ()


def test_manifest_sheet_removal_matches_legacy_structure_and_row_events():
    revisions = {100: files("100", bonus=True), 105: files("100")}
    commits = [commit(105, "designer", changed("Source/Table/Combat.xlsm"))]

    shadow = _shadow(revisions, commits)

    assert shadow.matches
    assert {
        (item.change_type.value, item.sheet_name)
        for item in shadow.incremental.result.changes
    } == {("field_removed", "Bonus"), ("row_deleted", "Bonus")}
    assert {
        item.attribution.revision for item in shadow.incremental.result.changes
    } == {105}


def test_same_csv_change_updates_every_manifest_owner():
    revisions = {100: files("100", shared=True), 106: files("120", shared=True)}
    commits = [
        commit(106, "shared-owner", changed("Source/TableCsv/shared.csv"))
    ]

    shadow = _shadow(revisions, commits)

    assert shadow.matches
    assert {
        (item.workbook, item.sheet_name, item.attribution.revision)
        for item in shadow.incremental.result.changes
    } == {
        ("Combat.xlsm", "Role", 106),
        ("Other.xlsm", "Other", 106),
    }


def test_casefold_csv_conflict_remains_partial_and_matches_legacy():
    revisions = {100: files("100")}
    target = dict(revisions[100])
    target.pop("Source/TableCsv/role.csv")
    target["Source/TableCsv/ROLE.csv"] = csv_bytes([("1", "110")])
    target["Source/TableCsv/Role.csv"] = csv_bytes([("1", "120")])
    revisions[107] = target
    commits = [
        commit(
            107,
            "designer",
            changed("Source/TableCsv/role.csv", "D"),
            changed("Source/TableCsv/ROLE.csv", "A"),
            changed("Source/TableCsv/Role.csv", "A"),
        )
    ]

    shadow = _shadow(revisions, commits)

    assert shadow.matches
    result = shadow.incremental.result
    assert result.changes == ()
    assert [(error.stage.value, error.sheet_name) for error in result.errors] == [
        ("csv_parse", "Role")
    ]


def test_local_csv_parse_failure_matches_legacy_error_coverage():
    revisions = {100: files("100")}
    target = dict(revisions[100])
    target["Source/TableCsv/role.csv"] = b"not,a,valid,table"
    revisions[108] = target
    commits = [commit(108, "designer", changed("Source/TableCsv/role.csv"))]

    shadow = _shadow(revisions, commits)

    assert shadow.matches
    result = shadow.incremental.result
    assert result.changes == ()
    assert len(result.errors) == 1
    assert result.errors[0].stage.value == "csv_parse"


def test_unknown_author_keeps_revision_and_does_not_create_error():
    revisions = {100: files("100"), 109: files("110")}
    commits = [commit(109, None, changed("Source/TableCsv/role.csv"))]

    shadow = _shadow(revisions, commits)

    assert shadow.matches
    result = shadow.incremental.result
    assert result.errors == ()
    assert result.changes[0].attribution.status == "unknown_author"
    assert result.changes[0].attribution.revision == 109


def test_no_commit_terminal_fallback_preserves_unresolved_semantics():
    revisions = {100: files("100"), 110: files("120")}

    shadow = _shadow(revisions, [])

    assert shadow.matches
    result = shadow.incremental.result
    assert result.changes[0].attribution.status == "unresolved"
    assert result.errors[0].stage.value == "attribution"


def test_missing_changed_paths_uses_full_snapshot_fallback():
    revisions = {100: files("100"), 111: files("120")}
    commits = [commit(111, "designer")]
    history, snapshot_reader = reader(revisions)
    replay = MonitorIncrementalReplayService(
        MonitorDiffService(snapshot_reader)
    ).replay(start_revision=100, end_revision=111, commits=commits)

    assert replay.plans[0].fallback_reason == "missing_changed_paths"
    assert any("Other" in path for path, revision in history.reads if revision == 111)
    assert _shadow(revisions, commits).matches


def test_workbook_delete_and_rebuild_intermediate_state_nets_to_zero():
    revisions = {100: files("100")}
    removed = dict(revisions[100])
    removed.pop("Source/Table/Combat.xlsm")
    revisions[112] = removed
    revisions[113] = files("100")
    commits = [
        commit(112, "deleter", changed("Source/Table/Combat.xlsm", "D")),
        commit(113, "rebuilder", changed("Source/Table/Combat.xlsm", "A")),
    ]

    shadow = _shadow(revisions, commits)

    assert shadow.matches
    assert shadow.incremental.result.changes == ()
