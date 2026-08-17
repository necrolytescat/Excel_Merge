"""Persistent, reproducible content facts for frozen SVN snapshots."""
from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass
from enum import Enum
import hashlib
import json
import os
from pathlib import Path
import re
import threading
import time
from typing import Callable, Iterable
from uuid import uuid4

from app.services.snapshot_phase_timing import SnapshotPhaseTiming

INDEX_SCHEMA_VERSION = 2
INDEX_FILENAME = "index.v1.json"
_SHA256 = re.compile(r"^[0-9a-f]{64}$")


@dataclass(frozen=True)
class SnapshotFileIdentity:
    repository_uuid: str
    canonical_url: str
    relative_path: str
    last_changed_revision: str
    configuration_sha256: str

    def payload(self) -> dict[str, str]:
        return {
            "repository_uuid": self.repository_uuid,
            "canonical_url": self.canonical_url,
            "relative_path": self.relative_path,
            "last_changed_revision": self.last_changed_revision,
            "configuration_sha256": self.configuration_sha256,
        }

    @property
    def key(self) -> str:
        return _hash_json(self.payload())


@dataclass(frozen=True)
class CachedSnapshotBytes:
    fact_key: str
    content_sha256: str
    size_bytes: int
    raw: bytes


class FrozenFileState(str, Enum):
    PRESENT = "present"
    MISSING = "missing"
    UNAVAILABLE = "unavailable"


@dataclass(frozen=True)
class FrozenFileLookup:
    state: FrozenFileState
    cached: CachedSnapshotBytes | None = None
    reason: str | None = None


@dataclass(frozen=True)
class SnapshotTreeIdentity:
    repository_uuid: str
    canonical_url: str
    revision: int
    table_path: str
    configuration_sha256: str

    def payload(self) -> dict[str, str | int]:
        return {
            "repository_uuid": self.repository_uuid,
            "canonical_url": self.canonical_url,
            "revision": self.revision,
            "table_path": self.table_path,
            "configuration_sha256": self.configuration_sha256,
        }

    @property
    def key(self) -> str:
        return _hash_json(self.payload())


@dataclass(frozen=True)
class SnapshotTreeLease:
    lease_id: str
    tree_keys: tuple[str, ...]


@dataclass
class _CacheFlight:
    event: threading.Event
    result: CachedSnapshotBytes | None = None
    error: BaseException | None = None


def _hash_json(value: object) -> str:
    raw = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()


def _normalize_tree_path(value: str) -> str:
    normalized = str(value).replace("\\", "/").strip("/")
    parts = [part for part in normalized.split("/") if part]
    if not parts or any(part in {".", ".."} for part in parts):
        raise ValueError("invalid frozen tree path")
    return "/".join(parts)


def _paths_sha256(paths: Iterable[str]) -> str:
    normalized = sorted(paths, key=lambda item: (item.casefold(), item))
    return _hash_json(normalized)


