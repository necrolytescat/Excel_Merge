from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, time, timedelta, timezone
import hashlib
import json
from pathlib import Path
import sys
from threading import Barrier, Event
import time as wall_time
from uuid import UUID, uuid4, uuid5

from fastapi.testclient import TestClient

from app.main import create_app
from app.schemas.monitor import (
    MonitorPublicErrorPayload,
    MonitorReportPayload,
    MonitorTaskCreateRequestPayload,
    serialize_monitor_json,
)
from app.services.branch_history_service import BranchHistoryService
from app.services.monitor_report_artifacts import FileSystemMonitorReportPublisher
from app.services.monitor_report_service import (
    REPORT_RETENTION,
    ReportDraft,
    render_monitor_report_html,
    report_reference,
)
from app.services.monitor_store import (
    MonitorIdempotencyConflict,
    MonitorStateConflict,
    MonitorStore,
)
from app.services.monitor_task_service import MonitorTaskService
from app.services.monitor_web_service import (
    COMMAND_NAMESPACE,
    MonitorWebError,
    MonitorWebService,
)
from app.services.windows_scheduler import (
    FakeSchedulerGateway,
    MonitorSchedulerService,
    ScheduledMonitorTaskService,
)
from core.svn_history import BranchCopyBoundary, BranchIdentity
from core.svn_provider import MockSVNProvider


NOW = datetime(2026, 8, 10, 10, 0, tzinfo=timezone.utc)
LAYOUT = {
    "workbook_source": {"directory_name": "Table"},
    "csv_export": {
        "directory_name": "TableCsv",
        "extension": ".csv",
        "filename_template": "{tbxName}.csv",
        "field_name_row": 2,
        "field_type_row": 3,
        "field_scope_row": 4,
        "data_start_row": 5,
        "primary_key_fields": ["Id", "id"],
    },
    "manifest": {
        "sheet_name": "main",
        "sheet_field": "sheetName",
        "csv_name_field": "tbxName",
        "export_flag_field": "isExport",
    },
}
ENDPOINTS = [
    {
        "id": "KR_FIX_1_0",
        "label": "KR FIX 1.0",
        "url": "file:///repo/branches/KR-Fix-1.0",
        "enabled": True,
    },
    {
        "id": "DISABLED",
        "label": "Disabled",
        "url": "file:///repo/branches/disabled",
        "enabled": False,
    },
]


class History:
    identity = BranchIdentity(
        canonical_url="file:///repo/branches/KR-Fix-1.0",
        repository_root="file:///repo",
        repository_uuid="20000000-0000-4000-8000-000000000001",
        repository_relative_path="branches/KR-Fix-1.0",
        bound_revision=120,
    )

    def resolve_branch_identity(self, endpoint):
        return self.identity

    def resolve_copy_boundary(self, identity):
        return BranchCopyBoundary(revision=10)

    def resolve_revision_at(self, identity, instant):
        return 100


class Runner:
    def __init__(self):
        self.calls = []

    def run_run(self, run_id, *, trigger):
        self.calls.append((run_id, trigger))


def build_service(
    tmp_path: Path,
    *,
    clock=None,
    runner=None,
) -> MonitorWebService:
    current_time = clock or (lambda: NOW)
    store = MonitorStore(tmp_path / "monitor.sqlite3")
    tasks = MonitorTaskService(store, clock=current_time)
    scheduler = MonitorSchedulerService(
        store,
        FakeSchedulerGateway(),
        database_path=tmp_path / "monitor.sqlite3",
        working_directory=tmp_path,
        python_executable=sys.executable,
        run_as="S-1-5-21-100",
        clock=current_time,
    )
    return MonitorWebService(
        store=store,
        tasks=tasks,
        scheduled_tasks=ScheduledMonitorTaskService(tasks, scheduler),
        scheduler=scheduler,
        history=BranchHistoryService(History()),
        endpoint_registry=lambda: ENDPOINTS,
        dataset_layout=LAYOUT,
        runner=runner or Runner(),
        publisher=FileSystemMonitorReportPublisher(tmp_path / "reports"),
        clock=current_time,
    )


def fail_and_end_task(service: MonitorWebService):
    task, _ = service.create_task(
        MonitorTaskCreateRequestPayload.model_validate(create_payload())
    )
    task_id = str(task.task_id)
    service.scheduled_tasks.pause(task_id)
    run = service.store.list_runs(task_id)[-1]
    claim = service.store.claim_run(
        run.run_id,
        now=NOW,
        lease_for=timedelta(minutes=5),
        trigger="manual_retry",
    )
    service.store.finish_run(
        run.run_id,
        claim.lease_token,
        now=NOW + timedelta(seconds=1),
        status="failed",
        errors=[
            MonitorPublicErrorPayload(
                code="MONITOR_PARSE_FAILED",
                stage="csv_parse",
                message="工作簿解析失败",
                retryable=False,
            )
        ],
    )
    service.scheduled_tasks.end(task_id)
    return task_id, service.store.get_run(run.run_id)


def wait_until(predicate, timeout=2.0):
    deadline = wall_time.monotonic() + timeout
    while wall_time.monotonic() < deadline:
        if predicate():
            return True
        wall_time.sleep(0.01)
    return predicate()


