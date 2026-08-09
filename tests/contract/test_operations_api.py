from __future__ import annotations

import logging
from pathlib import Path
from uuid import uuid4

from fastapi.testclient import TestClient

from app.main import create_app
from app.services.operations_service import SVNCacheService
from core.svn_provider import MockSVNProvider


class FakeCacheClient:
    def __init__(self):
        self.memory_entries = 3

    def cache_metrics(self):
        return {
            "memory_hits": 4,
            "disk_hits": 2,
            "misses": 2,
            "writes": 1,
            "memory_entries": self.memory_entries,
        }

    def clear_memory_cache(self):
        count = self.memory_entries
        self.memory_entries = 0
        return count


def app_with_logs(tmp_path: Path):
    return create_app(
        config={
            "svn": {"provider": "mock"},
            "operations": {
                "logging": {
                    "directory": str(tmp_path / "logs"),
                    "max_bytes": 2048,
                    "retention_days": 2,
                }
            },
        },
        provider=MockSVNProvider(),
    )


def test_operations_logs_are_correlated_redacted_paginated_and_etagged(tmp_path):
    app = app_with_logs(tmp_path)
    request_id = uuid4()
    task_id = uuid4()

    with TestClient(app) as client:
        health = client.get("/api/health", headers={"X-Request-ID": str(request_id)})
        assert health.status_code == 200
        assert health.headers["X-Request-ID"] == str(request_id)

        logging.getLogger("app.test").error(
            "task failed password=hunter2 C:\\private\\result.json "
            "svn+ssh://user@example/repo Traceback (most recent call last)\n"
            '  File "C:\\private\\worker.py", line 8',
            extra={
                "event": "batch.test",
                "request_id": str(request_id),
                "task_id": str(task_id),
            },
        )
        logging.getLogger("app.test").warning(
            "second entry",
            extra={"event": "batch.test", "task_id": str(task_id)},
        )

        response = client.get(
            "/api/operations/logs",
            params={"limit": 1, "task_id": str(task_id)},
        )
        assert response.status_code == 200
        body = response.json()
        assert body["schema_version"] == "m2.operations-log-list.v1"
        assert len(body["items"]) == 1
        assert body["has_more"] is True
        assert body["next_cursor"]
        assert "path" not in body["items"][0]

        next_page = client.get(
            "/api/operations/logs",
            params={
                "limit": 1,
                "task_id": str(task_id),
                "cursor": body["next_cursor"],
            },
        )
        assert next_page.status_code == 200
        messages = [body["items"][0]["message"], next_page.json()["items"][0]["message"]]
        serialized = " ".join(messages)
        assert "hunter2" not in serialized
        assert "C:\\private" not in serialized
        assert "svn+ssh://" not in serialized
        assert "Traceback" not in serialized
        assert "[redacted]" in serialized
        assert "[internal-path]" in serialized
        assert "[svn-endpoint]" in serialized

        etag = response.headers["ETag"]
        cached = client.get(
            "/api/operations/logs",
            params={"limit": 1, "task_id": str(task_id)},
            headers={"If-None-Match": etag},
        )
        assert cached.status_code == 304


def test_global_cache_status_and_clear_only_touch_managed_files(tmp_path):
    cache_dir = tmp_path / "svn-cache"
    cache_dir.mkdir()
    managed_a = cache_dir / ("rev_10__" + "a" * 32 + ".bin")
    managed_b = cache_dir / ("rev_HEAD__" + "b" * 32 + ".bin")
    ignored = cache_dir / "notes.txt"
    managed_a.write_bytes(b"abcd")
    managed_b.write_bytes(b"123456")
    ignored.write_text("keep", encoding="utf-8")
    replay = tmp_path / "var" / "m2-fixtures" / "fixture.m2fixture"
    replay.parent.mkdir(parents=True)
    replay.write_bytes(b"fixture")
    result = tmp_path / "var" / "m2-batch" / "results" / "result.json.gz"
    result.parent.mkdir(parents=True)
    result.write_bytes(b"result")

    app = app_with_logs(tmp_path)
    fake_client = FakeCacheClient()
    app.state.svn_cache_service = SVNCacheService(
        cache_dir,
        client=fake_client,
        enabled=True,
        excluded_roots=(replay.parent, result.parents[1]),
    )

    with TestClient(app) as client:
        status = client.get("/api/operations/svn-cache")
        assert status.status_code == 200
        payload = status.json()
        assert payload["schema_version"] == "m2.svn-cache-status.v1"
        assert payload["scope"] == "global_shared"
        assert payload["reproducible"] is True
        assert payload["enabled"] is True
        assert payload["can_clear"] is True
        assert payload["file_count"] == 2
        assert payload["size_bytes"] == 10
        assert payload["ignored_file_count"] == 1
        assert payload["memory_entry_count"] == 3
        assert payload["session_hit_rate"] == 0.75

        request_id = uuid4()
        clear_body = {
            "schema_version": "m2.svn-cache-clear.request.v1",
            "request_id": str(request_id),
            "confirmation": "清空全局 SVN 缓存",
        }
        cleared = client.post("/api/operations/svn-cache/clear", json=clear_body)
        assert cleared.status_code == 200
        assert cleared.json()["removed_file_count"] == 2
        assert cleared.json()["removed_size_bytes"] == 10
        assert cleared.json()["cleared_memory_entry_count"] == 3
        repeated = client.post("/api/operations/svn-cache/clear", json=clear_body)
        assert repeated.status_code == 200
        assert repeated.json() == cleared.json()

    assert not managed_a.exists()
    assert not managed_b.exists()
    assert ignored.read_text(encoding="utf-8") == "keep"
    assert replay.read_bytes() == b"fixture"
    assert result.read_bytes() == b"result"


def test_cache_clear_rejects_result_and_replay_roots(tmp_path):
    excluded = (
        tmp_path / "var" / "m2-batch",
        tmp_path / "var" / "m2-fixtures",
    )
    for unsafe in excluded:
        unsafe.mkdir(parents=True, exist_ok=True)
        service = SVNCacheService(
            unsafe,
            client=FakeCacheClient(),
            enabled=True,
            excluded_roots=excluded,
        )
        assert service.status().can_clear is False
