from __future__ import annotations

from copy import deepcopy
from datetime import timedelta
import hashlib
import json
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
