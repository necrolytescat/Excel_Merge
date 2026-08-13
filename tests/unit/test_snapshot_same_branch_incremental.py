from __future__ import annotations

import threading
import time

from app.services.snapshot_service import SnapshotService
from core.models import SvnInfo, TreeEntry
from core.svn_provider import SVNProviderError


def records():
    return [
        {
            "id": "BRANCH",
            "region": "KR",
            "track": "FIX",
            "label": "Same branch",
            "url": "mock://repository/branches/fix",
            "logical_scopes": ["TABLE"],
            "physical_path_filters": {"TABLE": "Source/Table"},
            "enabled": True,
        }
    ]


class IncrementalProvider:
    def __init__(self, *, file_count=197, changed_count=5, delay_seconds=0.0):
        self.file_count = file_count
        self.changed_count = changed_count
        self.delay_seconds = delay_seconds
        self.info_calls = 0
        self.list_calls = 0
        self.read_calls = 0
        self.repository_uuid = "same-branch-fixture"
        self.omit_revision = False
        self.fail_info = False
        self._lock = threading.Lock()

    def info(self, endpoint):
        with self._lock:
            self.info_calls += 1
        if self.fail_info:
            raise SVNProviderError("SVN_INFO_FAILED", "fixture info failure")
        return SvnInfo(
            url=endpoint.url,
            repository_root="mock://repository",
            repository_uuid=self.repository_uuid,
            revision=str(endpoint.revision),
            last_changed_revision=str(endpoint.revision),
        )

    def list_tree(self, endpoint, prefix=""):
        with self._lock:
            self.list_calls += 1
        revision = int(endpoint.revision)
        names = [f"Config{index:03d}.xlsx" for index in range(self.file_count)]
        if revision >= 200:
            names = names[1:] + ["Added.xlsx"]
        entries = [TreeEntry(path="Source/Table", kind="dir")]
        for index, name in enumerate(names):
            last_changed = 100
            if revision >= 200 and (name == "Added.xlsx" or index < self.changed_count):
                last_changed = 200
            entries.append(
                TreeEntry(
                    path=f"Source/Table/{name}",
                    kind="file",
                    size=32,
                    revision="" if self.omit_revision else str(last_changed),
                    author="fixture",
                    date="2026-08-13T00:00:00Z",
                )
            )
        return entries

    def read_bytes(self, endpoint, path):
        with self._lock:
            self.read_calls += 1
        if self.delay_seconds:
            time.sleep(self.delay_seconds)
        name = path.rsplit("/", 1)[-1]
        changed = int(endpoint.revision) >= 200 and (
            name == "Added.xlsx"
            or name in {f"Config{index:03d}.xlsx" for index in range(1, self.changed_count + 1)}
        )
        return f"{name}|{'changed' if changed else 'stable'}".encode()


def service(provider):
    return SnapshotService(
        provider,
        allowed_schemes=("mock",),
        max_workers=6,
    )


def run(snapshot, *, reuse):
    return snapshot.create_snapshot_at_revisions(
        records(),
        source_id="BRANCH",
        source_revision=100,
        target_id="BRANCH",
        target_revision=200,
        reuse=reuse,
    )


def semantic(snapshot):
    return snapshot.model_dump(mode="json", exclude={"captured_at"})


def test_same_branch_reuses_unchanged_hashes_and_preserves_full_semantics():
    incremental_provider = IncrementalProvider()
    incremental_service = service(incremental_provider)
    incremental = run(incremental_service, reuse=True)

    full_provider = IncrementalProvider()
    full = run(service(full_provider), reuse=False)

    assert semantic(incremental) == semantic(full)
    assert incremental_provider.list_calls == 2
    assert incremental_provider.read_calls == 197 + 6
    assert full_provider.read_calls == 197 * 2
    metrics = incremental_service.snapshot_reuse_metrics()
    assert metrics["incremental_pairs"] == 1
    assert metrics["incremental_reused_files"] == 191


def test_same_branch_fixed_delay_acceptance_is_at_least_1_7x():
    incremental_provider = IncrementalProvider(delay_seconds=0.004)
    incremental_service = service(incremental_provider)
    incremental_service._snapshot_endpoint_at_revision(
        records()[0],
        100,
        repository_uuid="mock://repository/branches/fix",
    )
    incremental_provider.info_calls = 0
    incremental_provider.list_calls = 0
    incremental_provider.read_calls = 0
    started = time.perf_counter()
    incremental = run(incremental_service, reuse=True)
    incremental_seconds = time.perf_counter() - started

    full_provider = IncrementalProvider(delay_seconds=0.004)
    started = time.perf_counter()
    full = run(service(full_provider), reuse=False)
    full_seconds = time.perf_counter() - started

    assert semantic(incremental) == semantic(full)
    assert incremental_provider.read_calls == 6
    assert full_seconds / incremental_seconds >= 1.7


def test_missing_last_changed_revision_falls_back_to_full_snapshot():
    provider = IncrementalProvider(file_count=12, changed_count=2)
    provider.omit_revision = True
    incremental = run(service(provider), reuse=True)

    full_provider = IncrementalProvider(file_count=12, changed_count=2)
    full_provider.omit_revision = True
    full = run(service(full_provider), reuse=False)

    assert semantic(incremental) == semantic(full)
    assert provider.read_calls == full_provider.read_calls
