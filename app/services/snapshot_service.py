"""M1 双端点全量 Excel 快照服务。

用户只选择两个已注册端点；服务在任务开始时分别冻结 HEAD，
然后读取 TABLE 逻辑目录绑定的全部 Excel 文件。该层不执行 Diff。
"""
from __future__ import annotations

from collections import OrderedDict
from contextlib import nullcontext
from concurrent.futures import Future, ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import json
import logging
import re
import threading
import time
from typing import Any, Callable, Mapping
from uuid import uuid4

from core.models import EndpointSpec, TreeEntry
from core.svn_history import (
    BranchIdentity,
    FrozenTreeDiff,
    canonicalize_svn_url,
    normalize_repository_path,
    repository_path_from_urls,
)
from core.svn_provider import SVNProvider, SVNProviderError, normalize_relative_path, validate_endpoint
from app.services.snapshot_content_cache import (
    PersistentSnapshotContentCache,
    SnapshotFileIdentity,
)
from app.services.snapshot_phase_timing import SnapshotPhaseTiming
from app.schemas.svn import (
    EndpointRecordPayload,
    SnapshotEndpointPayload,
    SnapshotFilePayload,
    SnapshotResponsePayload,
    SnapshotStatsPayload,
)


LOGICAL_SCOPES = ("TABLE",)
EXCEL_EXTENSIONS = (".xlsx", ".xlsm", ".xls")
_SNAPSHOT_SIDE_WORKERS = 2
_SNAPSHOT_REUSE_RULESET = "m1.table-excel-snapshot-facts.v1"
_SHA256_PATTERN = re.compile(r"^[0-9a-f]{64}$")


logger = logging.getLogger(__name__)


@dataclass
class _TrustedSnapshotEntry:
    snapshot: SnapshotResponsePayload
    facts_sha256: str
    expires_at: float


@dataclass
class _SnapshotBuildFlight:
    event: threading.Event
    build_context_id: str
    result: SnapshotResponsePayload | None = None
    error: BaseException | None = None


class _CrossBranchEvidenceError(RuntimeError):
    def __init__(self, reason: str):
        self.reason = reason