def create_payload(request_id=None):
    return {
        "schema_version": "m3.monitor-task-create.request.v1",
        "request_id": str(request_id or uuid4()),
        "name": "QA 每日报告",
        "endpoint_id": "KR_FIX_1_0",
        "effective_at": "2026-08-10T09:00:00Z",
        "end_at": None,
        "daily_trigger_time": "23:00:00",
    }


def command_payload(request_id=None):
    return {
        "schema_version": "m3.monitor-command.request.v1",
        "request_id": str(request_id or uuid4()),
    }


def publish_report(service: MonitorWebService, task_id: str):
    task = service.get_task(task_id)
    run = service.store.list_runs(task_id)[-1]
    claim = service.store.claim_run(
        run.run_id,
        now=NOW,
        lease_for=timedelta(minutes=5),
        trigger="manual_retry",
    )
    data = json.loads(
        (
            Path(__file__).parents[2]
            / "docs"
            / "contracts"
            / "m3.monitor-report.v1.example.json"
        ).read_text(encoding="utf-8")
    )
    data["report_id"] = str(uuid5(UUID(run.run_id), "m3.monitor-report.v1"))
    data["run_id"] = run.run_id
    data["task_id"] = task_id
    data["task_name"] = task.name
    data["branch"] = task.branch.model_dump(mode="json")
    data["interval"] = {
        "start_at": run.start_at.isoformat(),
        "end_at": run.end_at.isoformat(),
        "start_inclusive": False,
        "end_inclusive": True,
        "logical_cutoff_at": run.end_at.isoformat(),
        "boundary_kind": run.boundary_type.value,
    }
    data["generated_at"] = (NOW + timedelta(minutes=1)).isoformat()
    report = MonitorReportPayload.model_validate(data)
    canonical = serialize_monitor_json(report)
    html = render_monitor_report_html(report)
    draft = ReportDraft(
        payload=report,
        canonical_json=canonical,
        offline_html=html,
        report_ref=report_reference(report.report_id),
        json_sha256=hashlib.sha256(canonical).hexdigest(),
        html_sha256=hashlib.sha256(html).hexdigest(),
        report_expires_at=report.generated_at + REPORT_RETENTION,
    )
    service.store.prepare_publication(
        run.run_id,
        claim.lease_token,
        now=NOW + timedelta(seconds=1),
        status=report.status,
        start_revision=report.revisions.start_revision,
        end_revision=report.revisions.end_revision,
        summary={
            "workbook_count": report.summary.workbook_count,
            "changed_workbook_count": report.summary.changed_workbook_count,
            "change_count": report.summary.change_count,
            "error_count": report.summary.error_count,
        },
        errors=list(report.errors),
        report_ref=draft.report_ref,
        json_sha256=draft.json_sha256,
        html_sha256=draft.html_sha256,
        report_expires_at=draft.report_expires_at,
    )
    service.publisher.publish_history(draft)
    service.publisher.activate_latest(draft)
    service.store.finalize_publication(
        run.run_id,
        claim.lease_token,
        now=NOW + timedelta(seconds=2),
    )
    return run, draft


def test_monitor_create_list_etag_options_and_idempotency(tmp_path):
    service = build_service(tmp_path)
    app = create_app(
        config={"svn": {"provider": "mock"}},
        provider=MockSVNProvider(),
        monitor_web_service=service,
    )
    with TestClient(app) as client:
        options = client.get("/api/monitor/endpoint-options")
        assert options.status_code == 200
        assert options.json()["items"] == [
            {"endpoint_id": "KR_FIX_1_0", "label": "KR FIX 1.0"}
        ]
        assert "url" not in options.text

        request_id = uuid4()
        payload = create_payload(request_id)
        created = client.post("/api/monitor/tasks", json=payload)
        assert created.status_code == 201
        body = created.json()
        assert body["status"] == "active"
        assert body["pending_run_count"] == 0
        assert "canonical_url" not in created.text
        assert "windows_task" not in created.text

        paused = client.post(
            f"/api/monitor/tasks/{body['task_id']}/pause",
            json=command_payload(),
        )
        assert paused.status_code == 200
        replay = client.post("/api/monitor/tasks", json=payload)
        assert replay.status_code == 201
        assert replay.json() == body

        conflict = client.post(
            "/api/monitor/tasks",
            json={**payload, "name": "不同请求"},
        )
        assert conflict.status_code == 409
        assert conflict.json()["error"]["code"] == "MONITOR_IDEMPOTENCY_CONFLICT"

        listed = client.get("/api/monitor/tasks")
        assert listed.status_code == 200
        assert listed.json()["items"][0]["task_id"] == body["task_id"]
        weak = client.get(
            "/api/monitor/tasks",
            headers={"If-None-Match": "W/" + listed.headers["etag"]},
        )
        assert weak.status_code == 304
        assert not weak.content


