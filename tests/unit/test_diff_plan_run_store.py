from pathlib import Path
import sqlite3
from types import SimpleNamespace
from uuid import uuid4

import pytest

from app.schemas.diff_plan import DiffPlanRunStartRequestPayload
from app.services.diff_plan_run_store import DiffPlanRunStore
from app.services.diff_plan_store import DiffPlanError, DiffPlanStore
from app.schemas.diff_plan import DiffPlanCreateRequestPayload


def build_stores(tmp_path: Path):
    database = tmp_path / "m4.sqlite3"
    plans = DiffPlanStore(database)
    plan, _ = plans.create(DiffPlanCreateRequestPayload(
        schema_version="m4.diff-plan-create.request.v1",
        request_id=uuid4(),
        name="冻结计划",
        source_endpoint_id="source",
        target_endpoint_ids=["target-a", "target-b"],
        workbook_paths=["A.xlsx", "B.xlsx"],
    ))
    return plans, DiffPlanRunStore(database, tmp_path / "results"), plan


def test_run_store_create_progress_result_and_active_guard(tmp_path):
    _, store, plan = build_stores(tmp_path)
    request_id = uuid4()
    run, created = store.create_run(
        request_id=request_id,
        request_hash="frozen",
        plan=plan,
        source_revision=100,
        target_revisions={"target-a": 101, "target-b": 102},
    )
    replay, replay_created = store.create_run(
        request_id=request_id,
        request_hash="frozen",
        plan=plan,
        source_revision=100,
        target_revisions={"target-a": 101, "target-b": 102},
    )
    assert created is True and replay_created is False
    assert replay.run_id == run.run_id
    assert run.progress.total_items == 4

    with pytest.raises(DiffPlanError) as active:
        store.create_run(
            request_id=uuid4(), request_hash="other", plan=plan,
            source_revision=100, target_revisions={"target-a": 101},
        )
    assert active.value.code == "DIFF_PLAN_RUN_ACTIVE"

    claimed = store.claim_preparation()
    assert claimed["run_id"] == str(run.run_id)
    current = store.get_run(run.run_id)
    for item in current.items:
        store.update_candidate(
            str(item.item_id), status="identical", candidate_status="identical",
            source_exists=True, target_exists=True,
            source_sha256="a" * 64, target_sha256="a" * 64,
        )
    store.finish_preparation(str(run.run_id))
    finished = store.get_run(run.run_id)
    assert finished.status == "completed"
    assert finished.progress.identical_items == 4
    assert finished.progress.ratio == 1
    history = store.list_runs(plan.plan_id)
    assert history.runs[0].schema_version == "m4.diff-plan-run-summary.v1"


def test_run_store_cancel_is_idempotent_and_preparation_cannot_revive_items(tmp_path):
    _, store, plan = build_stores(tmp_path)
    run, _ = store.create_run(
        request_id=uuid4(), request_hash="cancel", plan=plan,
        source_revision=100, target_revisions={"target-a": 101, "target-b": 102},
    )
    store.claim_preparation()
    command_id = uuid4()
    cancelled = store.cancel(run.run_id, command_id)
    replay = store.cancel(run.run_id, command_id)
    assert cancelled.status == "cancelled"
    assert replay.status == "cancelled"
    first = cancelled.items[0]
    store.update_candidate(
        str(first.item_id), status="identical", candidate_status="identical",
        source_exists=True, target_exists=True, source_sha256="a", target_sha256="a",
    )
    assert store.get_run(run.run_id).items[0].status == "cancelled"


def test_run_store_recovery_and_result_round_trip(tmp_path):
    _, store, plan = build_stores(tmp_path)
    run, _ = store.create_run(
        request_id=uuid4(), request_hash="recover", plan=plan,
        source_revision=100, target_revisions={"target-a": 101, "target-b": 102},
    )
    store.claim_preparation()
    item = store.get_run(run.run_id).items[0]
    store.update_candidate(
        str(item.item_id), status="queued", candidate_status="modified",
        source_exists=True, target_exists=True, source_sha256="a", target_sha256="b",
    )
    store.finish_preparation(str(run.run_id))
    claim = store.claim_item()
    assert claim["status"] == "queued"
    store.recover()
    assert store.get_run(run.run_id).items[0].status == "queued"

    claim = store.claim_item()
    content = b'{"schema_version":"m2.diff.v1"}'
    result = store.write_result(str(run.run_id), claim["item_id"], content)
    assert store.complete_item(claim["item_id"], status="changed", diff_status="modified", result=result)
    loaded, etag = store.load_result(result["result_ref"])
    assert loaded == content
    assert len(etag) == 64


