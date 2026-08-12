"""M1 双端点全量 Excel 快照服务。

用户只选择两个已注册端点；服务在任务开始时分别冻结 HEAD，
然后读取 TABLE 逻辑目录绑定的全部 Excel 文件。该层不执行 Diff。
"""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
import hashlib
import threading
from typing import Any, Mapping

from core.models import EndpointSpec, TreeEntry
from core.svn_provider import SVNProvider, SVNProviderError, normalize_relative_path, validate_endpoint
from app.schemas.svn import (
    EndpointRecordPayload,
    SnapshotEndpointPayload,
    SnapshotFilePayload,
    SnapshotResponsePayload,
    SnapshotStatsPayload,
)


LOGICAL_SCOPES = ("TABLE",)
EXCEL_EXTENSIONS = (".xlsx", ".xlsm", ".xls")


class SnapshotService:
    def __init__(
        self,
        provider: SVNProvider,
        *,
        allowed_schemes: tuple[str, ...],
        max_workers: int = 6,
        preview_limit: int = 262144,
    ):
        self.provider = provider
        self.allowed_schemes = allowed_schemes
        self.max_workers = max(1, int(max_workers))
        self.preview_limit = max(1, int(preview_limit))
        self._content_cache: dict[tuple[str, str, str, str], bytes] = {}
        self._cache_lock = threading.RLock()

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

    def _resolve_head(self, record: Mapping[str, Any]) -> tuple[int | str, str]:
        url = self._validate_url(record)
        info = self.provider.info(
            EndpointSpec(url=url, revision="HEAD", label=str(record.get("label", "")))
        )
        revision = str(info.revision or info.last_changed_revision).strip()
        if not revision:
            raise SVNProviderError("SVN_INVALID_REVISION", f"端点没有返回 HEAD Revision：{record['id']}")
        resolved = int(revision) if revision.isdigit() else revision
        repository_uuid = info.repository_uuid or info.repository_root or url
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
            self.provider.list_tree(endpoint),
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
            entries = self.provider.list_tree(endpoint)
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
    ) -> bytes:
        key = (
            repository_uuid,
            str(endpoint.url).rstrip("/"),
            normalize_relative_path(entry.path),
            str(endpoint.revision),
        )
        with self._cache_lock:
            cached = self._content_cache.get(key)
        if cached is not None:
            return cached

        reader = getattr(self.provider, "read_bytes", None)
        if reader is not None:
            raw = reader(endpoint, entry.path)
        else:
            # 兼容尚未实现 read_bytes 的第三方 Provider；仅作为过渡。
            content = self.provider.read_content(endpoint, entry.path, self.preview_limit)
            raw = content.text.encode("utf-8")

        with self._cache_lock:
            self._content_cache[key] = raw
        return raw

    def _fetch_file(
        self,
        endpoint: EndpointSpec,
        entry: TreeEntry,
        logical_scope: str,
        repository_uuid: str,
    ) -> SnapshotFilePayload:
        try:
            raw = self._read_binary(endpoint, entry, repository_uuid)
            content_hash = hashlib.sha256(raw).hexdigest()
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
        all_entries = self.provider.list_tree(endpoint)
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
        entries.sort(key=lambda item: item.path.casefold())
        files: list[SnapshotFilePayload] = []
        with ThreadPoolExecutor(max_workers=self.max_workers) as executor:
            futures = {
                executor.submit(
                    self._fetch_file,
                    endpoint,
                    entry,
                    self._scope_for_path(entry.path, physical),
                    repository_uuid or url,
                ): entry.path
                for entry in entries
            }
            for future in as_completed(futures):
                files.append(future.result())
        files.sort(key=lambda item: item.path.casefold())
        total_size = sum(item.size or 0 for item in files)
        failed_count = sum(1 for item in files if item.error is not None)
        return SnapshotEndpointPayload(
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

    def create_snapshot(
        self,
        records: list[Mapping[str, Any]],
        *,
        source_id: str,
        target_id: str,
        source_revision: int | str = "HEAD",
        target_revision: int | str = "HEAD",
    ) -> SnapshotResponsePayload:
        normalized = self.normalize_registry([dict(record) for record in records])
        source_record = self._get_record(normalized, source_id)
        target_record = self._get_record(normalized, target_id)
        source_resolved, source_repository = (
            self._resolve_head(source_record)
            if source_revision == "HEAD"
            else (int(source_revision), None)
        )
        target_resolved, target_repository = (
            self._resolve_head(target_record)
            if target_revision == "HEAD"
            else (int(target_revision), None)
        )
        if not isinstance(source_resolved, int) or not isinstance(target_resolved, int):
            raise SVNProviderError(
                "SVN_INVALID_REVISION",
                "端点没有返回有效 HEAD Revision",
            )
        if source_id == target_id and source_resolved == target_resolved:
            raise SVNProviderError(
                "SVN_ENDPOINT_REVISIONS_MUST_DIFFER",
                "同一分支的左右 Revision 必须不同",
            )
        source = self._snapshot_endpoint_at_revision(
            source_record,
            source_resolved,
            repository_uuid=source_repository,
        )
        target = self._snapshot_endpoint_at_revision(
            target_record,
            target_resolved,
            repository_uuid=target_repository,
        )
        return SnapshotResponsePayload(
            captured_at=datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
            logical_scopes=list(LOGICAL_SCOPES),
            source=source,
            target=target,
        )

    def create_snapshot_at_revisions(
        self,
        records: list[Mapping[str, Any]],
        *,
        source_id: str,
        source_revision: int,
        target_id: str,
        target_revision: int,
    ) -> SnapshotResponsePayload:
        """按请求中的两侧具体 Revision 重建权威 M1 快照。"""
        normalized = self.normalize_registry([dict(record) for record in records])
        source_record = self._get_record(normalized, source_id)
        target_record = self._get_record(normalized, target_id)
        if source_id == target_id and source_revision == target_revision:
            raise SVNProviderError(
                "SVN_ENDPOINT_REVISIONS_MUST_DIFFER",
                "同一分支的左右 Revision 必须不同",
            )
        source = self._snapshot_endpoint_at_revision(source_record, source_revision)
        target = self._snapshot_endpoint_at_revision(target_record, target_revision)
        return SnapshotResponsePayload(
            captured_at=datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
            logical_scopes=list(LOGICAL_SCOPES),
            source=source,
            target=target,
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