class SnapshotService:
    def __init__(
        self,
        provider: SVNProvider,
        *,
        allowed_schemes: tuple[str, ...],
        max_workers: int = 6,
        content_read_workers: int | None = None,
        bulk_export_enabled: bool = True,
        bulk_export_min_files: int = 8,
        preview_limit: int = 262144,
        reuse_ttl_seconds: float = 300,
        reuse_max_entries: int = 8,
        reuse_configuration: Mapping[str, Any] | None = None,
        persistent_content_cache: PersistentSnapshotContentCache | None = None,
        monotonic_clock: Callable[[], float] = time.monotonic,
        phase_timing_enabled: bool = False,
        phase_timing_sink: Callable[[dict[str, Any]], None] | None = None,
    ):
        self.provider = provider
        self.allowed_schemes = allowed_schemes
        self.max_workers = max(1, int(max_workers))
        configured_content_workers = (
            content_read_workers if content_read_workers is not None else max_workers
        )
        self.content_read_workers = max(1, int(configured_content_workers))
        self.bulk_export_enabled = bool(bulk_export_enabled)
        self.bulk_export_min_files = max(1, int(bulk_export_min_files))
        self.preview_limit = max(1, int(preview_limit))
        self._content_cache: dict[tuple[str, str, str, str], bytes] = {}
        self._cache_lock = threading.RLock()
        self._reuse_ttl_seconds = max(0.0, float(reuse_ttl_seconds))
        self._reuse_max_entries = max(0, int(reuse_max_entries))
        self._monotonic_clock = monotonic_clock
        self._persistent_content_cache = persistent_content_cache
        self._phase_timing_enabled = bool(phase_timing_enabled)
        self._phase_timing_sink = phase_timing_sink
        self._repository_identity_cache: dict[tuple[str, int], str] = {}
        self._last_phase_metrics: dict[str, int | float | str] = {}
        self._reuse_configuration_sha256 = self._hash_json(
            {
                "ruleset": _SNAPSHOT_REUSE_RULESET,
                "logical_scopes": list(LOGICAL_SCOPES),
                "excel_extensions": list(EXCEL_EXTENSIONS),
                "preview_limit": self.preview_limit,
                "configuration": dict(reuse_configuration or {}),
            }
        )
        self._snapshot_fact_cache: OrderedDict[str, _TrustedSnapshotEntry] = OrderedDict()
        self._snapshot_build_flights: dict[str, _SnapshotBuildFlight] = {}
        self._snapshot_reuse_counters = {
            "hits": 0,
            "misses": 0,
            "builds": 0,
            "waits": 0,
            "expired": 0,
            "invalid": 0,
            "evicted": 0,
            "incremental_pairs": 0,
            "incremental_reused_files": 0,
            "incremental_fallbacks": 0,
            "cross_branch_pairs": 0,
            "cross_branch_reused_files": 0,
            "cross_branch_fallbacks": 0,
            "cross_branch_evidence_calls": 0,
            "cross_branch_evidence_seconds": 0.0,
            "directory_evidence_calls": 0,
            "file_reads": 0,
            "bulk_export_calls": 0,
            "bulk_export_files": 0,
            "bulk_export_bytes": 0,
            "bulk_export_fallbacks": 0,
            "bulk_export_missing_files": 0,
        }

    @staticmethod
    def _hash_json(value: Any) -> str:
        encoded = json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        return hashlib.sha256(encoded).hexdigest()

    @staticmethod
    def _hash_content(
        raw: bytes,
        *,
        timing: SnapshotPhaseTiming | None,
        side: str | None,
        kind: str,
    ) -> str:
        if timing is None or side is None:
            return hashlib.sha256(raw).hexdigest()
        with timing.phase(
            f"sha256.{kind}",
            side=side,
        ) as observation:
            result = hashlib.sha256(raw).hexdigest()
            observation.result(bytes_count=len(raw), items=1)
            return result

    def _new_phase_timing(
        self,
        *,
        request_context_id: str | None,
        source_endpoint_id: str,
        source_revision: int | str,
        target_endpoint_id: str,
        target_revision: int | str,
    ) -> SnapshotPhaseTiming | None:
        if not self._phase_timing_enabled:
            return None
        return SnapshotPhaseTiming(
            request_context_id=request_context_id,
            source_endpoint_id=source_endpoint_id,
            source_revision=source_revision,
            target_endpoint_id=target_endpoint_id,
            target_revision=target_revision,
        )

    def _finish_phase_timing(
        self,
        timing: SnapshotPhaseTiming | None,
        *,
        outcome: str,
    ) -> None:
        if timing is None:
            return
        try:
            metrics = timing.finish(outcome=outcome)
        except Exception:
            logger.warning(
                "快照分阶段计时收尾失败",
                exc_info=True,
                extra={"event": "snapshot.phase_timing_failed"},
            )
            return
        summary = metrics["summary"]
        lookup = summary["persistent_lookup"]
        provider_read = summary["provider_read"]
        fallback_reasons = {
            key.removeprefix("persistent_fallback.")
            for key, value in metrics["counters"].items()
            if key.startswith("persistent_fallback.") and value
        }
        persistent = self._persistent_content_cache
        if persistent is not None:
            startup_fallback = persistent.metrics().get(
                "persistent_startup_fallback"
            )
            if isinstance(startup_fallback, str) and startup_fallback:
                fallback_reasons.add(startup_fallback)
        phase: dict[str, int | float | str] = {
            "directory_evidence_calls": int(summary["list_tree"]["calls"]),
            "persistent_hash_hits": int(
                lookup.get("sources", {}).get("hit", 0)
            ),
            "disk_byte_hits": int(lookup.get("sources", {}).get("hit", 0)),
            "disk_bytes": int(lookup.get("bytes", 0)),
            "file_reads": int(provider_read["calls"]),
            "fallback_reasons": ",".join(sorted(fallback_reasons)) or "none",
            "wall_seconds": float(metrics["request"]["wall_seconds"]),
            "outcome": outcome,
        }
        with self._cache_lock:
            # Compatibility diagnostics now store one complete context, never a
            # process-global before/after delta.
            self._last_phase_metrics = phase
        logger.info(
            (
                "快照分阶段计时完成 outcome=%s source=%s@%s target=%s@%s "
                "wall=%.3fs provider_reads=%d"
            ),
            outcome,
            metrics["endpoints"]["source"]["endpoint_id"],
            metrics["endpoints"]["source"]["resolved_revision"],
            metrics["endpoints"]["target"]["endpoint_id"],
            metrics["endpoints"]["target"]["resolved_revision"],
            metrics["request"]["wall_seconds"],
            provider_read["calls"],
            extra={
                "event": "snapshot.phase_timing",
                "request_id": timing.request_context_id,
                "internal_metrics": metrics,
            },
        )
        if self._phase_timing_sink is not None:
            try:
                self._phase_timing_sink(metrics)
            except Exception:
                logger.warning(
                    "快照分阶段计时 sink 写入失败",
                    exc_info=True,
                    extra={"event": "snapshot.phase_timing_sink_failed"},
                )

    def _persistent_cache_enabled(self) -> bool:
        cache = self._persistent_content_cache
        return bool(cache is not None and cache.metrics()["persistent_enabled"])

    def _record_counter(self, key: str, amount: int = 1) -> None:
        with self._cache_lock:
            self._snapshot_reuse_counters[key] = (
                self._snapshot_reuse_counters.get(key, 0) + amount
            )

    def _list_tree(
        self,
        endpoint: EndpointSpec,
        prefix: str = "",
        *,
        timing: SnapshotPhaseTiming | None = None,
        side: str | None = None,
    ) -> list[TreeEntry]:
        self._record_counter("directory_evidence_calls")
        if timing is None or side is None:
            return self.provider.list_tree(endpoint, prefix)
        with timing.side_scope(side):
            with timing.phase("svn.list_tree", side=side) as observation:
                entries = self.provider.list_tree(endpoint, prefix)
                observation.result(items=len(entries))
                return entries

    def _read_provider_bytes(
        self,
        endpoint: EndpointSpec,
        path: str,
        *,
        timing: SnapshotPhaseTiming | None = None,
        side: str | None = None,
        prefetched_raw: bytes | None = None,
    ) -> bytes:
        self._record_counter("file_reads")
        if prefetched_raw is not None:
            if timing is not None and side is not None:
                with timing.phase("provider.read", side=side) as observation:
                    observation.result(
                        bytes_count=len(prefetched_raw),
                        source="svn_export",
                    )
            return prefetched_raw
        if timing is None or side is None:
            reader = getattr(self.provider, "read_bytes", None)
            if reader is not None:
                return reader(endpoint, path)
            content = self.provider.read_content(endpoint, path, self.preview_limit)
            return content.text.encode("utf-8")
        timing.provider_enter(side)
        try:
            with timing.phase("provider.read", side=side) as observation:
                reader_with_source = getattr(
                    self.provider,
                    "read_bytes_with_source",
                    None,
                )
                if reader_with_source is not None:
                    raw, source = reader_with_source(endpoint, path)
                else:
                    reader = getattr(self.provider, "read_bytes", None)
                    if reader is not None:
                        raw = reader(endpoint, path)
                        source = "provider"
                    else:
                        content = self.provider.read_content(
                            endpoint,
                            path,
                            self.preview_limit,
                        )
                        raw = content.text.encode("utf-8")
                        source = "provider_preview"
                observation.result(bytes_count=len(raw), source=source)
                return raw
        finally:
            timing.provider_exit(side)

    def _bulk_export_prefetch(
        self,
        endpoint: EndpointSpec,
        entries: list[TreeEntry],
        table_path: str,
        content_repository_uuid: str,
        persistent_repository_uuid: str | None,
        configuration_sha256: str | None,
        *,
        timing: SnapshotPhaseTiming | None = None,
        side: str | None = None,
    ) -> dict[str, bytes]:
        exporter = getattr(self.provider, "export_files", None)
        if not self.bulk_export_enabled or exporter is None or not entries:
            return {}

        persistent = self._persistent_content_cache
        canonical_url = canonicalize_svn_url(endpoint.url)
        missing: list[TreeEntry] = []
        for entry in entries:
            path = normalize_relative_path(entry.path)
            memory_key = (
                content_repository_uuid,
                str(endpoint.url).rstrip("/"),
                path,
                str(endpoint.revision),
            )
            with self._cache_lock:
                memory_hit = memory_key in self._content_cache
            if memory_hit:
                continue
            if (
                persistent is not None
                and configuration_sha256
                and persistent_repository_uuid
            ):
                identity = self._persistent_file_identity(
                    repository_uuid=persistent_repository_uuid,
                    canonical_url=canonical_url,
                    entry=entry,
                    configuration_sha256=configuration_sha256,
                )
                if persistent.may_have(
                    identity,
                    expected_size=entry.size,
                    timing=timing,
                    side=side,
                ):
                    continue
            missing.append(entry)

        if len(missing) < self.bulk_export_min_files:
            return {}
        requested_paths = [normalize_relative_path(entry.path) for entry in missing]
        requested_set = set(requested_paths)
        self._record_counter("bulk_export_calls")
        if timing is not None and side is not None:
            timing.increment("bulk_export.calls", side=side)
            timing.provider_enter(side)
        try:
            if timing is None or side is None:
                exported = exporter(endpoint, table_path, requested_paths)
            else:
                with timing.phase("provider.export", side=side) as observation:
                    exported = exporter(endpoint, table_path, requested_paths)
                    observation.result(
                        bytes_count=exported.exported_bytes,
                        items=exported.exported_file_count,
                        source="svn_export",
                    )
        except Exception:
            self._record_counter("bulk_export_fallbacks")
            if timing is not None and side is not None:
                timing.increment("bulk_export.fallbacks", side=side)
            return {}
        finally:
            if timing is not None and side is not None:
                timing.provider_exit(side)

        files = {
            normalize_relative_path(path): raw
            for path, raw in exported.files.items()
            if normalize_relative_path(path) in requested_set
            and isinstance(raw, bytes)
        }
        missing_count = len(requested_set - set(files))
        self._record_counter("bulk_export_files", exported.exported_file_count)
        self._record_counter("bulk_export_bytes", exported.exported_bytes)
        if missing_count:
            self._record_counter("bulk_export_missing_files", missing_count)
            if timing is not None and side is not None:
                timing.increment(
                    "bulk_export.missing_files",
                    side=side,
                    amount=missing_count,
                )
        return files

    def _endpoint_configuration_sha256(self, table_path: str) -> str:
        return self._hash_json(
            {
                "snapshot_configuration_sha256": self._reuse_configuration_sha256,
                "table_path": normalize_relative_path(table_path),
            }
        )

    def _persistent_repository_uuid(
        self,
        record: Mapping[str, Any],
        revision: int,
        *,
        hint: str | None = None,
        timing: SnapshotPhaseTiming | None = None,
        side: str | None = None,
    ) -> str | None:
        if not self._persistent_cache_enabled():
            return hint
        cache = self._persistent_content_cache
        assert cache is not None
        try:
            canonical_url = canonicalize_svn_url(self._validate_url(record))
        except Exception:
            cache.record_fallback("canonical_url", timing=timing, side=side)
            return None
        key = (canonical_url, revision)
        if hint and "://" not in hint:
            with self._cache_lock:
                self._repository_identity_cache[key] = hint
            if timing is not None and side is not None:
                timing.set_endpoint(side, repository_uuid=hint)
            return hint
        with self._cache_lock:
            cached = self._repository_identity_cache.get(key)
        if cached:
            if timing is not None and side is not None:
                timing.set_endpoint(side, repository_uuid=cached)
            return cached
        try:
            endpoint = EndpointSpec(
                url=canonical_url,
                revision=revision,
                label=str(record.get("label", "")),
            )
            if timing is None or side is None:
                info = self.provider.info(endpoint)
            else:
                with timing.phase("endpoint.info", side=side) as observation:
                    info = self.provider.info(endpoint)
                    observation.result(items=1, source="persistent_identity")
            repository_uuid = str(info.repository_uuid or "").strip()
            returned_url = canonicalize_svn_url(info.url or canonical_url)
            if not repository_uuid or returned_url != canonical_url:
                raise ValueError("incomplete frozen repository identity")
        except Exception:
            cache.record_fallback(
                "repository_identity",
                timing=timing,
                side=side,
            )
            return None
        if timing is not None and side is not None:
            timing.set_endpoint(
                side,
                resolved_revision=revision,
                repository_uuid=repository_uuid,
                )
        with self._cache_lock:
            self._repository_identity_cache[key] = repository_uuid
        return repository_uuid

    def _persistent_file_identity(
        self,
        *,
        repository_uuid: str,
        canonical_url: str,
        entry: TreeEntry,
        configuration_sha256: str,
    ) -> SnapshotFileIdentity:
        return SnapshotFileIdentity(
            repository_uuid=repository_uuid,
            canonical_url=canonical_url,
            relative_path=normalize_relative_path(entry.path),
            last_changed_revision=str(entry.revision),
            configuration_sha256=configuration_sha256,
        )

    def _persistent_configuration_for_tree(
        self,
        *,
        repository_uuid: str | None,
        all_entries: list[TreeEntry],
        excel_entries: list[TreeEntry],
        table_path: str,
        timing: SnapshotPhaseTiming | None = None,
        side: str | None = None,
    ) -> str | None:
        cache = self._persistent_content_cache
        if cache is None or not self._persistent_cache_enabled():
            return None
        if not repository_uuid:
            cache.record_fallback("repository_uuid_missing", timing=timing, side=side)
            return None
        try:
            table = normalize_relative_path(table_path)
            table_parts = table.split("/")
            variants: set[str] = set()
            for entry in all_entries:
                path = normalize_relative_path(entry.path)
                parts = path.split("/")
                if (
                    len(parts) >= len(table_parts)
                    and [part.casefold() for part in parts[: len(table_parts)]]
                    == [part.casefold() for part in table_parts]
                ):
                    variants.add("/".join(parts[: len(table_parts)]))
            if variants != {table}:
                cache.record_fallback("path_ambiguity", timing=timing, side=side)
                return None
            seen_paths: set[str] = set()
            for entry in excel_entries:
                path = normalize_relative_path(entry.path)
                folded = path.casefold()
                revision = str(entry.revision).strip()
                if not path or folded in seen_paths:
                    cache.record_fallback("path_ambiguity", timing=timing, side=side)
                    return None
                if not revision.isdigit() or int(revision) <= 0:
                    cache.record_fallback("last_changed_revision_missing", timing=timing, side=side)
                    return None
                seen_paths.add(folded)
            return self._endpoint_configuration_sha256(table)
        except Exception:
            cache.record_fallback("tree_metadata_invalid", timing=timing, side=side)
            return None

    def read_cached_snapshot_bytes(
        self,
        record: Mapping[str, Any],
        revision: int,
        table_path: str,
        relative_path: str,
    ) -> bytes | None:
        cache = self._persistent_content_cache
        if cache is None or not self._persistent_cache_enabled():
            return None
        try:
            canonical_url = canonicalize_svn_url(self._validate_url(record))
            normalized_table = normalize_relative_path(table_path)
            normalized_path = normalize_relative_path(relative_path)
            if not self._file_belongs_to_table(normalized_path, normalized_table):
                cache.record_fallback("materialization_scope")
                return None
            repository_uuid = self._persistent_repository_uuid(record, revision)
            if not repository_uuid:
                return None
            return cache.read_tree_bytes(
                repository_uuid=repository_uuid,
                canonical_url=canonical_url,
                revision=revision,
                table_path=normalized_table,
                configuration_sha256=self._endpoint_configuration_sha256(
                    normalized_table
                ),
                relative_path=normalized_path,
            )
        except Exception:
            cache.record_fallback("materialization_cache_error")
            return None

    @staticmethod
    def _snapshot_facts_sha256(snapshot: SnapshotResponsePayload) -> str:
        return SnapshotService._hash_json(snapshot.model_dump(mode="json"))

    def _endpoint_reuse_identity(
        self,
        record: Mapping[str, Any],
        revision: int,
    ) -> dict[str, Any]:
        return {
            "endpoint_id": str(record["id"]),
            "revision": revision,
            "region": str(record.get("region", "")),
            "track": str(record.get("track", "")),
            "label": str(record.get("label", "")),
            "url": self._validate_url(record),
            "logical_scopes": list(record.get("logical_scopes") or ()),
            "physical_path_filters": {
                str(key): normalize_relative_path(str(value))
                for key, value in sorted(
                    dict(record.get("physical_path_filters") or {}).items()
                )
            },
            "enabled": bool(record.get("enabled", True)),
        }

    def _snapshot_reuse_identity(
        self,
        source_record: Mapping[str, Any],
        source_revision: int,
        target_record: Mapping[str, Any],
        target_revision: int,
    ) -> dict[str, Any]:
        return {
            "ruleset": _SNAPSHOT_REUSE_RULESET,
            "configuration_sha256": self._reuse_configuration_sha256,
            "source": self._endpoint_reuse_identity(source_record, source_revision),
            "target": self._endpoint_reuse_identity(target_record, target_revision),
        }

    @staticmethod
    def _file_belongs_to_table(path: str, table_path: str) -> bool:
        normalized = normalize_relative_path(path).casefold()
        table = normalize_relative_path(table_path).casefold()
        return normalized.startswith(table + "/")

    @classmethod
    def _snapshot_matches_identity(
        cls,
        snapshot: SnapshotResponsePayload,
        identity: Mapping[str, Any],
    ) -> bool:
        if snapshot.logical_scopes != list(LOGICAL_SCOPES):
            return False
        for side_name in ("source", "target"):
            side = getattr(snapshot, side_name)
            expected = identity[side_name]
            if (
                side.endpoint_id != expected["endpoint_id"]
                or side.resolved_revision != expected["revision"]
                or side.url != expected["url"]
                or set(side.physical_path_filters) != set(LOGICAL_SCOPES)
            ):
                return False
            table_path = side.physical_path_filters.get("TABLE")
            if not table_path:
                return False
            seen_paths: set[str] = set()
            for item in side.files:
                normalized_path = normalize_relative_path(item.path)
                folded_path = normalized_path.casefold()
                if (
                    not normalized_path
                    or folded_path in seen_paths
                    or item.logical_scope != "TABLE"
                    or not folded_path.endswith(EXCEL_EXTENSIONS)
                    or not cls._file_belongs_to_table(normalized_path, table_path)
                ):
                    return False
                seen_paths.add(folded_path)
                if item.error is None:
                    if not item.content_hash or not _SHA256_PATTERN.fullmatch(item.content_hash):
                        return False
                elif item.content_hash is not None:
                    return False
            if side.stats.file_count != len(side.files):
                return False
            if side.stats.failed_count != sum(
                item.error is not None for item in side.files
            ):
                return False
            if side.stats.total_size != sum(item.size or 0 for item in side.files):
                return False
        return True

    def _prune_snapshot_fact_cache_locked(self, now: float) -> None:
        expired = [
            key
            for key, entry in self._snapshot_fact_cache.items()
            if entry.expires_at <= now
        ]
        for key in expired:
            self._snapshot_fact_cache.pop(key, None)
        self._snapshot_reuse_counters["expired"] += len(expired)

    def _load_snapshot_fact_locked(
        self,
        key: str,
        identity: Mapping[str, Any],
        now: float,
    ) -> SnapshotResponsePayload | None:
        self._prune_snapshot_fact_cache_locked(now)
        entry = self._snapshot_fact_cache.get(key)
        if entry is None:
            return None
        try:
            valid = (
                entry.facts_sha256 == self._snapshot_facts_sha256(entry.snapshot)
                and self._snapshot_matches_identity(entry.snapshot, identity)
            )
        except Exception:
            valid = False
        if not valid:
            self._snapshot_fact_cache.pop(key, None)
            self._snapshot_reuse_counters["invalid"] += 1
            return None
        self._snapshot_fact_cache.move_to_end(key)
        self._snapshot_reuse_counters["hits"] += 1
        return entry.snapshot.model_copy(deep=True)

    def _store_snapshot_fact_locked(
        self,
        key: str,
        identity: Mapping[str, Any],
        snapshot: SnapshotResponsePayload,
        now: float,
    ) -> bool:
        self._prune_snapshot_fact_cache_locked(now)
        try:
            valid = self._snapshot_matches_identity(snapshot, identity)
        except Exception:
            valid = False
        if self._reuse_ttl_seconds <= 0 or self._reuse_max_entries <= 0 or not valid:
            return False
        stored = snapshot.model_copy(deep=True)
        self._snapshot_fact_cache[key] = _TrustedSnapshotEntry(
            snapshot=stored,
            facts_sha256=self._snapshot_facts_sha256(stored),
            expires_at=now + self._reuse_ttl_seconds,
        )
        self._snapshot_fact_cache.move_to_end(key)
        while len(self._snapshot_fact_cache) > self._reuse_max_entries:
            self._snapshot_fact_cache.popitem(last=False)
            self._snapshot_reuse_counters["evicted"] += 1
        return True

    def snapshot_reuse_metrics(self) -> dict[str, int | float]:
        """Return redacted process-local counters for tests and diagnostics."""
        persistent = (
            self._persistent_content_cache.metrics()
            if self._persistent_content_cache is not None
            else {
                "persistent_enabled": False,
                "persistent_entries": 0,
                "persistent_trees": 0,
                "persistent_inflight": 0,
            }
        )
        with self._cache_lock:
            self._prune_snapshot_fact_cache_locked(self._monotonic_clock())
            return {
                **self._snapshot_reuse_counters,
                **persistent,
                "entries": len(self._snapshot_fact_cache),
                "inflight": len(self._snapshot_build_flights),
                **{
                    f"last_{key}": value
                    for key, value in self._last_phase_metrics.items()
                },
            }

    @staticmethod
    def normalize_registry(records: list[Mapping[str, Any]]) -> list[dict[str, Any]]:
        normalized: list[dict[str, Any]] = []
        seen: set[str] = set()
        for raw in records:
            record = EndpointRecordPayload.model_validate(raw)
            if record.id in seen:
                raise SVNProviderError("SVN_INVALID_ENDPOINT_CONFIG", f"端点 ID 重复：{record.id}")
            seen.add(record.id)
            requested_scopes = {str(scope).strip().upper() for scope in record.logical_scopes}
            if requested_scopes != set(LOGICAL_SCOPES):
                raise SVNProviderError(
                    "SVN_INVALID_ENDPOINT_CONFIG",
                    f"端点 {record.id} 必须只关注 TABLE 逻辑目录",
                )
            physical = {}
            for logical, path in record.physical_path_filters.items():
                canonical_logical = str(logical).strip().upper()
                if canonical_logical not in LOGICAL_SCOPES:
                    raise SVNProviderError("SVN_INVALID_ENDPOINT_CONFIG", f"未知逻辑目录：{logical}")
                if not path:
                    continue
                physical[canonical_logical] = normalize_relative_path(path)
            normalized.append(
                {
                    **record.model_dump(exclude={"physical_path_filters"}),
                    "logical_scopes": list(LOGICAL_SCOPES),
                    "physical_path_filters": physical,
                }
            )
        return normalized

    @staticmethod
    def record_map(records: list[Mapping[str, Any]]) -> dict[str, dict[str, Any]]:
        return {str(record["id"]): dict(record) for record in records}

    def _get_record(self, records: list[Mapping[str, Any]], endpoint_id: str) -> dict[str, Any]:
        record = self.record_map(records).get(endpoint_id)
        if record is None:
            raise SVNProviderError("SVN_ENDPOINT_NOT_FOUND", f"端点不存在：{endpoint_id}")
        if not bool(record.get("enabled", True)):
            raise SVNProviderError("SVN_ENDPOINT_DISABLED", f"端点未启用：{endpoint_id}")
        return record

    def _validate_url(self, record: Mapping[str, Any]) -> str:
        url = str(record.get("url", "")).strip()
        validate_endpoint(EndpointSpec(url=url, revision="HEAD"), self.allowed_schemes)
        return url

    def _resolve_head(
        self,
        record: Mapping[str, Any],
        *,
        timing: SnapshotPhaseTiming | None = None,
        side: str | None = None,
    ) -> tuple[int | str, str]:
        url = self._validate_url(record)
        endpoint = EndpointSpec(
            url=url,
            revision="HEAD",
            label=str(record.get("label", "")),
        )
        if timing is None or side is None:
            info = self.provider.info(endpoint)
        else:
            with timing.side_scope(side):
                with timing.phase("endpoint.info", side=side) as observation:
                    info = self.provider.info(endpoint)
                    observation.result(items=1, source="head")
        revision = str(info.revision or info.last_changed_revision).strip()
        if not revision:
            raise SVNProviderError("SVN_INVALID_REVISION", f"端点没有返回 HEAD Revision：{record['id']}")
        resolved = int(revision) if revision.isdigit() else revision
        repository_uuid = info.repository_uuid or info.repository_root or url
        if timing is not None and side is not None and isinstance(resolved, int):
            timing.set_endpoint(
                side,
                resolved_revision=resolved,
                repository_uuid=repository_uuid,
            )
        return resolved, repository_uuid

    def freeze_head(self, record: Mapping[str, Any]) -> int | str:
        return self._resolve_head(record)[0]

    def _discover_scope_paths_from_entries(
        self,
        record: Mapping[str, Any],
        entries: list[TreeEntry],
    ) -> dict[str, str]:
        directories = {
            normalize_relative_path(entry.path)
            for entry in entries
            if entry.kind == "dir"
        }
        # 某些 SVN 代理只返回文件项；从文件路径推导父目录，保持发现能力稳定。
        for entry in entries:
            if entry.kind != "file":
                continue
            parts = normalize_relative_path(entry.path).split("/")
            directories.update("/".join(parts[:index]) for index in range(1, len(parts)))
        resolved: dict[str, str] = {}
        for logical in LOGICAL_SCOPES:
            candidates = [
                path
                for path in directories
                if path.rsplit("/", 1)[-1].casefold() == logical.casefold()
            ]
            if not candidates:
                raise SVNProviderError(
                    "SVN_SCOPE_NOT_FOUND",
                    f"端点 {record['id']} 未找到逻辑目录 {logical}",
                )
            resolved[logical] = sorted(
                candidates,
                key=lambda path: (
                    path.count("/"),
                    path.rsplit("/", 1)[-1] != logical,
                    path.casefold(),
                ),
            )[0]
        return resolved

    def discover_scope_paths(
        self,
        record: Mapping[str, Any],
        revision: int | str = "HEAD",
    ) -> dict[str, str]:
        url = self._validate_url(record)
        endpoint = EndpointSpec(url=url, revision=revision, label=str(record.get("label", "")))
        return self._discover_scope_paths_from_entries(
            record,
            self._list_tree(endpoint),
        )

    def resolve_scope_paths(
        self,
        record: Mapping[str, Any],
        revision: int | str,
        *,
        entries: list[TreeEntry] | None = None,
    ) -> dict[str, str]:
        configured = {
            logical: normalize_relative_path(str(path))
            for logical, path in dict(record.get("physical_path_filters") or {}).items()
            if logical in LOGICAL_SCOPES and path
        }
        if entries is None:
            url = self._validate_url(record)
            endpoint = EndpointSpec(
                url=url,
                revision=revision,
                label=str(record.get("label", "")),
            )
            entries = self._list_tree(endpoint)
        known_paths = {
            normalize_relative_path(entry.path).casefold()
            for entry in entries
        }
        if set(configured) == set(LOGICAL_SCOPES) and all(
            any(
                path == prefix.casefold()
                or path.startswith(prefix.casefold() + "/")
                for path in known_paths
            )
            for prefix in configured.values()
        ):
            return configured
        return self._discover_scope_paths_from_entries(record, entries)

    @staticmethod
    def _scope_for_path(path: str, physical: Mapping[str, str]) -> str:
        normalized = normalize_relative_path(path)
        folded = normalized.casefold()
        for logical, prefix in physical.items():
            prefix_folded = normalize_relative_path(prefix).casefold()
            if folded == prefix_folded or folded.startswith(prefix_folded + "/"):
                return logical
        return "UNKNOWN"

    def _read_binary(
        self,
        endpoint: EndpointSpec,
        entry: TreeEntry,
        repository_uuid: str,
        persistent_repository_uuid: str | None = None,
        configuration_sha256: str | None = None,
        *,
        timing: SnapshotPhaseTiming | None = None,
        side: str | None = None,
        prefetched_files: Mapping[str, bytes] | None = None,
    ) -> bytes:
        normalized_path = normalize_relative_path(entry.path)
        key = (
            repository_uuid,
            str(endpoint.url).rstrip("/"),
            normalized_path,
            str(endpoint.revision),
        )
        if timing is None or side is None:
            with self._cache_lock:
                cached = self._content_cache.get(key)
        else:
            with timing.phase("content.memory_lookup", side=side) as observation:
                with self._cache_lock:
                    cached = self._content_cache.get(key)
                observation.result(
                    bytes_count=len(cached) if cached is not None else 0,
                    source="hit" if cached is not None else "miss",
                )
        if cached is not None:
            return cached

        persistent = self._persistent_content_cache
        if (
            persistent is not None
            and configuration_sha256
            and persistent_repository_uuid
        ):
            identity = self._persistent_file_identity(
                repository_uuid=persistent_repository_uuid,
                canonical_url=canonicalize_svn_url(endpoint.url),
                entry=entry,
                configuration_sha256=configuration_sha256,
            )
            cached_bytes, _ = persistent.get_or_load(
                identity,
                expected_size=entry.size,
                loader=lambda: self._read_provider_bytes(
                    endpoint,
                    entry.path,
                    timing=timing,
                    side=side,
                    prefetched_raw=(prefetched_files or {}).get(normalized_path),
                ),
                timing=timing,
                side=side,
            )
            raw = cached_bytes.raw
        else:
            raw = self._read_provider_bytes(
                endpoint,
                entry.path,
                timing=timing,
                side=side,
                prefetched_raw=(prefetched_files or {}).get(normalized_path),
            )

        with self._cache_lock:
            self._content_cache[key] = raw
        return raw

    def _fetch_file(
        self,
        endpoint: EndpointSpec,
        entry: TreeEntry,
        logical_scope: str,
        repository_uuid: str,
        persistent_repository_uuid: str | None = None,
        configuration_sha256: str | None = None,
        *,
        timing: SnapshotPhaseTiming | None = None,
        side: str | None = None,
        prefetched_files: Mapping[str, bytes] | None = None,
    ) -> SnapshotFilePayload:
        worker_scope = (
            timing.file_worker_scope(side)
            if timing is not None and side is not None
            else nullcontext()
        )
        with worker_scope:
            return self._fetch_file_impl(
                endpoint,
                entry,
                logical_scope,
                repository_uuid,
                persistent_repository_uuid,
                configuration_sha256,
                timing=timing,
                side=side,
                prefetched_files=prefetched_files,
            )


    def _fetch_file_impl(
        self,
        endpoint: EndpointSpec,
        entry: TreeEntry,
        logical_scope: str,
        repository_uuid: str,
        persistent_repository_uuid: str | None = None,
        configuration_sha256: str | None = None,
        *,
        timing: SnapshotPhaseTiming | None = None,
        side: str | None = None,
        prefetched_files: Mapping[str, bytes] | None = None,
    ) -> SnapshotFilePayload:
        try:
            raw = self._read_binary(
                endpoint,
                entry,
                repository_uuid,
                persistent_repository_uuid,
                configuration_sha256,
                timing=timing,
                side=side,
                prefetched_files=prefetched_files,
            )
            content_hash = self._hash_content(
                raw,
                timing=timing,
                side=side,
                kind="snapshot_content",
            )
            cache_key = hashlib.sha256(
                f"{repository_uuid}|{str(endpoint.url).rstrip('/')}|{entry.path}|{endpoint.revision}".encode("utf-8")
            ).hexdigest()
            return SnapshotFilePayload(
                path=entry.path,
                logical_scope=logical_scope,
                size=entry.size if entry.size is not None else len(raw),
                revision=entry.revision,
                author=entry.author,
                date=entry.date,
                encoding="binary",
                content_ref=f"memory://snapshot/{cache_key}",
                content_hash=content_hash,
            )
        except SVNProviderError as exc:
            return SnapshotFilePayload(
                path=entry.path,
                logical_scope=logical_scope,
                size=entry.size,
                revision=entry.revision,
                author=entry.author,
                date=entry.date,
                error={"code": exc.code, "message": exc.message},
            )
        except Exception:
            return SnapshotFilePayload(
                path=entry.path,
                logical_scope=logical_scope,
                size=entry.size,
                revision=entry.revision,
                author=entry.author,
                date=entry.date,
                error={"code": "SVN_FILE_READ_FAILED", "message": "文件读取失败"},
            )

    def _snapshot_endpoint_at_revision(
        self,
        record: Mapping[str, Any],
        revision: int,
        *,
        repository_uuid: str | None = None,
        timing: SnapshotPhaseTiming | None = None,
        side: str | None = None,
    ) -> SnapshotEndpointPayload:
        if timing is not None and side is not None:
            timing.set_endpoint(
                side,
                resolved_revision=revision,
                repository_uuid=repository_uuid,
            )
        return self._snapshot_endpoint_at_revision_impl(
            record,
            revision,
            repository_uuid=repository_uuid,
            timing=timing,
            side=side,
        )


    def _snapshot_endpoint_at_revision_impl(
        self,
        record: Mapping[str, Any],
        revision: int,
        *,
        repository_uuid: str | None = None,
        timing: SnapshotPhaseTiming | None = None,
        side: str | None = None,
    ) -> SnapshotEndpointPayload:
        """读取指定 Revision，不调用 info() 或重新解析 HEAD。"""
        url = self._validate_url(record)
        endpoint = validate_endpoint(
            EndpointSpec(
                url=url,
                revision=revision,
                label=str(record.get("label", "")),
            ),
            self.allowed_schemes,
        )
        all_entries = self._list_tree(endpoint, timing=timing, side=side)
        return self._snapshot_endpoint_from_entries(
            record,
            revision,
            all_entries,
            repository_uuid=repository_uuid,
            timing=timing,
            side=side,
        )

    def _snapshot_endpoint_from_entries(
        self,
        record: Mapping[str, Any],
        revision: int,
        all_entries: list[TreeEntry],
        *,
        repository_uuid: str | None = None,
        persistent_repository_uuid: str | None = None,
        reusable_files: Mapping[str, SnapshotFilePayload] | None = None,
        reusable_content_revision: int | None = None,
        reusable_content_url: str | None = None,
        reusable_content_repository_uuid: str | None = None,
        require_matching_entry_revision: bool = True,
        timing: SnapshotPhaseTiming | None = None,
        side: str | None = None,
    ) -> SnapshotEndpointPayload:
        scope = (
            timing.side_scope(side)
            if timing is not None and side is not None
            else nullcontext()
        )
        with scope:
            return self._snapshot_endpoint_from_entries_impl(
                record,
                revision,
                all_entries,
                repository_uuid=repository_uuid,
                persistent_repository_uuid=persistent_repository_uuid,
                reusable_files=reusable_files,
                reusable_content_revision=reusable_content_revision,
                reusable_content_url=reusable_content_url,
                reusable_content_repository_uuid=reusable_content_repository_uuid,
                require_matching_entry_revision=require_matching_entry_revision,
                timing=timing,
                side=side,
            )


    def _snapshot_endpoint_from_entries_impl(
        self,
        record: Mapping[str, Any],
        revision: int,
        all_entries: list[TreeEntry],
        *,
        repository_uuid: str | None = None,
        persistent_repository_uuid: str | None = None,
        reusable_files: Mapping[str, SnapshotFilePayload] | None = None,
        reusable_content_revision: int | None = None,
        reusable_content_url: str | None = None,
        reusable_content_repository_uuid: str | None = None,
        require_matching_entry_revision: bool = True,
        timing: SnapshotPhaseTiming | None = None,
        side: str | None = None,
    ) -> SnapshotEndpointPayload:
        if timing is not None and side is not None:
            timing.set_endpoint(
                side,
                resolved_revision=revision,
                repository_uuid=persistent_repository_uuid or repository_uuid,
            )
        url = self._validate_url(record)
        endpoint = validate_endpoint(
            EndpointSpec(url=url, revision=revision, label=str(record.get("label", ""))),
            self.allowed_schemes,
        )
        physical = self.resolve_scope_paths(
            record,
            revision,
            entries=all_entries,
        )
        entries = [
            entry
            for entry in all_entries
            if entry.kind == "file"
            and entry.path.casefold().endswith(EXCEL_EXTENSIONS)
            and self._scope_for_path(entry.path, physical) != "UNKNOWN"
        ]
        if timing is not None and side is not None:
            with timing.phase("sort.entries", side=side) as observation:
                entries.sort(key=lambda item: item.path.casefold())
                observation.result(items=len(entries))
        else:
            entries.sort(key=lambda item: item.path.casefold())
        persistent_repository_uuid = self._persistent_repository_uuid(
            record,
            revision,
            hint=persistent_repository_uuid or repository_uuid,
            timing=timing,
            side=side,
        )
        persistent_configuration_sha256 = self._persistent_configuration_for_tree(
            repository_uuid=persistent_repository_uuid,
            all_entries=all_entries,
            excel_entries=entries,
            table_path=physical["TABLE"],
            timing=timing,
            side=side,
        )
        content_repository_uuid = repository_uuid or url
        files: list[SnapshotFilePayload] = []
        pending: list[TreeEntry] = []
        reusable_files = reusable_files or {}
        for entry in entries:
            path = normalize_relative_path(entry.path)
            reused = reusable_files.get(path)
            if (
                reused is None
                or reused.error is not None
                or not reused.content_hash
                or not entry.revision
                or (
                    require_matching_entry_revision
                    and str(reused.revision) != str(entry.revision)
                )
            ):
                pending.append(entry)
                continue
            source_key = (
                reusable_content_repository_uuid or content_repository_uuid,
                (reusable_content_url or url).rstrip("/"),
                path,
                str(reusable_content_revision or reused.revision),
            )
            target_key = (
                content_repository_uuid,
                url.rstrip("/"),
                path,
                str(revision),
            )
            with self._cache_lock:
                raw = self._content_cache.get(source_key)
                if raw is not None:
                    self._content_cache[target_key] = raw
            cache_key = hashlib.sha256(
                f"{content_repository_uuid}|{url.rstrip('/')}|{path}|{revision}".encode("utf-8")
            ).hexdigest()
            files.append(
                SnapshotFilePayload(
                    path=path,
                    logical_scope=self._scope_for_path(path, physical),
                    size=entry.size if entry.size is not None else reused.size,
                    revision=entry.revision,
                    author=entry.author,
                    date=entry.date,
                    encoding="binary",
                    content_ref=f"memory://snapshot/{cache_key}",
                    content_hash=reused.content_hash,
                )
            )
        prefetched_files = self._bulk_export_prefetch(
            endpoint,
            pending,
            physical["TABLE"],
            content_repository_uuid,
            persistent_repository_uuid,
            persistent_configuration_sha256,
            timing=timing,
            side=side,
        )
        with ThreadPoolExecutor(
            max_workers=self.content_read_workers,
            thread_name_prefix="m1-snapshot-file",
        ) as executor:
            futures = {
                executor.submit(
                    self._fetch_file,
                    endpoint,
                    entry,
                    self._scope_for_path(entry.path, physical),
                    content_repository_uuid,
                    persistent_repository_uuid,
                    persistent_configuration_sha256,
                    timing=timing,
                    side=side,
                    prefetched_files=prefetched_files,
                ): entry.path
                for entry in pending
            }
            for future in as_completed(futures):
                files.append(future.result())
        if timing is not None and side is not None:
            with timing.phase("sort.files", side=side) as observation:
                files.sort(key=lambda item: item.path.casefold())
                observation.result(items=len(files))
        else:
            files.sort(key=lambda item: item.path.casefold())
        response_scope = (
            timing.phase("response.endpoint", side=side)
            if timing is not None and side is not None
            else nullcontext()
        )
        with response_scope as observation:
            total_size = sum(item.size or 0 for item in files)
            failed_count = sum(1 for item in files if item.error is not None)
            snapshot = SnapshotEndpointPayload(
                endpoint_id=str(record["id"]),
                label=str(record.get("label", record["id"])),
                url=url,
                resolved_revision=revision,
                physical_path_filters=physical,
                files=files,
                stats=SnapshotStatsPayload(
                    file_count=len(files),
                    total_size=total_size,
                    failed_count=failed_count,
                ),
            )
            if observation is not None:
                observation.result(
                    bytes_count=total_size,
                    items=len(files),
                )
        persistent = self._persistent_content_cache
        if (
            persistent is not None
            and persistent_repository_uuid
            and persistent_configuration_sha256
        ):
            files_by_path = {
                normalize_relative_path(item.path): item
                for item in files
                if item.error is None and item.content_hash
            }
            persistent.commit_tree(
                repository_uuid=persistent_repository_uuid,
                canonical_url=canonicalize_svn_url(url),
                revision=revision,
                table_path=normalize_relative_path(physical["TABLE"]),
                configuration_sha256=persistent_configuration_sha256,
                files=(
                    (
                        normalize_relative_path(entry.path),
                        self._persistent_file_identity(
                            repository_uuid=persistent_repository_uuid,
                            canonical_url=canonicalize_svn_url(url),
                            entry=entry,
                            configuration_sha256=persistent_configuration_sha256,
                        ).key,
                    )
                    for entry in entries
                    if normalize_relative_path(entry.path) in files_by_path
                ),
                timing=timing,
                side=side,
            )
        return snapshot

    @staticmethod
    def _pair_results(
        executor: ThreadPoolExecutor,
        source_future: Future[SnapshotEndpointPayload],
        target_future: Future[SnapshotEndpointPayload],
        *,
        timing: SnapshotPhaseTiming | None = None,
    ) -> tuple[SnapshotEndpointPayload, SnapshotEndpointPayload]:
        wait_scope = (
            timing.phase("pairing.future_wait")
            if timing is not None
            else nullcontext()
        )
        try:
            with wait_scope:
                source = source_future.result()
                target = target_future.result()
        finally:
            shutdown_scope = (
                timing.phase("pairing.executor_shutdown")
                if timing is not None
                else nullcontext()
            )
            with shutdown_scope:
                executor.shutdown(wait=True, cancel_futures=True)
        return source, target

    def _snapshot_pair_at_revisions(
        self,
        source_record: Mapping[str, Any],
        source_revision: int,
        target_record: Mapping[str, Any],
        target_revision: int,
        *,
        source_repository_uuid: str | None = None,
        target_repository_uuid: str | None = None,
        timing: SnapshotPhaseTiming | None = None,
    ) -> tuple[SnapshotEndpointPayload, SnapshotEndpointPayload]:
        executor = ThreadPoolExecutor(
            max_workers=_SNAPSHOT_SIDE_WORKERS,
            thread_name_prefix="m1-snapshot-side",
        )
        source_future = executor.submit(
            self._snapshot_endpoint_at_revision,
            source_record,
            source_revision,
            repository_uuid=source_repository_uuid,
            timing=timing,
            side="source",
        )
        target_future = executor.submit(
            self._snapshot_endpoint_at_revision,
            target_record,
            target_revision,
            repository_uuid=target_repository_uuid,
            timing=timing,
            side="target",
        )
        return self._pair_results(
            executor,
            source_future,
            target_future,
            timing=timing,
        )

    @staticmethod
    def _reusable_files_by_path(
        snapshot: SnapshotEndpointPayload,
    ) -> dict[str, SnapshotFilePayload] | None:
        result: dict[str, SnapshotFilePayload] = {}
        folded_paths: set[str] = set()
        for item in snapshot.files:
            path = normalize_relative_path(item.path)
            folded = path.casefold()
            if not path or folded in folded_paths or not item.revision:
                return None
            folded_paths.add(folded)
            result[path] = item
        return result

    def _same_branch_incremental_pair(
        self,
        source_record: Mapping[str, Any],
        source_revision: int,
        target_record: Mapping[str, Any],
        target_revision: int,
        *,
        timing: SnapshotPhaseTiming | None = None,
    ) -> tuple[SnapshotEndpointPayload, SnapshotEndpointPayload] | None:
        try:
            source_url = canonicalize_svn_url(self._validate_url(source_record))
            target_url = canonicalize_svn_url(self._validate_url(target_record))
            if source_url != target_url:
                return None
            source_entries = self._list_tree(
                EndpointSpec(url=source_url, revision=source_revision),
                timing=timing,
                side="source",
            )
            target_entries = self._list_tree(
                EndpointSpec(url=target_url, revision=target_revision),
                timing=timing,
                side="target",
            )
            source_folded_paths: set[str] = set()
            for entry in source_entries:
                if entry.kind != "file":
                    continue
                path = normalize_relative_path(entry.path)
                folded = path.casefold()
                if not path or not entry.revision or folded in source_folded_paths:
                    return None
                source_folded_paths.add(folded)
            source = self._snapshot_endpoint_from_entries(
                source_record,
                source_revision,
                source_entries,
                timing=timing,
                side="source",
            )
            reusable = self._reusable_files_by_path(source)
            if reusable is None:
                return None
            target = self._snapshot_endpoint_from_entries(
                target_record,
                target_revision,
                target_entries,
                reusable_files=reusable,
                reusable_content_revision=source_revision,
                timing=timing,
                side="target",
            )
            if source.physical_path_filters != target.physical_path_filters:
                return None
            reused_count = sum(
                1
                for item in target.files
                if (base := reusable.get(normalize_relative_path(item.path))) is not None
                and base.error is None
                and item.error is None
                and base.revision
                and str(base.revision) == str(item.revision)
                and base.content_hash == item.content_hash
            )
        except Exception:
            with self._cache_lock:
                self._snapshot_reuse_counters["incremental_fallbacks"] += 1
            return None
        with self._cache_lock:
            self._snapshot_reuse_counters["incremental_pairs"] += 1
            self._snapshot_reuse_counters["incremental_reused_files"] += reused_count
        return source, target

    @staticmethod
    def _frozen_identity_matches(
        identity: BranchIdentity,
        url: str,
        revision: int,
    ) -> bool:
        try:
            expected_path = repository_path_from_urls(
                identity.repository_root,
                identity.canonical_url,
            )
            identity_path = normalize_repository_path(
                identity.repository_relative_path
            )
        except ValueError:
            return False
        return (
            identity.canonical_url == canonicalize_svn_url(url)
            and identity.bound_revision == revision
            and bool(identity.repository_uuid)
            and identity_path == expected_path
        )

    @staticmethod
    def _require_unambiguous_table_root(
        entries: list[TreeEntry],
        table_path: str,
    ) -> None:
        table = normalize_relative_path(table_path)
        table_parts = table.split("/")
        variants: set[str] = set()
        for entry in entries:
            path = normalize_relative_path(entry.path)
            parts = path.split("/")
            if (
                len(parts) >= len(table_parts)
                and [part.casefold() for part in parts[: len(table_parts)]]
                == [part.casefold() for part in table_parts]
            ):
                variants.add("/".join(parts[: len(table_parts)]))
        if variants != {table}:
            raise _CrossBranchEvidenceError("path_ambiguity")

    @staticmethod
    def _excel_entries_by_relative_path(
        entries: list[TreeEntry],
        table_path: str,
    ) -> dict[str, TreeEntry]:
        table = normalize_relative_path(table_path)
        result: dict[str, TreeEntry] = {}
        folded: set[str] = set()
        for entry in entries:
            if (
                entry.kind != "file"
                or not entry.path.casefold().endswith(EXCEL_EXTENSIONS)
                or not SnapshotService._file_belongs_to_table(entry.path, table)
            ):
                continue
            path = normalize_relative_path(entry.path)
            relative = path[len(table) + 1 :]
            relative_folded = relative.casefold()
            if not relative or relative_folded in folded:
                raise _CrossBranchEvidenceError("path_ambiguity")
            folded.add(relative_folded)
            result[relative] = entry
        return result

    @staticmethod
    def _changed_by_evidence(path: str, evidence: FrozenTreeDiff) -> bool:
        normalized = normalize_relative_path(path)
        folded = normalized.casefold()
        for change in evidence.changes:
            changed = normalize_relative_path(change.relative_path)
            changed_folded = changed.casefold()
            if change.kind == "dir":
                if not changed or folded == changed_folded or folded.startswith(changed_folded + "/"):
                    return True
            elif changed and folded == changed_folded:
                return True
        return False

    def _cross_branch_reusable_files(
        self,
        *,
        source: SnapshotEndpointPayload,
        source_entries: list[TreeEntry],
        target_entries: list[TreeEntry],
        evidence: FrozenTreeDiff,
    ) -> dict[str, SnapshotFilePayload]:
        changed_paths: set[str] = set()
        for change in evidence.changes:
            path = normalize_relative_path(change.relative_path)
            folded = path.casefold()
            if (
                change.action not in {"A", "M", "D", "R"}
                or change.kind not in {"file", "dir"}
                or (change.kind == "file" and not path)
                or folded in changed_paths
            ):
                raise _CrossBranchEvidenceError("path_ambiguity")
            changed_paths.add(folded)
        self._require_unambiguous_table_root(source_entries, evidence.source_root)
        self._require_unambiguous_table_root(target_entries, evidence.target_root)
        source_table = source.physical_path_filters["TABLE"]
        target_table = evidence.target_root
        source_tree = self._excel_entries_by_relative_path(source_entries, source_table)
        target_tree = self._excel_entries_by_relative_path(target_entries, target_table)
        source_files = self._reusable_files_by_path(source)
        if source_files is None or source.stats.failed_count != 0:
            raise _CrossBranchEvidenceError("source_snapshot_incomplete")
        source_by_relative = {
            path[len(normalize_relative_path(source_table)) + 1 :]: item
            for path, item in source_files.items()
        }

        for path in set(source_tree) ^ set(target_tree):
            if not self._changed_by_evidence(path, evidence):
                raise _CrossBranchEvidenceError("tree_diff_incomplete")

        reusable: dict[str, SnapshotFilePayload] = {}
        for relative in sorted(set(source_tree) & set(target_tree), key=str.casefold):
            changed = self._changed_by_evidence(relative, evidence)
            source_entry = source_tree[relative]
            target_entry = target_tree[relative]
            if (
                not changed
                and source_entry.size is not None
                and target_entry.size is not None
                and source_entry.size != target_entry.size
            ):
                raise _CrossBranchEvidenceError("tree_diff_inconsistent")
            if changed:
                continue
            source_file = source_by_relative.get(relative)
            if (
                source_file is None
                or source_file.error is not None
                or not source_file.content_hash
            ):
                raise _CrossBranchEvidenceError("source_snapshot_incomplete")
            reusable[normalize_relative_path(target_entry.path)] = source_file
        return reusable

    def _cross_branch_incremental_pair(
        self,
        source_record: Mapping[str, Any],
        source_revision: int,
        target_record: Mapping[str, Any],
        target_revision: int,
        *,
        source_content_identity: str | None = None,
        target_content_identity: str | None = None,
        timing: SnapshotPhaseTiming | None = None,
    ) -> tuple[SnapshotEndpointPayload, SnapshotEndpointPayload] | None:
        resolve_identity = getattr(self.provider, "resolve_branch_identity", None)
        summarize = getattr(self.provider, "summarize_frozen_tree_diff", None)
        if resolve_identity is None or summarize is None:
            return None
        try:
            source_url = canonicalize_svn_url(self._validate_url(source_record))
            target_url = canonicalize_svn_url(self._validate_url(target_record))
            if source_url == target_url:
                return None
            source_identity = resolve_identity(
                EndpointSpec(url=source_url, revision=source_revision)
            )
            target_identity = resolve_identity(
                EndpointSpec(url=target_url, revision=target_revision)
            )
            if (
                not self._frozen_identity_matches(
                    source_identity, source_url, source_revision
                )
                or not self._frozen_identity_matches(
                    target_identity, target_url, target_revision
                )
                or source_identity.repository_uuid != target_identity.repository_uuid
                or canonicalize_svn_url(source_identity.repository_root)
                != canonicalize_svn_url(target_identity.repository_root)
            ):
                raise _CrossBranchEvidenceError("repository_identity")
            source_entries = self._list_tree(
                EndpointSpec(url=source_url, revision=source_revision),
                timing=timing,
                side="source",
            )
            target_entries = self._list_tree(
                EndpointSpec(url=target_url, revision=target_revision),
                timing=timing,
                side="target",
            )
            source_physical = self.resolve_scope_paths(
                source_record, source_revision, entries=source_entries
            )
            target_physical = self.resolve_scope_paths(
                target_record, target_revision, entries=target_entries
            )
            if source_physical != target_physical:
                raise _CrossBranchEvidenceError("table_layout")
            source = self._snapshot_endpoint_from_entries(
                source_record,
                source_revision,
                source_entries,
                repository_uuid=source_content_identity or source_url,
                persistent_repository_uuid=source_identity.repository_uuid,
                timing=timing,
                side="source",
            )
            evidence_started = time.perf_counter()
            try:
                evidence = summarize(
                    source_identity,
                    source_physical["TABLE"],
                    target_identity,
                    target_physical["TABLE"],
                )
            finally:
                evidence_seconds = time.perf_counter() - evidence_started
                with self._cache_lock:
                    self._snapshot_reuse_counters["cross_branch_evidence_calls"] += 1
                    self._snapshot_reuse_counters[
                        "cross_branch_evidence_seconds"
                    ] += evidence_seconds
            if (
                evidence.repository_uuid != source_identity.repository_uuid
                or canonicalize_svn_url(evidence.source_canonical_url)
                != source_identity.canonical_url
                or canonicalize_svn_url(evidence.target_canonical_url)
                != target_identity.canonical_url
                or evidence.source_revision != source_revision
                or evidence.target_revision != target_revision
                or normalize_relative_path(evidence.source_root)
                != normalize_relative_path(source_physical["TABLE"])
                or normalize_relative_path(evidence.target_root)
                != normalize_relative_path(target_physical["TABLE"])
            ):
                raise _CrossBranchEvidenceError("evidence_identity")
            reusable = self._cross_branch_reusable_files(
                source=source,
                source_entries=source_entries,
                target_entries=target_entries,
                evidence=evidence,
            )
            target = self._snapshot_endpoint_from_entries(
                target_record,
                target_revision,
                target_entries,
                repository_uuid=target_content_identity or target_url,
                persistent_repository_uuid=target_identity.repository_uuid,
                reusable_files=reusable,
                reusable_content_revision=source_revision,
                reusable_content_url=source_url,
                reusable_content_repository_uuid=source_content_identity or source_url,
                require_matching_entry_revision=False,
                timing=timing,
                side="target",
            )
        except Exception as exc:
            reason = (
                exc.reason
                if isinstance(exc, _CrossBranchEvidenceError)
                else "provider_error"
            )
            with self._cache_lock:
                self._snapshot_reuse_counters["cross_branch_fallbacks"] += 1
                key = f"cross_branch_fallback.{reason}"
                self._snapshot_reuse_counters[key] = (
                    self._snapshot_reuse_counters.get(key, 0) + 1
                )
            return None
        with self._cache_lock:
            self._snapshot_reuse_counters["cross_branch_pairs"] += 1
            self._snapshot_reuse_counters["cross_branch_reused_files"] += len(reusable)
        return source, target

    def _snapshot_endpoint(self, record: Mapping[str, Any]) -> SnapshotEndpointPayload:
        resolved_revision, repository_uuid = self._resolve_head(record)
        if not isinstance(resolved_revision, int):
            raise SVNProviderError(
                "SVN_INVALID_REVISION",
                f"端点没有返回有效 HEAD Revision：{record['id']}",
            )
        return self._snapshot_endpoint_at_revision(
            record,
            resolved_revision,
            repository_uuid=repository_uuid,
        )

    def _snapshot_at_resolved_records(
        self,
        source_record: Mapping[str, Any],
        source_revision: int,
        target_record: Mapping[str, Any],
        target_revision: int,
        *,
        source_repository_uuid: str | None = None,
        target_repository_uuid: str | None = None,
        reuse: bool = True,
        timing: SnapshotPhaseTiming | None = None,
    ) -> SnapshotResponsePayload:
        return self._snapshot_at_resolved_records_impl(
            source_record,
            source_revision,
            target_record,
            target_revision,
            source_repository_uuid=source_repository_uuid,
            target_repository_uuid=target_repository_uuid,
            reuse=reuse,
            timing=timing,
        )

    @staticmethod
    def _build_snapshot_response(
        source: SnapshotEndpointPayload,
        target: SnapshotEndpointPayload,
        *,
        timing: SnapshotPhaseTiming | None,
    ) -> SnapshotResponsePayload:
        scope = (
            timing.phase("response.snapshot")
            if timing is not None
            else nullcontext()
        )
        with scope as observation:
            snapshot = SnapshotResponsePayload(
                captured_at=datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
                logical_scopes=list(LOGICAL_SCOPES),
                source=source,
                target=target,
            )
            if observation is not None:
                observation.result(
                    bytes_count=(
                        source.stats.total_size
                        + target.stats.total_size
                    ),
                    items=source.stats.file_count + target.stats.file_count,
                )
            return snapshot


    def _snapshot_reuse_lookup(
        self,
        key: str,
        identity: Mapping[str, Any],
        now: float,
        *,
        timing: SnapshotPhaseTiming | None,
    ) -> tuple[
        SnapshotResponsePayload | None,
        _SnapshotBuildFlight | None,
        bool,
    ]:
        scope = (
            timing.phase("reuse.snapshot_lookup")
            if timing is not None
            else nullcontext()
        )
        with scope as observation:
            with self._cache_lock:
                cached = self._load_snapshot_fact_locked(key, identity, now)
                if cached is not None:
                    flight = None
                    builder = False
                    status = "process_hot"
                else:
                    self._snapshot_reuse_counters["misses"] += 1
                    flight = self._snapshot_build_flights.get(key)
                    if flight is None:
                        flight = _SnapshotBuildFlight(
                            event=threading.Event(),
                            build_context_id=str(uuid4()),
                        )
                        self._snapshot_build_flights[key] = flight
                        self._snapshot_reuse_counters["builds"] += 1
                        builder = True
                        status = "builder"
                    else:
                        self._snapshot_reuse_counters["waits"] += 1
                        builder = False
                        status = "singleflight_waiter"
            if observation is not None:
                observation.result(source=status)
            return cached, flight, builder

    def _snapshot_at_resolved_records_impl(
        self,
        source_record: Mapping[str, Any],
        source_revision: int,
        target_record: Mapping[str, Any],
        target_revision: int,
        *,
        source_repository_uuid: str | None = None,
        target_repository_uuid: str | None = None,
        reuse: bool = True,
        timing: SnapshotPhaseTiming | None = None,
    ) -> SnapshotResponsePayload:
        if not reuse:
            build_context_id = str(uuid4())
            if timing is not None:
                timing.set_build_context(build_context_id, "reuse_disabled")
            source, target = self._snapshot_pair_at_revisions(
                source_record,
                source_revision,
                target_record,
                target_revision,
                source_repository_uuid=source_repository_uuid,
                target_repository_uuid=target_repository_uuid,
                timing=timing,
            )
            return self._build_snapshot_response(
                source,
                target,
                timing=timing,
            )
        identity_scope = (
            timing.phase("reuse.identity")
            if timing is not None
            else nullcontext()
        )
        with identity_scope as observation:
            identity = self._snapshot_reuse_identity(
                source_record,
                source_revision,
                target_record,
                target_revision,
            )
            key = self._hash_json(identity)
            if observation is not None:
                observation.result(items=1)
        now = self._monotonic_clock()
        cached, flight, builder = self._snapshot_reuse_lookup(
            key,
            identity,
            now,
            timing=timing,
        )
        if cached is not None:
            if timing is not None:
                timing.set_build_context(None, "process_hot")
            return cached
        if flight is None:
            raise RuntimeError("snapshot reuse lookup returned no build flight")
        if timing is not None:
            timing.set_build_context(
                flight.build_context_id,
                "builder" if builder else "singleflight_waiter",
            )
        if not builder:
            wait_scope = (
                timing.phase("reuse.singleflight_wait")
                if timing is not None
                else nullcontext()
            )
            with wait_scope:
                flight.event.wait()
            if flight.error is not None:
                raise flight.error
            if flight.result is None:
                raise RuntimeError("snapshot single-flight completed without a result")
            copy_scope = (
                timing.phase("response.singleflight_copy")
                if timing is not None
                else nullcontext()
            )
            with copy_scope:
                return flight.result.model_copy(deep=True)

        try:
            same_branch = self._same_branch_incremental_pair(
                source_record,
                source_revision,
                target_record,
                target_revision,
                timing=timing,
            )
            cross_branch = None
            if same_branch is None:
                cross_branch = self._cross_branch_incremental_pair(
                    source_record,
                    source_revision,
                    target_record,
                    target_revision,
                    source_content_identity=source_repository_uuid,
                    target_content_identity=target_repository_uuid,
                    timing=timing,
                )
            if same_branch is not None:
                source, target = same_branch
                reuse_mode = "same_branch_incremental"
            elif cross_branch is not None:
                source, target = cross_branch
                reuse_mode = "cross_branch_incremental"
            else:
                source, target = self._snapshot_pair_at_revisions(
                    source_record,
                    source_revision,
                    target_record,
                    target_revision,
                    source_repository_uuid=source_repository_uuid,
                    target_repository_uuid=target_repository_uuid,
                    timing=timing,
                )
                reuse_mode = "full_build"
            if timing is not None:
                timing.set_build_context(flight.build_context_id, reuse_mode)
            snapshot = self._build_snapshot_response(
                source,
                target,
                timing=timing,
            )
            with self._cache_lock:
                self._store_snapshot_fact_locked(
                    key,
                    identity,
                    snapshot,
                    self._monotonic_clock(),
                )
                flight.result = snapshot.model_copy(deep=True)
            return snapshot
        except BaseException as exc:
            with self._cache_lock:
                flight.error = exc
            raise
        finally:
            with self._cache_lock:
                self._snapshot_build_flights.pop(key, None)
                flight.event.set()

    def register_trusted_snapshot(
        self,
        records: list[Mapping[str, Any]],
        snapshot: SnapshotResponsePayload,
    ) -> bool:
        """Register a server-built snapshot after endpoint bindings are persisted."""
        try:
            normalized = self.normalize_registry([dict(record) for record in records])
            source_revision = snapshot.source.resolved_revision
            target_revision = snapshot.target.resolved_revision
            if not isinstance(source_revision, int) or not isinstance(target_revision, int):
                return False
            source_record = self._get_record(normalized, snapshot.source.endpoint_id)
            target_record = self._get_record(normalized, snapshot.target.endpoint_id)
            identity = self._snapshot_reuse_identity(
                source_record,
                source_revision,
                target_record,
                target_revision,
            )
        except Exception:
            return False
        key = self._hash_json(identity)
        with self._cache_lock:
            return self._store_snapshot_fact_locked(
                key,
                identity,
                snapshot,
                self._monotonic_clock(),
            )

    def create_snapshot(
        self,
        records: list[Mapping[str, Any]],
        *,
        source_id: str,
        target_id: str,
        source_revision: int | str = "HEAD",
        target_revision: int | str = "HEAD",
        request_context_id: str | None = None,
    ) -> SnapshotResponsePayload:
        timing = self._new_phase_timing(
            request_context_id=request_context_id,
            source_endpoint_id=source_id,
            source_revision=source_revision,
            target_endpoint_id=target_id,
            target_revision=target_revision,
        )
        outcome = "failed"
        try:
            result = self._create_snapshot_impl(
                records,
                source_id=source_id,
                target_id=target_id,
                source_revision=source_revision,
                target_revision=target_revision,
                timing=timing,
            )
            outcome = "succeeded"
            return result
        finally:
            self._finish_phase_timing(
                timing,
                outcome=outcome,
            )


    def _create_snapshot_impl(
        self,
        records: list[Mapping[str, Any]],
        *,
        source_id: str,
        target_id: str,
        source_revision: int | str = "HEAD",
        target_revision: int | str = "HEAD",
        timing: SnapshotPhaseTiming | None = None,
    ) -> SnapshotResponsePayload:
        normalized = self.normalize_registry([dict(record) for record in records])
        source_record = self._get_record(normalized, source_id)
        target_record = self._get_record(normalized, target_id)
        source_resolved, source_repository = (
            self._resolve_head(
                source_record,
                timing=timing,
                side="source",
            )
            if source_revision == "HEAD"
            else (int(source_revision), None)
        )
        target_resolved, target_repository = (
            self._resolve_head(
                target_record,
                timing=timing,
                side="target",
            )
            if target_revision == "HEAD"
            else (int(target_revision), None)
        )
        if not isinstance(source_resolved, int) or not isinstance(target_resolved, int):
            raise SVNProviderError(
                "SVN_INVALID_REVISION",
                "端点没有返回有效 HEAD Revision",
            )
        if timing is not None:
            timing.set_endpoint(
                "source",
                resolved_revision=source_resolved,
                repository_uuid=source_repository,
            )
            timing.set_endpoint("target", resolved_revision=target_resolved)

        if source_id == target_id and source_resolved == target_resolved:
            raise SVNProviderError(
                "SVN_ENDPOINT_REVISIONS_MUST_DIFFER",
                "同一分支的左右 Revision 必须不同",
            )
        return self._snapshot_at_resolved_records(
            source_record,
            source_resolved,
            target_record,
            target_resolved,
            source_repository_uuid=source_repository,
            target_repository_uuid=target_repository,
            timing=timing,
        )

    def create_snapshot_at_revisions(
        self,
        records: list[Mapping[str, Any]],
        *,
        source_id: str,
        source_revision: int,
        target_id: str,
        target_revision: int,
        reuse: bool = True,
        request_context_id: str | None = None,
    ) -> SnapshotResponsePayload:
        timing = self._new_phase_timing(
            request_context_id=request_context_id,
            source_endpoint_id=source_id,
            source_revision=source_revision,
            target_endpoint_id=target_id,
            target_revision=target_revision,
        )
        outcome = "failed"
        try:
            result = self._create_snapshot_at_revisions_impl(
                records,
                source_id=source_id,
                source_revision=source_revision,
                target_id=target_id,
                target_revision=target_revision,
                reuse=reuse,
                timing=timing,
            )
            outcome = "succeeded"
            return result
        finally:
            self._finish_phase_timing(
                timing,
                outcome=outcome,
            )


    def _create_snapshot_at_revisions_impl(
        self,
        records: list[Mapping[str, Any]],
        *,
        source_id: str,
        source_revision: int,
        target_id: str,
        target_revision: int,
        reuse: bool = True,
        timing: SnapshotPhaseTiming | None = None,
    ) -> SnapshotResponsePayload:
        """按请求中的两侧具体 Revision 重建权威 M1 快照。"""
        normalized = self.normalize_registry([dict(record) for record in records])
        source_record = self._get_record(normalized, source_id)
        target_record = self._get_record(normalized, target_id)
        if timing is not None:
            timing.set_endpoint("source", resolved_revision=source_revision)
            timing.set_endpoint("target", resolved_revision=target_revision)

        if source_id == target_id and source_revision == target_revision:
            raise SVNProviderError(
                "SVN_ENDPOINT_REVISIONS_MUST_DIFFER",
                "同一分支的左右 Revision 必须不同",
            )
        return self._snapshot_at_resolved_records(
            source_record,
            source_revision,
            target_record,
            target_revision,
            reuse=reuse,
            timing=timing,
        )

    @staticmethod
    def bind_snapshot_scopes(
        records: list[Mapping[str, Any]],
        snapshot: SnapshotResponsePayload,
        *,
        bind_source: bool = True,
        bind_target: bool = True,
    ) -> list[dict[str, Any]]:
        """将本次快照解析出的 Table 物理路径回写端点注册表。"""
        normalized = SnapshotService.normalize_registry([dict(record) for record in records])
        bindings = {}
        if bind_source:
            bindings[snapshot.source.endpoint_id] = snapshot.source.physical_path_filters
        if bind_target:
            bindings[snapshot.target.endpoint_id] = snapshot.target.physical_path_filters
        for record in normalized:
            physical = bindings.get(str(record["id"]))
            if physical:
                record["physical_path_filters"] = dict(physical)
        return normalized
    def discover_and_bind(
        self,
        records: list[Mapping[str, Any]],
        *,
        endpoint_id: str,
    ) -> list[dict[str, Any]]:
        normalized = self.normalize_registry([dict(record) for record in records])
        record = self._get_record(normalized, endpoint_id)
        revision = self.freeze_head(record)
        physical = self.discover_scope_paths(record, revision)
        updated = []
        for item in normalized:
            if item["id"] == endpoint_id:
                item = {**item, "physical_path_filters": physical}
            updated.append(item)
        return updated
