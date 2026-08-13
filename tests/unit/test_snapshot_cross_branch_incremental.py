from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
import threading
import time

import pytest

from app.services.snapshot_service import SnapshotService
from core.models import SvnInfo, TreeEntry
from core.svn_history import BranchIdentity, FrozenTreeChange, FrozenTreeDiff
from core.svn_provider import SVNProviderError


SOURCE_URL = "mock://repository/branches/source"
TARGET_URL = "mock://repository/branches/target"
SOURCE_REVISION = 101
TARGET_REVISION = 205
TABLE = "Source/Table"


def records(*, source_table=TABLE, target_table=TABLE):
    return [
        {
            "id": "SOURCE",
            "region": "KR",
            "track": "FIX",
            "label": "Source",
            "url": SOURCE_URL,
            "logical_scopes": ["TABLE"],
            "physical_path_filters": {"TABLE": source_table},
            "enabled": True,
        },
        {
            "id": "TARGET",
            "region": "KR",
            "track": "FIX",
            "label": "Target",
            "url": TARGET_URL,
            "logical_scopes": ["TABLE"],
            "physical_path_filters": {"TABLE": target_table},
            "enabled": True,
        },
    ]


class CrossBranchProvider:
    def __init__(
        self,
        *,
        file_count=197,
        changed_count=5,
        delay_seconds=0.0,
    ):
        self.file_count = file_count
        self.delay_seconds = delay_seconds
        self.source_names = [
            f"Config{index:03d}.xlsx" for index in range(file_count)
        ]
        self.target_names = list(self.source_names)
        self.changes = [
            FrozenTreeChange(
                relative_path=f"Config{index:03d}.xlsx",
                action="M",
                kind="file",
            )
            for index in range(changed_count)
        ]
        self.changed_target_names = {
            f"Config{index:03d}.xlsx" for index in range(changed_count)
        }
        self.repository_uuid_by_url = {
            SOURCE_URL: "repository-uuid",
            TARGET_URL: "repository-uuid",
        }
        self.identity_url_by_url = {
            SOURCE_URL: SOURCE_URL,
            TARGET_URL: TARGET_URL,
        }
        self.identity_revision_by_url = {
            SOURCE_URL: SOURCE_REVISION,
            TARGET_URL: TARGET_REVISION,
        }
        self.repository_root_by_url = {
            SOURCE_URL: "mock://repository",
            TARGET_URL: "mock://repository",
        }
        self.content_suffix_by_key = {}
        self.table_by_url = {
            SOURCE_URL: TABLE,
            TARGET_URL: TABLE,
        }
        self.extra_entries_by_url = {
            SOURCE_URL: [],
            TARGET_URL: [],
        }
        self.missing_size_by_url = {
            SOURCE_URL: set(),
            TARGET_URL: set(),
        }
        self.evidence_override = None
        self.evidence_error = None
        self.source_read_error = None
        self.fail_list_calls = set()
        self.identity_calls = 0
        self.list_calls = 0
        self.read_calls = 0
        self.evidence_calls = 0
        self.copy_boundary_calls = 0
        self._lock = threading.Lock()

    def info(self, endpoint):
        url = endpoint.url.rstrip("/")
        return SvnInfo(
            url=url,
            repository_root=self.repository_root_by_url[url],
            repository_uuid=self.repository_uuid_by_url[url],
            revision=str(self.identity_revision_by_url[url]),
            last_changed_revision=str(self.identity_revision_by_url[url]),
        )

    def resolve_branch_identity(self, endpoint):
        with self._lock:
            self.identity_calls += 1
        url = endpoint.url.rstrip("/")
        return BranchIdentity(
            canonical_url=self.identity_url_by_url[url],
            repository_root=self.repository_root_by_url[url],
            repository_uuid=self.repository_uuid_by_url[url],
            repository_relative_path=url.split("mock://repository/", 1)[-1],
            bound_revision=self.identity_revision_by_url[url],
        )

    def resolve_copy_boundary(self, identity):
        with self._lock:
            self.copy_boundary_calls += 1
        raise AssertionError("copy history must not be used as hash-equivalence evidence")

    def list_tree(self, endpoint, prefix=""):
        with self._lock:
            self.list_calls += 1
            call_number = self.list_calls
        if call_number in self.fail_list_calls:
            raise SVNProviderError("SVN_LIST_FAILED", "fixture list failure")
        url = endpoint.url.rstrip("/")
        table = self.table_by_url[url]
        names = self.source_names if url == SOURCE_URL else self.target_names
        result = [TreeEntry(path=table, kind="dir", revision=str(endpoint.revision))]
        for name in names:
            raw = self._content(url, name)
            result.append(
                TreeEntry(
                    path=f"{table}/{name}",
                    kind="file",
                    size=None if name in self.missing_size_by_url[url] else len(raw),
                    revision=str(endpoint.revision),
                    author="fixture",
                    date="2026-08-13T00:00:00Z",
                )
            )
        result.extend(self.extra_entries_by_url[url])
        return result

    def read_bytes(self, endpoint, path):
        with self._lock:
            self.read_calls += 1
        if self.delay_seconds:
            time.sleep(self.delay_seconds)
        url = endpoint.url.rstrip("/")
        name = path.split("/", 2)[-1]
        if url == SOURCE_URL and name == self.source_read_error:
            raise SVNProviderError("SVN_PATH_NOT_FOUND", "fixture source read failure")
        return self._content(url, name)

    def summarize_frozen_tree_diff(
        self,
        source,
        source_root,
        target,
        target_root,
    ):
        with self._lock:
            self.evidence_calls += 1
        if self.evidence_error is not None:
            raise self.evidence_error
        if self.evidence_override is not None:
            return self.evidence_override
        return FrozenTreeDiff(
            repository_uuid=source.repository_uuid,
            source_canonical_url=source.canonical_url,
            source_revision=source.bound_revision,
            source_root=source_root,
            target_canonical_url=target.canonical_url,
            target_revision=target.bound_revision,
            target_root=target_root,
            changes=tuple(self.changes),
        )

    def _content(self, url, name):
        changed = url == TARGET_URL and name in self.changed_target_names
        raw = f"{name}|{'changed' if changed else 'stable'}".encode()
        return raw + self.content_suffix_by_key.get((url, name), b"")


