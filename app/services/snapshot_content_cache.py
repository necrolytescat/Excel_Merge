"""Persistent, reproducible content facts for frozen SVN snapshots."""
from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
import os
from pathlib import Path
import re
import threading
import time
from typing import Callable, Iterable
from uuid import uuid4


INDEX_SCHEMA_VERSION = 1
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
            if version != INDEX_SCHEMA_VERSION:
                raise ValueError("unsupported snapshot cache index version")
            facts = raw.get("facts")
            trees = raw.get("trees")
            if not isinstance(facts, dict) or not isinstance(trees, dict):
                raise ValueError("invalid snapshot cache index")
            self._facts = facts
            self._trees = trees
        except (OSError, UnicodeError, ValueError, TypeError, json.JSONDecodeError):
            self._facts = {}
            self._trees = {}
            self._startup_fallback = "index_corrupt"
            self._counters["persistent_corruptions"] += 1

    @staticmethod
    def _atomic_write(path: Path, raw: bytes) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.parent / f".{path.name}.{uuid4().hex}.tmp"
        try:
            with temporary.open("xb") as stream:
                stream.write(raw)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, path)
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
        try:
            raw = path.read_bytes()
        except OSError:
            raw = b""
            valid = False
        else:
            valid = (
                len(raw) == record["size_bytes"]
                and hashlib.sha256(raw).hexdigest() == record["content_sha256"]
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
    ) -> CachedSnapshotBytes:
        content_sha256 = hashlib.sha256(raw).hexdigest()
        blob_path = self._blob_path(content_sha256)
        try:
            existing = blob_path.read_bytes()
        except OSError:
            existing = None
        if (
            existing is None
            or len(existing) != len(raw)
            or hashlib.sha256(existing).hexdigest() != content_sha256
        ):
            self._atomic_write(blob_path, raw)
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

    def get_or_load(
        self,
        identity: SnapshotFileIdentity,
        *,
        expected_size: int | None,
        loader: Callable[[], bytes],
    ) -> tuple[CachedSnapshotBytes, bool]:
        """Return trusted bytes and whether they came from the persistent cache."""
        if not self.enabled or self._read_only:
            raw = loader()
            return CachedSnapshotBytes(
                fact_key=identity.key,
                content_sha256=hashlib.sha256(raw).hexdigest(),
                size_bytes=len(raw),
                raw=raw,
            ), False
        with self._lock:
            cached = self._lookup_locked(identity, expected_size)
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
                content_sha256=hashlib.sha256(raw).hexdigest(),
                size_bytes=len(raw),
                raw=raw,
            )
            try:
                with self._lock:
                    if not self._read_only:
                        result = self._store_bytes_locked(identity, raw)
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

    @staticmethod
    def tree_key(
        *,
        repository_uuid: str,
        canonical_url: str,
        revision: int,
        table_path: str,
        configuration_sha256: str,
    ) -> str:
        return _hash_json(
            {
                "repository_uuid": repository_uuid,
                "canonical_url": canonical_url,
                "revision": revision,
                "table_path": table_path,
                "configuration_sha256": configuration_sha256,
            }
        )

    def commit_tree(
        self,
        *,
        repository_uuid: str,
        canonical_url: str,
        revision: int,
        table_path: str,
        configuration_sha256: str,
        files: Iterable[tuple[str, str]],
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
        with self._lock:
            try:
                file_map = {
                    path: fact_key
                    for path, fact_key in files
                    if fact_key in self._facts
                }
                self._trees[tree_key] = {
                    "identity": {
                        "repository_uuid": repository_uuid,
                        "canonical_url": canonical_url,
                        "revision": revision,
                        "table_path": table_path,
                        "configuration_sha256": configuration_sha256,
                    },
                    "files": file_map,
                    "last_access_ns": now,
                }
                self._prune_locked()
                self._persist_index_locked()
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
        if not self.enabled or self._read_only:
            return None
        key = self.tree_key(
            repository_uuid=repository_uuid,
            canonical_url=canonical_url,
            revision=revision,
            table_path=table_path,
            configuration_sha256=configuration_sha256,
        )
        with self._lock:
            tree = self._trees.get(key)
            if not isinstance(tree, dict):
                return None
            files = tree.get("files")
            if not isinstance(files, dict):
                return None
            fact_key = files.get(relative_path)
            fact = self._facts.get(fact_key) if isinstance(fact_key, str) else None
            if not isinstance(fact, dict):
                return None
            identity_payload = fact.get("identity")
            if not isinstance(identity_payload, dict):
                return None
            try:
                identity = SnapshotFileIdentity(**identity_payload)
            except TypeError:
                return None
            cached = self._lookup_locked(identity, None)
            if cached is None or cached.fact_key != fact_key:
                return None
            tree["last_access_ns"] = self._wall_clock_ns()
            return cached.raw

    def _persist_index_locked(self) -> None:
        raw = json.dumps(
            {
                "schema_version": INDEX_SCHEMA_VERSION,
                "facts": self._facts,
                "trees": self._trees,
            },
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        self._atomic_write(self._index_path, raw)

    def _prune_locked(self) -> None:
        if self.max_tree_entries and len(self._trees) > self.max_tree_entries:
            ordered_trees = sorted(
                self._trees.items(),
                key=lambda item: int(item[1].get("last_access_ns", 0)),
            )
            for key, _ in ordered_trees[: len(self._trees) - self.max_tree_entries]:
                self._trees.pop(key, None)

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
            self._facts.items(),
            key=lambda item: int(item[1].get("last_access_ns", 0)),
        )
        while ordered_facts and (
            len(self._facts) > self.max_file_entries or total_bytes > self.max_bytes
        ):
            fact_key, _ = ordered_facts.pop(0)
            self._facts.pop(fact_key, None)
            self._drop_fact_references_locked(fact_key)
            self._counters["persistent_evictions"] += 1
            total_bytes, _ = blob_usage()
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

    def record_fallback(self, reason: str) -> None:
        key = f"persistent_fallback.{reason}"
        with self._lock:
            self._counters[key] = self._counters.get(key, 0) + 1

    def metrics(self) -> dict[str, int | str | bool | None]:
        with self._lock:
            return {
                **self._counters,
                "persistent_enabled": self.enabled and not self._read_only,
                "persistent_startup_fallback": self._startup_fallback,
                "persistent_entries": len(self._facts),
                "persistent_trees": len(self._trees),
                "persistent_inflight": len(self._flights),
            }
