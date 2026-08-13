"""单工作簿 Web API 的可替换数据集解析与 SVN 物化边界。"""
from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import dataclass, field
import logging
from pathlib import Path, PurePosixPath
import posixpath
from tempfile import TemporaryDirectory
from typing import Any, Protocol

from app.schemas.workbook_compare import WorkbookCompareRequestPayload
from app.services.directory_fact_cache import DirectoryFactCache
from core.m2_errors import M2ProcessingError
from core.models import EndpointSpec
from core.svn_provider import (
    SVNProvider,
    SVNProviderError,
    normalize_relative_path,
    validate_endpoint,
)
from core.svn_history import canonicalize_svn_url
from core.workbook_manifest_parser import WorkbookManifest, parse_workbook_manifest


logger = logging.getLogger(__name__)
EXCEL_EXTENSIONS = (".xlsx", ".xlsm", ".xls")
_MISSING_PATH_CODES = {"SVN_NOT_FOUND", "SVN_PATH_NOT_FOUND"}
_DIRECTORY_FACT_CACHE_SIZE = 256
_CSV_READ_WORKERS = 4


@dataclass(frozen=True)
class _DirectoryFactKey:
    kind: str
    endpoint_id: str
    canonical_url: str
    revision: int | str
    physical_paths: tuple[tuple[str, str], ...]
    table_directory_name: str
    csv_directory_name: str
    table_directory: str = ""


@dataclass
class WorkbookDataset:
    source_directory: Path
    target_directory: Path
    _cleanup: Callable[[], None] | None = field(
        default=None,
        repr=False,
        compare=False,
    )
    _closed: bool = field(default=False, init=False, repr=False, compare=False)

    def close(self) -> None:
        if not self._closed and self._cleanup is not None:
            self._cleanup()
        self._closed = True

    def __enter__(self) -> "WorkbookDataset":
        return self

    def __exit__(self, exc_type, exc, traceback) -> None:
        self.close()


class WorkbookCompareError(Exception):
    def __init__(self, code: str, message: str, *, status_code: int):
        super().__init__(message)
        self.code = code
        self.message = message
        self.status_code = status_code


class WorkbookDatasetResolver(Protocol):
    def resolve(self, payload: WorkbookCompareRequestPayload) -> WorkbookDataset:
        """把可信快照身份解析为本次比较使用的隔离本地数据集。"""


class UnavailableWorkbookDatasetResolver:
    def resolve(self, payload: WorkbookCompareRequestPayload) -> WorkbookDataset:
        raise WorkbookCompareError(
            "DIFF_DATASET_UNAVAILABLE",
            "当前尚未配置该冻结 Revision 的工作簿数据集",
            status_code=404,
        )


@dataclass(frozen=True)
class BoundWorkbookDatasetResolver:
    """供自动化测试或显式本地验证使用的固定身份绑定。"""

    source_endpoint_id: str
    source_revision: int
    target_endpoint_id: str
    target_revision: int
    workbook_path: str
    source_directory: Path
    target_directory: Path
    candidate_status: str = "modified"

    def resolve(self, payload: WorkbookCompareRequestPayload) -> WorkbookDataset:
        known_endpoint_ids = {self.source_endpoint_id, self.target_endpoint_id}
        if (
            payload.source.endpoint_id not in known_endpoint_ids
            or payload.target.endpoint_id not in known_endpoint_ids
        ):
            raise WorkbookCompareError(
                "DIFF_ENDPOINT_NOT_FOUND",
                "请求中的端点不存在或未启用",
                status_code=404,
            )
        if (
            payload.source.endpoint_id != self.source_endpoint_id
            or payload.target.endpoint_id != self.target_endpoint_id
            or payload.source.revision != self.source_revision
            or payload.target.revision != self.target_revision
        ):
            raise WorkbookCompareError(
                "DIFF_SNAPSHOT_CONTEXT_MISMATCH",
                "端点或 Revision 与冻结任务上下文不一致",
                status_code=409,
            )
        if payload.workbook_path != self.workbook_path:
            raise WorkbookCompareError(
                "DIFF_WORKBOOK_NOT_FOUND",
                "冻结 Revision 下不存在该工作簿",
                status_code=404,
            )
        if self.candidate_status != "modified":
            raise WorkbookCompareError(
                "DIFF_CANDIDATE_NOT_COMPARABLE",
                "当前候选不是左右均存在的 modified 工作簿",
                status_code=422,
            )
        return WorkbookDataset(
            source_directory=self.source_directory,
            target_directory=self.target_directory,
        )


