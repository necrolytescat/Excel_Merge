from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Event, Lock
import time
from uuid import uuid4

from fastapi.testclient import TestClient
import pytest

from app.main import create_app
from app.schemas.batch import BatchEndpointPayload
from app.schemas.diff import (
    DiffDirectionPayload,
    DiffResultPayload,
    WorkbookDiffPayload,
    WorkbookStatus,
    WorkbookSummaryPayload,
    serialize_diff_json,
)
from app.services.batch_diff_service import (
    BatchDiffService,
    SnapshotBatchCandidateResolver,
)
from app.services.batch_store import BatchStore
from app.services.config_service import ConfigStore
from app.services.snapshot_service import SnapshotService
from core.models import SvnInfo, TreeEntry
from core.svn_provider import SVNProviderError


SOURCE = BatchEndpointPayload(endpoint_id="LEFT", revision=101)
TARGET = BatchEndpointPayload(endpoint_id="RIGHT", revision=202)
TERMINAL = {"completed", "completed_with_failures", "cancelled", "failed"}


def records(*, left_label: str = "Left", left_url: str = "mock://left"):
    return [
        {
            "id": "LEFT",
            "region": "KR",
            "track": "FIX",
            "label": left_label,
            "url": left_url,
            "logical_scopes": ["TABLE"],
            "physical_path_filters": {"TABLE": "Source/Table"},
            "enabled": True,
        },
        {
            "id": "RIGHT",
            "region": "KR",
            "track": "FIX",
            "label": "Right",
            "url": "mock://right",
            "logical_scopes": ["TABLE"],
            "physical_path_filters": {"TABLE": "Source/Table"},
            "enabled": True,
        },
    ]


class CountingProvider:
    def __init__(self, *, delay_seconds: float = 0.0):
        self.delay_seconds = delay_seconds
        self.info_calls = 0
        self.list_calls = 0
        self.read_calls = 0
        self.fail_tree = False
        self._lock = Lock()

    def info(self, endpoint):
        with self._lock:
            self.info_calls += 1
        revision = "101" if endpoint.url.endswith("left") else "202"
        return SvnInfo(
            url=endpoint.url,
            repository_root="mock://repository",
            repository_uuid="snapshot-reuse-fixture",
            revision=revision,
            last_changed_revision=revision,
        )

    def list_tree(self, endpoint, prefix=""):
        with self._lock:
            self.list_calls += 1
        if self.delay_seconds:
            time.sleep(self.delay_seconds)
        if self.fail_tree and endpoint.url.endswith("left"):
            raise SVNProviderError("SVN_TREE_FAILED", "fixture tree failure")
        shared = ["Modified.xlsx", "ReadError.xls", "Same.xlsm"]
        side_only = ["LeftOnly.xlsx"] if endpoint.url.endswith("left") else ["RightOnly.xlsx"]
        return [TreeEntry(path="Source/Table", kind="dir")] + [
            TreeEntry(
                path=f"Source/Table/{name}",
                kind="file",
                size=16,
                revision=str(endpoint.revision),
            )
            for name in shared + side_only
        ]

    def read_bytes(self, endpoint, path):
        with self._lock:
            self.read_calls += 1
        if self.delay_seconds:
            time.sleep(self.delay_seconds / 4)
        if path.endswith("ReadError.xls") and endpoint.url.endswith("left"):
            raise SVNProviderError("SVN_READ_FAILED", "fixture read failure")
        if path.endswith("Same.xlsm"):
            return b"same"
        return f"{endpoint.url}|{endpoint.revision}|{path}".encode()


def snapshot_service(
    provider,
    *,
    ttl: float = 300,
    max_entries: int = 8,
    clock=time.monotonic,
    configuration=None,
):
    return SnapshotService(
        provider,
        allowed_schemes=("mock",),
        max_workers=4,
        reuse_ttl_seconds=ttl,
        reuse_max_entries=max_entries,
        monotonic_clock=clock,
        reuse_configuration=configuration,
    )


def prepare(resolver):
    return resolver.prepare(SOURCE, TARGET)


def candidate_dump(candidates):
    return [candidate.model_dump(mode="json") for candidate in candidates]


def test_page_snapshot_is_reused_by_batch_prepare_without_provider_calls():
    provider = CountingProvider(delay_seconds=0.03)
    service = snapshot_service(provider)
    registry = records()

    service.create_snapshot_at_revisions(
        registry,
        source_id="LEFT",
        source_revision=101,
        target_id="RIGHT",
        target_revision=202,
    )
    list_after_page = provider.list_calls
    reads_after_page = provider.read_calls

    warm_started = time.perf_counter()
    warm = prepare(SnapshotBatchCandidateResolver(service, lambda: registry))
    warm_seconds = time.perf_counter() - warm_started

    cold_provider = CountingProvider(delay_seconds=0.03)
    cold_service = snapshot_service(cold_provider)
    cold_started = time.perf_counter()
    cold = prepare(SnapshotBatchCandidateResolver(cold_service, lambda: registry))
    cold_seconds = time.perf_counter() - cold_started

    assert provider.list_calls == list_after_page
    assert provider.read_calls == reads_after_page
    assert cold_provider.list_calls == 2
    assert cold_provider.read_calls == 8
    assert candidate_dump(warm) == candidate_dump(cold)
    assert [(item.path, item.status) for item in warm] == [
        ("LeftOnly.xlsx", "left_only"),
        ("Modified.xlsx", "modified"),
        ("ReadError.xls", "read_error"),
        ("RightOnly.xlsx", "right_only"),
    ]
    assert warm_seconds < cold_seconds * 0.25
    assert service.snapshot_reuse_metrics()["hits"] == 1