def test_monitor_patch_archive_and_retry_outbox_are_state_safe(tmp_path):
    service = build_service(tmp_path)
    app = create_app(
        config={"svn": {"provider": "mock"}},
        provider=MockSVNProvider(),
        monitor_web_service=service,
    )
    with TestClient(app) as client:
        task = client.post("/api/monitor/tasks", json=create_payload()).json()
        task_id = task["task_id"]
        patched = client.patch(
            f"/api/monitor/tasks/{task_id}",
            json={
                "schema_version": "m3.monitor-task-patch.request.v1",
                "request_id": str(uuid4()),
                "daily_trigger_time": "22:00:00",
                "end_at": None,
            },
        )
        assert patched.status_code == 200
        assert patched.json()["schedule"]["daily_trigger_time"] == "22:00:00"

        paused = client.post(
            f"/api/monitor/tasks/{task_id}/pause", json=command_payload()
        )
        assert paused.status_code == 200
        run = service.store.list_runs(task_id)[-1]
        claim = service.store.claim_run(
            run.run_id,
            now=NOW,
            lease_for=timedelta(minutes=5),
            trigger="manual_retry",
        )
        service.store.finish_run(
            run.run_id,
            claim.lease_token,
            now=NOW + timedelta(seconds=1),
            status="failed",
            errors=[
                MonitorPublicErrorPayload(
                    code="MONITOR_PARSE_FAILED",
                    stage="csv_parse",
                    message="工作簿解析失败",
                    retryable=False,
                )
            ],
        )

        ended = client.post(
            f"/api/monitor/tasks/{task_id}/end", json=command_payload()
        )
        assert ended.status_code == 200
        archived = client.post(
            f"/api/monitor/tasks/{task_id}/archive", json=command_payload()
        )
        assert archived.status_code == 200
        assert archived.json()["status"] == "archived"

        retry = client.post(
            f"/api/monitor/runs/{run.run_id}/retry",
            json={
                "schema_version": "m3.monitor-run-retry.request.v1",
                "request_id": str(uuid4()),
            },
        )
        assert retry.status_code == 409
        assert retry.json()["error"]["code"] == "MONITOR_STATE_CONFLICT"

        sync = client.post(
            f"/api/monitor/tasks/{task_id}/scheduler-sync",
            json=command_payload(),
        )
        assert sync.status_code == 200
        assert sync.json()["status"] == "archived"


def test_monitor_retry_acceptance_is_durable_and_replayed(tmp_path):
    service = build_service(tmp_path)
    app = create_app(
        config={"svn": {"provider": "mock"}},
        provider=MockSVNProvider(),
        monitor_web_service=service,
    )
    with TestClient(app) as client:
        task = client.post("/api/monitor/tasks", json=create_payload()).json()
        task_id = task["task_id"]
        client.post(f"/api/monitor/tasks/{task_id}/pause", json=command_payload())
        run = service.store.list_runs(task_id)[-1]
        claim = service.store.claim_run(
            run.run_id,
            now=NOW,
            lease_for=timedelta(minutes=5),
            trigger="manual_retry",
        )
        service.store.finish_run(
            run.run_id,
            claim.lease_token,
            now=NOW + timedelta(seconds=1),
            status="failed",
            errors=[
                MonitorPublicErrorPayload(
                    code="MONITOR_PARSE_FAILED",
                    stage="csv_parse",
                    message="工作簿解析失败",
                    retryable=False,
                )
            ],
        )
        request_id = uuid4()
        payload = {
            "schema_version": "m3.monitor-run-retry.request.v1",
            "request_id": str(request_id),
        }
        accepted = client.post(
            f"/api/monitor/runs/{run.run_id}/retry", json=payload
        )
        assert accepted.status_code == 202
        assert accepted.json()["dispatch_state"] == "pending"
        replay = client.post(
            f"/api/monitor/runs/{run.run_id}/retry", json=payload
        )
        assert replay.status_code == 202
        assert replay.json() == accepted.json()
        with service.store._connect() as connection:
            assert connection.execute(
                "SELECT COUNT(*) FROM monitor_commands WHERE request_id=?",
                (str(request_id),),
            ).fetchone()[0] == 1
            assert connection.execute(
                "SELECT COUNT(*) FROM monitor_retry_outbox WHERE request_id=?",
                (str(request_id),),
            ).fetchone()[0] == 1


def test_retry_dispatcher_reclaims_failed_dispatch_only_after_lease(tmp_path):
    class FlakyRunner:
        def __init__(self):
            self.calls = []
            self.first = Event()
            self.second = Event()

        def run_run(self, run_id, *, trigger):
            self.calls.append((run_id, trigger))
            if len(self.calls) == 1:
                self.first.set()
                raise RuntimeError("transient dispatch failure")
            self.second.set()

    current = [NOW]
    runner = FlakyRunner()
    service = build_service(
        tmp_path,
        clock=lambda: current[0],
        runner=runner,
    )
    _, run = fail_and_end_task(service)
    service.start_retry_dispatcher()
    service.accept_retry(UUID(run.run_id), uuid4())
    assert runner.first.wait(2)

    def outbox():
        with service.store._connect() as connection:
            return connection.execute(
                """SELECT state,dispatch_count FROM monitor_retry_outbox
                   WHERE run_id=?""",
                (run.run_id,),
            ).fetchone()

    assert wait_until(lambda: outbox()["state"] == "dispatching")
    service.wake_retry_dispatcher()
    wall_time.sleep(0.05)
    assert len(runner.calls) == 1
    assert outbox()["dispatch_count"] == 1

    current[0] += timedelta(minutes=5)
    service.wake_retry_dispatcher()
    assert runner.second.wait(2)
    assert wait_until(lambda: outbox()["state"] == "dispatched")
    assert len(runner.calls) == 2
    assert outbox()["dispatch_count"] == 2
    service.close()
    assert not service._dispatcher_thread.is_alive()


