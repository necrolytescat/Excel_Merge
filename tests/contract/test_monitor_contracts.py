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

    row_100_events = [
        (commit["revision"], commit["author"], event["value"])
        for commit in commits
        for event in commit["target_events"]
        if event.get("event") == "field_set" and event.get("row_key") == "100"
    ]
    assert row_100_events == [(101, "alice", "120"), (102, "bob", "110")]
    assert expected["net_changes"][0] == {
        "change_type": "field_modified",
        "row_key": "100",
        "field_name": "Hp",
        "source": "100",
        "target": "110",
        "final_revision": 102,
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
