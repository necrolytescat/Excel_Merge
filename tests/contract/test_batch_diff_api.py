from __future__ import annotations

from copy import deepcopy
from datetime import timedelta
import hashlib
import json
import logging
from pathlib import Path
from threading import Event, Lock
import time
from uuid import UUID, uuid4

from fastapi.testclient import TestClient
import pytest

from app.main import create_app
from app.schemas.batch import (
    BatchCandidatePayload,
    BatchCandidateSidePayload,
    BatchCreateRequestPayload,
    BatchEndpointPayload,
    BatchRetryRequestPayload,
)
from app.schemas.diff import (
    DiffDirectionPayload,
    DiffErrorPayload,
    DiffResultPayload,
    ErrorStage,
    WorkbookDiffPayload,
    WorkbookStatus,
    WorkbookSummaryPayload,
    serialize_diff_json,
)
from app.services.batch_diff_service import (
    BatchDiffService,
    SnapshotBatchCandidateResolver,
)
from app.services.batch_store import BatchDiffError, BatchStore, isoformat, utc_now
from app.services.snapshot_service import SnapshotService
from app.services.workbook_execution_gate import WorkbookExecutionGate
from app.services.workbook_execution_scheduler import (
    PersistentWorkbookExecutionScheduler,
)
from core.svn_provider import MockSVNProvider


SOURCE = BatchEndpointPayload(endpoint_id="LEFT", revision=101)
TARGET = BatchEndpointPayload(endpoint_id="RIGHT", revision=202)
TERMINAL = {"completed", "completed_with_failures", "cancelled", "failed"}


def create_payload(*, request_id: UUID | None = None) -> BatchCreateRequestPayload:
    return BatchCreateRequestPayload(
        schema_version="m2.batch-create.request.v1",
        request_id=request_id or uuid4(),
        source=SOURCE,
        target=TARGET,
    )


def create_json(*, request_id: UUID | None = None) -> dict:
    return create_payload(request_id=request_id).model_dump(mode="json")


def side(*, exists: bool, digest: str | None = None, read_error: bool = False):
    return BatchCandidateSidePayload(
        exists=exists,
        size_bytes=4 if exists else None,
        content_sha256=None if read_error else digest,
        read_error=(
            {"code": "SVN_READ_FAILED", "message": "fixture read failed"}
            if read_error
            else None
        ),
    )


