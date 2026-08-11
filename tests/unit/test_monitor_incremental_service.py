from __future__ import annotations

import csv
from datetime import datetime, timezone
from io import BytesIO, StringIO
import json

from openpyxl import Workbook

from app.services.branch_history_service import BranchHistoryService
from app.services.monitor_diff_service import MonitorDiffService, SvnMonitorSnapshotReader
from app.services.monitor_incremental_service import (
    MonitorChangedPathPlanner,
    MonitorIncrementalReplayService,
    MonitorManifestIndex,
    compare_legacy_and_incremental,
)
from app.services.monitor_performance import MonitorPerformanceRecorder
from app.services.workbook_diff_service import DatasetLayout
from core.models import TreeEntry
from core.svn_history import BranchChangedPath, BranchCommit, BranchIdentity
from core.svn_provider import SVNProviderError


def workbook_bytes(entries: list[tuple[str, str]]) -> bytes:
    workbook = Workbook()
    sheet = workbook.active
    sheet.title = "main"
    sheet.append(["sheetName", "tbxName", "isExport"])
    for sheet_name, tbx_name in entries:
        sheet.append([sheet_name, tbx_name, 1])
    output = BytesIO()
    workbook.save(output)
    workbook.close()
    return output.getvalue()


def csv_bytes(rows: list[tuple[str, str]], *, value_name: str = "Value") -> bytes:
    output = StringIO(newline="")
    writer = csv.writer(output, lineterminator="\n")
    writer.writerows(
        [
            ["编号", "数值"],
            ["Id", value_name],
            ["uint32", "uint32"],
            ["All", "Client"],
            ["meta", "meta"],
            ["meta", "meta"],
            ["meta", "meta"],
            *rows,
        ]
    )
    return output.getvalue().encode("utf-8")


def files(combat_value: str, *, bonus: bool = False, shared: bool = False):
    combat_tbx = "shared" if shared else "role"
    combat_entries = [("Role", combat_tbx)]
    if bonus:
        combat_entries.append(("Bonus", "bonus"))
    other_tbx = "shared" if shared else "other"
    result = {
        "Source/Table/Combat.xlsm": workbook_bytes(combat_entries),
        "Source/Table/Other.xlsm": workbook_bytes([("Other", other_tbx)]),
        f"Source/TableCsv/{combat_tbx}.csv": csv_bytes([("1", combat_value)]),
    }
    if not shared:
        result["Source/TableCsv/other.csv"] = csv_bytes([("2", "50")])
    if bonus:
        result["Source/TableCsv/bonus.csv"] = csv_bytes([("10", "7")])
    return result


class History:
    def __init__(self, revisions):
        self.files = revisions
        self.reads = []
        self.lists = []

    def list_paths_at_revision(self, identity, revision):
        self.lists.append(revision)
        return [TreeEntry(path=path, kind="file") for path in self.files[revision]]

    def read_path_bytes_at_revision(self, identity, path, revision):
        self.reads.append((path, revision))
        try:
            return self.files[revision][path]
        except KeyError as error:
            raise SVNProviderError("SVN_PATH_NOT_FOUND", "missing") from error


def layout():
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


def reader(revisions):
    history = History(revisions)
    identity = BranchIdentity(
        canonical_url="https://svn.example/repo/branches/fixed",
        repository_root="https://svn.example/repo",
        repository_uuid="20000000-0000-4000-8000-000000000001",
        repository_relative_path="branches/fixed",
        bound_revision=max(revisions),
    )
    snapshot_reader = SvnMonitorSnapshotReader(
        BranchHistoryService(history),
        identity,
        layout(),
        table_directory="Source/Table",
    )
    return history, snapshot_reader


def changed(path: str, action: str = "M"):
    return BranchChangedPath(
        repository_path="branches/fixed/" + path,
        branch_relative_path=path,
        action=action,
    )


def commit(revision: int, author: str | None, *paths):
    return BranchCommit(
        revision=revision,
        author=author,
        changed_at=datetime(2026, 8, 10, revision % 24, tzinfo=timezone.utc),
        message=f"r{revision}",
        changed_paths=tuple(paths),
    )


