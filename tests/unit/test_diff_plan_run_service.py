import json
import logging
from pathlib import Path
import time
from uuid import uuid4

from app.schemas.diff_plan import (
    DiffPlanCreateRequestPayload,
    DiffPlanRunRetryRequestPayload,
    DiffPlanRunStartRequestPayload,
)
from app.services.diff_plan_run_service import DiffPlanRunService
from app.services.diff_plan_run_store import DiffPlanRunStore
from app.services.diff_plan_store import DiffPlanStore
from app.services.snapshot_service import SnapshotService
from core.models import SvnInfo, TreeEntry
from core.svn_provider import MockSVNProvider


def diff_bytes(path: str, status: str) -> bytes:
    error_count = 1 if status == "failed" else 0
    return json.dumps({
        "schema_version": "m2.diff.v1",
        "direction": {"source": "left", "target": "right"},
        "workbook": {
            "name": Path(path).name,
            "status": status,
            "source_sha256": "a" * 64,
            "target_sha256": "b" * 64,
        },
        "summary": {
            "total_sheets": 0, "unchanged_sheets": 0, "modified_sheets": 0,
            "source_only_sheets": 0, "target_only_sheets": 0, "failed_sheets": error_count,
            "source_only_rows": 0, "target_only_rows": 0, "modified_rows": 0,
            "modified_fields": 0, "error_count": error_count,
        },
        "sheets": [],
        "errors": [] if not error_count else [{
            "code": "TEST_FAILURE", "message": "测试业务失败", "stage": "diff",
            "sheet_name": None, "details": {},
        }],
    }, ensure_ascii=False).encode("utf-8")


class RunProvider(MockSVNProvider):
    def __init__(self):
        super().__init__()
        self.info_calls = []
        self.content = {
            ("source", "Source/Table/Identical.xlsx"): b"same",
            ("target", "Source/Table/Identical.xlsx"): b"same",
            ("source", "Source/Table/Semantic.xlsx"): b"left-semantic",
            ("target", "Source/Table/Semantic.xlsx"): b"right-semantic",
            ("source", "Source/Table/Changed.xlsx"): b"left-changed",
            ("target", "Source/Table/Changed.xlsx"): b"right-changed",
            ("source", "Source/Table/TargetMissing.xlsx"): b"source-only",
        }

    @staticmethod
    def endpoint_id(endpoint):
        return endpoint.url.rsplit("/", 1)[-1]

    def info(self, endpoint):
        endpoint_id = self.endpoint_id(endpoint)
        self.info_calls.append(endpoint_id)
        revision = "100" if endpoint_id == "source" else "200"
        return SvnInfo(
            url=endpoint.url, repository_root="mock://repo", repository_uuid="m4-run",
            revision=revision, last_changed_revision=revision, last_changed_author="tester",
            last_changed_date="2026-08-12T00:00:00Z",
        )

    def list_tree(self, endpoint, prefix=""):
        endpoint_id = self.endpoint_id(endpoint)
        entries = [TreeEntry(path="Source/Table", kind="dir")]
        for (side, path), content in self.content.items():
            if side == endpoint_id:
                entries.append(TreeEntry(path=path, kind="file", size=len(content), revision=str(endpoint.revision)))
        return entries

    def read_bytes(self, endpoint, path):
        return self.content[(self.endpoint_id(endpoint), path)]


class RunWorkbookRunner:
    def __init__(self):
        self.calls = []

    def run(self, source, target, workbook_path):
        self.calls.append((source.revision, target.revision, workbook_path))
        if workbook_path == "Semantic.xlsx":
            return diff_bytes(workbook_path, "unchanged")
        return diff_bytes(workbook_path, "modified")


def wait_terminal(service, run_id, timeout=5):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        run = service.get_run(run_id)
        if run.status in {"completed", "completed_with_failures", "cancelled", "failed"}:
            return run
        time.sleep(0.03)
    raise AssertionError("M4 运行未在测试超时内结束")