def service(provider):
    return SnapshotService(
        provider,
        allowed_schemes=("mock",),
        max_workers=8,
    )


def run(snapshot_service, *, reuse=True, endpoint_records=None):
    return snapshot_service.create_snapshot_at_revisions(
        endpoint_records or records(),
        source_id="SOURCE",
        source_revision=SOURCE_REVISION,
        target_id="TARGET",
        target_revision=TARGET_REVISION,
        reuse=reuse,
    )


def semantic(snapshot):
    return snapshot.model_dump(mode="json", exclude={"captured_at"})


def assert_matches_full(provider, endpoint_records=None):
    incremental_service = service(provider)
    incremental = run(
        incremental_service,
        reuse=True,
        endpoint_records=endpoint_records,
    )
    baseline_provider = clone_provider(provider)
    full = run(
        service(baseline_provider),
        reuse=False,
        endpoint_records=endpoint_records,
    )
    assert semantic(incremental) == semantic(full)
    return incremental_service, baseline_provider


def clone_provider(provider):
    cloned = CrossBranchProvider(
        file_count=provider.file_count,
        changed_count=0,
        delay_seconds=provider.delay_seconds,
    )
    cloned.source_names = list(provider.source_names)
    cloned.target_names = list(provider.target_names)
    cloned.changes = list(provider.changes)
    cloned.changed_target_names = set(provider.changed_target_names)
    cloned.repository_uuid_by_url = dict(provider.repository_uuid_by_url)
    cloned.identity_url_by_url = dict(provider.identity_url_by_url)
    cloned.identity_revision_by_url = dict(provider.identity_revision_by_url)
    cloned.repository_root_by_url = dict(provider.repository_root_by_url)
    cloned.table_by_url = dict(provider.table_by_url)
    cloned.content_suffix_by_key = dict(provider.content_suffix_by_key)
    cloned.extra_entries_by_url = {
        url: list(entries)
        for url, entries in provider.extra_entries_by_url.items()
    }
    cloned.missing_size_by_url = {
        url: set(names)
        for url, names in provider.missing_size_by_url.items()
    }
    cloned.source_read_error = provider.source_read_error
    return cloned