def test_snapshot_build_is_single_flight_for_concurrent_batch_prepares():
    provider = CountingProvider(delay_seconds=0.05)
    service = snapshot_service(provider)
    resolver = SnapshotBatchCandidateResolver(service, records)

    with ThreadPoolExecutor(max_workers=6) as executor:
        results = list(executor.map(lambda _: prepare(resolver), range(6)))

    assert provider.list_calls == 2
    assert provider.read_calls == 8
    expected = candidate_dump(results[0])
    assert all(candidate_dump(result) == expected for result in results)
    metrics = service.snapshot_reuse_metrics()
    assert metrics["builds"] == 1
    assert metrics["waits"] == 5
    assert metrics["entries"] == 1
    assert metrics["inflight"] == 0


def test_expiry_restart_configuration_change_and_tamper_fall_back_to_rebuild():
    now = [10.0]
    provider = CountingProvider()
    service = snapshot_service(provider, ttl=5, clock=lambda: now[0])
    resolver = SnapshotBatchCandidateResolver(service, records)

    first = prepare(resolver)
    assert provider.list_calls == 2
    now[0] = 16.0
    assert candidate_dump(prepare(resolver)) == candidate_dump(first)
    assert provider.list_calls == 4
    assert service.snapshot_reuse_metrics()["expired"] == 1

    changed_registry = records(left_label="Renamed server endpoint")
    changed_resolver = SnapshotBatchCandidateResolver(service, lambda: changed_registry)
    assert candidate_dump(prepare(changed_resolver)) == candidate_dump(first)
    assert provider.list_calls == 6

    entry = next(reversed(service._snapshot_fact_cache.values()))
    entry.snapshot.source.files[0].content_hash = "0" * 64
    assert candidate_dump(prepare(changed_resolver)) == candidate_dump(first)
    assert provider.list_calls == 8
    assert service.snapshot_reuse_metrics()["invalid"] == 1

    restarted = snapshot_service(provider)
    assert candidate_dump(
        prepare(SnapshotBatchCandidateResolver(restarted, lambda: changed_registry))
    ) == candidate_dump(first)
    assert provider.list_calls == 10
    assert restarted.snapshot_reuse_metrics()["hits"] == 0


def test_failed_single_flight_is_not_cached_and_next_prepare_recovers():
    provider = CountingProvider(delay_seconds=0.03)
    provider.fail_tree = True
    service = snapshot_service(provider)
    resolver = SnapshotBatchCandidateResolver(service, records)

    def run():
        with pytest.raises(SVNProviderError) as caught:
            prepare(resolver)
        return caught.value.code

    with ThreadPoolExecutor(max_workers=4) as executor:
        codes = list(executor.map(lambda _: run(), range(4)))

    assert codes == ["SVN_TREE_FAILED"] * 4
    metrics = service.snapshot_reuse_metrics()
    assert metrics["entries"] == 0
    assert metrics["inflight"] == 0

    provider.fail_tree = False
    assert prepare(resolver)
    assert service.snapshot_reuse_metrics()["entries"] == 1


def test_lru_capacity_evicts_old_snapshot_facts():
    provider = CountingProvider()
    service = snapshot_service(provider, max_entries=1)
    registry = records()

    for source_revision, target_revision in ((101, 202), (102, 203), (101, 202)):
        service.create_snapshot_at_revisions(
            registry,
            source_id="LEFT",
            source_revision=source_revision,
            target_id="RIGHT",
            target_revision=target_revision,
        )

    assert provider.list_calls == 6
    metrics = service.snapshot_reuse_metrics()
    assert metrics["entries"] == 1
    assert metrics["evicted"] == 2


def test_swapped_direction_table_layout_and_dataset_layout_do_not_hit():
    provider = CountingProvider()
    service = snapshot_service(
        provider,
        configuration={"dataset_layout": {"version": 1}},
    )
    registry = records()
    resolver = SnapshotBatchCandidateResolver(service, lambda: registry)
    first = prepare(resolver)
    assert provider.list_calls == 2

    swapped = resolver.prepare(
        BatchEndpointPayload(endpoint_id="RIGHT", revision=202),
        BatchEndpointPayload(endpoint_id="LEFT", revision=101),
    )
    assert provider.list_calls == 4
    assert swapped

    moved_registry = records()
    moved_registry[0]["physical_path_filters"] = {"TABLE": "Moved/Table"}
    moved = prepare(
        SnapshotBatchCandidateResolver(service, lambda: moved_registry)
    )
    assert provider.list_calls == 6
    assert candidate_dump(moved) == candidate_dump(first)

    service._reuse_configuration_sha256 = service._hash_json(
        {"dataset_layout": {"version": 2}}
    )
    assert candidate_dump(prepare(resolver)) == candidate_dump(first)
    assert provider.list_calls == 8


