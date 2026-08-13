from __future__ import annotations

import hashlib
import threading
import time

import pytest

from app.services.snapshot_service import SnapshotService
from core.models import TreeEntry
from core.svn_provider import SVNProviderError


class DelayedSnapshotProvider:
    def __init__(self, *, files_per_side=24, delay_seconds=0.02):
        self.files_per_side = files_per_side
        self.delay_seconds = delay_seconds
        self.active = 0
        self.max_active = 0
        self._lock = threading.Lock()

    def list_tree(self, endpoint, prefix=""):
        return [TreeEntry(path="Source/Table", kind="dir")] + [
            TreeEntry(
                path=f"Source/Table/Config{index:02d}.xlsx",
                kind="file",
                size=16,
                revision=str(endpoint.revision),
            )
            for index in range(self.files_per_side)
        ]

    def read_bytes(self, endpoint, path):
        with self._lock:
            self.active += 1
            self.max_active = max(self.max_active, self.active)
        try:
            time.sleep(self.delay_seconds)
            return f"{endpoint.url}|{endpoint.revision}|{path}".encode()
        finally:
            with self._lock:
                self.active -= 1


class FailingTreeProvider(DelayedSnapshotProvider):
    def list_tree(self, endpoint, prefix=""):
        if endpoint.url.endswith("source"):
            time.sleep(0.02)
            raise SVNProviderError("SOURCE_FAILED", "source failure")
        raise SVNProviderError("TARGET_FAILED", "target failure")


def records():
    return [
        {
            "id": "SOURCE",
            "region": "KR",
            "track": "FIX",
            "label": "Source",
            "url": "mock://source",
            "logical_scopes": ["TABLE"],
            "physical_path_filters": {"TABLE": "Source/Table"},
            "enabled": True,
        },
        {
            "id": "TARGET",
            "region": "KR",
            "track": "FIX",
            "label": "Target",
            "url": "mock://target",
            "logical_scopes": ["TABLE"],
            "physical_path_filters": {"TABLE": "Source/Table"},
            "enabled": True,
        },
    ]


def service(provider):
    return SnapshotService(
        provider,
        allowed_schemes=("mock",),
        max_workers=6,
    )


def run_serial(snapshot):
    source, target = records()
    started = time.perf_counter()
    left = snapshot._snapshot_endpoint_at_revision(source, 101)
    right = snapshot._snapshot_endpoint_at_revision(target, 202)
    return time.perf_counter() - started, left, right


def run_parallel(snapshot):
    started = time.perf_counter()
    result = snapshot.create_snapshot_at_revisions(
        records(),
        source_id="SOURCE",
        source_revision=101,
        target_id="TARGET",
        target_revision=202,
    )
    return time.perf_counter() - started, result.source, result.target


def snapshot_digest(source, target):
    values = [
        f"{side.endpoint_id}|{item.path}|{item.content_hash}"
        for side in (source, target)
        for item in side.files
    ]
    return hashlib.sha256("\n".join(values).encode()).hexdigest()


def test_snapshot_pair_is_bounded_faster_and_semantically_identical():
    serial_provider = DelayedSnapshotProvider()
    serial_seconds, serial_source, serial_target = run_serial(
        service(serial_provider)
    )
    parallel_provider = DelayedSnapshotProvider()
    parallel_seconds, parallel_source, parallel_target = run_parallel(
        service(parallel_provider)
    )

    assert serial_seconds / parallel_seconds >= 1.7
    assert 6 < parallel_provider.max_active <= 12
    assert parallel_provider.active == 0
    assert snapshot_digest(parallel_source, parallel_target) == snapshot_digest(
        serial_source,
        serial_target,
    )
    assert not any(
        thread.name.startswith("m1-snapshot-")
        for thread in threading.enumerate()
    )


def test_snapshot_pair_preserves_source_error_priority_and_reclaims_threads():
    with pytest.raises(SVNProviderError) as caught:
        service(FailingTreeProvider()).create_snapshot_at_revisions(
            records(),
            source_id="SOURCE",
            source_revision=101,
            target_id="TARGET",
            target_revision=202,
        )

    assert caught.value.code == "SOURCE_FAILED"
    assert not any(
        thread.name.startswith("m1-snapshot-")
        for thread in threading.enumerate()
    )