def test_cross_branch_reuses_192_of_197_hashes_and_matches_full_baseline():
    provider = CrossBranchProvider()

    snapshot_service, baseline = assert_matches_full(provider)

    assert provider.list_calls == 2
    assert provider.read_calls == 197 + 5
    assert baseline.read_calls == 197 * 2
    assert provider.evidence_calls == 1
    assert provider.copy_boundary_calls == 0
    metrics = snapshot_service.snapshot_reuse_metrics()
    assert metrics["cross_branch_pairs"] == 1
    assert metrics["cross_branch_reused_files"] == 192
    assert metrics["cross_branch_evidence_calls"] == 1
    assert metrics["cross_branch_evidence_seconds"] >= 0


def test_cross_branch_head_snapshot_preserves_full_content_refs_and_semantics():
    provider = CrossBranchProvider(file_count=12, changed_count=2)
    snapshot_service = service(provider)

    incremental = snapshot_service.create_snapshot(
        records(),
        source_id="SOURCE",
        target_id="TARGET",
    )
    baseline_provider = clone_provider(provider)
    baseline_service = service(baseline_provider)
    source, target = records()
    baseline = baseline_service._snapshot_at_resolved_records(
        source,
        SOURCE_REVISION,
        target,
        TARGET_REVISION,
        source_repository_uuid="repository-uuid",
        target_repository_uuid="repository-uuid",
        reuse=False,
    )

    assert semantic(incremental) == semantic(baseline)
    assert provider.read_calls == 12 + 2
    assert baseline_provider.read_calls == 12 * 2
    assert all(
        item.content_ref == expected.content_ref
        for actual_side, expected_side in (
            (incremental.source, baseline.source),
            (incremental.target, baseline.target),
        )
        for item, expected in zip(actual_side.files, expected_side.files)
    )


def test_missing_target_list_size_preserves_full_snapshot_semantics():
    provider = CrossBranchProvider(file_count=8, changed_count=1)
    missing_name = provider.target_names[-1]
    provider.missing_size_by_url[TARGET_URL].add(missing_name)

    snapshot_service, baseline = assert_matches_full(provider)

    target_file = next(
        item for item in run(snapshot_service).target.files if item.path.endswith(missing_name)
    )
    assert target_file.size == len(provider._content(TARGET_URL, missing_name))
    assert baseline.read_calls == 8 * 2


def test_cross_branch_warm_source_fixed_delay_is_at_least_1_7x():
    incremental_provider = CrossBranchProvider(delay_seconds=0.004)
    incremental_service = service(incremental_provider)
    incremental_service._snapshot_endpoint_at_revision(
        records()[0],
        SOURCE_REVISION,
        repository_uuid=SOURCE_URL,
    )
    incremental_provider.identity_calls = 0
    incremental_provider.list_calls = 0
    incremental_provider.read_calls = 0
    incremental_provider.evidence_calls = 0
    started = time.perf_counter()
    incremental = run(incremental_service)
    incremental_seconds = time.perf_counter() - started

    full_provider = CrossBranchProvider(delay_seconds=0.004)
    started = time.perf_counter()
    full = run(service(full_provider), reuse=False)
    full_seconds = time.perf_counter() - started

    assert semantic(incremental) == semantic(full)
    assert incremental_provider.read_calls == 5
    assert full_provider.read_calls == 197 * 2
    assert full_seconds / incremental_seconds >= 1.7
    assert incremental_service.snapshot_reuse_metrics()[
        "cross_branch_evidence_calls"
    ] == 1


@pytest.mark.parametrize(
    "history_shape",
    [
        "direct-copyfrom",
        "copy-before-source-revision",
        "copy-after-source-revision",
        "multi-level-copy-chain",
        "no-copy-relationship",
    ],
)
def test_copy_history_shape_does_not_replace_frozen_tree_evidence(history_shape):
    provider = CrossBranchProvider(file_count=12, changed_count=2)
    provider.history_shape = history_shape

    snapshot_service, _ = assert_matches_full(provider)

    assert provider.evidence_calls == 1
    assert provider.copy_boundary_calls == 0
    assert snapshot_service.snapshot_reuse_metrics()["cross_branch_pairs"] == 1


def test_case_variant_evidence_path_is_invalidated_conservatively():
    provider = CrossBranchProvider(file_count=8, changed_count=1)
    provider.changes = [
        FrozenTreeChange("config000.XLSX", "M", "file"),
    ]

    snapshot_service, baseline = assert_matches_full(provider)

    assert provider.read_calls == 8 + 1
    assert baseline.read_calls == 8 * 2
    assert snapshot_service.snapshot_reuse_metrics()[
        "cross_branch_reused_files"
    ] == 7