@pytest.mark.parametrize(
    ("ttl", "max_entries"),
    [(0, 8), (300, 0)],
)
def test_zero_reuse_limits_disable_cache_and_keep_full_rebuild(ttl, max_entries):
    provider = CountingProvider()
    service = snapshot_service(provider, ttl=ttl, max_entries=max_entries)
    resolver = SnapshotBatchCandidateResolver(service, records)

    first = prepare(resolver)
    second = prepare(resolver)

    assert candidate_dump(first) == candidate_dump(second)
    assert provider.list_calls == 4
    metrics = service.snapshot_reuse_metrics()
    assert metrics["entries"] == 0
    assert metrics["hits"] == 0
    assert metrics["builds"] == 2


def test_fresh_prepare_bypasses_hot_snapshot_and_registry_is_captured_once():
    provider = CountingProvider()
    service = snapshot_service(provider)
    callback_count = 0

    def registry():
        nonlocal callback_count
        callback_count += 1
        return records()

    resolver = SnapshotBatchCandidateResolver(service, registry)
    first = resolver.prepare(SOURCE, TARGET)
    assert callback_count == 1
    assert provider.list_calls == 2

    fresh = resolver.prepare_fresh(SOURCE, TARGET)
    assert callback_count == 2
    assert provider.list_calls == 4
    assert candidate_dump(fresh) == candidate_dump(first)


class BlockingRunner:
    def __init__(self):
        self.started = Event()
        self.release = Event()
        self.calls = 0

    def run(self, source, target, workbook_path):
        self.calls += 1
        self.started.set()
        self.release.wait(timeout=5)
        return serialize_diff_json(
            DiffResultPayload(
                direction=DiffDirectionPayload(source="left", target="right"),
                workbook=WorkbookDiffPayload(
                    name=Path(workbook_path).name,
                    status=WorkbookStatus.MODIFIED,
                    source_sha256="a" * 64,
                    target_sha256="b" * 64,
                ),
                summary=WorkbookSummaryPayload(),
            )
        )


def wait_for_task(client, task_id, *, timeout=5):
    deadline = time.monotonic() + timeout
    last = None
    while time.monotonic() < deadline:
        response = client.get(f"/api/diff/batches/{task_id}")
        assert response.status_code == 200
        last = response.json()
        if last["status"] in TERMINAL:
            return last
        time.sleep(0.02)
    raise AssertionError(f"task did not converge: {last}")


def test_api_snapshot_batch_idempotency_and_cancel_use_trusted_facts(tmp_path):
    provider = CountingProvider()
    registry = records()
    app = create_app(
        config={
            "svn": {
                "provider": "mock",
                "allowed_schemes": ["mock"],
                "endpoint_registry": registry,
            },
            "snapshot_reuse": {"ttl_seconds": 300, "max_entries": 4},
        },
        provider=provider,
    )
    app.state.config_store = ConfigStore(tmp_path / "settings.json")
    runner = BlockingRunner()
    batch_service = BatchDiffService(
        BatchStore(tmp_path / "batch-state"),
        SnapshotBatchCandidateResolver(
            app.state.snapshot_service,
            lambda: app.state.endpoint_registry,
        ),
        runner,
        poll_interval_seconds=0.02,
        heartbeat_seconds=0.05,
    )
    app.state.batch_diff_service = batch_service

    with TestClient(app) as client:
        snapshot = client.post(
            "/api/svn/snapshots",
            json={
                "source": {"endpoint_id": "LEFT", "revision": 101},
                "target": {"endpoint_id": "RIGHT", "revision": 202},
            },
        )
        assert snapshot.status_code == 200
        page_counts = (provider.list_calls, provider.read_calls)

        request_id = str(uuid4())
        payload = {
            "schema_version": "m2.batch-create.request.v1",
            "request_id": request_id,
            "source": {"endpoint_id": "LEFT", "revision": 101},
            "target": {"endpoint_id": "RIGHT", "revision": 202},
        }
        created = client.post("/api/diff/batches", json=payload)
        replayed = client.post("/api/diff/batches", json=payload)
        assert created.status_code == 202
        assert replayed.status_code == 200
        assert replayed.json()["task_id"] == created.json()["task_id"]
        assert runner.started.wait(timeout=3)
        assert (provider.list_calls, provider.read_calls) == page_counts

        cancelled = client.post(
            f"/api/diff/batches/{created.json()['task_id']}/cancel",
            json={
                "schema_version": "m2.batch-cancel.request.v1",
                "request_id": str(uuid4()),
                "reason": "reuse cancellation fixture",
            },
        )
        assert cancelled.status_code == 202
        runner.release.set()
        terminal = wait_for_task(client, created.json()["task_id"])
        assert terminal["status"] == "cancelled"
        assert runner.calls == 1
        assert (provider.list_calls, provider.read_calls) == page_counts