def build_service(tmp_path):
    provider = RunProvider()
    records = [
        {"id": "source", "region": "TC", "track": "DEV", "label": "基准", "url": "mock://source", "logical_scopes": ["TABLE"], "enabled": True, "physical_path_filters": {"TABLE": "Source/Table"}},
        {"id": "target", "region": "TC", "track": "FIX", "label": "目标", "url": "mock://target", "logical_scopes": ["TABLE"], "enabled": True, "physical_path_filters": {"TABLE": "Source/Table"}},
    ]
    database = tmp_path / "m4.sqlite3"
    plans = DiffPlanStore(database)
    plan, _ = plans.create(DiffPlanCreateRequestPayload(
        schema_version="m4.diff-plan-create.request.v1", request_id=uuid4(), name="运行计划",
        source_endpoint_id="source", target_endpoint_ids=["target"],
        workbook_paths=["Identical.xlsx", "Semantic.xlsx", "Changed.xlsx", "TargetMissing.xlsx"],
    ))
    runner = RunWorkbookRunner()
    service = DiffPlanRunService(
        plan_store=plans,
        run_store=DiffPlanRunStore(database, tmp_path / "results"),
        snapshot_service=SnapshotService(provider, allowed_schemes=("mock",)),
        provider=provider,
        endpoint_registry=lambda: records,
        workbook_runner=runner,
        poll_interval_seconds=0.02,
    )
    return provider, runner, service, plan


def test_run_service_freezes_head_once_and_only_executes_modified_items(tmp_path):
    provider, runner, service, plan = build_service(tmp_path)
    try:
        request = DiffPlanRunStartRequestPayload(
            schema_version="m4.diff-plan-run-start.request.v1", request_id=uuid4(), revisions={},
        )
        run, created = service.start_run(plan.plan_id, request)
        assert created is True
        replay, replay_created = service.start_run(plan.plan_id, request)
        assert replay_created is False and replay.run_id == run.run_id
        assert provider.info_calls.count("source") == 1
        assert provider.info_calls.count("target") == 1
        finished = wait_terminal(service, run.run_id)
        assert finished.source_revision == 100
        assert finished.target_revisions == {"target": 200}
        assert {item.workbook_path: item.status for item in finished.items} == {
            "Identical.xlsx": "identical",
            "Semantic.xlsx": "semantic_equal",
            "Changed.xlsx": "changed",
            "TargetMissing.xlsx": "target_missing",
        }
        assert [call[2] for call in runner.calls] == ["Semantic.xlsx", "Changed.xlsx"]
        changed = next(item for item in finished.items if item.workbook_path == "Changed.xlsx")
        payload, _ = service.load_result(changed.result_ref)
        assert json.loads(payload)["schema_version"] == "m2.diff.v1"
    finally:
        service.close()


def test_retry_uses_parent_frozen_revisions_without_refreezing_head(tmp_path):
    provider, runner, service, plan = build_service(tmp_path)
    original = runner.run
    failed_once = {"value": False}

    def flaky(source, target, workbook_path):
        if workbook_path == "Changed.xlsx" and not failed_once["value"]:
            failed_once["value"] = True
            raise RuntimeError("temporary")
        return original(source, target, workbook_path)

    runner.run = flaky
    try:
        parent, _ = service.start_run(plan.plan_id, DiffPlanRunStartRequestPayload(
            schema_version="m4.diff-plan-run-start.request.v1", request_id=uuid4(), revisions={},
        ))
        parent = wait_terminal(service, parent.run_id)
        failed = next(item for item in parent.items if item.status == "orchestration_failed")
        calls_before = list(provider.info_calls)
        child, created = service.retry(parent.run_id, DiffPlanRunRetryRequestPayload(
            schema_version="m4.diff-plan-run-retry.request.v1", request_id=uuid4(), item_ids=[failed.item_id],
        ))
        assert created is True
        child = wait_terminal(service, child.run_id)
        assert child.retry_of_run_id == parent.run_id
        assert child.source_revision == parent.source_revision
        assert child.target_revisions == parent.target_revisions
        assert provider.info_calls == calls_before
        assert child.items[0].retry_of_item_id == failed.item_id
        assert child.items[0].status == "changed"
    finally:
        service.close()


def test_cleanup_failure_is_redacted_and_does_not_block_scheduler(tmp_path, caplog, monkeypatch):
    _, _, service, _ = build_service(tmp_path)
    monkeypatch.setattr(
        service.run_store,
        "cleanup_expired_results",
        lambda: (_ for _ in ()).throw(RuntimeError("password=hunter2 C:\\private\\result.json")),
    )
    with caplog.at_level(logging.ERROR, logger="app.services.diff_plan_run_service"):
        service._cleanup_expired_results()
    serialized = " ".join(record.getMessage() for record in caplog.records)
    assert "清理失败" in serialized
    assert "hunter2" not in serialized
    assert "C:\\private" not in serialized
    service.close()