def completed_run_with_result(tmp_path: Path):
    _, store, plan = build_stores(tmp_path)
    run, _ = store.create_run(
        request_id=uuid4(), request_hash="retention", plan=plan,
        source_revision=100, target_revisions={"target-a": 101, "target-b": 102},
    )
    store.claim_preparation()
    items = store.get_run(run.run_id).items
    for index, item in enumerate(items):
        store.update_candidate(
            str(item.item_id), status="queued" if index == 0 else "identical",
            candidate_status="modified" if index == 0 else "identical",
            source_exists=True, target_exists=True,
            source_sha256="a", target_sha256="b" if index == 0 else "a",
        )
    store.finish_preparation(str(run.run_id))
    claim = store.claim_item()
    content = b'{"schema_version":"m2.diff.v1"}'
    result = store.write_result(str(run.run_id), claim["item_id"], content)
    assert store.complete_item(claim["item_id"], status="changed", diff_status="modified", result=result)
    return store, store.get_run(run.run_id), result, content


def test_expired_cleanup_removes_only_detail_and_preserves_matrix_summary(tmp_path):
    store, run, result, _ = completed_run_with_result(tmp_path)
    with sqlite3.connect(store.database_path) as connection:
        connection.execute(
            "UPDATE diff_plan_runs SET details_expires_at=? WHERE run_id=?",
            ("2000-01-01T00:00:00.000000Z", str(run.run_id)),
        )
    before = store.get_run(run.run_id)
    assert before.details_expired is True
    assert before.progress.changed_items == 1
    assert before.items[0].result_ref == result["result_ref"]

    cleaned = store.cleanup_expired_results()
    repeated = store.cleanup_expired_results()
    assert cleaned["expired_result_count"] == 1
    assert cleaned["removed_file_count"] == 1
    assert cleaned["removed_size_bytes"] > 0
    assert repeated == {"expired_result_count": 0, "removed_file_count": 0, "removed_size_bytes": 0}
    assert not (store.results_directory / result["result_path"]).exists()

    after = store.get_run(run.run_id)
    assert after.progress == before.progress
    assert after.source_revision == 100
    assert after.target_revisions == {"target-a": 101, "target-b": 102}
    assert after.items[0].result_ref == result["result_ref"]
    with pytest.raises(DiffPlanError) as expired:
        store.load_result(result["result_ref"])
    assert expired.value.code == "DIFF_PLAN_RESULT_EXPIRED"
    assert expired.value.status_code == 410


def test_result_corruption_is_sanitized_and_cleanup_retries_failed_delete(tmp_path, monkeypatch):
    store, run, result, _ = completed_run_with_result(tmp_path)
    result_path = store.results_directory / result["result_path"]
    result_path.write_bytes(b"not-gzip")
    with pytest.raises(DiffPlanError) as corrupt:
        store.load_result(result["result_ref"])
    assert corrupt.value.code == "DIFF_PLAN_RESULT_CORRUPT"
    assert "not-gzip" not in corrupt.value.message

    with sqlite3.connect(store.database_path) as connection:
        connection.execute(
            "UPDATE diff_plan_runs SET details_expires_at=? WHERE run_id=?",
            ("2000-01-01T00:00:00.000000Z", str(run.run_id)),
        )
    original_unlink = Path.unlink
    monkeypatch.setattr(Path, "unlink", lambda self, missing_ok=False: (_ for _ in ()).throw(OSError("password=hunter2")))
    failed = store.cleanup_expired_results()
    assert failed["expired_result_count"] == 1
    assert failed["removed_file_count"] == 0
    monkeypatch.setattr(Path, "unlink", original_unlink)
    retried = store.cleanup_expired_results()
    assert retried["removed_file_count"] == 1
