from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
import hashlib
import json
from pathlib import Path
from threading import Lock
import time

import pytest

from app.services.snapshot_content_cache import (
    PersistentSnapshotContentCache,
    SnapshotFileIdentity,
)
from app.services.snapshot_service import SnapshotService
from core.models import SvnInfo, TreeEntry


BRANCH_URL = "mock://repository/branches/fix"
RECORDS = [
    {
        "id": "BRANCH",
        "region": "KR",
        "track": "FIX",
        "label": "Same branch",
        "url": BRANCH_URL,
        "logical_scopes": ["TABLE"],
        "physical_path_filters": {"TABLE": "Source/Table"},
        "enabled": True,
    }
]


class PersistentFixtureProvider:
    def __init__(
        self,
        *,
        repository_uuid: str = "persistent-snapshot-fixture",
        omit_last_changed_revision: bool = False,
        read_delay_seconds: float = 0.0,
    ):
        self.repository_uuid = repository_uuid
        self.omit_last_changed_revision = omit_last_changed_revision
        self.read_delay_seconds = read_delay_seconds
        self.info_calls = 0
        self.list_calls = 0
        self.read_calls = 0
        self._lock = Lock()

    @staticmethod
    def _facts(revision: int) -> dict[str, tuple[int, bytes]]:
        names = [f"Config{index:03d}.xlsx" for index in range(55)]
        facts = {
            name: (100, f"{name}|r100".encode())
            for name in names
        }
        if revision >= 200:
            for index in range(4):
                name = f"Config{index:03d}.xlsx"
                facts[name] = (200, f"{name}|r200".encode())
            facts["Added200.xlsx"] = (200, b"Added200.xlsx|r200")
        if revision >= 300:
            facts.pop("Config054.xlsx")
            for index in range(4, 8):
                name = f"Config{index:03d}.xlsx"
                facts[name] = (300, f"{name}|r300".encode())
            facts["Added300.xlsx"] = (300, b"Added300.xlsx|r300")
        return facts

    def info(self, endpoint):
        with self._lock:
            self.info_calls += 1
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
        facts = self._facts(int(endpoint.revision))
        entries = [TreeEntry(path="Source/Table", kind="dir")]
        for name, (last_changed, raw) in facts.items():
            entries.append(
                TreeEntry(
                    path=f"Source/Table/{name}",
                    kind="file",
                    size=len(raw),
                    revision=(
                        ""
                        if self.omit_last_changed_revision
                        else str(last_changed)
                    ),
                )
            )
        return entries

    def read_bytes(self, endpoint, path):
        with self._lock:
            self.read_calls += 1
        if self.read_delay_seconds:
            time.sleep(self.read_delay_seconds)
        name = path.rsplit("/", 1)[-1]
        return self._facts(int(endpoint.revision))[name][1]


def service(
    provider,
    cache_dir: Path | None,
    *,
    configuration_version: int = 1,
):
    persistent = PersistentSnapshotContentCache(
        cache_dir,
        enabled=cache_dir is not None,
        max_bytes=32 * 1024 * 1024,
        max_file_entries=1_000,
        max_tree_entries=16,
    )
    return SnapshotService(
        provider,
        allowed_schemes=("mock",),
        max_workers=8,
        persistent_content_cache=persistent,
        phase_timing_enabled=True,
        reuse_configuration={
            "dataset_layout": {"fixture_version": configuration_version}
        },
    )


def run(snapshot, source_revision: int, target_revision: int, *, reuse=True):
    return snapshot.create_snapshot_at_revisions(
        RECORDS,
        source_id="BRANCH",
        source_revision=source_revision,
        target_id="BRANCH",
        target_revision=target_revision,
        reuse=reuse,
    )


def canonical_snapshot_sha(snapshot) -> str:
    payload = snapshot.model_dump(mode="json", exclude={"captured_at"})
    raw = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode()
    return hashlib.sha256(raw).hexdigest()


