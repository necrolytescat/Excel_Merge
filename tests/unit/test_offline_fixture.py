from __future__ import annotations

from datetime import datetime, timezone
from hashlib import sha256
from io import BytesIO
import json
from pathlib import Path
from uuid import UUID
from zipfile import ZIP_DEFLATED, ZipFile

from fastapi.testclient import TestClient
import pytest

from app.main import create_app
from app.schemas.batch import BatchTaskPayload
from app.schemas.diff import serialize_diff_json
from app.services.offline_fixture import (
    FixtureInputData,
    FixtureLimits,
    OfflineFixtureError,
    OfflineFixtureService,
    create_offline_fixture_bytes,
    load_offline_fixture,
)
from app.services.workbook_diff_service import DatasetLayout, WorkbookDiffService
from core.svn_provider import MockSVNProvider


PROJECT_ROOT = Path(__file__).resolve().parents[2]
SOURCE_DIR = PROJECT_ROOT / "tests" / "excel" / "left"
TARGET_DIR = PROJECT_ROOT / "tests" / "excel" / "right"
CONFIG = json.loads((PROJECT_ROOT / "config" / "settings.json").read_text("utf-8"))
WORKBOOK = "AtlasConfig.xlsm"
TASK_ID = UUID("10000000-0000-4000-8000-000000000001")
ITEM_ID = UUID("20000000-0000-4000-8000-000000000002")
NOW = datetime(2026, 8, 6, 8, 0, tzinfo=timezone.utc)


class NoExternalReadsProvider(MockSVNProvider):
    def __init__(self):
        super().__init__({})
        self.calls = 0

    def _unexpected(self, *args, **kwargs):
        self.calls += 1
        raise AssertionError("offline replay must not access SVN")

    info = _unexpected
    list_tree = _unexpected
    list_children = _unexpected
    read_bytes = _unexpected


def _sample_fixture() -> bytes:
    service = WorkbookDiffService(DatasetLayout.from_config(CONFIG["dataset_layout"]))
    result = service.compare_local(SOURCE_DIR, TARGET_DIR, WORKBOOK)
    result_raw = serialize_diff_json(result)
    source_raw = (SOURCE_DIR / WORKBOOK).read_bytes()
    target_raw = (TARGET_DIR / WORKBOOK).read_bytes()
    status = "succeeded" if result.workbook.status.value in {"unchanged", "modified"} else "business_failed"
    task_status = "completed" if status == "succeeded" else "completed_with_failures"
    progress_key = "succeeded_items" if status == "succeeded" else "business_failed_items"
    task = BatchTaskPayload.model_validate(
        {
            "task_id": str(TASK_ID),
            "request_id": "30000000-0000-4000-8000-000000000003",
            "status": task_status,
            "source": {"endpoint_id": "LEFT", "revision": 101},
            "target": {"endpoint_id": "RIGHT", "revision": 202},
            "candidate_source": {
                "scope": "all",
                "status": "ready",
                "manifest_sha256": "d" * 64,
            },
            "progress": {
                "total_items": 1,
                "queued_items": 0,
                "running_items": 0,
                "succeeded_items": 0,
                "business_failed_items": 0,
                "orchestration_failed_items": 0,
                "skipped_items": 0,
                "cancelled_items": 0,
                "processed_items": 1,
                "ratio": 1.0,
                progress_key: 1,
            },
            "items": [
                {
                    "item_id": str(ITEM_ID),
                    "ordinal": 0,
                    "candidate": {
                        "path": WORKBOOK,
                        "status": "modified",
                        "source": {
                            "exists": True,
                            "size_bytes": len(source_raw),
                            "content_sha256": sha256(source_raw).hexdigest(),
                        },
                        "target": {
                            "exists": True,
                            "size_bytes": len(target_raw),
                            "content_sha256": sha256(target_raw).hexdigest(),
                        },
                        "fingerprint_sha256": "e" * 64,
                    },
                    "status": status,
                    "diff_status": result.workbook.status.value,
                    "diff_error_count": result.summary.error_count,
                    "result_ref": "m2r_" + "a" * 22,
                    "result_sha256": sha256(result_raw).hexdigest(),
                    "result_size_bytes": len(result_raw),
                    "result_expires_at": "2026-09-05T08:00:00Z",
                    "attempt_count": 1,
                    "recovery_count": 0,
                    "created_at": NOW,
                    "started_at": NOW,
                    "finished_at": NOW,
                    "updated_at": NOW,
                }
            ],
            "errors": [],
            "created_at": NOW,
            "updated_at": NOW,
            "preparation_started_at": NOW,
            "prepared_at": NOW,
            "started_at": NOW,
            "finished_at": NOW,
            "expires_at": "2026-09-05T08:00:00Z",
        }
    )
    input_files = []
    for side, directory in (("source", SOURCE_DIR), ("target", TARGET_DIR)):
        for path in sorted(directory.iterdir(), key=lambda value: value.name.casefold()):
            input_files.append(
                FixtureInputData(
                    side=side,
                    workbook_path=WORKBOOK,
                    filename=path.name,
                    kind="workbook" if path.name == WORKBOOK else "csv",
                    content=path.read_bytes(),
                )
            )
    return create_offline_fixture_bytes(
        task=task,
        dataset_layout=CONFIG["dataset_layout"],
        input_files=input_files,
        missing_files=[],
        golden_results={str(ITEM_ID): result_raw},
    )