def test_all_change_actions_and_directory_changes_invalidate_affected_hashes():
    provider = CrossBranchProvider(file_count=0, changed_count=0)
    provider.source_names = [
        "Stable.xlsx",
        "Modified.xlsx",
        "Removed.xlsx",
        "Replaced.xlsx",
        "OldName.xlsx",
        "Nested/Copied.xlsx",
    ]
    provider.target_names = [
        "Stable.xlsx",
        "Modified.xlsx",
        "Added.xlsx",
        "Replaced.xlsx",
        "NewName.xlsx",
        "Nested/Copied.xlsx",
    ]
    provider.changed_target_names = {
        "Modified.xlsx",
        "Added.xlsx",
        "Replaced.xlsx",
        "NewName.xlsx",
        "Nested/Copied.xlsx",
    }
    provider.changes = [
        FrozenTreeChange("Modified.xlsx", "M", "file"),
        FrozenTreeChange("Removed.xlsx", "D", "file"),
        FrozenTreeChange("Added.xlsx", "A", "file"),
        FrozenTreeChange("Replaced.xlsx", "R", "file"),
        FrozenTreeChange("OldName.xlsx", "D", "file"),
        FrozenTreeChange("NewName.xlsx", "A", "file"),
        FrozenTreeChange("Nested", "R", "dir"),
    ]

    snapshot_service, baseline = assert_matches_full(provider)

    assert provider.read_calls == len(provider.source_names) + 5
    assert baseline.read_calls == len(provider.source_names) + len(provider.target_names)
    assert snapshot_service.snapshot_reuse_metrics()["cross_branch_reused_files"] == 1


@pytest.mark.parametrize(
    "failure,reason",
    [
        ("different_repository", "repository_identity"),
        ("table_layout", "table_layout"),
        ("case_conflict", "path_ambiguity"),
        ("identity_url_drift", "repository_identity"),
        ("identity_revision_drift", "repository_identity"),
        ("identity_path_drift", "repository_identity"),
        ("table_root_case_conflict", "path_ambiguity"),
        ("incomplete_evidence", "tree_diff_incomplete"),
        ("wrong_evidence_identity", "evidence_identity"),
        ("provider_error", "provider_error"),
        ("source_snapshot_incomplete", "source_snapshot_incomplete"),
        ("size_mismatch", "tree_diff_inconsistent"),
        ("empty_file_change", "path_ambiguity"),
        ("unknown_change_action", "path_ambiguity"),
    ],
)
def test_incomplete_or_ambiguous_evidence_falls_back_to_full_hashes(failure, reason):
    provider = CrossBranchProvider(file_count=8, changed_count=1)
    endpoint_records = records()
    if failure == "different_repository":
        provider.repository_uuid_by_url[TARGET_URL] = "different-repository"
    elif failure == "table_layout":
        provider.table_by_url[TARGET_URL] = "Game/Table"
        endpoint_records = records(target_table="Game/Table")
    elif failure == "case_conflict":
        provider.source_names = ["Book.xlsx", "book.xlsx"]
        provider.target_names = ["Book.xlsx", "book.xlsx"]
        provider.changed_target_names = set()
        provider.changes = []
        provider.file_count = 2
    elif failure == "identity_url_drift":
        provider.identity_url_by_url[TARGET_URL] = SOURCE_URL
    elif failure == "identity_revision_drift":
        provider.identity_revision_by_url[TARGET_URL] = TARGET_REVISION - 1
    elif failure == "identity_path_drift":
        provider.identity_url_by_url[TARGET_URL] = TARGET_URL + "/moved"
    elif failure == "table_root_case_conflict":
        for url in (SOURCE_URL, TARGET_URL):
            provider.extra_entries_by_url[url] = [
                TreeEntry(
                    path="Source/table/Other.xlsx",
                    kind="file",
                    size=len(provider._content(url, "Other.xlsx")),
                    revision=str(
                        SOURCE_REVISION if url == SOURCE_URL else TARGET_REVISION
                    ),
                )
            ]
    elif failure == "incomplete_evidence":
        provider.target_names.append("Added.xlsx")
        provider.changed_target_names.add("Added.xlsx")
    elif failure == "wrong_evidence_identity":
        provider.evidence_override = FrozenTreeDiff(
            repository_uuid="repository-uuid",
            source_canonical_url=SOURCE_URL,
            source_revision=SOURCE_REVISION,
            source_root=TABLE,
            target_canonical_url=TARGET_URL,
            target_revision=TARGET_REVISION + 1,
            target_root=TABLE,
            changes=tuple(provider.changes),
        )
    elif failure == "provider_error":
        provider.evidence_error = SVNProviderError(
            "SVN_TREE_DIFF_UNAVAILABLE",
            "fixture summarize unavailable",
        )
    elif failure == "source_snapshot_incomplete":
        provider.source_read_error = provider.source_names[-1]
    elif failure == "size_mismatch":
        provider.content_suffix_by_key[
            (TARGET_URL, provider.target_names[-1])
        ] = b"-different-size"
    elif failure == "empty_file_change":
        provider.changes.append(FrozenTreeChange("", "M", "file"))
    elif failure == "unknown_change_action":
        provider.changes.append(
            FrozenTreeChange("Config001.xlsx", "X", "file")
        )

    snapshot_service, _ = assert_matches_full(provider, endpoint_records)

    metrics = snapshot_service.snapshot_reuse_metrics()
    assert metrics["cross_branch_pairs"] == 0
    assert metrics["cross_branch_fallbacks"] == 1
    assert metrics[f"cross_branch_fallback.{reason}"] == 1