def test_retry_dispatcher_recovers_pending_intent_after_restart(tmp_path):
    first = build_service(tmp_path)
    _, run = fail_and_end_task(first)
    first.accept_retry(UUID(run.run_id), uuid4())
    with first.store._connect() as connection:
        assert connection.execute(
            "SELECT state FROM monitor_retry_outbox WHERE run_id=?",
            (run.run_id,),
        ).fetchone()[0] == "pending"
    first.close()

    runner = Runner()
    restarted = build_service(tmp_path, runner=runner)
    restarted.start_retry_dispatcher()
    assert wait_until(lambda: runner.calls == [(run.run_id, "manual_retry")])
    with restarted.store._connect() as connection:
        assert wait_until(
            lambda: connection.execute(
                "SELECT state FROM monitor_retry_outbox WHERE run_id=?",
                (run.run_id,),
            ).fetchone()[0]
            == "dispatched"
        )
    restarted.close()
    assert not restarted._dispatcher_thread.is_alive()


def test_retry_and_archive_are_atomic_and_only_one_can_win(tmp_path):
    service = build_service(tmp_path)
    task_id, run = fail_and_end_task(service)
    barrier = Barrier(2)

    def retry():
        barrier.wait()
        try:
            service.accept_retry(UUID(run.run_id), uuid4())
            return "retry"
        except Exception as error:
            return error

    def archive():
        barrier.wait()
        try:
            service.tasks.archive(task_id)
            return "archive"
        except Exception as error:
            return error

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = [executor.submit(retry), executor.submit(archive)]
        results = [future.result() for future in results]
    winners = [result for result in results if isinstance(result, str)]
    assert winners in (["retry"], ["archive"])
    loser = next(result for result in results if not isinstance(result, str))
    assert isinstance(loser, (MonitorWebError, MonitorStateConflict))
    service.close()


def test_retry_denial_is_persisted_and_replayed_after_winner_dispatches(tmp_path):
    service = build_service(tmp_path)
    _, run = fail_and_end_task(service)
    winner_id = uuid4()
    loser_id = uuid4()
    service.accept_retry(UUID(run.run_id), winner_id)
    try:
        service.accept_retry(UUID(run.run_id), loser_id)
        raise AssertionError("active retry must deny the second request")
    except MonitorWebError as error:
        assert (error.code, error.status_code) == ("MONITOR_STATE_CONFLICT", 409)

    with service.store._connect() as connection:
        denied = connection.execute(
            "SELECT * FROM monitor_commands WHERE request_id=?",
            (str(loser_id),),
        ).fetchone()
        assert (denied["state"], denied["response_status"]) == ("completed", 409)
        assert json.loads(denied["response_json"]) == {
            "error": {
                "code": "MONITOR_STATE_CONFLICT",
                "message": "当前运行状态不允许人工重试",
            }
        }
        assert denied["payload_hash"] == hashlib.sha256(b"{}").hexdigest()
        assert connection.execute(
            "SELECT COUNT(*) FROM monitor_retry_outbox WHERE request_id=?",
            (str(loser_id),),
        ).fetchone()[0] == 0

    intent = service.store.claim_retry_intents(
        now=NOW,
        lease_for=timedelta(minutes=5),
    )[0]
    assert service.store.finish_retry_intent(
        intent.request_id,
        intent.lease_token,
        now=NOW,
    )
    try:
        service.accept_retry(UUID(run.run_id), loser_id)
        raise AssertionError("completed denial must replay")
    except MonitorWebError as error:
        assert (error.code, error.status_code) == ("MONITOR_STATE_CONFLICT", 409)
    with service.store._connect() as connection:
        assert connection.execute(
            "SELECT COUNT(*) FROM monitor_retry_outbox WHERE request_id=?",
            (str(loser_id),),
        ).fetchone()[0] == 0

    try:
        service.store.accept_retry_intent(
            request_id=str(loser_id),
            run_id=run.run_id,
            method="POST",
            target=f"POST /api/monitor/runs/{run.run_id}/retry",
            payload_hash="different-payload",
            payload_json='{"different":true}',
            response_status=202,
            response_json="{}",
            conflict_response_json="{}",
            now=NOW,
        )
        raise AssertionError("same request_id with another payload must conflict")
    except MonitorIdempotencyConflict:
        pass
    service.close()


