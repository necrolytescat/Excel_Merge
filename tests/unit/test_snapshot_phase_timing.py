from concurrent.futures import CancelledError, ThreadPoolExecutor
import threading
import time

import pytest

from app.services.snapshot_content_cache import (
    PersistentSnapshotContentCache,
    SnapshotFileIdentity,
)
from app.main import create_app
from app.services import snapshot_phase_timing as timing_module
from app.services.snapshot_phase_timing import SnapshotPhaseTiming
from app.tools import version_comparison_snapshot_phase_timing_acceptance as tool
from core.svn_provider import SVNProviderError



@pytest.mark.parametrize("logging_enabled", [True, False])
def test_app_phase_timing_switch_follows_operations_logging(logging_enabled):
    provider = tool._TimingProvider(
        files=12,
        read_delay_seconds=0,
        list_delay_seconds=0,
        payload_bytes=1024,
    )
    app = create_app(
        config={
            "svn": {
                "provider": "mock",
                "allowed_schemes": ["mock"],
            },
            "operations": {
                "logging": {"enabled": logging_enabled},
            },
        },
        provider=provider,
    )

    assert app.state.snapshot_service._phase_timing_enabled is logging_enabled


def test_nested_worker_scopes_count_cpu_once(monkeypatch):
    ticks = iter(
        [
            0,
            1_000_000_000,
            2_000_000_000,
            3_000_000_000,
            4_000_000_000,
            5_000_000_000,
        ]
    )
    monkeypatch.setattr(timing_module.time, "thread_time_ns", lambda: next(ticks))
    timing = SnapshotPhaseTiming(
        request_context_id="nested-cpu",
        source_endpoint_id="SOURCE",
        source_revision=100,
        target_endpoint_id="TARGET",
        target_revision=200,
    )
    timing._request_thread_id = -1

    with timing.side_scope("source"):
        with timing.side_scope("source"):
            pass

    metrics = timing.finish(outcome="succeeded")
    assert metrics["parallel"]["source_cpu_seconds"] == 3.0
    assert metrics["request"]["worker_cpu_seconds"] == 3.0
    assert metrics["request"]["cpu_seconds"] == 8.0


def test_snapshot_singleflight_keeps_request_contexts_and_shares_build_context():
    sink = []
    provider = tool._TimingProvider(
        files=12,
        read_delay_seconds=0.03,
        list_delay_seconds=0.01,
        payload_bytes=4096,
    )
    service = tool._service(
        provider,
        cache_root=None,
        timing_enabled=True,
        sink=sink,
    )
    barrier = threading.Barrier(5)

    def run(index):
        barrier.wait(timeout=5)
        return tool._run(
            service,
            source_revision=100,
            target_revision=200,
            request_context_id=f"request-{index}",
        )

    with ThreadPoolExecutor(max_workers=5) as executor:
        snapshots = list(executor.map(run, range(5)))

    assert len({tool._digest(snapshot) for snapshot in snapshots}) == 1
    assert len(sink) == 5
    assert {item["request"]["request_context_id"] for item in sink} == {
        f"request-{index}" for index in range(5)
    }
    build_contexts = {
        item["request"]["build_context_id"]
        for item in sink
    }
    assert len(build_contexts) == 1
    builders = [
        item
        for item in sink
        if item["request"]["reuse_mode"] == "same_branch_incremental"
    ]
    waiters = [
        item
        for item in sink
        if item["request"]["reuse_mode"] == "singleflight_waiter"
    ]
    assert len(builders) == 1
    assert len(waiters) == 4
    assert builders[0]["summary"]["provider_read"]["calls"] == 17
    assert all(item["summary"]["provider_read"]["calls"] == 0 for item in waiters)
    assert all(
        item["stages"]["reuse.singleflight_wait"]["calls"] == 1
        for item in waiters
    )


class _SingleFileFailureProvider(tool._TimingProvider):
    def read_bytes_with_source(self, endpoint, path):
        if endpoint.revision == 100 and path.endswith("Config007.xlsx"):
            with self._lock:
                self.read_calls += 1
                self.active_reads += 1
                self.peak_reads = max(self.peak_reads, self.active_reads)
            try:
                time.sleep(self.read_delay_seconds)
                raise SVNProviderError("MOCK_READ_FAILED", "mock file failure")
            finally:
                with self._lock:
                    self.active_reads -= 1
        return super().read_bytes_with_source(endpoint, path)