def candidate(path: str, status: str) -> BatchCandidatePayload:
    source = side(
        exists=status != "right_only",
        digest="a" * 64 if status not in {"right_only", "read_error"} else None,
        read_error=status == "read_error",
    )
    target = side(
        exists=status != "left_only",
        digest="b" * 64 if status not in {"left_only", "read_error"} else None,
    )
    facts = {
        "path": path,
        "status": status,
        "source": source.model_dump(mode="json"),
        "target": target.model_dump(mode="json"),
    }
    fingerprint = hashlib.sha256(
        json.dumps(facts, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    return BatchCandidatePayload(**facts, fingerprint_sha256=fingerprint)


def result_bytes(path: str, status: WorkbookStatus) -> bytes:
    errors = []
    error_count = 0
    if status in {WorkbookStatus.PARTIAL, WorkbookStatus.FAILED}:
        errors = [
            DiffErrorPayload(
                code="M2_FIXTURE_FAILURE",
                stage=ErrorStage.DIFF,
                workbook=path,
                message="fixture business failure",
            )
        ]
        error_count = 1
    payload = DiffResultPayload(
        direction=DiffDirectionPayload(source="left", target="right"),
        workbook=WorkbookDiffPayload(
            name=Path(path).name,
            status=status,
            source_sha256="a" * 64,
            target_sha256="b" * 64,
        ),
        summary=WorkbookSummaryPayload(error_count=error_count),
        errors=errors,
    )
    return serialize_diff_json(payload)


class StaticResolver:
    def __init__(self, candidates):
        self.candidates = list(candidates)
        self.prepare_calls = []
        self.validate_calls = []

    def validate_endpoints(self, source, target):
        self.validate_calls.append((source, target))

    def prepare(self, source, target):
        self.prepare_calls.append((source, target))
        return list(self.candidates)


class LeaseResolver(StaticResolver):
    def __init__(self, candidates, *, restore_succeeds=True):
        super().__init__(candidates)
        self.restore_succeeds = restore_succeeds
        self.restore_calls = []
        self.released = []

    def prepare_for_task(self, task_id, source, target, *, fresh=False):
        self.prepare_calls.append((source, target, fresh))
        return list(self.candidates), {"lease_id": f"m2:{task_id}"}

    def restore_dataset_lease(
        self, task_id, source, target, candidates
    ):
        self.restore_calls.append((task_id, source, target, list(candidates)))
        if not self.restore_succeeds:
            return None
        return {"lease_id": f"m2:{task_id}"}

    def release_dataset_lease(self, lease):
        self.released.append(lease)
        return True


class MappingRunner:
    def __init__(self, outcomes):
        self.outcomes = dict(outcomes)
        self.calls = []

    def run(self, source, target, workbook_path):
        self.calls.append((source, target, workbook_path))
        outcome = self.outcomes[workbook_path]
        if isinstance(outcome, Exception):
            raise outcome
        return result_bytes(workbook_path, outcome)


class BlockingRunner:
    def __init__(self):
        self.started = Event()
        self.release = Event()
        self.calls = []

    def run(self, source, target, workbook_path):
        self.calls.append(workbook_path)
        self.started.set()
        self.release.wait(timeout=5)
        return result_bytes(workbook_path, WorkbookStatus.MODIFIED)


def service(tmp_path, resolver, runner, **kwargs) -> BatchDiffService:
    return BatchDiffService(
        BatchStore(tmp_path / "batch-state"),
        resolver,
        runner,
        poll_interval_seconds=0.02,
        heartbeat_seconds=0.05,
        **kwargs,
    )


def wait_for_task(batch_service, task_id, predicate=None, timeout=5):
    deadline = time.monotonic() + timeout
    last = None
    while time.monotonic() < deadline:
        last = batch_service.get_task(task_id)
        if predicate is not None:
            if predicate(last):
                return last
        elif last.status in TERMINAL:
            return last
        time.sleep(0.02)
    raise AssertionError(f"task did not converge: {last}")


def test_create_api_is_strict_idempotent_and_initial_response_is_queued(tmp_path):
    request_id = uuid4()
    resolver = StaticResolver([])
    runner = MappingRunner({})
    batch_service = service(tmp_path, resolver, runner)
    app = create_app(
        config={"svn": {"provider": "mock"}},
        provider=MockSVNProvider(),
        batch_diff_service=batch_service,
    )

    with TestClient(app) as client:
        unknown = create_json()
        unknown["candidate_paths"] = ["forged.xlsm"]
        response = client.post("/api/diff/batches", json=unknown)
        assert response.status_code == 400
        assert response.json()["error"]["code"] == "BATCH_INVALID_REQUEST"

        invalid_revision = create_json()
        invalid_revision["source"]["revision"] = "HEAD"
        response = client.post("/api/diff/batches", json=invalid_revision)
        assert response.status_code == 400
        assert not resolver.validate_calls

        body = create_json(request_id=request_id)
        created = client.post("/api/diff/batches", json=body)
        assert created.status_code == 202
        assert created.json()["schema_version"] == "m2.batch.v1"
        assert created.json()["status"] == "queued"
        task_id = created.json()["task_id"]

        replay = client.post("/api/diff/batches", json=body)
        assert replay.status_code == 200
        assert replay.json()["task_id"] == task_id

        conflict = deepcopy(body)
        conflict["target"]["revision"] += 1
        response = client.post("/api/diff/batches", json=conflict)
        assert response.status_code == 409
        assert response.json()["error"]["code"] == "BATCH_IDEMPOTENCY_CONFLICT"


def test_same_endpoint_allows_different_revisions_and_rejects_identical_pair(tmp_path):
    resolver = StaticResolver([])
    runner = MappingRunner({})
    batch_service = service(tmp_path, resolver, runner)
    try:
        different = BatchCreateRequestPayload(
            schema_version="m2.batch-create.request.v1",
            request_id=uuid4(),
            source=BatchEndpointPayload(endpoint_id="SAME", revision=101),
            target=BatchEndpointPayload(endpoint_id="SAME", revision=102),
        )
        task, created = batch_service.create_task(different)
        assert created is True
        assert task.source.endpoint_id == task.target.endpoint_id == "SAME"
        assert task.source.revision == 101
        assert task.target.revision == 102

        identical = BatchCreateRequestPayload(
            schema_version="m2.batch-create.request.v1",
            request_id=uuid4(),
            source=BatchEndpointPayload(endpoint_id="SAME", revision=101),
            target=BatchEndpointPayload(endpoint_id="SAME", revision=101),
        )
        with pytest.raises(BatchDiffError) as captured:
            batch_service.create_task(identical)
        assert captured.value.code == "BATCH_ENDPOINT_REVISIONS_MUST_DIFFER"
    finally:
        batch_service.close()


def test_snapshot_candidates_use_fixed_revisions_without_info_or_head():
    fixture = {
        "tree": [
            {"path": "Table/A.xlsm", "kind": "file", "size": 4, "revision": "202"},
        ],
        "content": {
            "Table/A.xlsm": {"101": b"left", "202": b"right"},
        },
    }

    class NoHeadProvider(MockSVNProvider):
        def __init__(self):
            super().__init__(fixture)
            self.info_calls = 0
            self.revisions = []

        def info(self, endpoint):
            self.info_calls += 1
            raise AssertionError("fixed revision flow must not call info")

        def read_bytes(self, endpoint, path):
            self.revisions.append(endpoint.revision)
            return super().read_bytes(endpoint, path)

    provider = NoHeadProvider()
    snapshot_service = SnapshotService(provider, allowed_schemes=("https",))
    records = [
        {
            "id": "LEFT",
            "region": "KR",
            "track": "FIX",
            "label": "Left fixture",
            "url": "https://mock.local/left",
            "logical_scopes": ["TABLE"],
            "physical_path_filters": {"TABLE": "Table"},
            "enabled": True,
        },
        {
            "id": "RIGHT",
            "region": "KR",
            "track": "FIX",
            "label": "Right fixture",
            "url": "https://mock.local/right",
            "logical_scopes": ["TABLE"],
            "physical_path_filters": {"TABLE": "Table"},
            "enabled": True,
        },
    ]
    resolver = SnapshotBatchCandidateResolver(snapshot_service, lambda: records)

    candidates = resolver.prepare(SOURCE, TARGET)

    assert provider.info_calls == 0
    assert provider.revisions == [101, 202]
    assert [(item.path, item.status) for item in candidates] == [("A.xlsm", "modified")]


def test_candidate_rebuild_covers_four_states_and_excludes_unchanged():
    class SnapshotFixture:
        def __init__(self):
            self.calls = []

        def create_snapshot_at_revisions(self, records, **kwargs):
            from app.schemas.svn import SnapshotEndpointPayload, SnapshotFilePayload, SnapshotResponsePayload, SnapshotStatsPayload

            self.calls.append(kwargs)

            def file(path, digest=None, error=None):
                return SnapshotFilePayload(
                    path=path,
                    logical_scope="TABLE",
                    size=4,
                    revision=101,
                    content_hash=digest,
                    error=error,
                )

            source_files = [
                file("Table/Modified.xlsm", "a" * 64),
                file("Table/Left.xlsm", "c" * 64),
                file("Table/Read.xlsm", error={"code": "SVN_READ", "message": "bad"}),
                file("Table/Same.xlsm", "d" * 64),
            ]
            target_files = [
                file("Table/Modified.xlsm", "b" * 64),
                file("Table/Right.xlsm", "e" * 64),
                file("Table/Read.xlsm", "f" * 64),
                file("Table/Same.xlsm", "d" * 64),
            ]

            def endpoint(endpoint_id, revision, files):
                return SnapshotEndpointPayload(
                    endpoint_id=endpoint_id,
                    label=endpoint_id,
                    url="https://mock.local/" + endpoint_id,
                    resolved_revision=revision,
                    physical_path_filters={"TABLE": "Table"},
                    files=files,
                    stats=SnapshotStatsPayload(
                        file_count=len(files), total_size=16, failed_count=0
                    ),
                )

            return SnapshotResponsePayload(
                captured_at="2026-08-05T00:00:00Z",
                logical_scopes=["TABLE"],
                source=endpoint("LEFT", 101, source_files),
                target=endpoint("RIGHT", 202, target_files),
            )

    fixture = SnapshotFixture()
    records = [
        {"id": "LEFT", "enabled": True},
        {"id": "RIGHT", "enabled": True},
    ]
    resolver = SnapshotBatchCandidateResolver(fixture, lambda: records)

    first = resolver.prepare(SOURCE, TARGET)
    second = resolver.prepare(SOURCE, TARGET)

    assert fixture.calls[0] == {
        "source_id": "LEFT",
        "source_revision": 101,
        "target_id": "RIGHT",
        "target_revision": 202,
    }
    assert [(item.path, item.status) for item in first] == [
        ("Left.xlsm", "left_only"),
        ("Modified.xlsm", "modified"),
        ("Read.xlsm", "read_error"),
        ("Right.xlsm", "right_only"),
    ]
    assert [item.fingerprint_sha256 for item in first] == [
        item.fingerprint_sha256 for item in second
    ]


def test_mixed_results_are_isolated_persisted_and_readable(tmp_path):
    candidates = [
        candidate("Success.xlsm", "modified"),
        candidate("Partial.xlsm", "modified"),
        candidate("Explode.xlsm", "modified"),
        candidate("Left.xlsm", "left_only"),
        candidate("Right.xlsm", "right_only"),
        candidate("Read.xlsm", "read_error"),
    ]
    runner = MappingRunner(
        {
            "Success.xlsm": WorkbookStatus.MODIFIED,
            "Partial.xlsm": WorkbookStatus.PARTIAL,
            "Explode.xlsm": RuntimeError("internal path"),
        }
    )
    batch_service = service(tmp_path, StaticResolver(candidates), runner)
    try:
        initial, created = batch_service.create_task(create_payload())
        assert created and initial.status == "queued"
        task = wait_for_task(batch_service, initial.task_id)

        assert task.status == "completed_with_failures"
        assert [call[2] for call in runner.calls] == [
            "Success.xlsm",
            "Partial.xlsm",
            "Explode.xlsm",
        ]
        assert task.progress.model_dump() == {
            "total_items": 6,
            "queued_items": 0,
            "running_items": 0,
            "succeeded_items": 1,
            "business_failed_items": 1,
            "orchestration_failed_items": 1,
            "skipped_items": 3,
            "cancelled_items": 0,
            "processed_items": 6,
            "ratio": 1.0,
        }
        by_path = {item.candidate.path: item for item in task.items}
        assert by_path["Success.xlsm"].status == "succeeded"
        assert by_path["Partial.xlsm"].status == "business_failed"
        assert by_path["Explode.xlsm"].status == "orchestration_failed"
        assert by_path["Explode.xlsm"].result_ref is None
        assert by_path["Explode.xlsm"].orchestration_error.code == "BATCH_ITEM_UNEXPECTED"
        assert "internal path" not in by_path["Explode.xlsm"].orchestration_error.message
        for path in ("Success.xlsm", "Partial.xlsm"):
            item = by_path[path]
            content, sha256 = batch_service.load_result(item.result_ref)
            parsed = DiffResultPayload.model_validate_json(content)
            assert parsed.workbook.name == path
            assert sha256 == item.result_sha256
            assert item.result_expires_at == task.expires_at
    finally:
        batch_service.close()


def test_result_api_returns_raw_diff_and_etag(tmp_path):
    resolver = StaticResolver([candidate("Ready.xlsm", "modified")])
    runner = MappingRunner({"Ready.xlsm": WorkbookStatus.MODIFIED})
    batch_service = service(tmp_path, resolver, runner)
    app = create_app(
        config={"svn": {"provider": "mock"}},
        provider=MockSVNProvider(),
        batch_diff_service=batch_service,
    )

    with TestClient(app) as client:
        created = client.post("/api/diff/batches", json=create_json()).json()
        task = wait_for_task(batch_service, created["task_id"])
        item = task.items[0]
        response = client.get("/api/diff/batch-results/" + item.result_ref)
        assert response.status_code == 200
        assert response.headers["etag"] == item.result_sha256
        assert DiffResultPayload.model_validate(response.json()).workbook.name == "Ready.xlsm"
        assert "task_id" not in response.json()

        cached = client.get(
            "/api/diff/batch-results/" + item.result_ref,
            headers={"If-None-Match": item.result_sha256},
        )
        assert cached.status_code == 304
        missing = client.get("/api/diff/batch-results/m2r_" + "A" * 22)
        assert missing.status_code == 404


def test_cancel_does_not_kill_running_item_and_retry_creates_child(tmp_path):
    resolver = StaticResolver(
        [candidate("Running.xlsm", "modified"), candidate("Queued.xlsm", "modified")]
    )
    runner = BlockingRunner()
    batch_service = service(tmp_path, resolver, runner)
    try:
        initial, _ = batch_service.create_task(create_payload())
        assert runner.started.wait(timeout=3)
        cancelling = batch_service.cancel_task(
            initial.task_id,
            request_id=uuid4(),
            reason="user cancel",
        )
        assert cancelling.status == "cancelling"
        assert {item.status for item in cancelling.items} == {"running", "cancelled"}
        runner.release.set()
        cancelled = wait_for_task(batch_service, initial.task_id)
        assert cancelled.status == "cancelled"
        by_path = {item.candidate.path: item for item in cancelled.items}
        assert by_path["Running.xlsm"].status == "succeeded"
        assert by_path["Queued.xlsm"].status == "cancelled"

        retry, created = batch_service.retry_task(
            initial.task_id,
            BatchRetryRequestPayload(
                schema_version="m2.batch-retry.request.v1",
                request_id=uuid4(),
            ),
        )
        assert created
        assert retry.retry_of_task_id == initial.task_id
        child = wait_for_task(batch_service, retry.task_id)
        assert len(child.items) == 1
        assert child.items[0].candidate.path == "Queued.xlsm"
        assert child.items[0].retry_of_item_id == by_path["Queued.xlsm"].item_id

        with pytest.raises(BatchDiffError) as exc_info:
            batch_service.retry_task(
                initial.task_id,
                BatchRetryRequestPayload(
                    schema_version="m2.batch-retry.request.v1",
                    request_id=uuid4(),
                    item_ids=[by_path["Running.xlsm"].item_id],
                ),
            )
        assert exc_info.value.code == "BATCH_ITEM_NOT_RETRYABLE"
    finally:
        runner.release.set()
        batch_service.close()


def test_timeout_is_orchestration_failure_and_late_result_is_rejected(tmp_path):
    resolver = StaticResolver([candidate("Slow.xlsm", "modified")])
    runner = BlockingRunner()
    batch_service = service(
        tmp_path,
        resolver,
        runner,
        item_timeout_seconds=0.1,
    )
    try:
        initial, _ = batch_service.create_task(create_payload())
        assert runner.started.wait(timeout=3)
        task = wait_for_task(batch_service, initial.task_id)
        item = task.items[0]
        assert task.status == "completed_with_failures"
        assert item.status == "orchestration_failed"
        assert item.orchestration_error.code == "BATCH_ITEM_TIMEOUT"
        assert item.result_ref is None
        runner.release.set()
        time.sleep(0.1)
        assert batch_service.get_task(initial.task_id).items[0].result_ref is None
    finally:
        runner.release.set()
        batch_service.close()


def test_expired_lease_recovers_once_then_fails_and_limits_are_shared(tmp_path):
    store_a = BatchStore(tmp_path / "state")
    store_b = BatchStore(tmp_path / "state")

    def prepared_task(paths):
        task_id, _ = store_a.create_task(
            request_id=uuid4(),
            request_hash=uuid4().hex,
            source=SOURCE,
            target=TARGET,
        )
        store_a.claim_preparation()
        store_a.complete_preparation(
            task_id,
            [(candidate(path, "modified"), None, None) for path in paths],
        )
        return task_id

    first_task = prepared_task(["A.xlsm", "B.xlsm"])
    first_claim = store_a.claim_next_item()
    assert first_claim["task_id"] == first_task
    second_task = prepared_task(["C.xlsm"])
    second_claim = store_b.claim_next_item()
    assert second_claim["task_id"] == second_task
    assert store_a.claim_next_item() is None

    expired = isoformat(utc_now() - timedelta(seconds=1))
    with store_a._connect() as connection:
        connection.execute(
            "UPDATE items SET lease_expires_at=? WHERE item_id=?",
            (expired, first_claim["item_id"]),
        )
    store_a.recover_expired_leases()
    recovered = store_a.get_task(first_task).items[0]
    assert recovered.status == "queued"
    assert recovered.recovery_count == 1

    store_b.fail_item(
        item_id=second_claim["item_id"],
        lease_token=second_claim["lease_token"],
        code="DONE",
        message="release shared slot",
        retryable=True,
    )
    retry_claim = store_a.claim_next_item()
    assert retry_claim["item_id"] == first_claim["item_id"]
    assert retry_claim["attempt_count"] == 1
    with store_a._connect() as connection:
        connection.execute(
            "UPDATE items SET lease_expires_at=? WHERE item_id=?",
            (expired, retry_claim["item_id"]),
        )
    store_b.recover_expired_leases()
    exhausted = store_a.get_task(first_task).items[0]
    assert exhausted.status == "orchestration_failed"
    assert exhausted.orchestration_error.code == "BATCH_ITEM_RECOVERY_EXHAUSTED"



def test_task_dataset_lease_is_held_until_running_item_finishes(
    tmp_path, caplog
):
    caplog.set_level(
        logging.INFO,
        logger="app.services.batch_diff_service",
    )
    resolver = LeaseResolver([candidate("Lease.xlsm", "modified")])
    runner = BlockingRunner()
    batch_service = service(tmp_path, resolver, runner)
    try:
        initial, _ = batch_service.create_task(create_payload())
        assert runner.started.wait(timeout=5)
        assert str(initial.task_id) in batch_service._dataset_leases
        assert resolver.released == []

        runner.release.set()
        task = wait_for_task(batch_service, initial.task_id)
        deadline = time.monotonic() + 2
        while not resolver.released and time.monotonic() < deadline:
            time.sleep(0.02)
        assert task.status == "completed"
        assert resolver.released == [
            {"lease_id": f"m2:{initial.task_id}"}
        ]
        assert str(initial.task_id) not in batch_service._dataset_leases
        phases = {
            record.internal_metrics["phase"]
            for record in caplog.records
            if getattr(record, "event", None) == "batch.phase_timing"
        }
        assert {"compare_items", "finalize"} <= phases
    finally:
        runner.release.set()
        batch_service.close()


def test_restart_restores_dataset_lease_before_claiming_item(tmp_path):
    state_directory = tmp_path / "restore-state"
    store = BatchStore(state_directory)
    task_id, _ = store.create_task(
        request_id=uuid4(),
        request_hash=uuid4().hex,
        source=SOURCE,
        target=TARGET,
    )
    store.claim_preparation()
    store.complete_preparation(
        task_id,
        [(candidate("Restore.xlsm", "modified"), None, None)],
    )

    blocked_resolver = LeaseResolver([], restore_succeeds=False)
    blocked_runner = MappingRunner(
        {"Restore.xlsm": WorkbookStatus.MODIFIED}
    )
    blocked_service = BatchDiffService(
        BatchStore(state_directory),
        blocked_resolver,
        blocked_runner,
        poll_interval_seconds=0.02,
    )
    blocked_service.start()
    time.sleep(0.15)
    assert blocked_resolver.restore_calls
    assert blocked_runner.calls == []
    assert blocked_service.get_task(task_id).items[0].status == "queued"
    blocked_service.close()

    restored_resolver = LeaseResolver([])
    restored_runner = MappingRunner(
        {"Restore.xlsm": WorkbookStatus.MODIFIED}
    )
    restored_service = BatchDiffService(
        BatchStore(state_directory),
        restored_resolver,
        restored_runner,
        poll_interval_seconds=0.02,
    )
    try:
        restored_service.start()
        task = wait_for_task(restored_service, task_id)
        deadline = time.monotonic() + 2
        while not restored_resolver.released and time.monotonic() < deadline:
            time.sleep(0.02)
        assert task.status == "completed"
        assert len(restored_resolver.restore_calls) == 1
        assert restored_runner.calls
        assert restored_resolver.released == [
            {"lease_id": f"m2:{task_id}"}
        ]
    finally:
        restored_service.close()


def test_duplicate_logical_dataset_lease_is_not_released(tmp_path):
    resolver = LeaseResolver([])
    batch_service = service(tmp_path, resolver, MappingRunner({}))
    first = {"lease_id": "m2:duplicate"}
    duplicate = {"lease_id": "m2:duplicate"}
    batch_service._register_dataset_lease("duplicate", first)
    batch_service._register_dataset_lease("duplicate", duplicate)
    assert resolver.released == []
    batch_service.close()
    assert resolver.released == [first]


def test_four_way_service_reports_actual_execution_policy(tmp_path):
    scheduler = PersistentWorkbookExecutionScheduler(
        tmp_path / "execution.sqlite3",
        WorkbookExecutionGate(4),
        global_limit=4,
        per_flow_limit=4,
    )
    batch_service = BatchDiffService(
        BatchStore(tmp_path / "policy-state"),
        StaticResolver([]),
        MappingRunner({}),
        execution_scheduler=scheduler,
        item_concurrency=4,
    )
    try:
        task, _ = batch_service.create_task(create_payload())
        assert task.execution_policy.global_concurrency == 4
        assert task.execution_policy.per_task_concurrency == 4
    finally:
        batch_service.close()


def test_one_m2_task_can_claim_four_items_when_four_way_is_enabled(tmp_path):
    store = BatchStore(tmp_path / "state")
    task_id, _ = store.create_task(
        request_id=uuid4(),
        request_hash=uuid4().hex,
        source=SOURCE,
        target=TARGET,
    )
    store.claim_preparation()
    store.complete_preparation(
        task_id,
        [
            (candidate(f"{index}.xlsm", "modified"), None, None)
            for index in range(5)
        ],
    )

    claims = [
        store.claim_next_item(
            task_id=task_id,
            global_limit=4,
            per_task_limit=4,
        )
        for _ in range(4)
    ]
    assert all(claim is not None and claim["task_id"] == task_id for claim in claims)
    assert store.claim_next_item(
        task_id=task_id, global_limit=4, per_task_limit=4
    ) is None
def test_restart_persists_results_and_cleanup_leaves_tombstones(tmp_path):
    state_dir = tmp_path / "state"
    resolver = StaticResolver([candidate("Persist.xlsm", "modified")])
    runner = MappingRunner({"Persist.xlsm": WorkbookStatus.MODIFIED})
    batch_service = BatchDiffService(
        BatchStore(state_dir), resolver, runner, poll_interval_seconds=0.02
    )
    initial, _ = batch_service.create_task(create_payload())
    task = wait_for_task(batch_service, initial.task_id)
    result_ref = task.items[0].result_ref
    batch_service.close()

    reopened = BatchStore(state_dir)
    assert reopened.get_task(str(initial.task_id)).items[0].result_ref == result_ref
    assert DiffResultPayload.model_validate_json(reopened.load_result(result_ref)[0])

    with reopened._connect() as connection:
        connection.execute(
            "UPDATE tasks SET expires_at=? WHERE task_id=?",
            (isoformat(utc_now() - timedelta(seconds=1)), str(initial.task_id)),
        )
    with pytest.raises(BatchDiffError) as immediate_error:
        reopened.get_task(str(initial.task_id))
    assert immediate_error.value.code == "BATCH_TASK_EXPIRED"
    assert not reopened.list_tasks(limit=20).items
    assert reopened.cleanup_expired() == 1
    with pytest.raises(BatchDiffError) as task_error:
        reopened.get_task(str(initial.task_id))
    assert task_error.value.code == "BATCH_TASK_EXPIRED"
    with pytest.raises(BatchDiffError) as result_error:
        reopened.load_result(result_ref)
    assert result_error.value.code == "BATCH_RESULT_EXPIRED"


def test_result_store_failure_has_dedicated_error_code(tmp_path):
    class FailingStore(BatchStore):
        def write_result_blob(self, task_id, item_id, content):
            raise OSError("disk fixture")

    resolver = StaticResolver([candidate("Store.xlsm", "modified")])
    runner = MappingRunner({"Store.xlsm": WorkbookStatus.MODIFIED})
    batch_service = BatchDiffService(
        FailingStore(tmp_path / "state"),
        resolver,
        runner,
        poll_interval_seconds=0.02,
    )
    try:
        initial, _ = batch_service.create_task(create_payload())
        task = wait_for_task(batch_service, initial.task_id)
        item = task.items[0]
        assert item.status == "orchestration_failed"
        assert item.orchestration_error.code == "BATCH_ITEM_RESULT_STORE_FAILED"
        assert item.result_ref is None
    finally:
        batch_service.close()

def test_history_list_api_supports_cursor_filters_and_etag(tmp_path):
    resolver = StaticResolver([candidate("History.xlsm", "modified")])
    runner = MappingRunner({"History.xlsm": WorkbookStatus.MODIFIED})
    batch_service = service(tmp_path, resolver, runner)
    app = create_app(
        config={"svn": {"provider": "mock"}},
        provider=MockSVNProvider(),
        batch_diff_service=batch_service,
    )

    with TestClient(app) as client:
        task_ids = []
        for _ in range(3):
            created = client.post("/api/diff/batches", json=create_json())
            assert created.status_code == 202
            task_id = created.json()["task_id"]
            task_ids.append(task_id)
            assert wait_for_task(batch_service, task_id).status == "completed"

        first = client.get("/api/diff/batches", params={"limit": 2})
        assert first.status_code == 200
        body = first.json()
        assert body["schema_version"] == "m2.batch-list.v1"
        assert len(body["items"]) == 2
        assert body["has_more"] is True
        assert body["next_cursor"]
        assert body["items"][0]["progress"]["succeeded_items"] == 1
        serialized = json.dumps(body)
        assert "request_id" not in serialized
        assert "result_ref" not in serialized
        assert "result_path" not in serialized

        second = client.get(
            "/api/diff/batches",
            params={"limit": 2, "cursor": body["next_cursor"]},
        )
        assert second.status_code == 200
        assert len(second.json()["items"]) == 1
        listed_ids = {item["task_id"] for item in body["items"] + second.json()["items"]}
        assert listed_ids == set(task_ids)

        filtered = client.get(
            "/api/diff/batches",
            params=[("status", "completed"), ("q", "left")],
        )
        assert filtered.status_code == 200
        assert len(filtered.json()["items"]) == 3

        cached = client.get(
            "/api/diff/batches",
            params={"limit": 2},
            headers={"If-None-Match": first.headers["etag"]},
        )
        assert cached.status_code == 304

        invalid_cursor = client.get(
            "/api/diff/batches",
            params={"cursor": "not-a-cursor"},
        )
        assert invalid_cursor.status_code == 400
        assert invalid_cursor.json()["error"]["code"] == "BATCH_INVALID_CURSOR"

        invalid_range = client.get(
            "/api/diff/batches",
            params={
                "created_from": "2026-08-10T00:00:00Z",
                "created_to": "2026-08-09T00:00:00Z",
            },
        )
        assert invalid_range.status_code == 400
        assert invalid_range.json()["error"]["code"] == "BATCH_INVALID_TIME_RANGE"


def test_task_management_events_and_manual_delete_api(tmp_path):
    svn_cache = tmp_path / ".cache" / "svn" / "shared.cache"
    application_log = tmp_path / "logs" / "app.log"
    replay_fixture = tmp_path / "replay" / "golden.m2fixture"
    for sentinel in (svn_cache, application_log, replay_fixture):
        sentinel.parent.mkdir(parents=True, exist_ok=True)
        sentinel.write_text("keep", encoding="utf-8")

    resolver = StaticResolver([candidate("Managed.xlsm", "modified")])
    runner = MappingRunner({"Managed.xlsm": WorkbookStatus.MODIFIED})
    batch_service = service(tmp_path, resolver, runner)
    app = create_app(
        config={"svn": {"provider": "mock"}},
        provider=MockSVNProvider(),
        batch_diff_service=batch_service,
    )

    with TestClient(app) as client:
        created = client.post("/api/diff/batches", json=create_json())
        task_id = created.json()["task_id"]
        task = wait_for_task(batch_service, task_id)
        result_ref = task.items[0].result_ref
        assert result_ref
        with batch_service.store._connect() as connection:
            result_path = connection.execute(
                "SELECT result_path FROM items WHERE result_ref=?",
                (result_ref,),
            ).fetchone()["result_path"]
        formal_result = batch_service.store.state_directory / result_path
        assert formal_result.is_file()

        detail = client.get(f"/api/diff/batches/{task_id}/management")
        assert detail.status_code == 200
        body = detail.json()
        assert body["schema_version"] == "m2.batch-management.v1"
        assert body["can_delete"] is True
        assert body["results"]["count"] == 1
        assert body["results"]["size_bytes"] > 0
        event_types = {event["event_type"] for event in body["events"]}
        assert {
            "task.created",
            "status.preparing",
            "status.running",
            "status.completed",
        } <= event_types
        assert "result_path" not in json.dumps(body)
        assert "svn" not in json.dumps(body).lower()

        cached = client.get(
            f"/api/diff/batches/{task_id}/management",
            headers={"If-None-Match": detail.headers["etag"]},
        )
        assert cached.status_code == 304

        request_id = str(uuid4())
        delete_payload = {
            "schema_version": "m2.batch-delete.request.v1",
            "request_id": request_id,
            "reason": "history cleanup",
        }
        deleted = client.request(
            "DELETE",
            f"/api/diff/batches/{task_id}",
            json=delete_payload,
        )
        assert deleted.status_code == 200
        assert deleted.json()["schema_version"] == "m2.batch-delete.result.v1"
        assert deleted.json()["deleted_result_count"] == 1
        assert deleted.json()["deleted_result_size_bytes"] > 0

        replay = client.request(
            "DELETE",
            f"/api/diff/batches/{task_id}",
            json=delete_payload,
        )
        assert replay.status_code == 200
        assert replay.json() == deleted.json()
        assert client.get(f"/api/diff/batches/{task_id}").status_code == 410
        assert client.get(f"/api/diff/batch-results/{result_ref}").status_code == 410
        assert all(
            item["task_id"] != task_id
            for item in client.get("/api/diff/batches").json()["items"]
        )
        assert not formal_result.exists()
        assert svn_cache.read_text(encoding="utf-8") == "keep"
        assert application_log.read_text(encoding="utf-8") == "keep"
        assert replay_fixture.read_text(encoding="utf-8") == "keep"

        batch_service.store.cleanup_expired()
        with batch_service.store._connect() as connection:
            deletion_event = connection.execute(
                """
                SELECT 1 FROM task_events
                WHERE task_id=? AND event_type='command.delete'
                """,
                (task_id,),
            ).fetchone()
        assert deletion_event is not None


def test_manual_delete_rejects_active_task_and_does_not_cascade_retry_child(tmp_path):
    store = BatchStore(tmp_path / "queued-state")
    queued_id, _ = store.create_task(
        request_id=uuid4(),
        request_hash="queued",
        source=SOURCE,
        target=TARGET,
    )
    with pytest.raises(BatchDiffError) as active_error:
        store.delete_task(
            task_id=queued_id,
            request_id=uuid4(),
            reason=None,
        )
    assert active_error.value.code == "BATCH_TASK_NOT_DELETABLE"

    resolver = StaticResolver([candidate("Retry.xlsm", "modified")])
    runner = MappingRunner({"Retry.xlsm": WorkbookStatus.PARTIAL})
    batch_service = service(tmp_path, resolver, runner)
    try:
        parent, _ = batch_service.create_task(create_payload())
        parent = wait_for_task(batch_service, parent.task_id)
        child, _ = batch_service.retry_task(
            parent.task_id,
            BatchRetryRequestPayload(
                schema_version="m2.batch-retry.request.v1",
                request_id=uuid4(),
                item_ids=[parent.items[0].item_id],
            ),
        )
        child = wait_for_task(batch_service, child.task_id)
        management = batch_service.get_task_management(parent.task_id)
        assert management.retry_child_task_ids == [child.task_id]

        batch_service.delete_task(
            parent.task_id,
            request_id=uuid4(),
            reason="delete parent only",
        )
        with pytest.raises(BatchDiffError) as deleted_parent:
            batch_service.get_task(parent.task_id)
        assert deleted_parent.value.code == "BATCH_TASK_DELETED"
        assert batch_service.get_task(child.task_id).task_id == child.task_id
        assert batch_service.load_result(child.items[0].result_ref)[0]
    finally:
        batch_service.close()