def test_retry_unique_index_fallback_persists_denial_without_loser_outbox(tmp_path):
    service = build_service(tmp_path)
    _, run = fail_and_end_task(service)
    competitor_id = uuid4()
    loser_id = uuid4()
    timestamp = NOW.isoformat().replace("+00:00", "Z")
    target = f"POST /api/monitor/runs/{run.run_id}/retry"
    with service.store._transaction(write=True) as connection:
        connection.execute(
            """INSERT INTO monitor_commands
               (request_id,method,target,payload_hash,payload_json,state,
                response_status,response_json,created_at,updated_at)
               VALUES (?,?,?,?,?,'completed',202,'{}',?,?)""",
            (
                str(competitor_id),
                "POST",
                target,
                "competitor",
                "{}",
                timestamp,
                timestamp,
            ),
        )
        connection.execute(
            f"""CREATE TRIGGER inject_competing_retry
                BEFORE INSERT ON monitor_retry_outbox
                WHEN NEW.request_id='{loser_id}'
                BEGIN
                    INSERT INTO monitor_retry_outbox
                    (request_id,task_id,run_id,state,created_at,updated_at)
                    VALUES
                    ('{competitor_id}',NEW.task_id,NEW.run_id,'pending',
                     NEW.created_at,NEW.updated_at);
                END"""
        )

    try:
        service.accept_retry(UUID(run.run_id), loser_id)
        raise AssertionError("partial unique conflict must deny the loser")
    except MonitorWebError as error:
        assert (error.code, error.status_code) == ("MONITOR_STATE_CONFLICT", 409)
    with service.store._connect() as connection:
        denied = connection.execute(
            "SELECT state,response_status FROM monitor_commands WHERE request_id=?",
            (str(loser_id),),
        ).fetchone()
        assert tuple(denied) == ("completed", 409)
        assert connection.execute(
            "SELECT COUNT(*) FROM monitor_retry_outbox WHERE request_id=?",
            (str(loser_id),),
        ).fetchone()[0] == 0
        assert connection.execute(
            "SELECT COUNT(*) FROM monitor_retry_outbox WHERE request_id=?",
            (str(competitor_id),),
        ).fetchone()[0] == 0
    service.close()


def test_one_run_allows_only_one_active_retry_and_blocks_archive(tmp_path):
    service = build_service(tmp_path)
    task_id, run = fail_and_end_task(service)
    barrier = Barrier(2)

    def retry():
        request_id = uuid4()
        barrier.wait()
        try:
            return request_id, service.accept_retry(UUID(run.run_id), request_id)
        except MonitorWebError as error:
            return request_id, error

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = [executor.submit(retry), executor.submit(retry)]
        results = [future.result() for future in results]
    accepted = [result for result in results if isinstance(result[1], tuple)]
    rejected = [
        result for result in results if isinstance(result[1], MonitorWebError)
    ]
    assert len(accepted) == 1
    assert accepted[0][1][1] == 202
    assert accepted[0][1][0].run_id == UUID(run.run_id)
    assert len(rejected) == 1
    loser_id, denied = rejected[0]
    assert denied.status_code == 409
    with service.store._connect() as connection:
        assert connection.execute(
            """SELECT COUNT(*) FROM monitor_retry_outbox
               WHERE run_id=? AND state IN ('pending','dispatching')""",
            (run.run_id,),
        ).fetchone()[0] == 1
        loser = connection.execute(
            "SELECT state,response_status FROM monitor_commands WHERE request_id=?",
            (str(loser_id),),
        ).fetchone()
        assert tuple(loser) == ("completed", 409)
        assert connection.execute(
            "SELECT COUNT(*) FROM monitor_retry_outbox WHERE request_id=?",
            (str(loser_id),),
        ).fetchone()[0] == 0
    try:
        service.tasks.archive(task_id)
        raise AssertionError("active retry must block archive")
    except MonitorStateConflict:
        pass
    intent = service.store.claim_retry_intents(
        now=NOW,
        lease_for=timedelta(minutes=5),
    )[0]
    service.store.finish_retry_intent(
        intent.request_id,
        intent.lease_token,
        now=NOW,
    )
    try:
        service.accept_retry(UUID(run.run_id), loser_id)
        raise AssertionError("concurrent loser must replay its first denial")
    except MonitorWebError as error:
        assert (error.code, error.status_code) == ("MONITOR_STATE_CONFLICT", 409)
    with service.store._connect() as connection:
        assert connection.execute(
            "SELECT COUNT(*) FROM monitor_retry_outbox WHERE request_id=?",
            (str(loser_id),),
        ).fetchone()[0] == 0
    service.close()


def test_archived_task_manual_claim_does_not_create_attempt(tmp_path):
    service = build_service(tmp_path)
    task_id, run = fail_and_end_task(service)
    archived = service.tasks.archive(task_id)
    assert archived.status == "archived"
    before = service.store.get_run(run.run_id).attempt_count
    assert service.store.claim_run(
        run.run_id,
        now=NOW + timedelta(minutes=1),
        lease_for=timedelta(minutes=5),
        trigger="manual_retry",
    ) is None
    assert service.store.get_run(run.run_id).attempt_count == before
    service.close()


def test_monitor_api_unavailable_and_validation_errors_are_sanitized(tmp_path):
    app = create_app(
        config={"svn": {"provider": "mock"}},
        provider=MockSVNProvider(),
    )
    with TestClient(app) as client:
        unavailable = client.get("/api/monitor/tasks")
        assert unavailable.status_code == 503
        assert unavailable.json() == {
            "error": {
                "code": "MONITOR_SERVICE_UNAVAILABLE",
                "message": "版本监控服务尚未配置",
            }
        }
    service = build_service(tmp_path)
    checked_app = create_app(
        config={"svn": {"provider": "mock"}},
        provider=MockSVNProvider(),
        monitor_web_service=service,
    )
    with TestClient(checked_app) as client:
        invalid = client.post("/api/monitor/tasks", json={"url": "private"})
        assert invalid.status_code == 400
        assert invalid.json()["error"]["code"] == "MONITOR_INVALID_REQUEST"
        assert "fields" not in invalid.text