class SVNWorkbookDatasetResolver:
    """从两侧冻结 Revision 只读物化单工作簿及其清单 CSV。"""

    def __init__(
        self,
        provider: SVNProvider,
        endpoint_registry: Callable[[], Sequence[Mapping[str, Any]]],
        dataset_layout: Mapping[str, Any],
        *,
        allowed_schemes: tuple[str, ...],
    ):
        self.provider = provider
        self.endpoint_registry = endpoint_registry
        self.allowed_schemes = allowed_schemes
        self._directory_facts = DirectoryFactCache[_DirectoryFactKey](
            _DIRECTORY_FACT_CACHE_SIZE
        )
        try:
            workbook_source = dict(dataset_layout["workbook_source"])
            csv_export = dict(dataset_layout["csv_export"])
            manifest = dict(dataset_layout["manifest"])
            self.table_directory_name = str(workbook_source["directory_name"])
            self.csv_directory_name = str(csv_export["directory_name"])
            self.csv_filename_template = str(csv_export["filename_template"])
            self.manifest_sheet_name = str(manifest["sheet_name"])
            self.manifest_sheet_field = str(manifest["sheet_field"])
            self.manifest_csv_name_field = str(manifest["csv_name_field"])
            self.manifest_export_flag_field = str(manifest["export_flag_field"])
        except (KeyError, TypeError, ValueError) as exc:
            raise ValueError("dataset_layout 缺少 SVN 数据集物化配置") from exc

    def _record(self, endpoint_id: str) -> Mapping[str, Any]:
        for record in self.endpoint_registry():
            if str(record.get("id", "")) != endpoint_id:
                continue
            if not bool(record.get("enabled", True)):
                break
            return record
        raise WorkbookCompareError(
            "DIFF_ENDPOINT_NOT_FOUND",
            "请求中的端点不存在或未启用",
            status_code=404,
        )

    def _endpoint(self, record: Mapping[str, Any], revision: int) -> EndpointSpec:
        try:
            return validate_endpoint(
                EndpointSpec(
                    url=str(record.get("url", "")),
                    revision=revision,
                    label=str(record.get("label", "")),
                ),
                self.allowed_schemes,
            )
        except SVNProviderError as exc:
            raise WorkbookCompareError(
                "DIFF_DATASET_CONFIG_INVALID",
                "冻结端点配置无效",
                status_code=500,
            ) from exc

    def _provider_failure(self, exc: SVNProviderError) -> WorkbookCompareError:
        logger.warning("冻结 Revision 数据集读取失败 code=%s", exc.code)
        return WorkbookCompareError(
            "DIFF_DATASET_READ_FAILED",
            "无法读取冻结 Revision 数据集",
            status_code=500,
        )

    def _directory_fact_key(
        self,
        kind: str,
        record: Mapping[str, Any],
        endpoint: EndpointSpec,
        *,
        table_directory: str = "",
    ) -> _DirectoryFactKey:
        physical_paths = tuple(
            sorted(
                (
                    str(logical).strip().upper(),
                    str(path).strip(),
                )
                for logical, path in dict(
                    record.get("physical_path_filters") or {}
                ).items()
            )
        )
        return _DirectoryFactKey(
            kind=kind,
            endpoint_id=str(record.get("id", "")),
            canonical_url=canonicalize_svn_url(endpoint.url),
            revision=endpoint.revision,
            physical_paths=physical_paths,
            table_directory_name=self.table_directory_name,
            csv_directory_name=self.csv_directory_name,
            table_directory=table_directory,
        )

    def _table_directory(
        self,
        record: Mapping[str, Any],
        endpoint: EndpointSpec,
    ) -> str:
        configured = None
        for logical, path in dict(record.get("physical_path_filters") or {}).items():
            if str(logical).strip().upper() == "TABLE" and path:
                try:
                    configured = normalize_relative_path(str(path))
                except SVNProviderError as exc:
                    raise WorkbookCompareError(
                        "DIFF_DATASET_CONFIG_INVALID",
                        "冻结端点的 TABLE 目录配置无效",
                        status_code=500,
                    ) from exc

        try:
            entries = self.provider.list_tree(endpoint)
        except SVNProviderError as exc:
            raise self._provider_failure(exc) from exc
        directories = {
            normalize_relative_path(entry.path)
            for entry in entries
            if entry.kind == "dir"
        }
        for entry in entries:
            if entry.kind != "file":
                continue
            parts = normalize_relative_path(entry.path).split("/")
            directories.update("/".join(parts[:index]) for index in range(1, len(parts)))
        candidates = [
            path
            for path in directories
            if PurePosixPath(path).name.casefold()
            == self.table_directory_name.casefold()
        ]
        if not candidates:
            raise WorkbookCompareError(
                "DIFF_WORKBOOK_NOT_FOUND",
                "冻结 Revision 下不存在该工作簿",
                status_code=404,
            )
        if configured:
            configured_folded = configured.casefold()
            configured_matches = sorted(
                (
                    path
                    for path in directories
                    if path.casefold() == configured_folded
                ),
                key=lambda path: path.casefold(),
            )
            if configured_matches:
                return configured_matches[0]
        return sorted(
            candidates,
            key=lambda path: (path.count("/"), path.casefold()),
        )[0]

    def _cached_table_directory(
        self,
        record: Mapping[str, Any],
        endpoint: EndpointSpec,
    ) -> str:
        def discover() -> str | None:
            try:
                return self._table_directory(record, endpoint)
            except WorkbookCompareError as exc:
                if exc.code == "DIFF_WORKBOOK_NOT_FOUND":
                    return None
                raise

        result = self._directory_facts.get_or_load(
            self._directory_fact_key("table", record, endpoint),
            discover,
        )
        if result is None:
            raise WorkbookCompareError(
                "DIFF_WORKBOOK_NOT_FOUND",
                "冻结 Revision 下不存在该工作簿",
                status_code=404,
            )
        return result

    def _csv_directory(
        self,
        endpoint: EndpointSpec,
        table_directory: str,
    ) -> str | None:
        parent = posixpath.dirname(table_directory)
        try:
            children = self.provider.list_children(endpoint, parent)
        except SVNProviderError as exc:
            if exc.code in _MISSING_PATH_CODES:
                raise
            raise self._provider_failure(exc) from exc
        candidates = [
            normalize_relative_path(entry.path)
            for entry in children
            if entry.kind == "dir"
            and PurePosixPath(normalize_relative_path(entry.path)).name.casefold()
            == self.csv_directory_name.casefold()
        ]
        if len(candidates) > 1:
            raise WorkbookCompareError(
                "DIFF_DATASET_CONFIG_INVALID",
                "冻结端点存在多个同级 TableCsv 目录",
                status_code=500,
            )
        return candidates[0] if candidates else None

    def _cached_csv_directory(
        self,
        record: Mapping[str, Any],
        endpoint: EndpointSpec,
        table_directory: str,
    ) -> str | None:
        try:
            return self._directory_facts.get_or_load(
                self._directory_fact_key(
                    "csv",
                    record,
                    endpoint,
                    table_directory=table_directory,
                ),
                lambda: self._csv_directory(endpoint, table_directory),
            )
        except SVNProviderError as exc:
            if exc.code in _MISSING_PATH_CODES:
                return None
            raise

    @staticmethod
    def _join(*parts: str) -> str:
        return normalize_relative_path(posixpath.join(*parts))

    def _read_workbook(
        self,
        endpoint: EndpointSpec,
        table_directory: str,
        workbook_path: str,
    ) -> bytes | None:
        path = self._join(table_directory, workbook_path)
        try:
            return self.provider.read_bytes(endpoint, path)
        except SVNProviderError as exc:
            if exc.code in _MISSING_PATH_CODES:
                return None
            raise self._provider_failure(exc) from exc

    def _manifest(self, raw: bytes) -> WorkbookManifest | None:
        try:
            return parse_workbook_manifest(
                raw,
                sheet_name=self.manifest_sheet_name,
                sheet_field=self.manifest_sheet_field,
                csv_name_field=self.manifest_csv_name_field,
                export_flag_field=self.manifest_export_flag_field,
            )
        except M2ProcessingError:
            # 工作簿原样交给 Diff 服务，使清单错误保持 HTTP 200 业务结果。
            return None

    def _read_csv_files(
        self,
        endpoint: EndpointSpec,
        csv_directory: str | None,
        manifest: WorkbookManifest,
    ) -> dict[str, bytes]:
        if csv_directory is None:
            return {}
        result: dict[str, bytes] = {}
        casefold_paths: dict[str, list[str]] | None = None
        for entry in manifest.entries:
            filename = self.csv_filename_template.format(tbxName=entry.tbx_name)
            if filename in result:
                continue
            try:
                raw = self.provider.read_bytes(
                    endpoint,
                    self._join(csv_directory, filename),
                )
            except SVNProviderError as exc:
                if exc.code not in _MISSING_PATH_CODES:
                    raise self._provider_failure(exc) from exc
                if casefold_paths is None:
                    try:
                        children = self.provider.list_children(
                            endpoint,
                            csv_directory,
                        )
                    except SVNProviderError as list_exc:
                        if list_exc.code in _MISSING_PATH_CODES:
                            casefold_paths = {}
                        else:
                            raise self._provider_failure(list_exc) from list_exc
                    else:
                        casefold_paths = {}
                        for child in children:
                            if child.kind != "file":
                                continue
                            child_path = normalize_relative_path(child.path)
                            child_name = PurePosixPath(child_path).name
                            casefold_paths.setdefault(
                                child_name.casefold(),
                                [],
                            ).append(child_path)
                matches = sorted(
                    set(casefold_paths.get(filename.casefold(), [])),
                    key=str.casefold,
                )
                if len(matches) > 1:
                    raise WorkbookCompareError(
                        "DIFF_DATASET_CONFIG_INVALID",
                        "冻结 Revision 的 TableCsv 文件名大小写匹配不唯一",
                        status_code=500,
                    )
                if not matches:
                    continue
                try:
                    raw = self.provider.read_bytes(endpoint, matches[0])
                except SVNProviderError as retry_exc:
                    if retry_exc.code in _MISSING_PATH_CODES:
                        continue
                    raise self._provider_failure(retry_exc) from retry_exc
            result[filename] = raw
        return result
    def _csv_filenames(self, manifest: WorkbookManifest) -> tuple[str, ...]:
        result: list[str] = []
        seen: set[str] = set()
        for entry in manifest.entries:
            filename = self.csv_filename_template.format(tbxName=entry.tbx_name)
            if filename in seen:
                continue
            seen.add(filename)
            result.append(filename)
        return tuple(result)

    def _read_csv_side(
        self,
        executor: ThreadPoolExecutor,
        endpoint: EndpointSpec,
        csv_directory: str | None,
        filenames: tuple[str, ...],
        exact_futures: Mapping[str, Future[bytes]],
    ) -> dict[str, bytes]:
        if csv_directory is None:
            return {}
        result: dict[str, bytes] = {}
        exact_outcomes: dict[str, bytes | Exception] = {}
        for filename in filenames:
            try:
                exact_outcomes[filename] = exact_futures[filename].result()
            except Exception as exc:
                exact_outcomes[filename] = exc

        casefold_paths: dict[str, list[str]] | None = None
        retry_matches: dict[str, tuple[str, ...]] = {}
        retry_futures: dict[str, Future[bytes]] = {}

        def prepare_retries() -> None:
            nonlocal casefold_paths
            try:
                children = self.provider.list_children(endpoint, csv_directory)
            except SVNProviderError as exc:
                if exc.code in _MISSING_PATH_CODES:
                    casefold_paths = {}
                else:
                    raise self._provider_failure(exc) from exc
            else:
                casefold_paths = {}
                for child in children:
                    if child.kind != "file":
                        continue
                    child_path = normalize_relative_path(child.path)
                    child_name = PurePosixPath(child_path).name
                    casefold_paths.setdefault(
                        child_name.casefold(), []
                    ).append(child_path)
            paths: list[str] = []
            for missing_filename in filenames:
                outcome = exact_outcomes[missing_filename]
                if not (
                    isinstance(outcome, SVNProviderError)
                    and outcome.code in _MISSING_PATH_CODES
                ):
                    continue
                matches = tuple(
                    sorted(
                        set(
                            casefold_paths.get(
                                missing_filename.casefold(), []
                            )
                        ),
                        key=str.casefold,
                    )
                )
                retry_matches[missing_filename] = matches
                if len(matches) == 1:
                    paths.append(matches[0])
            retry_futures.update(
                {
                    path: executor.submit(
                        self.provider.read_bytes, endpoint, path
                    )
                    for path in dict.fromkeys(paths)
                }
            )

        for filename in filenames:
            outcome = exact_outcomes[filename]
            if isinstance(outcome, Exception):
                if not (
                    isinstance(outcome, SVNProviderError)
                    and outcome.code in _MISSING_PATH_CODES
                ):
                    if isinstance(outcome, SVNProviderError):
                        raise self._provider_failure(outcome) from outcome
                    raise outcome
                if casefold_paths is None:
                    prepare_retries()
                matches = retry_matches[filename]
                if len(matches) > 1:
                    raise WorkbookCompareError(
                        "DIFF_DATASET_CONFIG_INVALID",
                        "冻结 Revision 的 TableCsv 文件名大小写匹配不唯一",
                        status_code=500,
                    )
                if not matches:
                    continue
                try:
                    outcome = retry_futures[matches[0]].result()
                except SVNProviderError as exc:
                    if exc.code in _MISSING_PATH_CODES:
                        continue
                    raise self._provider_failure(exc) from exc
            result[filename] = outcome
        return {
            filename: result[filename]
            for filename in filenames
            if filename in result
        }

    def _read_csv_files_pair(
        self,
        source_endpoint: EndpointSpec,
        source_csv_directory: str | None,
        source_manifest: WorkbookManifest,
        target_endpoint: EndpointSpec,
        target_csv_directory: str | None,
        target_manifest: WorkbookManifest,
    ) -> tuple[dict[str, bytes], dict[str, bytes]]:
        source_filenames = self._csv_filenames(source_manifest)
        target_filenames = self._csv_filenames(target_manifest)
        executor = ThreadPoolExecutor(
            max_workers=_CSV_READ_WORKERS,
            thread_name_prefix="m2-csv-read",
        )
        try:
            source_futures = {
                filename: executor.submit(
                    self.provider.read_bytes,
                    source_endpoint,
                    self._join(source_csv_directory, filename),
                )
                for filename in source_filenames
                if source_csv_directory is not None
            }
            target_futures = {
                filename: executor.submit(
                    self.provider.read_bytes,
                    target_endpoint,
                    self._join(target_csv_directory, filename),
                )
                for filename in target_filenames
                if target_csv_directory is not None
            }
            source = self._read_csv_side(
                executor,
                source_endpoint,
                source_csv_directory,
                source_filenames,
                source_futures,
            )
            target = self._read_csv_side(
                executor,
                target_endpoint,
                target_csv_directory,
                target_filenames,
                target_futures,
            )
        finally:
            executor.shutdown(wait=True, cancel_futures=True)
        return source, target


    @staticmethod
    def _write_side(
        directory: Path,
        workbook_name: str,
        workbook_raw: bytes,
        csv_files: Mapping[str, bytes],
    ) -> None:
        directory.mkdir(parents=True, exist_ok=False)
        (directory / workbook_name).write_bytes(workbook_raw)
        for filename, raw in csv_files.items():
            (directory / filename).write_bytes(raw)

    def resolve(self, payload: WorkbookCompareRequestPayload) -> WorkbookDataset:
        workbook_name = PurePosixPath(payload.workbook_path).name
        if PurePosixPath(workbook_name).suffix.casefold() not in EXCEL_EXTENSIONS:
            raise WorkbookCompareError(
                "DIFF_CANDIDATE_NOT_COMPARABLE",
                "当前候选不是左右均存在的 modified 工作簿",
                status_code=422,
            )

        source_record = self._record(payload.source.endpoint_id)
        target_record = self._record(payload.target.endpoint_id)
        source_endpoint = self._endpoint(source_record, payload.source.revision)
        target_endpoint = self._endpoint(target_record, payload.target.revision)
        source_table = self._cached_table_directory(source_record, source_endpoint)
        target_table = self._cached_table_directory(target_record, target_endpoint)
        source_raw = self._read_workbook(
            source_endpoint,
            source_table,
            payload.workbook_path,
        )
        target_raw = self._read_workbook(
            target_endpoint,
            target_table,
            payload.workbook_path,
        )
        if source_raw is None and target_raw is None:
            raise WorkbookCompareError(
                "DIFF_WORKBOOK_NOT_FOUND",
                "冻结 Revision 下不存在该工作簿",
                status_code=404,
            )
        if source_raw is None or target_raw is None or source_raw == target_raw:
            raise WorkbookCompareError(
                "DIFF_CANDIDATE_NOT_COMPARABLE",
                "当前候选不是左右均存在的 modified 工作簿",
                status_code=422,
            )

        source_manifest = self._manifest(source_raw)
        target_manifest = self._manifest(target_raw)
        source_csv: dict[str, bytes] = {}
        target_csv: dict[str, bytes] = {}
        if source_manifest is not None and target_manifest is not None:
            source_csv_directory = self._cached_csv_directory(
                source_record,
                source_endpoint,
                source_table,
            )
            target_csv_directory = self._cached_csv_directory(
                target_record,
                target_endpoint,
                target_table,
            )
            source_csv, target_csv = self._read_csv_files_pair(
                source_endpoint,
                source_csv_directory,
                source_manifest,
                target_endpoint,
                target_csv_directory,
                target_manifest,
            )

        temporary = TemporaryDirectory(prefix="excel-merge-diff-")
        root = Path(temporary.name)
        source_directory = root / "source"
        target_directory = root / "target"
        try:
            self._write_side(
                source_directory,
                workbook_name,
                source_raw,
                source_csv,
            )
            self._write_side(
                target_directory,
                workbook_name,
                target_raw,
                target_csv,
            )
        except Exception:
            temporary.cleanup()
            raise
        return WorkbookDataset(
            source_directory=source_directory,
            target_directory=target_directory,
            _cleanup=temporary.cleanup,
        )