def test_persistent_hashes_survive_restart_and_read_only_five_changes(tmp_path):
    cache_dir = tmp_path / ".cache" / "snapshot"

    cold_provider = PersistentFixtureProvider()
    cold_service = service(cold_provider, cache_dir)
    cold = run(cold_service, 100, 200)
    assert cold_provider.list_calls == 2
    assert cold_provider.read_calls == 60
    cold_metrics = cold_service.snapshot_reuse_metrics()
    assert cold_metrics["last_directory_evidence_calls"] == 2
    assert cold_metrics["last_file_reads"] == 60

    hot = run(cold_service, 100, 200)
    assert canonical_snapshot_sha(hot) == canonical_snapshot_sha(cold)
    assert cold_provider.list_calls == 2
    assert cold_provider.read_calls == 60
    assert cold_service.snapshot_reuse_metrics()["last_file_reads"] == 0

    restarted_provider = PersistentFixtureProvider()
    restarted_service = service(restarted_provider, cache_dir)
    restarted = run(restarted_service, 100, 200)
    assert canonical_snapshot_sha(restarted) == canonical_snapshot_sha(cold)
    assert restarted_provider.list_calls == 2
    assert restarted_provider.read_calls == 0
    restart_metrics = restarted_service.snapshot_reuse_metrics()
    assert restart_metrics["last_persistent_hash_hits"] == 60
    assert restart_metrics["last_file_reads"] == 0

    changed_provider = PersistentFixtureProvider()
    changed_service = service(changed_provider, cache_dir)
    changed = run(changed_service, 200, 300)
    assert changed_provider.list_calls == 2
    assert changed_provider.read_calls == 5
    assert all(
        not item.path.endswith("Config054.xlsx")
        for item in changed.target.files
    )
    changed_metrics = changed_service.snapshot_reuse_metrics()
    assert changed_metrics["last_file_reads"] == 5
    assert changed_metrics["last_persistent_hash_hits"] == 56

    baseline_provider = PersistentFixtureProvider()
    baseline = run(service(baseline_provider, None), 200, 300, reuse=False)
    assert canonical_snapshot_sha(changed) == canonical_snapshot_sha(baseline)
    assert not list(cache_dir.rglob("*.tmp"))


def test_persistent_content_single_flight_and_failure_recovery(tmp_path):
    cache = PersistentSnapshotContentCache(
        tmp_path / "snapshot",
        max_bytes=1024 * 1024,
        max_file_entries=32,
        max_tree_entries=4,
    )
    identity = SnapshotFileIdentity(
        repository_uuid="single-flight-repository",
        canonical_url=BRANCH_URL,
        relative_path="Source/Table/Config.xlsx",
        last_changed_revision="100",
        configuration_sha256="a" * 64,
    )
    calls = 0
    lock = Lock()

    def loader():
        nonlocal calls
        with lock:
            calls += 1
        time.sleep(0.03)
        return b"trusted bytes"

    with ThreadPoolExecutor(max_workers=8) as executor:
        results = list(
            executor.map(
                lambda _: cache.get_or_load(
                    identity,
                    expected_size=len(b"trusted bytes"),
                    loader=loader,
                )[0].raw,
                range(8),
            )
        )
    assert results == [b"trusted bytes"] * 8
    assert calls == 1
    assert cache.metrics()["persistent_waits"] == 7

    failed_identity = SnapshotFileIdentity(
        repository_uuid="single-flight-repository",
        canonical_url=BRANCH_URL,
        relative_path="Source/Table/Failed.xlsx",
        last_changed_revision="101",
        configuration_sha256="a" * 64,
    )
    failed_calls = 0

    def failing_loader():
        nonlocal failed_calls
        with lock:
            failed_calls += 1
        time.sleep(0.03)
        raise RuntimeError("fixture failure")

    def fail_once():
        with pytest.raises(RuntimeError, match="fixture failure"):
            cache.get_or_load(
                failed_identity,
                expected_size=None,
                loader=failing_loader,
            )

    with ThreadPoolExecutor(max_workers=4) as executor:
        list(executor.map(lambda _: fail_once(), range(4)))
    assert failed_calls == 1
    recovered, hit = cache.get_or_load(
        failed_identity,
        expected_size=9,
        loader=lambda: b"recovered",
    )
    assert recovered.raw == b"recovered"
    assert hit is False
    assert cache.metrics()["persistent_inflight"] == 0
    assert not list((tmp_path / "snapshot").rglob("*.tmp"))