def test_monitor_unknown_errors_are_scoped_sanitized_and_command_recovers(tmp_path):
    service = build_service(tmp_path)
    app = create_app(
        config={"svn": {"provider": "mock"}},
        provider=MockSVNProvider(),
        monitor_web_service=service,
    )

    @app.get("/api/non-monitor-crash")
    def non_monitor_crash():
        raise RuntimeError("non-monitor-private-detail")

    with TestClient(app, raise_server_exceptions=False) as client:
        original_list = service.list_tasks

        def fail_list(**_):
            raise RuntimeError(r"C:\private\monitor.sqlite3 secret traceback")

        service.list_tasks = fail_list
        failed = client.get("/api/monitor/tasks")
        assert failed.status_code == 500
        assert failed.json() == {
            "error": {
                "code": "MONITOR_API_INTERNAL_ERROR",
                "message": "版本监控服务内部错误",
            }
        }
        assert "private" not in failed.text
        service.list_tasks = original_list

        outside = client.get("/api/non-monitor-crash")
        assert outside.status_code == 500
        assert "MONITOR_API_INTERNAL_ERROR" not in outside.text

        task = client.post("/api/monitor/tasks", json=create_payload()).json()
        request_id = uuid4()
        original_pause = service.scheduled_tasks.pause

        def fail_pause(_):
            raise RuntimeError(r"C:\private\schtasks stderr")

        service.scheduled_tasks.pause = fail_pause
        command = client.post(
            f"/api/monitor/tasks/{task['task_id']}/pause",
            json=command_payload(request_id),
        )
        assert command.status_code == 500
        assert command.json()["error"]["code"] == "MONITOR_API_INTERNAL_ERROR"
        assert [item.request_id for item in service.store.list_pending_commands()] == [
            str(request_id)
        ]

        service.scheduled_tasks.pause = original_pause
        assert service.recover_pending_commands() == 1
        assert service.store.list_pending_commands() == []


def test_monitor_patch_rejects_syncing_but_allows_other_editable_states(tmp_path):
    service = build_service(tmp_path)
    app = create_app(
        config={"svn": {"provider": "mock"}},
        provider=MockSVNProvider(),
        monitor_web_service=service,
    )
    with TestClient(app) as client:
        task = client.post("/api/monitor/tasks", json=create_payload()).json()
        task_id = task["task_id"]
        current = service.store.get_task(task_id)
        service.store.update_task(
            task_id,
            {"scheduler_sync_status": "pending"},
            NOW,
            expected_generation=current.generation,
            expected_scheduler_sync_status="synced",
        )
        syncing = client.patch(
            f"/api/monitor/tasks/{task_id}",
            json={
                "schema_version": "m3.monitor-task-patch.request.v1",
                "request_id": str(uuid4()),
                "daily_trigger_time": "21:00:00",
                "end_at": None,
            },
        )
        assert syncing.status_code == 409
        assert syncing.json()["error"]["code"] == "MONITOR_STATE_CONFLICT"

        service.store.update_task(
            task_id,
            {
                "scheduler_sync_status": "error",
                "scheduler_error": MonitorPublicErrorPayload(
                    code="MONITOR_SCHEDULER_SYNC_FAILED",
                    stage="scheduler",
                    message="计划任务同步失败",
                    retryable=True,
                ),
            },
            NOW,
            expected_generation=current.generation,
            expected_scheduler_sync_status="pending",
        )
        scheduler_error = client.patch(
            f"/api/monitor/tasks/{task_id}",
            json={
                "schema_version": "m3.monitor-task-patch.request.v1",
                "request_id": str(uuid4()),
                "daily_trigger_time": "21:30:00",
                "end_at": None,
            },
        )
        assert scheduler_error.status_code == 200, scheduler_error.text

        paused = client.post(
            f"/api/monitor/tasks/{task_id}/pause",
            json=command_payload(),
        )
        assert paused.status_code == 200
        paused_patch = client.patch(
            f"/api/monitor/tasks/{task_id}",
            json={
                "schema_version": "m3.monitor-task-patch.request.v1",
                "request_id": str(uuid4()),
                "daily_trigger_time": "22:00:00",
                "end_at": None,
            },
        )
        assert paused_patch.status_code == 200