def _rewrite_member(raw: bytes, target: str, replacement: bytes) -> bytes:
    output = BytesIO()
    with ZipFile(BytesIO(raw), "r") as source, ZipFile(
        output,
        "w",
        compression=ZIP_DEFLATED,
    ) as target_archive:
        for info in source.infolist():
            content = replacement if info.filename == target else source.read(info.filename)
            target_archive.writestr(info.filename, content)
    return output.getvalue()


def test_fixture_is_deterministic_and_current_recompute_matches_golden():
    first = _sample_fixture()
    second = _sample_fixture()
    assert first == second

    loaded = load_offline_fixture(first)
    assert loaded.task.task_id == TASK_ID
    assert len(loaded.inputs.inputs) == 34
    assert not loaded.missing_files.missing_files

    service = OfflineFixtureService()
    session = service.load(first)
    assert session["current"]["available_count"] == 0
    session = service.recompute_all()
    assert session["current"]["available_count"] == 1
    assert session["current"]["matched_count"] == 1
    assert session["current"]["mismatched_count"] == 0
    current, _, matches = service.result(ITEM_ID, mode="current")
    golden, _, _ = service.result(ITEM_ID, mode="golden")
    assert matches is True
    assert current == golden


def test_fixture_rejects_tampering_traversal_duplicates_and_limits():
    raw = _sample_fixture()
    loaded = load_offline_fixture(raw)
    blob_path = next(path for path in loaded.members if path.startswith("blobs/"))
    with pytest.raises(OfflineFixtureError, match="哈希"):
        load_offline_fixture(_rewrite_member(raw, blob_path, b"tampered"))
    result_path = next(path for path in loaded.members if path.startswith("expected/results/"))
    with pytest.raises(OfflineFixtureError, match="哈希"):
        load_offline_fixture(_rewrite_member(raw, result_path, b"{}"))


    traversal = BytesIO()
    with ZipFile(traversal, "w") as archive:
        archive.writestr("../escape", b"x")
    with pytest.raises(OfflineFixtureError, match="不安全路径"):
        load_offline_fixture(traversal.getvalue())

    duplicate = BytesIO()
    with ZipFile(duplicate, "w") as archive:
        archive.writestr("manifest.json", b"{}")
        archive.writestr("manifest.json", b"{}")
    with pytest.raises(OfflineFixtureError, match="重复成员"):
        load_offline_fixture(duplicate.getvalue())

    with pytest.raises(OfflineFixtureError, match="上传大小限制"):
        load_offline_fixture(
            raw,
            limits=FixtureLimits(max_archive_bytes=len(raw) - 1),
        )


def test_replay_api_is_dev_only_and_never_calls_provider():
    raw = _sample_fixture()
    provider = NoExternalReadsProvider()
    with TestClient(
        create_app(
            config={"web": {"dev_mode": True}, "svn": {"provider": "mock"}},
            provider=provider,
        )
    ) as client:
        assert client.get("/compare/replay").status_code == 200
        wrong_type = client.post("/api/replay/fixture", content=raw)
        assert wrong_type.status_code == 415
        loaded = client.post(
            "/api/replay/fixture",
            content=raw,
            headers={"Content-Type": "application/octet-stream"},
        )
        assert loaded.status_code == 200
        recomputed = client.post("/api/replay/recompute")
        assert recomputed.status_code == 200
        assert recomputed.json()["current"]["matched_count"] == 1
        result = client.get(f"/api/replay/results/{ITEM_ID}?mode=current")
        assert result.status_code == 200
        assert result.headers["x-m2-golden-match"] == "true"
    assert provider.calls == 0

    with TestClient(
        create_app(
            config={"web": {"dev_mode": False}, "svn": {"provider": "mock"}},
            provider=MockSVNProvider(),
        )
    ) as client:
        assert client.get("/compare/replay").status_code == 404
        assert client.post("/api/replay/fixture", content=raw).status_code == 404
