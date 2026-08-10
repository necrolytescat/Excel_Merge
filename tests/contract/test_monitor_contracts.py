from __future__ import annotations

from copy import deepcopy
import json
from pathlib import Path

import pytest
from pydantic import ValidationError

from app.schemas.monitor import (
    MonitorBoundaryKind,
    MonitorChangeType,
    MonitorErrorCode,
    MonitorErrorStage,
    MonitorReportPayload,
    MonitorRunPayload,
    MonitorRunStatus,
    MonitorSchedulerSyncStatus,
    MonitorTaskListPayload,
    MonitorTaskPayload,
    MonitorTaskStatus,
    serialize_monitor_json,
)


ROOT = Path(__file__).resolve().parents[2]
CONTRACTS = ROOT / "docs" / "contracts"
FIXTURE = ROOT / "tests" / "fixtures" / "m3_monitor" / "mock_svn_history.json"

EXAMPLES = (
    ("m3.monitor-task.v1.example.json", MonitorTaskPayload),
    ("m3.monitor-task-list.v1.example.json", MonitorTaskListPayload),
    ("m3.monitor-run.v1.example.json", MonitorRunPayload),
    ("m3.monitor-report.v1.example.json", MonitorReportPayload),
)


def load_json(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def make_run(status: str) -> dict:
    run = load_json(CONTRACTS / "m3.monitor-run.v1.example.json")
    if status == "partial":
        return run
    if status == "succeeded":
        run["status"] = "succeeded"
        run["attempts"][-1]["status"] = "succeeded"
        run["attempts"][-1]["errors"] = []
        run["summary"]["error_count"] = 0
        run["errors"] = []
        return run

    run["start_revision"] = None
    run["end_revision"] = None
    run["summary"] = None
    run["report_ref"] = None
    run["report_sha256"] = None
    run["report_expires_at"] = None
    if status == "failed":
        run["status"] = "failed"
        run["attempts"][-1]["status"] = "failed"
        return run
    if status == "running":
        run["status"] = "running"
        run["finished_at"] = None
        run["errors"] = []
        run["attempts"][-1]["status"] = "running"
        run["attempts"][-1]["finished_at"] = None
        run["attempts"][-1]["errors"] = []
        return run

    run["status"] = "queued"
    run["attempt_count"] = 0
    run["attempts"] = []
    run["errors"] = []
    run["started_at"] = None
    run["finished_at"] = None
    return run


def make_single_change_report() -> dict:
    report = load_json(CONTRACTS / "m3.monitor-report.v1.example.json")
    report["summary"].update(
        {
            "workbook_count": 1,
            "changed_workbook_count": 1,
            "sheet_count": 1,
            "changed_row_count": 1,
            "changed_field_count": 1,
            "author_count": 1,
            "change_count": 1,
            "error_count": 0,
        }
    )
    report["summary"]["by_change_type"].update(
        {
            "field_modified": 1,
            "row_added": 0,
            "row_deleted": 0,
            "field_definition_modified": 0,
        }
    )
    report["coverage"]["failed_workbook_count"] = 0
    report["changes"] = report["changes"][:1]
    report["errors"] = []
    return report


@pytest.mark.parametrize(("filename", "model"), EXAMPLES)
def test_monitor_examples_parse_and_are_canonical(filename, model):
    path = CONTRACTS / filename
    data = load_json(path)

    payload = model.model_validate(data)

    assert serialize_monitor_json(payload) == path.read_bytes()
    assert model.model_validate_json(serialize_monitor_json(payload)) == payload


@pytest.mark.parametrize(("filename", "model"), EXAMPLES)
def test_monitor_examples_reject_unknown_top_level_fields(filename, model):
    data = load_json(CONTRACTS / filename)
    data["internal_path"] = "not-public"

    with pytest.raises(ValidationError):
        model.model_validate(data)


def test_monitor_contracts_reject_unknown_nested_fields_and_internal_diagnostics():
    task = load_json(CONTRACTS / "m3.monitor-task.v1.example.json")
    task["scheduler"]["windows_task_name"] = "internal"
    with pytest.raises(ValidationError):
        MonitorTaskPayload.model_validate(task)

    run = load_json(CONTRACTS / "m3.monitor-run.v1.example.json")
    run["errors"][0]["stderr"] = "private command output"
    with pytest.raises(ValidationError):
        MonitorRunPayload.model_validate(run)

    report = load_json(CONTRACTS / "m3.monitor-report.v1.example.json")
    report["changes"][0]["attribution"]["traceback"] = "private stack"
    with pytest.raises(ValidationError):
        MonitorReportPayload.model_validate(report)


def test_monitor_status_and_enum_domains_are_frozen():
    assert {status.value for status in MonitorTaskStatus} == {
        "syncing",
        "active",
        "paused",
        "scheduler_error",
        "ended",
        "archived",
    }
    assert {status.value for status in MonitorSchedulerSyncStatus} == {
        "pending",
        "synced",
        "drifted",
        "error",
        "not_present",
    }
    assert {status.value for status in MonitorRunStatus} == {
        "queued",
        "running",
        "succeeded",
        "partial",
        "failed",
    }
    assert {kind.value for kind in MonitorBoundaryKind} == {
        "scheduled",
        "pause",
        "end",
    }
    assert {change.value for change in MonitorChangeType} == {
        "field_modified",
        "row_added",
        "row_deleted",
        "field_added",
        "field_removed",
        "field_definition_modified",
    }
    assert {code.value for code in MonitorErrorCode} == {
        "MONITOR_SVN_TIMEOUT",
        "MONITOR_SVN_AUTH_FAILED",
        "MONITOR_BRANCH_BINDING_INVALID",
        "MONITOR_CONFIGURATION_INVALID",
        "MONITOR_PARSE_FAILED",
        "MONITOR_ATTRIBUTION_INCOMPLETE",
        "MONITOR_REPORT_PUBLISH_FAILED",
        "MONITOR_SCHEDULER_SYNC_FAILED",
        "MONITOR_INTERNAL_ERROR",
    }
    assert {stage.value for stage in MonitorErrorStage} == {
        "scheduler",
        "branch_identity",
        "history",
        "snapshot",
        "manifest_parse",
        "csv_parse",
        "diff",
        "attribution",
        "report_publish",
    }


@pytest.mark.parametrize(
    ("path", "value"),
    (
        (("status",), "running-now"),
        (("schedule", "timezone"), "UTC"),
        (("scheduler", "sync_status"), "healthy"),
        (("latest_run", "interval", "boundary_kind"), "resume"),
    ),
)
def test_monitor_task_rejects_values_outside_frozen_domains(path, value):
    data = load_json(CONTRACTS / "m3.monitor-task.v1.example.json")
    target = data
    for key in path[:-1]:
        target = target[key]
    target[path[-1]] = value

    with pytest.raises(ValidationError):
        MonitorTaskPayload.model_validate(data)


@pytest.mark.parametrize(("remaining_field",), (("summary",), ("report_ref",)))
def test_unpublished_run_digest_rejects_half_published_result(remaining_field):
    task = load_json(CONTRACTS / "m3.monitor-task.v1.example.json")
    task["latest_run"]["status"] = "failed"
    removed_field = "report_ref" if remaining_field == "summary" else "summary"
    task["latest_run"][removed_field] = None

    with pytest.raises(ValidationError):
        MonitorTaskPayload.model_validate(task)


@pytest.mark.parametrize("status", ("paused", "ended", "archived"))
@pytest.mark.parametrize("contract", ("task", "task-list"))
def test_inactive_task_and_task_list_item_reject_enabled_scheduler(status, contract):
    if contract == "task":
        data = load_json(CONTRACTS / "m3.monitor-task.v1.example.json")
        item = data
        item["paused_at"] = "2026-08-10T10:03:00Z" if status == "paused" else None
        item["ended_at"] = "2026-08-10T10:03:00Z" if status == "ended" else None
        item["archived_at"] = "2026-08-10T10:03:00Z" if status == "archived" else None
        model = MonitorTaskPayload
    else:
        data = load_json(CONTRACTS / "m3.monitor-task-list.v1.example.json")
        item = data["items"][0]
        model = MonitorTaskListPayload
    item["status"] = status
    item["schedule"]["next_logical_cutoff_at"] = None

    with pytest.raises(ValidationError):
        model.model_validate(data)


@pytest.mark.parametrize(
    ("status", "desired_state"),
    (("paused", "disabled"), ("ended", "removed"), ("archived", "removed")),
)
def test_inactive_task_list_lifecycle_combinations_are_valid(status, desired_state):
    data = load_json(CONTRACTS / "m3.monitor-task-list.v1.example.json")
    item = data["items"][0]
    item["status"] = status
    item["schedule"]["next_logical_cutoff_at"] = None
    item["scheduler"]["desired_state"] = desired_state

    MonitorTaskListPayload.model_validate(data)


def test_monitor_interval_and_run_state_combinations_are_strict():
    run = load_json(CONTRACTS / "m3.monitor-run.v1.example.json")
    run["interval"]["start_inclusive"] = True
    with pytest.raises(ValidationError):
        MonitorRunPayload.model_validate(run)

    run = load_json(CONTRACTS / "m3.monitor-run.v1.example.json")
    run["interval"]["logical_cutoff_at"] = "2026-08-10T09:59:59Z"
    with pytest.raises(ValidationError):
        MonitorRunPayload.model_validate(run)

    run = load_json(CONTRACTS / "m3.monitor-run.v1.example.json")
    run["status"] = "failed"
    with pytest.raises(ValidationError):
        MonitorRunPayload.model_validate(run)


@pytest.mark.parametrize("status", ("queued", "running", "succeeded", "partial", "failed"))
def test_monitor_run_valid_state_shapes(status):
    MonitorRunPayload.model_validate(make_run(status))


@pytest.mark.parametrize("boundary_kind", ("pause", "end"))
def test_pause_and_end_runs_keep_left_open_right_closed_interval(boundary_kind):
    run = make_run("succeeded")
    run["interval"]["boundary_kind"] = boundary_kind

    payload = MonitorRunPayload.model_validate(run)

    assert payload.interval.start_inclusive is False
    assert payload.interval.end_inclusive is True
    assert payload.interval.logical_cutoff_at == payload.interval.end_at


def test_monitor_run_rejects_non_contiguous_attempt_numbers():
    run = make_run("partial")
    run["attempts"][0]["attempt"] = 2

    with pytest.raises(ValidationError):
        MonitorRunPayload.model_validate(run)


def test_monitor_run_rejects_final_status_that_disagrees_with_last_attempt():
    run = make_run("partial")
    run["status"] = "failed"
    run["start_revision"] = None
    run["end_revision"] = None
    run["summary"] = None
    run["report_ref"] = None
    run["report_sha256"] = None
    run["report_expires_at"] = None

    with pytest.raises(ValidationError):
        MonitorRunPayload.model_validate(run)


def test_monitor_run_rejects_public_errors_that_disagree_with_last_attempt():
    run = make_run("partial")
    run["errors"][0]["message"] = "与最终 attempt 不一致的错误"

    with pytest.raises(ValidationError):
        MonitorRunPayload.model_validate(run)


def test_monitor_run_rejects_summary_error_count_that_disagrees_with_errors():
    run = make_run("partial")
    run["summary"]["error_count"] = 0

    with pytest.raises(ValidationError):
        MonitorRunPayload.model_validate(run)


def test_unpublished_monitor_run_rejects_revision_metadata():
    run = make_run("failed")
    run["start_revision"] = 100
    run["end_revision"] = 106

    with pytest.raises(ValidationError):
        MonitorRunPayload.model_validate(run)


def test_monitor_times_require_offsets_and_normalize_to_utc():
    task = load_json(CONTRACTS / "m3.monitor-task.v1.example.json")
    task["schedule"]["effective_at"] = "2026-08-09T18:00:00+08:00"
    payload = MonitorTaskPayload.model_validate(task)
    serialized = json.loads(serialize_monitor_json(payload))
    assert serialized["schedule"]["effective_at"] == "2026-08-09T10:00:00Z"

    task["schedule"]["effective_at"] = "2026-08-09T18:00:00"
    with pytest.raises(ValidationError):
        MonitorTaskPayload.model_validate(task)


def test_monitor_report_change_shapes_and_summary_are_strict():
    report = load_json(CONTRACTS / "m3.monitor-report.v1.example.json")
    report["changes"][0]["change_type"] = "row_added"
    with pytest.raises(ValidationError):
        MonitorReportPayload.model_validate(report)


@pytest.mark.parametrize(
    "change_type",
    ("field_added", "field_removed", "field_definition_modified"),
)
def test_structural_changes_require_null_row_key(change_type):
    report = load_json(CONTRACTS / "m3.monitor-report.v1.example.json")
    change = next(
        item
        for item in report["changes"]
        if item["change_type"] == "field_definition_modified"
    )
    change["change_type"] = change_type
    if change_type == "field_added":
        change["source"] = None
    elif change_type == "field_removed":
        change["target"] = None
    if change_type != "field_definition_modified":
        report["summary"]["by_change_type"]["field_definition_modified"] = 0
        report["summary"]["by_change_type"][change_type] = 1
    MonitorReportPayload.model_validate(report)

    change["row_key"] = "__schema__"
    with pytest.raises(ValidationError):
        MonitorReportPayload.model_validate(report)

    change.pop("row_key")
    with pytest.raises(ValidationError):
        MonitorReportPayload.model_validate(report)


@pytest.mark.parametrize("change_type", ("field_modified", "row_added", "row_deleted"))
def test_row_and_value_changes_require_non_empty_row_key(change_type):
    report = load_json(CONTRACTS / "m3.monitor-report.v1.example.json")
    change = next(item for item in report["changes"] if item["change_type"] == change_type)
    change["row_key"] = None

    with pytest.raises(ValidationError):
        MonitorReportPayload.model_validate(report)


def test_structural_changes_do_not_increase_changed_row_count():
    report = MonitorReportPayload.model_validate(
        load_json(CONTRACTS / "m3.monitor-report.v1.example.json")
    )

    assert report.summary.changed_row_count == 3
    assert len(report.changes) == 4
    assert sum(change.row_key is None for change in report.changes) == 1

    report = load_json(CONTRACTS / "m3.monitor-report.v1.example.json")
    report["summary"]["by_change_type"]["field_modified"] = 2
    with pytest.raises(ValidationError):
        MonitorReportPayload.model_validate(report)

    report = load_json(CONTRACTS / "m3.monitor-report.v1.example.json")
    report["coverage"]["excluded_content"].pop()
    with pytest.raises(ValidationError):
        MonitorReportPayload.model_validate(report)


def test_monitor_report_rejects_invented_unresolved_attribution():
    report = load_json(CONTRACTS / "m3.monitor-report.v1.example.json")
    attribution = report["changes"][0]["attribution"]
    attribution.update(
        {
            "status": "unresolved",
            "author": "未知",
            "revision": 999,
            "changed_at": None,
            "commit_message": None,
        }
    )

    with pytest.raises(ValidationError):
        MonitorReportPayload.model_validate(report)


def test_empty_succeeded_monitor_report_is_valid():
    report = load_json(CONTRACTS / "m3.monitor-report.v1.example.json")
    report["status"] = "succeeded"
    report["summary"].update(
        {
            "changed_workbook_count": 0,
            "sheet_count": 0,
            "changed_row_count": 0,
            "changed_field_count": 0,
            "author_count": 0,
            "change_count": 0,
            "error_count": 0,
        }
    )
    for change_type in report["summary"]["by_change_type"]:
        report["summary"]["by_change_type"][change_type] = 0
    report["coverage"].update(
        {
            "unknown_author_count": 0,
            "unattributed_change_count": 0,
            "failed_workbook_count": 0,
        }
    )
    report["changes"] = []
    report["errors"] = []

    MonitorReportPayload.model_validate(report)


def test_unknown_author_does_not_force_partial_report():
    report = make_single_change_report()
    report["status"] = "succeeded"
    report["summary"]["author_count"] = 0
    report["coverage"]["unknown_author_count"] = 1
    report["changes"][0]["attribution"].update(
        {
            "status": "unknown_author",
            "author": "未知",
        }
    )

    MonitorReportPayload.model_validate(report)


def test_unresolved_attribution_is_a_valid_partial_report():
    report = make_single_change_report()
    report["status"] = "partial"
    report["summary"]["author_count"] = 0
    report["coverage"]["unattributed_change_count"] = 1
    report["changes"][0]["attribution"].update(
        {
            "status": "unresolved",
            "author": "未知",
            "revision": None,
            "changed_at": None,
            "commit_message": None,
        }
    )

    MonitorReportPayload.model_validate(report)


@pytest.mark.parametrize(
    ("section", "field", "value"),
    (
        ("summary", "workbook_count", 1),
        ("summary", "changed_workbook_count", 0),
        ("summary", "sheet_count", 0),
        ("summary", "changed_row_count", 2),
        ("summary", "changed_field_count", 0),
        ("summary", "author_count", 2),
        ("coverage", "unknown_author_count", 1),
        ("coverage", "unattributed_change_count", 1),
        ("coverage", "failed_workbook_count", 0),
    ),
)
def test_monitor_report_rejects_statistics_that_disagree_with_changes_and_errors(
    section, field, value
):
    report = load_json(CONTRACTS / "m3.monitor-report.v1.example.json")
    report[section][field] = value

    with pytest.raises(ValidationError):
        MonitorReportPayload.model_validate(report)


def test_deterministic_mock_svn_fixture_covers_phase_zero_cases():
    fixture = load_json(FIXTURE)
    commits = fixture["commits"]
    expected = fixture["expected"]

    mixed = next(commit for commit in commits if commit["revision"] == 101)
    assert fixture["target_branch"] + "/TableCsv/Role.csv" in mixed["changed_paths"]
    assert fixture["other_branch"] + "/TableCsv/Role.csv" in mixed["changed_paths"]
    assert expected["ignored_revision_paths"] == [
        fixture["other_branch"] + "/TableCsv/Role.csv"
    ]
    numbering = fixture["revision_numbering"]
    assert numbering["scope"] == "repository_global"
    assert numbering["target_branch_log_revisions"] == expected["included_revisions"]
    assert set(numbering["target_branch_log_revisions"]).isdisjoint(
        numbering["other_branch_only_revisions"]
    )
    assert any(
        right - left > 1
        for left, right in zip(
            numbering["target_branch_log_revisions"],
            numbering["target_branch_log_revisions"][1:],
        )
    )

    row_100_events = [
        (commit["revision"], commit["author"], event["value"])
        for commit in commits
        for event in commit["target_events"]
        if event.get("event") == "field_set" and event.get("row_key") == "100"
    ]
    assert row_100_events == [(101, "alice", "120"), (103, "bob", "110")]
    assert expected["net_changes"][0] == {
        "change_type": "field_modified",
        "row_key": "100",
        "field_name": "Hp",
        "source": "100",
        "target": "110",
        "final_revision": 103,
        "final_author": "bob",
    }

    assert expected["reverted_to_start"] == [
        {
            "row_key": "400",
            "field_name": "Hp",
            "start": "100",
            "intermediate": "120",
            "end": "100",
        }
    ]
    assert {change["change_type"] for change in expected["net_changes"]} == {
        "field_modified",
        "field_definition_modified",
        "row_added",
        "row_deleted",
    }
    assert expected["public_errors"] == [
        {
            "code": "MONITOR_PARSE_FAILED",
            "workbook": "BrokenConfig.xlsm",
            "sheet_name": "Broken",
        }
    ]


def test_mock_svn_expected_net_result_matches_public_report_example():
    fixture = load_json(FIXTURE)
    report_data = load_json(CONTRACTS / "m3.monitor-report.v1.example.json")
    report = MonitorReportPayload.model_validate(deepcopy(report_data))

    report_changes = [
        {
            "change_type": change.change_type.value,
            "row_key": change.row_key,
            "field_name": change.field_name,
            "final_revision": change.attribution.revision,
            "final_author": change.attribution.author,
        }
        for change in report.changes
    ]
    fixture_changes = [
        {
            "change_type": change["change_type"],
            "row_key": change["row_key"],
            "field_name": change.get("field_name"),
            "final_revision": change["final_revision"],
            "final_author": change["final_author"],
        }
        for change in fixture["expected"]["net_changes"]
    ]

    assert report_changes == fixture_changes
    assert "400" not in {change.row_key for change in report.changes}
    assert [error.code.value for error in report.errors] == [
        error["code"] for error in fixture["expected"]["public_errors"]
    ]