def test_csv_only_replay_matches_legacy_and_skips_unaffected_workbook():
    revisions = {100: files("100"), 101: files("120"), 102: files("110")}
    commits = [
        commit(101, "alice", changed("Source/TableCsv/role.csv")),
        commit(102, "bob", changed("Source/TableCsv/role.csv")),
    ]
    performance = MonitorPerformanceRecorder(enabled=True)
    history, snapshot_reader = reader(revisions)
    replay = MonitorIncrementalReplayService(
        MonitorDiffService(snapshot_reader), performance=performance
    ).replay(start_revision=100, end_revision=102, commits=commits)

    assert replay.result.workbook_count == 2
    assert len(replay.result.changes) == 1
    change_result = replay.result.changes[0]
    assert (change_result.source.display_value, change_result.target.display_value) == (
        "100",
        "110",
    )
    assert (change_result.attribution.author, change_result.attribution.revision) == (
        "bob",
        102,
    )
    assert all(
        "Other" not in path
        for path, revision in history.reads
        if revision in {101, 102}
    )
    assert [plan.affected_sheets for plan in replay.plans] == [
        (("Combat.xlsm", "Role"),),
        (("Combat.xlsm", "Role"),),
    ]

    _, shadow_reader = reader(revisions)
    shadow = compare_legacy_and_incremental(
        MonitorDiffService(shadow_reader),
        start_revision=100,
        end_revision=102,
        commits=commits,
    )
    assert shadow.matches is True
    metrics = json.dumps(performance.snapshot(), ensure_ascii=False)
    assert "svn.example" not in metrics
    assert "Source/" not in metrics


def test_manifest_addition_reloads_only_changed_workbook_and_matches_legacy():
    revisions = {100: files("100"), 105: files("100", bonus=True)}
    commits = [
        commit(
            105,
            "designer",
            changed("Source/Table/Combat.xlsm"),
            changed("Source/TableCsv/bonus.csv", "A"),
        )
    ]
    history, snapshot_reader = reader(revisions)
    replay = MonitorIncrementalReplayService(
        MonitorDiffService(snapshot_reader)
    ).replay(start_revision=100, end_revision=105, commits=commits)

    assert replay.plans[0].workbook_actions == (("Combat.xlsm", "M"),)
    assert {
        (item.change_type.value, item.sheet_name) for item in replay.result.changes
    } == {("field_added", "Bonus"), ("row_added", "Bonus")}
    assert {item.attribution.revision for item in replay.result.changes} == {105}
    assert all(
        "Other" not in path for path, revision in history.reads if revision == 105
    )

    _, shadow_reader = reader(revisions)
    assert compare_legacy_and_incremental(
        MonitorDiffService(shadow_reader),
        start_revision=100,
        end_revision=105,
        commits=commits,
    ).matches


def test_directory_change_uses_safe_full_snapshot_fallback():
    revisions = {100: files("100"), 101: files("120")}
    event = commit(101, "alice", changed("Source/TableCsv"))
    history, snapshot_reader = reader(revisions)
    replay = MonitorIncrementalReplayService(
        MonitorDiffService(snapshot_reader)
    ).replay(start_revision=100, end_revision=101, commits=[event])

    assert replay.plans[0].fallback_reason == "csv_scope_change"
    assert any("Other" in path for path, revision in history.reads if revision == 101)


def test_irrelevant_commit_advances_state_without_snapshot_reload():
    revisions = {100: files("100"), 101: files("100")}
    event = commit(101, "writer", changed("Docs/readme.txt"))
    history, snapshot_reader = reader(revisions)
    replay = MonitorIncrementalReplayService(
        MonitorDiffService(snapshot_reader)
    ).replay(start_revision=100, end_revision=101, commits=[event])

    assert replay.result.changes == ()
    assert replay.plans[0].irrelevant_path_count == 1
    assert not any(revision == 101 for _, revision in history.reads)
    assert history.lists == [100]


def test_reverse_index_preserves_all_csv_owners():
    revisions = {100: files("100", shared=True)}
    _, snapshot_reader = reader(revisions)
    snapshot = snapshot_reader.load_snapshot(100)
    index = MonitorManifestIndex.from_snapshot(
        snapshot, csv_directory=snapshot_reader.csv_directory
    )
    event = commit(101, "alice", changed("Source/TableCsv/shared.csv"))

    plan = MonitorChangedPathPlanner(snapshot_reader).plan(event, index)

    assert plan.affected_sheets == (
        ("Combat.xlsm", "Role"),
        ("Other.xlsm", "Other"),
    )