def test_identity_drift_and_missing_metadata_do_not_reuse_hashes(tmp_path):
    cache_dir = tmp_path / ".cache" / "snapshot"
    original_provider = PersistentFixtureProvider()
    original = run(service(original_provider, cache_dir), 100, 200)

    changed_configuration_provider = PersistentFixtureProvider()
    changed_configuration = run(
        service(
            changed_configuration_provider,
            cache_dir,
            configuration_version=2,
        ),
        100,
        200,
    )
    assert changed_configuration_provider.read_calls == 60
    assert canonical_snapshot_sha(changed_configuration) == canonical_snapshot_sha(
        original
    )

    changed_uuid_provider = PersistentFixtureProvider(
        repository_uuid="replacement-repository-uuid"
    )
    changed_uuid = run(service(changed_uuid_provider, cache_dir), 100, 200)
    assert changed_uuid_provider.read_calls == 60
    assert canonical_snapshot_sha(changed_uuid) == canonical_snapshot_sha(original)

    missing_provider = PersistentFixtureProvider(
        omit_last_changed_revision=True
    )
    missing_service = service(missing_provider, cache_dir)
    missing = run(missing_service, 100, 200)
    missing_baseline_provider = PersistentFixtureProvider(
        omit_last_changed_revision=True
    )
    missing_baseline = run(
        service(missing_baseline_provider, None),
        100,
        200,
        reuse=False,
    )
    assert missing_provider.read_calls == 111
    assert canonical_snapshot_sha(missing) == canonical_snapshot_sha(
        missing_baseline
    )
    assert missing_service.snapshot_reuse_metrics()[
        "persistent_fallback.last_changed_revision_missing"
    ] >= 2


def test_corrupt_blob_and_index_fail_safe_and_recover(tmp_path):
    cache_dir = tmp_path / ".cache" / "snapshot"
    original_provider = PersistentFixtureProvider()
    original = run(service(original_provider, cache_dir), 100, 200)

    blob = next((cache_dir / "blobs").glob("*.bin"))
    blob.write_bytes(b"corrupt")
    repaired_provider = PersistentFixtureProvider()
    repaired = run(service(repaired_provider, cache_dir), 100, 200)
    assert repaired_provider.read_calls == 1
    assert canonical_snapshot_sha(repaired) == canonical_snapshot_sha(original)
    assert hashlib.sha256(blob.read_bytes()).hexdigest() == blob.stem

    (cache_dir / "index.v1.json").write_text(
        "{broken",
        encoding="utf-8",
    )
    corrupt_index_provider = PersistentFixtureProvider()
    corrupt_index_service = service(corrupt_index_provider, cache_dir)
    corrupt_index = run(corrupt_index_service, 100, 200)
    assert corrupt_index_provider.read_calls == 60
    assert canonical_snapshot_sha(corrupt_index) == canonical_snapshot_sha(original)
    assert (
        corrupt_index_service.snapshot_reuse_metrics()["last_fallback_reasons"]
        == "index_corrupt"
    )
    rebuilt_index = json.loads(
        (cache_dir / "index.v1.json").read_text(encoding="utf-8")
    )
    assert rebuilt_index["schema_version"] == 1
    assert not list(cache_dir.rglob("*.tmp"))

    (cache_dir / "index.v1.json").write_text(
        json.dumps(
            {
                "schema_version": 999,
                "facts": {},
                "trees": {},
            }
        ),
        encoding="utf-8",
    )
    newer_provider = PersistentFixtureProvider()
    newer_service = service(newer_provider, cache_dir)
    newer = run(newer_service, 100, 200)
    assert newer_provider.read_calls == 60
    assert canonical_snapshot_sha(newer) == canonical_snapshot_sha(original)
    assert (
        newer_service.snapshot_reuse_metrics()["last_fallback_reasons"]
        == "index_version_newer"
    )
    preserved = json.loads(
        (cache_dir / "index.v1.json").read_text(encoding="utf-8")
    )
    assert preserved["schema_version"] == 999


def test_capacity_prunes_old_facts_and_unreferenced_blobs(tmp_path):
    cache = PersistentSnapshotContentCache(
        tmp_path / "snapshot",
        max_bytes=8,
        max_file_entries=1,
        max_tree_entries=1,
    )
    identities = [
        SnapshotFileIdentity(
            repository_uuid="capacity-repository",
            canonical_url=BRANCH_URL,
            relative_path=f"Source/Table/{name}.xlsx",
            last_changed_revision=str(revision),
            configuration_sha256="b" * 64,
        )
        for name, revision in (("A", 100), ("B", 101))
    ]
    for identity, raw in zip(identities, (b"aaaa", b"bbbb")):
        cache.get_or_load(
            identity,
            expected_size=len(raw),
            loader=lambda raw=raw: raw,
        )
    cache.commit_tree(
        repository_uuid="capacity-repository",
        canonical_url=BRANCH_URL,
        revision=101,
        table_path="Source/Table",
        configuration_sha256="b" * 64,
        files=[
            (identity.relative_path, identity.key)
            for identity in identities
        ],
    )
    metrics = cache.metrics()
    assert metrics["persistent_entries"] == 1
    assert metrics["persistent_trees"] == 1
    assert metrics["persistent_evictions"] == 1
    assert len(list((tmp_path / "snapshot" / "blobs").glob("*.bin"))) == 1
    assert not list((tmp_path / "snapshot").rglob("*.tmp"))