def test_single_file_failure_is_attributed_without_failing_snapshot():
    sink = []
    provider = _SingleFileFailureProvider(
        files=12,
        read_delay_seconds=0.002,
        list_delay_seconds=0.001,
        payload_bytes=4096,
    )
    service = tool._service(
        provider,
        cache_root=None,
        timing_enabled=True,
        sink=sink,
    )

    snapshot = tool._run(
        service,
        source_revision=100,
        target_revision=200,
        request_context_id="single-file-failure",
        reuse=False,
    )

    assert snapshot.source.stats.failed_count == 1
    assert snapshot.target.stats.failed_count == 0
    metrics = sink[-1]
    assert metrics["request"]["outcome"] == "succeeded"
    assert metrics["summary"]["provider_read"]["calls"] == 24
    assert metrics["summary"]["provider_read"]["failures"] == 1
    assert (
        metrics["summary"]["provider_read"]["by_side"]["source"]["failures"]
        == 1
    )


class _CancelledTreeProvider(tool._TimingProvider):
    def list_tree(self, endpoint, prefix=""):
        if endpoint.revision == 100:
            with self._lock:
                self.list_calls += 1
            raise CancelledError("mock cancellation")
        return super().list_tree(endpoint, prefix)


def test_cancelled_build_logs_failed_request_and_reclaims_pair_executor():
    sink = []
    provider = _CancelledTreeProvider(
        files=12,
        read_delay_seconds=0,
        list_delay_seconds=0,
        payload_bytes=1024,
    )
    service = tool._service(
        provider,
        cache_root=None,
        timing_enabled=True,
        sink=sink,
    )

    with pytest.raises(CancelledError, match="mock cancellation"):
        tool._run(
            service,
            source_revision=100,
            target_revision=200,
            request_context_id="cancelled-request",
            reuse=False,
        )

    metrics = sink[-1]
    assert metrics["request"]["outcome"] == "failed"
    assert metrics["summary"]["list_tree"]["failures"] == 1
    assert metrics["stages"]["pairing.future_wait"]["failures"] == 1
    assert metrics["stages"]["pairing.executor_shutdown"]["calls"] == 1
    assert not any(
        thread.name.startswith("m1-snapshot-")
        for thread in threading.enumerate()
    )


def test_persistent_file_singleflight_and_fallback_are_request_scoped(tmp_path):
    cache = PersistentSnapshotContentCache(
        tmp_path / "snapshot",
        max_bytes=1024 * 1024,
        max_file_entries=32,
        max_tree_entries=4,
    )
    identity = SnapshotFileIdentity(
        repository_uuid="timing-singleflight",
        canonical_url=tool._REPOSITORY_URL,
        relative_path="Source/Table/Config.xlsx",
        last_changed_revision="100",
        configuration_sha256="a" * 64,
    )
    barrier = threading.Barrier(4)
    loader_calls = 0
    loader_lock = threading.Lock()

    def worker(index):
        nonlocal loader_calls
        timing = SnapshotPhaseTiming(
            request_context_id=f"file-{index}",
            source_endpoint_id="BRANCH",
            source_revision=100,
            target_endpoint_id="BRANCH",
            target_revision=200,
        )

        def loader():
            nonlocal loader_calls
            with loader_lock:
                loader_calls += 1
            time.sleep(0.05)
            return b"trusted bytes"

        barrier.wait(timeout=5)
        cached, _ = cache.get_or_load(
            identity,
            expected_size=len(b"trusted bytes"),
            loader=loader,
            timing=timing,
            side="source",
        )
        cache.record_fallback(
            "fixture",
            timing=timing,
            side="source",
        )
        return cached.raw, timing.finish(outcome="succeeded")

    with ThreadPoolExecutor(max_workers=4) as executor:
        results = list(executor.map(worker, range(4)))

    assert loader_calls == 1
    assert [raw for raw, _ in results] == [b"trusted bytes"] * 4
    metrics = [item for _, item in results]
    assert sum(
        item["stages"].get("persistent.singleflight_wait", {}).get("calls", 0)
        for item in metrics
    ) == 3
    assert all(
        item["counters_by_side"]["source"]["persistent_fallback.fixture"] == 1
        for item in metrics
    )