def test_create_replay_precedes_endpoint_layout_and_svn_validation(tmp_path):
    service = build_service(tmp_path)
    app = create_app(
        config={"svn": {"provider": "mock"}},
        provider=MockSVNProvider(),
        monitor_web_service=service,
    )
    request_id = uuid4()
    payload = create_payload(request_id)
    with TestClient(app) as client:
        created = client.post("/api/monitor/tasks", json=payload)
        assert created.status_code == 201
        service.endpoint_registry = lambda: []
        service.dataset_layout = None
        service.history.resolve_branch_identity = lambda endpoint: (_ for _ in ()).throw(
            AssertionError("SVN must not be called during completed replay")
        )

        replay = client.post("/api/monitor/tasks", json=payload)
        assert replay.status_code == 201
        assert replay.json() == created.json()
        conflict = client.post(
            "/api/monitor/tasks",
            json={**payload, "endpoint_id": "MISSING"},
        )
        assert conflict.status_code == 409
        assert conflict.json()["error"]["code"] == "MONITOR_IDEMPOTENCY_CONFLICT"


def test_pending_create_is_recovered_from_persisted_payload(tmp_path):
    service = build_service(tmp_path)
    payload = MonitorTaskCreateRequestPayload.model_validate(create_payload())
    service.store.claim_command(
        request_id=str(payload.request_id),
        method="POST",
        target="POST /api/monitor/tasks",
        payload_hash=service._payload_hash(payload),
        payload_json=service._payload_json(payload),
        now=NOW,
    )

    assert service.recover_pending_commands() == 1
    assert service.store.list_pending_commands() == []
    task_id = str(uuid5(COMMAND_NAMESPACE, str(payload.request_id)))
    assert service.store.get_task(task_id) is not None
    service.close()


def test_task_list_uses_sql_page_and_batched_overviews(tmp_path, monkeypatch):
    service = build_service(tmp_path)
    for index in range(35):
        payload = MonitorTaskCreateRequestPayload.model_validate(
            {**create_payload(), "name": f"QA 任务 {index:02d}"}
        )
        service.create_task(payload)
    monkeypatch.setattr(
        service.store,
        "list_tasks",
        lambda: (_ for _ in ()).throw(AssertionError("full task scan is forbidden")),
    )
    monkeypatch.setattr(
        service.store,
        "list_runs",
        lambda task_id: (_ for _ in ()).throw(AssertionError("per-task run scan is forbidden")),
    )

    first = service.list_tasks(limit=10, cursor=None, statuses=None, query=None)
    assert len(first.items) == 10
    assert first.has_more is True
    assert first.next_cursor
    second = service.list_tasks(
        limit=10,
        cursor=first.next_cursor,
        statuses=None,
        query=None,
    )
    assert len(second.items) == 10
    assert {item.task_id for item in first.items}.isdisjoint(
        {item.task_id for item in second.items}
    )
    filtered = service.list_tasks(
        limit=10, cursor=None, statuses=["active"], query="任务 34"
    )
    assert [item.name for item in filtered.items] == ["QA 任务 34"]
    app = create_app(
        config={"svn": {"provider": "mock"}},
        provider=MockSVNProvider(),
        monitor_web_service=service,
    )
    with TestClient(app) as client:
        listed = client.get("/api/monitor/tasks?limit=10")
        assert listed.status_code == 200
        cached = client.get(
            "/api/monitor/tasks?limit=10",
            headers={"If-None-Match": listed.headers["etag"]},
        )
        assert cached.status_code == 304
        assert not cached.content


def test_latest_report_survives_history_retention_cleanup(tmp_path):
    service = build_service(tmp_path)
    app = create_app(
        config={"svn": {"provider": "mock"}},
        provider=MockSVNProvider(),
        monitor_web_service=service,
    )
    with TestClient(app) as client:
        task = client.post("/api/monitor/tasks", json=create_payload()).json()
        client.post(
            f"/api/monitor/tasks/{task['task_id']}/pause",
            json=command_payload(),
        )
        run, draft = publish_report(service, task["task_id"])
        service.publisher.cleanup_expired(
            task["task_id"], now=draft.report_expires_at
        )
        service.clock = lambda: draft.report_expires_at

        history = client.get(f"/api/monitor/runs/{run.run_id}/report")
        assert history.status_code == 410
        latest = client.get(
            f"/api/monitor/tasks/{task['task_id']}/latest-report"
        )
        assert latest.status_code == 200
        assert latest.content == draft.offline_html
        assert latest.headers["etag"] == f'"{draft.html_sha256}"'
        cached = client.get(
            f"/api/monitor/tasks/{task['task_id']}/latest-report",
            headers={"If-None-Match": "W/" + latest.headers["etag"]},
        )
        assert cached.status_code == 304
        assert not cached.content


def test_scheduler_failure_returns_task_state_and_sync_repairs_it(tmp_path):
    service = build_service(tmp_path)
    service.scheduler.gateway.fail_next = "create_or_update"
    app = create_app(
        config={"svn": {"provider": "mock"}},
        provider=MockSVNProvider(),
        monitor_web_service=service,
    )
    with TestClient(app) as client:
        created = client.post("/api/monitor/tasks", json=create_payload())
        assert created.status_code == 201
        assert created.json()["status"] == "scheduler_error"
        assert created.json()["scheduler"]["sync_status"] == "error"
        assert (
            created.json()["scheduler"]["last_error"]["code"]
            == "MONITOR_SCHEDULER_SYNC_FAILED"
        )
        repaired = client.post(
            f"/api/monitor/tasks/{created.json()['task_id']}/scheduler-sync",
            json=command_payload(),
        )
        assert repaired.status_code == 200
        assert repaired.json()["status"] == "active"
        assert repaired.json()["scheduler"]["sync_status"] == "synced"