@pytest.mark.parametrize(
    "error",
    [
        SVNProviderError("SVN_AUTH_FAILED", "fixture auth filtering"),
        SVNProviderError("SVN_HISTORY_INVALID", "fixture history truncated"),
        SVNProviderError("SVN_TREE_DIFF_UNAVAILABLE", "fixture command unsupported"),
        SVNProviderError("SVN_DECODE_ERROR", "fixture invalid xml"),
    ],
)
def test_permissions_history_command_and_xml_failures_all_fall_back(error):
    provider = CrossBranchProvider(file_count=8, changed_count=1)
    provider.evidence_error = error

    snapshot_service, _ = assert_matches_full(provider)

    assert snapshot_service.snapshot_reuse_metrics()[
        "cross_branch_fallback.provider_error"
    ] == 1


def test_same_pair_concurrency_uses_single_flight_for_evidence_and_reads():
    provider = CrossBranchProvider(
        file_count=40,
        changed_count=3,
        delay_seconds=0.002,
    )
    snapshot_service = service(provider)
    barrier = threading.Barrier(6)

    def invoke():
        barrier.wait()
        return semantic(run(snapshot_service))

    with ThreadPoolExecutor(max_workers=6) as executor:
        results = list(executor.map(lambda _: invoke(), range(6)))

    assert all(result == results[0] for result in results)
    assert provider.evidence_calls == 1
    assert provider.read_calls == 40 + 3
    metrics = snapshot_service.snapshot_reuse_metrics()
    assert metrics["builds"] == 1
    assert metrics["waits"] == 5
    assert metrics["inflight"] == 0


def test_failed_build_is_not_cached_and_next_call_recovers():
    provider = CrossBranchProvider(file_count=6, changed_count=1)
    provider.evidence_error = SVNProviderError(
        "SVN_TREE_DIFF_UNAVAILABLE",
        "fixture summarize unavailable",
    )
    provider.fail_list_calls = {3, 4}
    snapshot_service = service(provider)

    with pytest.raises(SVNProviderError):
        run(snapshot_service)

    assert snapshot_service.snapshot_reuse_metrics()["entries"] == 0
    assert snapshot_service.snapshot_reuse_metrics()["inflight"] == 0
    provider.fail_list_calls.clear()
    provider.evidence_error = None

    recovered = run(snapshot_service)
    baseline = run(service(clone_provider(provider)), reuse=False)

    assert semantic(recovered) == semantic(baseline)
    assert provider.evidence_calls == 2
    assert snapshot_service.snapshot_reuse_metrics()["entries"] == 1


def test_new_service_revalidates_evidence_after_restart():
    provider = CrossBranchProvider(file_count=10, changed_count=2)

    run(service(provider))
    run(service(provider))

    assert provider.evidence_calls == 2


def test_snapshot_worker_threads_are_released():
    provider = CrossBranchProvider(file_count=20, changed_count=2)

    run(service(provider))

    deadline = time.monotonic() + 1
    while time.monotonic() < deadline:
        workers = [
            thread.name
            for thread in threading.enumerate()
            if thread.name.startswith(("m1-snapshot-file", "m1-snapshot-side"))
        ]
        if not workers:
            break
        time.sleep(0.01)
    assert not workers