class PersistentSnapshotContentCache:
    """Versioned file-fact index backed by content-addressed byte blobs."""

    def __init__(
        self,
        root: Path | None,
        *,
        enabled: bool = True,
        max_bytes: int = 2 * 1024 * 1024 * 1024,
        max_file_entries: int = 20_000,
        max_tree_entries: int = 256,
        wall_clock_ns: Callable[[], int] = time.time_ns,
    ) -> None:
        self.root = root.resolve() if root is not None else None
        self.enabled = bool(enabled and root is not None)
        self.max_bytes = max(0, int(max_bytes))
        self.max_file_entries = max(0, int(max_file_entries))
        self.max_tree_entries = max(0, int(max_tree_entries))
        self._wall_clock_ns = wall_clock_ns
        self._lock = threading.RLock()
        self._flights: dict[str, _CacheFlight] = {}
        self._facts: dict[str, dict] = {}
        self._trees: dict[str, dict] = {}
        self._leases: dict[str, tuple[str, ...]] = {}
        self._pinned_tree_counts: dict[str, int] = {}
        self._startup_fallback: str | None = None
        self._read_only = False
        self._counters: dict[str, int] = {
            "persistent_hash_hits": 0,
            "disk_byte_hits": 0,
            "disk_bytes": 0,
            "persistent_misses": 0,
            "persistent_writes": 0,
            "persistent_waits": 0,
            "persistent_evictions": 0,
            "persistent_corruptions": 0,
            "persistent_write_failures": 0,
            "persistent_temp_cleanups": 0,
            "persistent_aliases": 0,
            "persistent_capacity_deferred": 0,
            "persistent_complete_trees": 0,
        }
        if not self.enabled:
            return
        if not self._safe_root() or not self.max_bytes or not self.max_file_entries:
            self.enabled = False
            self._startup_fallback = "unsafe_or_disabled"
            return
        self._load_index()

    def _safe_root(self) -> bool:
        root = self.root
        if root is None or root.parent == root:
            return False
        return root.name.casefold() not in {
            ".git",
            "m2-batch",
            "m2-fixtures",
            "m4-diff-plan",
        }

    @property
    def _index_path(self) -> Path:
        assert self.root is not None
        return self.root / INDEX_FILENAME

    @property
    def _blob_root(self) -> Path:
        assert self.root is not None
        return self.root / "blobs"

    def _load_index(self) -> None:
        assert self.root is not None
        path = self._index_path
        if not path.exists():
            return
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
            version = raw.get("schema_version")
            if isinstance(version, int) and version > INDEX_SCHEMA_VERSION:
                self._read_only = True
                self._startup_fallback = "index_version_newer"
                return
            if version not in {1, INDEX_SCHEMA_VERSION}:
                raise ValueError("unsupported snapshot cache index version")
            facts = raw.get("facts")
            trees = raw.get("trees")
            if not isinstance(facts, dict) or not isinstance(trees, dict):
                raise ValueError("invalid snapshot cache index")
            self._facts = facts
            if version == 1:
                migrated: dict[str, dict] = {}
                for key, value in trees.items():
                    if not isinstance(value, dict):
                        continue
                    files = value.get("files")
                    if not isinstance(files, dict):
                        files = {}
                    required = sorted(
                        (str(path) for path in files),
                        key=lambda item: (item.casefold(), item),
                    )
                    migrated[key] = {
                        **value,
                        "tree_kind": "legacy",
                        "complete": False,
                        "required_paths": required,
                        "required_paths_sha256": _paths_sha256(required),
                        "missing_paths": [],
                    }
                self._trees = migrated
            else:
                self._trees = trees
        except (OSError, UnicodeError, ValueError, TypeError, json.JSONDecodeError):
            self._facts = {}
            self._trees = {}
            self._startup_fallback = "index_corrupt"
            self._counters["persistent_corruptions"] += 1

    @contextmanager
    def _locked(
        self,
        timing: SnapshotPhaseTiming | None,
        side: str | None,
        stage: str,
    ):
        if timing is None:
            with self._lock:
                yield
            return
        with timing.phase(f"{stage}.lock_wait", side=side):
            self._lock.acquire()
        try:
            with timing.phase(f"{stage}.lock_held", side=side):
                yield
        finally:
            self._lock.release()

    @staticmethod
    def _sha256(
        raw: bytes,
        *,
        timing: SnapshotPhaseTiming | None,
        side: str | None,
        kind: str,
    ) -> str:
        if timing is None:
            return hashlib.sha256(raw).hexdigest()
        with timing.phase(f"sha256.{kind}", side=side) as observation:
            result = hashlib.sha256(raw).hexdigest()
            observation.result(bytes_count=len(raw), items=1)
            return result

    def _atomic_write(
        self,
        path: Path,
        raw: bytes,
        *,
        artifact: str,
        timing: SnapshotPhaseTiming | None,
        side: str | None,
    ) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.parent / f".{path.name}.{uuid4().hex}.tmp"
        try:
            if timing is None:
                stream = temporary.open("xb")
            else:
                with timing.phase(f"{artifact}.temp_open", side=side):
                    stream = temporary.open("xb")
            with stream:
                if timing is None:
                    stream.write(raw)
                else:
                    with timing.phase(
                        f"{artifact}.temp_write", side=side
                    ) as observation:
                        stream.write(raw)
                        observation.result(bytes_count=len(raw))
                if timing is None:
                    stream.flush()
                else:
                    with timing.phase(f"{artifact}.flush", side=side) as observation:
                        stream.flush()
                        observation.result(bytes_count=len(raw))
                if timing is None:
                    os.fsync(stream.fileno())
                else:
                    with timing.phase(f"{artifact}.fsync", side=side) as observation:
                        os.fsync(stream.fileno())
                        observation.result(bytes_count=len(raw))
            if timing is None:
                os.replace(temporary, path)
            else:
                with timing.phase(f"{artifact}.replace", side=side) as observation:
                    os.replace(temporary, path)
                    observation.result(bytes_count=len(raw))
        finally:
            try:
                temporary.unlink()
            except FileNotFoundError:
                pass

    def _blob_path(self, content_sha256: str) -> Path:
        if not _SHA256.fullmatch(content_sha256):
            raise ValueError("invalid snapshot content hash")
        return self._blob_root / f"{content_sha256}.bin"

    def _valid_fact_record(
        self,
        identity: SnapshotFileIdentity,
        record: object,
        expected_size: int | None,
    ) -> tuple[dict, Path] | None:
        if not isinstance(record, dict) or record.get("identity") != identity.payload():
            return None
        content_sha256 = record.get("content_sha256")
        size_bytes = record.get("size_bytes")
        if (
            not isinstance(content_sha256, str)
            or not _SHA256.fullmatch(content_sha256)
            or not isinstance(size_bytes, int)
            or isinstance(size_bytes, bool)
            or size_bytes < 0
            or (expected_size is not None and size_bytes != expected_size)
        ):
            return None
        try:
            path = self._blob_path(content_sha256)
        except ValueError:
            return None
        return record, path

    def _lookup_locked(
        self,
        identity: SnapshotFileIdentity,
        expected_size: int | None,
        timing: SnapshotPhaseTiming | None,
        side: str | None,
    ) -> CachedSnapshotBytes | None:
        record_and_path = self._valid_fact_record(
            identity,
            self._facts.get(identity.key),
            expected_size,
        )
        if record_and_path is None:
            self._counters["persistent_misses"] += 1
            return None
        record, path = record_and_path
        if timing is None:
            try:
                raw = path.read_bytes()
            except OSError:
                raw = b""
                valid = False
            else:
                valid = (
                    len(raw) == record["size_bytes"]
                    and self._sha256(
                        raw,
                        timing=None,
                        side=side,
                        kind="persistent_validation",
                    ) == record["content_sha256"]
                )
        else:
            with timing.phase("blob.read", side=side) as observation:
                try:
                    raw = path.read_bytes()
                except OSError:
                    raw = b""
                    valid = False
                    observation.result(source="missing")
                else:
                    observation.result(bytes_count=len(raw), source="disk")
                    valid = (
                        len(raw) == record["size_bytes"]
                        and self._sha256(
                            raw,
                            timing=timing,
                            side=side,
                            kind="persistent_validation",
                        ) == record["content_sha256"]
                    )
        if not valid:
            self._facts.pop(identity.key, None)
            self._drop_fact_references_locked(identity.key)
            self._counters["persistent_corruptions"] += 1
            self._counters["persistent_misses"] += 1
            return None
        record["last_access_ns"] = self._wall_clock_ns()
        self._counters["persistent_hash_hits"] += 1
        self._counters["disk_byte_hits"] += 1
        self._counters["disk_bytes"] += len(raw)
        return CachedSnapshotBytes(
            fact_key=identity.key,
            content_sha256=record["content_sha256"],
            size_bytes=record["size_bytes"],
            raw=raw,
        )

    def _drop_fact_references_locked(self, fact_key: str) -> None:
        for tree in self._trees.values():
            files = tree.get("files") if isinstance(tree, dict) else None
            if not isinstance(files, dict):
                continue
            for path, referenced in list(files.items()):
                if referenced == fact_key:
                    files.pop(path, None)

    def _store_bytes_locked(
        self,
        identity: SnapshotFileIdentity,
        raw: bytes,
        timing: SnapshotPhaseTiming | None,
        side: str | None,
    ) -> CachedSnapshotBytes:
        content_sha256 = self._sha256(
            raw,
            timing=timing,
            side=side,
            kind="persistent_store",
        )
        blob_path = self._blob_path(content_sha256)
        if timing is None:
            try:
                existing = blob_path.read_bytes()
            except OSError:
                existing = None
        else:
            with timing.phase("blob.existing_read", side=side) as observation:
                try:
                    existing = blob_path.read_bytes()
                except OSError:
                    existing = None
                    observation.result(source="missing")
                else:
                    observation.result(bytes_count=len(existing), source="disk")
        if (
            existing is None
            or len(existing) != len(raw)
            or self._sha256(
                existing,
                timing=timing,
                side=side,
                kind="blob_existing_validation",
            ) != content_sha256
        ):
            self._atomic_write(
                blob_path,
                raw,
                artifact="blob",
                timing=timing,
                side=side,
            )
        now = self._wall_clock_ns()
        self._facts[identity.key] = {
            "identity": identity.payload(),
            "content_sha256": content_sha256,
            "size_bytes": len(raw),
            "last_access_ns": now,
        }
        self._counters["persistent_writes"] += 1
        return CachedSnapshotBytes(
            fact_key=identity.key,
            content_sha256=content_sha256,
            size_bytes=len(raw),
            raw=raw,
        )

    def may_have(
        self,
        identity: SnapshotFileIdentity,
        *,
        expected_size: int | None,
        timing: SnapshotPhaseTiming | None = None,
        side: str | None = None,
    ) -> bool:
        """Cheap advisory probe used only to decide whether bulk export is useful."""
        if not self.enabled or self._read_only:
            return False
        with self._locked(timing, side, "persistent.probe"):
            def probe() -> bool:
                record_and_path = self._valid_fact_record(
                    identity,
                    self._facts.get(identity.key),
                    expected_size,
                )
                if record_and_path is None:
                    return False
                record, path = record_and_path
                try:
                    return (
                        not path.is_symlink()
                        and path.is_file()
                        and path.stat().st_size == record["size_bytes"]
                    )
                except OSError:
                    return False

            if timing is None:
                return probe()
            with timing.phase("persistent.probe", side=side) as observation:
                present = probe()
                observation.result(source="possible_hit" if present else "miss")
                return present

    def get_or_load(
        self,
        identity: SnapshotFileIdentity,
        *,
        expected_size: int | None,
        loader: Callable[[], bytes],
        timing: SnapshotPhaseTiming | None = None,
        side: str | None = None,
    ) -> tuple[CachedSnapshotBytes, bool]:
        """Return trusted bytes and whether they came from the persistent cache."""
        if not self.enabled or self._read_only:
            raw = loader()
            return CachedSnapshotBytes(
                fact_key=identity.key,
                content_sha256=self._sha256(
                    raw,
                    timing=timing,
                    side=side,
                    kind="uncached_content",
                ),
                size_bytes=len(raw),
                raw=raw,
            ), False
        with self._locked(timing, side, "persistent.lookup"):
            if timing is None:
                cached = self._lookup_locked(
                    identity,
                    expected_size,
                    timing,
                    side,
                )
            else:
                with timing.phase("persistent.lookup", side=side) as observation:
                    cached = self._lookup_locked(
                        identity,
                        expected_size,
                        timing,
                        side,
                    )
                    observation.result(
                        bytes_count=cached.size_bytes if cached else 0,
                        source="hit" if cached else "miss",
                    )
            if cached is not None:
                return cached, True
            flight = self._flights.get(identity.key)
            if flight is None:
                flight = _CacheFlight(event=threading.Event())
                self._flights[identity.key] = flight
                builder = True
            else:
                self._counters["persistent_waits"] += 1
                builder = False
        if not builder:
            if timing is None:
                flight.event.wait()
            else:
                with timing.phase("persistent.singleflight_wait", side=side):
                    flight.event.wait()
            if flight.error is not None:
                raise flight.error
            if flight.result is None:
                raise RuntimeError("snapshot content single-flight completed without result")
            return flight.result, True
        try:
            raw = loader()
            result = CachedSnapshotBytes(
                fact_key=identity.key,
                content_sha256=self._sha256(
                    raw,
                    timing=timing,
                    side=side,
                    kind="loaded_content",
                ),
                size_bytes=len(raw),
                raw=raw,
            )
            try:
                with self._locked(timing, side, "blob"):
                    if not self._read_only:
                        result = self._store_bytes_locked(
                            identity,
                            raw,
                            timing,
                            side,
                        )
            except OSError:
                with self._lock:
                    self._counters["persistent_write_failures"] += 1
            with self._lock:
                flight.result = result
            return result, False
        except BaseException as exc:
            with self._lock:
                flight.error = exc
            raise
        finally:
            with self._lock:
                self._flights.pop(identity.key, None)
                flight.event.set()

    def alias_verified_fact(
        self,
        source_identity: SnapshotFileIdentity,
        target_identity: SnapshotFileIdentity,
        *,
        expected_size: int | None = None,
        timing: SnapshotPhaseTiming | None = None,
        side: str | None = None,
    ) -> CachedSnapshotBytes | None:
        if not self.enabled or self._read_only:
            return None
        with self._locked(timing, side, "persistent.alias"):
            existing = self._lookup_locked(
                target_identity, expected_size, timing, side
            )
            if existing is not None:
                return existing
            source = self._lookup_locked(
                source_identity, expected_size, timing, side
            )
            if source is None:
                return None
            now = self._wall_clock_ns()
            self._facts[target_identity.key] = {
                "identity": target_identity.payload(),
                "content_sha256": source.content_sha256,
                "size_bytes": source.size_bytes,
                "last_access_ns": now,
            }
            self._counters["persistent_aliases"] += 1
            return CachedSnapshotBytes(
                fact_key=target_identity.key,
                content_sha256=source.content_sha256,
                size_bytes=source.size_bytes,
                raw=source.raw,
            )

    @staticmethod
    def _normalized_paths(values: Iterable[str]) -> set[str]:
        result: dict[str, str] = {}
        for value in values:
            path = _normalize_tree_path(value)
            folded = path.casefold()
            previous = result.get(folded)
            if previous is not None and previous != path:
                raise ValueError("frozen tree paths are ambiguous")
            result[folded] = path
        return set(result.values())

    def commit_complete_tree(
        self,
        identity: SnapshotTreeIdentity,
        *,
        tree_kind: str,
        required_paths: Iterable[str],
        files: Iterable[tuple[str, str]],
        missing_paths: Iterable[str] = (),
        timing: SnapshotPhaseTiming | None = None,
        side: str | None = None,
    ) -> bool:
        if not self.enabled or self._read_only:
            return False
        if tree_kind not in {"excel", "tablecsv"}:
            return False
        try:
            required = self._normalized_paths(required_paths)
            missing = self._normalized_paths(missing_paths)
            file_map: dict[str, str] = {}
            folded_files: dict[str, str] = {}
            for raw_path, fact_key in files:
                path = _normalize_tree_path(raw_path)
                folded = path.casefold()
                previous = folded_files.get(folded)
                if previous is not None and previous != path:
                    raise ValueError("frozen tree files are ambiguous")
                if path in file_map and file_map[path] != fact_key:
                    raise ValueError("frozen tree file has conflicting facts")
                folded_files[folded] = path
                file_map[path] = str(fact_key)
            if set(file_map) & missing:
                raise ValueError("present and missing paths overlap")
            if required != set(file_map) | missing:
                raise ValueError("required paths are not completely partitioned")
        except (TypeError, ValueError):
            return False

        now = self._wall_clock_ns()
        key = identity.key
        with self._locked(timing, side, "index.complete"):
            for path, fact_key in file_map.items():
                fact = self._facts.get(fact_key)
                if not isinstance(fact, dict):
                    return False
                payload = fact.get("identity")
                if not isinstance(payload, dict):
                    return False
                try:
                    fact_path = _normalize_tree_path(str(payload["relative_path"]))
                except (KeyError, ValueError):
                    return False
                if fact_path != path:
                    return False
                record_and_path = self._valid_fact_record(
                    SnapshotFileIdentity(**payload), fact, None
                )
                if record_and_path is None:
                    return False
            previous = self._trees.get(key)
            if isinstance(previous, dict) and bool(previous.get("complete")):
                if (
                    previous.get("identity") != identity.payload()
                    or previous.get("tree_kind") != tree_kind
                ):
                    return False
                previous_files = previous.get("files")
                previous_missing = previous.get("missing_paths")
                previous_required = previous.get("required_paths")
                if not (
                    isinstance(previous_files, dict)
                    and isinstance(previous_missing, list)
                    and isinstance(previous_required, list)
                ):
                    return False
                if any(
                    path in previous_missing
                    or (
                        path in previous_files
                        and previous_files[path] != fact_key
                    )
                    for path, fact_key in file_map.items()
                ):
                    return False
                if any(path in previous_files for path in missing):
                    return False
                try:
                    previous_file_map = {
                        str(path): str(fact_key)
                        for path, fact_key in previous_files.items()
                    }
                    combined_file_paths = self._normalized_paths(
                        [*previous_file_map, *file_map]
                    )
                    required = self._normalized_paths(
                        [*previous_required, *required]
                    )
                    missing = self._normalized_paths(
                        [*previous_missing, *missing]
                    )
                except (TypeError, ValueError):
                    return False
                if (
                    {path.casefold() for path in combined_file_paths}
                    & {path.casefold() for path in missing}
                ):
                    return False
                if required != combined_file_paths | missing:
                    return False
                file_map = {**previous_file_map, **file_map}
            self._trees[key] = {
                "identity": identity.payload(),
                "tree_kind": tree_kind,
                "complete": True,
                "required_paths": sorted(
                    required, key=lambda item: (item.casefold(), item)
                ),
                "required_paths_sha256": _paths_sha256(required),
                "files": dict(
                    sorted(
                        file_map.items(),
                        key=lambda item: (item[0].casefold(), item[0]),
                    )
                ),
                "missing_paths": sorted(
                    missing, key=lambda item: (item.casefold(), item)
                ),
                "last_access_ns": now,
            }
            self._pinned_tree_counts[key] = (
                self._pinned_tree_counts.get(key, 0) + 1
            )
            try:
                self._prune_locked()
                self._persist_index_locked(timing=timing, side=side)
            except Exception:
                if previous is None:
                    self._trees.pop(key, None)
                else:
                    self._trees[key] = previous
                self._counters["persistent_write_failures"] += 1
                return False
            finally:
                remaining = self._pinned_tree_counts.get(key, 1) - 1
                if remaining > 0:
                    self._pinned_tree_counts[key] = remaining
                else:
                    self._pinned_tree_counts.pop(key, None)
            self._counters["persistent_complete_trees"] += 1
            return True

    def lookup_tree_file(
        self,
        identity: SnapshotTreeIdentity,
        *,
        relative_path: str,
    ) -> FrozenFileLookup:
        if not self.enabled or self._read_only:
            return FrozenFileLookup(
                FrozenFileState.UNAVAILABLE, reason="cache_disabled"
            )
        try:
            path = _normalize_tree_path(relative_path)
        except ValueError:
            return FrozenFileLookup(
                FrozenFileState.UNAVAILABLE, reason="invalid_path"
            )
        with self._lock:
            tree = self._trees.get(identity.key)
            if not isinstance(tree, dict):
                return FrozenFileLookup(
                    FrozenFileState.UNAVAILABLE, reason="tree_unavailable"
                )
            files = tree.get("files")
            if not isinstance(files, dict):
                return FrozenFileLookup(
                    FrozenFileState.UNAVAILABLE, reason="tree_invalid"
                )
            required = tree.get("required_paths")
            missing = tree.get("missing_paths")
            if not isinstance(required, list) or not isinstance(missing, list):
                return FrozenFileLookup(
                    FrozenFileState.UNAVAILABLE, reason="tree_invalid"
                )
            if path not in files and path not in missing:
                matches = {
                    candidate
                    for candidate in [*files, *missing]
                    if str(candidate).casefold() == path.casefold()
                }
                if len(matches) > 1:
                    return FrozenFileLookup(
                        FrozenFileState.UNAVAILABLE, reason="path_ambiguous"
                    )
                if len(matches) == 1:
                    path = matches.pop()
            fact_key = files.get(path)
            if isinstance(fact_key, str):
                fact = self._facts.get(fact_key)
                payload = fact.get("identity") if isinstance(fact, dict) else None
                if isinstance(payload, dict):
                    try:
                        file_identity = SnapshotFileIdentity(**payload)
                    except TypeError:
                        file_identity = None
                    if file_identity is not None:
                        cached = self._lookup_locked(
                            file_identity, None, None, None
                        )
                        if cached is not None and cached.fact_key == fact_key:
                            tree["last_access_ns"] = self._wall_clock_ns()
                            return FrozenFileLookup(
                                FrozenFileState.PRESENT, cached=cached
                            )
                return FrozenFileLookup(
                    FrozenFileState.UNAVAILABLE, reason="fact_unavailable"
                )
            if not bool(tree.get("complete")):
                return FrozenFileLookup(
                    FrozenFileState.UNAVAILABLE, reason="tree_incomplete"
                )
            if path in required and path in missing:
                tree["last_access_ns"] = self._wall_clock_ns()
                return FrozenFileLookup(FrozenFileState.MISSING)
            return FrozenFileLookup(
                FrozenFileState.UNAVAILABLE, reason="path_not_required"
            )

    def acquire_tree_lease(
        self,
        identities: Iterable[SnapshotTreeIdentity],
        *,
        lease_id: str | None = None,
    ) -> SnapshotTreeLease | None:
        keys = tuple(dict.fromkeys(identity.key for identity in identities))
        if not keys or not self.enabled or self._read_only:
            return None
        resolved_lease_id = lease_id or uuid4().hex
        with self._lock:
            existing = self._leases.get(resolved_lease_id)
            if existing is not None:
                return (
                    SnapshotTreeLease(resolved_lease_id, existing)
                    if existing == keys
                    else None
                )
            for key in keys:
                tree = self._trees.get(key)
                if not isinstance(tree, dict) or not bool(tree.get("complete")):
                    return None
            self._leases[resolved_lease_id] = keys
            for key in keys:
                self._pinned_tree_counts[key] = (
                    self._pinned_tree_counts.get(key, 0) + 1
                )
            return SnapshotTreeLease(resolved_lease_id, keys)

    def release_tree_lease(self, lease: SnapshotTreeLease) -> bool:
        with self._lock:
            keys = self._leases.pop(lease.lease_id, None)
            if keys is None:
                return True
            for key in keys:
                remaining = self._pinned_tree_counts.get(key, 1) - 1
                if remaining > 0:
                    self._pinned_tree_counts[key] = remaining
                else:
                    self._pinned_tree_counts.pop(key, None)
            try:
                self._prune_locked()
                self._persist_index_locked(timing=None, side=None)
            except Exception:
                self._counters["persistent_write_failures"] += 1
                return False
            return True

    @staticmethod
    def tree_key(
        *,
        repository_uuid: str,
        canonical_url: str,
        revision: int,
        table_path: str,
        configuration_sha256: str,
    ) -> str:
        return SnapshotTreeIdentity(
            repository_uuid=repository_uuid,
            canonical_url=canonical_url,
            revision=revision,
            table_path=table_path,
            configuration_sha256=configuration_sha256,
        ).key

    def commit_tree(
        self,
        *,
        repository_uuid: str,
        canonical_url: str,
        revision: int,
        table_path: str,
        configuration_sha256: str,
        files: Iterable[tuple[str, str]],
        timing: SnapshotPhaseTiming | None = None,
        side: str | None = None,
    ) -> bool:
        """Atomically publish one complete frozen tree after snapshot construction."""
        if not self.enabled or self._read_only:
            return False
        now = self._wall_clock_ns()
        tree_key = self.tree_key(
            repository_uuid=repository_uuid,
            canonical_url=canonical_url,
            revision=revision,
            table_path=table_path,
            configuration_sha256=configuration_sha256,
        )
        with self._locked(timing, side, "index"):
            try:
                if timing is None:
                    file_map = {
                        path: fact_key
                        for path, fact_key in files
                        if fact_key in self._facts
                    }
                else:
                    with timing.phase("index.tree_build", side=side) as observation:
                        file_map = {
                            path: fact_key
                            for path, fact_key in files
                            if fact_key in self._facts
                        }
                        observation.result(items=len(file_map))
                self._trees[tree_key] = {
                    "identity": {
                        "repository_uuid": repository_uuid,
                        "canonical_url": canonical_url,
                        "revision": revision,
                        "table_path": table_path,
                        "configuration_sha256": configuration_sha256,
                    },
                    "tree_kind": "legacy",
                    "complete": False,
                    "required_paths": sorted(file_map),
                    "required_paths_sha256": _paths_sha256(file_map),
                    "missing_paths": [],
                    "files": file_map,
                    "last_access_ns": now,
                }
                if timing is None:
                    self._prune_locked()
                else:
                    with timing.phase("index.prune", side=side):
                        self._prune_locked()
                self._persist_index_locked(timing=timing, side=side)
            except Exception:
                self._counters["persistent_write_failures"] += 1
                return False
        return True

    def read_tree_bytes(
        self,
        *,
        repository_uuid: str,
        canonical_url: str,
        revision: int,
        table_path: str,
        configuration_sha256: str,
        relative_path: str,
    ) -> bytes | None:
        lookup = self.lookup_tree_file(
            SnapshotTreeIdentity(
                repository_uuid=repository_uuid,
                canonical_url=canonical_url,
                revision=revision,
                table_path=table_path,
                configuration_sha256=configuration_sha256,
            ),
            relative_path=relative_path,
        )
        return (
            lookup.cached.raw
            if lookup.state is FrozenFileState.PRESENT and lookup.cached
            else None
        )

    def _persist_index_locked(
        self,
        *,
        timing: SnapshotPhaseTiming | None,
        side: str | None,
    ) -> None:
        def serialize() -> bytes:
            return json.dumps(
                {
                    "schema_version": INDEX_SCHEMA_VERSION,
                    "facts": self._facts,
                    "trees": self._trees,
                },
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")

        if timing is None:
            raw = serialize()
        else:
            with timing.phase("index.serialize", side=side) as observation:
                raw = serialize()
                observation.result(bytes_count=len(raw), items=1)
        self._atomic_write(
            self._index_path,
            raw,
            artifact="index",
            timing=timing,
            side=side,
        )

    def _prune_locked(self) -> None:
        pinned_trees = {
            key
            for key, count in self._pinned_tree_counts.items()
            if count > 0
        }
        if self.max_tree_entries and len(self._trees) > self.max_tree_entries:
            ordered_trees = sorted(
                (
                    item
                    for item in self._trees.items()
                    if item[0] not in pinned_trees
                ),
                key=lambda item: int(item[1].get("last_access_ns", 0)),
            )
            excess = len(self._trees) - self.max_tree_entries
            for key, _ in ordered_trees[:excess]:
                self._trees.pop(key, None)

        pinned_facts: set[str] = set()
        for key in pinned_trees:
            tree = self._trees.get(key)
            files = tree.get("files") if isinstance(tree, dict) else None
            if isinstance(files, dict):
                pinned_facts.update(
                    value for value in files.values() if isinstance(value, str)
                )

        def blob_usage() -> tuple[int, dict[str, int]]:
            blobs: dict[str, int] = {}
            for fact in self._facts.values():
                if not isinstance(fact, dict):
                    continue
                content_hash = fact.get("content_sha256")
                size_bytes = fact.get("size_bytes")
                if isinstance(content_hash, str) and isinstance(size_bytes, int):
                    blobs[content_hash] = size_bytes
            return sum(blobs.values()), blobs

        total_bytes, _ = blob_usage()
        ordered_facts = sorted(
            (
                item
                for item in self._facts.items()
                if item[0] not in pinned_facts
            ),
            key=lambda item: int(item[1].get("last_access_ns", 0)),
        )
        while ordered_facts and (
            len(self._facts) > self.max_file_entries
            or total_bytes > self.max_bytes
        ):
            fact_key, _ = ordered_facts.pop(0)
            self._facts.pop(fact_key, None)
            self._drop_fact_references_locked(fact_key)
            self._counters["persistent_evictions"] += 1
            total_bytes, _ = blob_usage()
        if (
            (self.max_tree_entries and len(self._trees) > self.max_tree_entries)
            or len(self._facts) > self.max_file_entries
            or total_bytes > self.max_bytes
        ):
            self._counters["persistent_capacity_deferred"] += 1
        self._remove_unreferenced_files_locked()

    def _remove_unreferenced_files_locked(self) -> None:
        if self.root is None:
            return
        referenced = {
            str(fact.get("content_sha256"))
            for fact in self._facts.values()
            if isinstance(fact, dict)
            and isinstance(fact.get("content_sha256"), str)
        }
        if self._blob_root.exists():
            for path in self._blob_root.iterdir():
                if path.is_symlink() or not path.is_file():
                    continue
                if path.name.startswith(".") and path.name.endswith(".tmp"):
                    try:
                        path.unlink()
                        self._counters["persistent_temp_cleanups"] += 1
                    except OSError:
                        pass
                    continue
                match = re.fullmatch(r"([0-9a-f]{64})\.bin", path.name)
                if match and match.group(1) not in referenced:
                    try:
                        path.unlink()
                    except OSError:
                        pass
        if self.root.exists():
            for path in self.root.iterdir():
                if (
                    path.is_file()
                    and not path.is_symlink()
                    and path.name.startswith(".")
                    and path.name.endswith(".tmp")
                ):
                    try:
                        path.unlink()
                        self._counters["persistent_temp_cleanups"] += 1
                    except OSError:
                        pass

    def record_fallback(
        self,
        reason: str,
        *,
        timing: SnapshotPhaseTiming | None = None,
        side: str | None = None,
    ) -> None:
        key = f"persistent_fallback.{reason}"
        with self._lock:
            self._counters[key] = self._counters.get(key, 0) + 1
        if timing is not None:
            timing.increment(key, side=side)

    def metrics(self) -> dict[str, int | str | bool | None]:
        with self._lock:
            return {
                **self._counters,
                "persistent_enabled": self.enabled and not self._read_only,
                "persistent_startup_fallback": self._startup_fallback,
                "persistent_entries": len(self._facts),
                "persistent_trees": len(self._trees),
                "persistent_inflight": len(self._flights),
                "persistent_leases": len(self._leases),
                "persistent_pinned_trees": len(self._pinned_tree_counts),
            }