def test_task_list_filters_derived_public_statuses_with_query_and_cursor(tmp_path):
    service = build_service(tmp_path)

    def create_named(name: str):
        return service.create_task(
            MonitorTaskCreateRequestPayload.model_validate(
                {**create_payload(), "name": name}
            )
        )[0]

    active = create_named("Status Filter active")
    syncing = create_named("Status Filter syncing")
    syncing_record = service.store.get_task(str(syncing.task_id))
    service.store.update_task(
        str(syncing.task_id),
        {"scheduler_sync_status": "pending"},
        NOW,
        expected_generation=syncing_record.generation,
        expected_lifecycle="active",
    )
    scheduler_error = create_named("Status Filter scheduler error")
    error_record = service.store.get_task(str(scheduler_error.task_id))
    service.store.update_task(
        str(scheduler_error.task_id),
        {
            "scheduler_sync_status": "error",
            "scheduler_error": MonitorPublicErrorPayload(
                code="MONITOR_SCHEDULER_SYNC_FAILED",
                stage="scheduler",
                message="计划任务同步失败",
                retryable=True,
            ),
        },
        NOW,
        expected_generation=error_record.generation,
        expected_lifecycle="active",
    )
    paused = create_named("Status Filter paused")
    service.task_command(paused.task_id, "pause", uuid4())

    app = create_app(
        config={"svn": {"provider": "mock"}},
        provider=MockSVNProvider(),
        monitor_web_service=service,
    )
    with TestClient(app) as client:
        active_page = client.get("/api/monitor/tasks", params={"status": "active"})
        assert [item["task_id"] for item in active_page.json()["items"]] == [
            str(active.task_id)
        ]
        syncing_page = client.get(
            "/api/monitor/tasks", params={"status": "syncing"}
        )
        assert [item["task_id"] for item in syncing_page.json()["items"]] == [
            str(syncing.task_id)
        ]
        error_page = client.get(
            "/api/monitor/tasks", params={"status": "scheduler_error"}
        )
        assert [item["task_id"] for item in error_page.json()["items"]] == [
            str(scheduler_error.task_id)
        ]
        filters = [
            ("limit", "1"),
            ("q", "status filter"),
            ("status", "active"),
            ("status", "paused"),
            ("status", "scheduler_error"),
        ]
        first = client.get("/api/monitor/tasks", params=filters)
        assert first.status_code == 200
        assert len(first.json()["items"]) == 1
        assert first.json()["has_more"] is True
        cached = client.get(
            "/api/monitor/tasks",
            params=filters,
            headers={"If-None-Match": first.headers["etag"]},
        )
        assert cached.status_code == 304
        second = client.get(
            "/api/monitor/tasks",
            params=[*filters, ("cursor", first.json()["next_cursor"])],
        )
        assert second.status_code == 200
        assert first.json()["items"][0]["task_id"] != second.json()["items"][0]["task_id"]
        wrong_scope = client.get(
            "/api/monitor/tasks",
            params=[
                ("limit", "1"),
                ("q", "different"),
                ("status", "active"),
                ("cursor", first.json()["next_cursor"]),
            ],
        )
        assert wrong_scope.status_code == 400
        assert wrong_scope.json()["error"]["code"] == "MONITOR_INVALID_CURSOR"


def test_run_list_uses_sql_page_and_batched_attempts(tmp_path, monkeypatch):
    service = build_service(tmp_path)
    payload = create_payload()
    payload["effective_at"] = "2026-08-07T09:00:00Z"
    task = service.create_task(
        MonitorTaskCreateRequestPayload.model_validate(payload)
    )[0]
    assert len(service.store.list_runs(str(task.task_id))) >= 3
    monkeypatch.setattr(
        service.store,
        "list_runs",
        lambda task_id: (_ for _ in ()).throw(AssertionError("full run scan is forbidden")),
    )
    monkeypatch.setattr(
        service.store,
        "get_run",
        lambda run_id: (_ for _ in ()).throw(AssertionError("per-run lookup is forbidden")),
    )
    monkeypatch.setattr(
        service.store,
        "attempts",
        lambda run_id: (_ for _ in ()).throw(AssertionError("per-run attempts are forbidden")),
    )
    app = create_app(
        config={"svn": {"provider": "mock"}},
        provider=MockSVNProvider(),
        monitor_web_service=service,
    )
    with TestClient(app) as client:
        first = client.get(f"/api/monitor/tasks/{task.task_id}/runs?limit=2")
        assert first.status_code == 200
        assert len(first.json()["items"]) == 2
        assert first.json()["has_more"] is True
        cached = client.get(
            f"/api/monitor/tasks/{task.task_id}/runs?limit=2",
            headers={"If-None-Match": first.headers["etag"]},
        )
        assert cached.status_code == 304
        second = client.get(
            f"/api/monitor/tasks/{task.task_id}/runs",
            params={"limit": 2, "cursor": first.json()["next_cursor"]},
        )
        assert second.status_code == 200
        assert {item["run_id"] for item in first.json()["items"]}.isdisjoint(
            {item["run_id"] for item in second.json()["items"]}
        )
