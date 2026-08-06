"""Deterministic, self-contained fixtures for offline M2 Diff replay."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from hashlib import sha256
from io import BytesIO
import json
from pathlib import Path, PurePosixPath
from tempfile import TemporaryDirectory
from threading import RLock
from typing import Any, Literal, Mapping, Sequence
from uuid import UUID
from zipfile import ZIP_DEFLATED, ZIP_STORED, BadZipFile, ZipFile, ZipInfo

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from app.schemas.batch import BatchEndpointPayload, BatchTaskPayload
from app.schemas.diff import DiffResultPayload, serialize_diff_json
from app.schemas.workbook_compare import WorkbookCompareRequestPayload
from app.services.batch_store import BatchStore
from app.services.workbook_dataset_service import WorkbookDatasetResolver
from app.services.workbook_diff_service import DatasetLayout, WorkbookDiffService
from core.m2_errors import M2ProcessingError
from core.workbook_manifest_parser import parse_workbook_manifest


FIXTURE_SCHEMA_VERSION = "m2.fixture.v1"
INPUTS_SCHEMA_VERSION = "m2.fixture-inputs.v1"
MISSING_SCHEMA_VERSION = "m2.fixture-missing.v1"
AUDIT_SCHEMA_VERSION = "m2.fixture-audit.v1"
SESSION_SCHEMA_VERSION = "m2.fixture-session.v1"
_HASH_PATTERN = r"^[0-9a-f]{64}$"
_CONTROL_PATHS = {
    "expected/task.json",
    "inputs.json",
    "missing-files.json",
    "audit/task-items.json",
}


class OfflineFixtureError(Exception):
    def __init__(self, code: str, message: str, *, status_code: int = 400):
        super().__init__(message)
        self.code = code
        self.message = message
        self.status_code = status_code


class _StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


def _portable_relative_path(value: str, *, filename_only: bool = False) -> str:
    if not isinstance(value, str) or not value or len(value) > 1024:
        raise ValueError("路径不能为空或过长")
    if "\\" in value or "\x00" in value or ":" in value:
        raise ValueError("路径必须是可移植的 POSIX 相对路径")
    path = PurePosixPath(value)
    if path.is_absolute() or any(part in {"", ".", ".."} for part in path.parts):
        raise ValueError("路径必须是可移植的 POSIX 相对路径")
    if path.as_posix() != value:
        raise ValueError("路径不是规范形式")
    if filename_only and path.name != value:
        raise ValueError("文件名不能包含目录")
    return value


class FixtureFilePayload(_StrictModel):
    sha256: str = Field(..., pattern=_HASH_PATTERN, strict=True)
    size_bytes: int = Field(..., ge=0, strict=True)
    kind: Literal["metadata", "audit", "input_blob", "golden_result"]


class FixtureResultPayload(_StrictModel):
    item_id: UUID
    ordinal: int = Field(..., ge=0, strict=True)
    workbook_path: str = Field(..., min_length=1, max_length=1024, strict=True)
    result_path: str = Field(..., min_length=1, max_length=1024, strict=True)
    sha256: str = Field(..., pattern=_HASH_PATTERN, strict=True)
    size_bytes: int = Field(..., ge=0, strict=True)

    @field_validator("workbook_path", "result_path")
    @classmethod
    def validate_paths(cls, value: str) -> str:
        return _portable_relative_path(value)


class OfflineFixtureManifest(_StrictModel):
    schema_version: Literal[FIXTURE_SCHEMA_VERSION]
    fixture_id: UUID
    task_id: UUID
    captured_at: datetime
    source: BatchEndpointPayload
    target: BatchEndpointPayload
    dataset_layout: dict[str, Any]
    results: list[FixtureResultPayload]
    files: dict[str, FixtureFilePayload]

    @field_validator("files")
    @classmethod
    def validate_file_paths(
        cls,
        value: dict[str, FixtureFilePayload],
    ) -> dict[str, FixtureFilePayload]:
        for path in value:
            _portable_relative_path(path)
            if path == "manifest.json":
                raise ValueError("manifest.json 不能自引用")
        if len({path.casefold() for path in value}) != len(value):
            raise ValueError("文件路径大小写重复")
        return value

    @model_validator(mode="after")
    def validate_identity(self) -> "OfflineFixtureManifest":
        if self.fixture_id != self.task_id:
            raise ValueError("fixture_id 必须等于 task_id")
        DatasetLayout.from_config(self.dataset_layout)
        return self


class FixtureInputPayload(_StrictModel):
    side: Literal["source", "target"]
    workbook_path: str = Field(..., min_length=1, max_length=1024, strict=True)
    filename: str = Field(..., min_length=1, max_length=255, strict=True)
    kind: Literal["workbook", "csv"]
    blob_sha256: str = Field(..., pattern=_HASH_PATTERN, strict=True)
    size_bytes: int = Field(..., ge=0, strict=True)

    @field_validator("workbook_path")
    @classmethod
    def validate_workbook_path(cls, value: str) -> str:
        return _portable_relative_path(value)

    @field_validator("filename")
    @classmethod
    def validate_filename(cls, value: str) -> str:
        return _portable_relative_path(value, filename_only=True)


class FixtureInputsPayload(_StrictModel):
    schema_version: Literal[INPUTS_SCHEMA_VERSION]
    inputs: list[FixtureInputPayload]


class FixtureMissingFilePayload(_StrictModel):
    side: Literal["source", "target"]
    workbook_path: str = Field(..., min_length=1, max_length=1024, strict=True)
    filename: str = Field(..., min_length=1, max_length=255, strict=True)
    kind: Literal["csv"] = "csv"
    reason: Literal["not_found"] = "not_found"

    @field_validator("workbook_path")
    @classmethod
    def validate_workbook_path(cls, value: str) -> str:
        return _portable_relative_path(value)

    @field_validator("filename")
    @classmethod
    def validate_filename(cls, value: str) -> str:
        return _portable_relative_path(value, filename_only=True)


class FixtureMissingFilesPayload(_StrictModel):
    schema_version: Literal[MISSING_SCHEMA_VERSION]
    missing_files: list[FixtureMissingFilePayload]


class FixtureAuditItemPayload(_StrictModel):
    item_id: UUID
    ordinal: int = Field(..., ge=0, strict=True)
    workbook_path: str
    status: str
    diff_status: str | None = None
    diff_error_count: int | None = None
    attempt_count: int = Field(..., ge=0, strict=True)
    recovery_count: int = Field(..., ge=0, strict=True)
    created_at: datetime
    started_at: datetime | None = None
    finished_at: datetime | None = None
    result_sha256: str | None = Field(default=None, pattern=_HASH_PATTERN)
    result_size_bytes: int | None = Field(default=None, ge=0)

    @field_validator("workbook_path")
    @classmethod
    def validate_workbook_path(cls, value: str) -> str:
        return _portable_relative_path(value)


class FixtureAuditPayload(_StrictModel):
    schema_version: Literal[AUDIT_SCHEMA_VERSION]
    task_id: UUID
    task_status: str
    captured_at: datetime
    items: list[FixtureAuditItemPayload]


@dataclass(frozen=True)
class FixtureLimits:
    max_archive_bytes: int = 256 * 1024 * 1024
    max_uncompressed_bytes: int = 512 * 1024 * 1024
    max_member_bytes: int = 128 * 1024 * 1024
    max_entries: int = 10_000


@dataclass(frozen=True)
class FixtureInputData:
    side: Literal["source", "target"]
    workbook_path: str
    filename: str
    kind: Literal["workbook", "csv"]
    content: bytes


@dataclass(frozen=True)
class LoadedOfflineFixture:
    archive_sha256: str
    manifest: OfflineFixtureManifest
    task: BatchTaskPayload
    inputs: FixtureInputsPayload
    missing_files: FixtureMissingFilesPayload
    audit: FixtureAuditPayload
    members: Mapping[str, bytes]
    golden_results: Mapping[str, bytes]

    def materialize(self, workbook_path: str):
        return _MaterializedFixtureDataset(self, workbook_path)


class _MaterializedFixtureDataset:
    def __init__(self, fixture: LoadedOfflineFixture, workbook_path: str):
        self.fixture = fixture
        self.workbook_path = _portable_relative_path(workbook_path)
        self._temporary: TemporaryDirectory[str] | None = None
        self.source_directory: Path | None = None
        self.target_directory: Path | None = None

    def __enter__(self) -> "_MaterializedFixtureDataset":
        temporary = TemporaryDirectory(prefix="excel-merge-replay-")
        self._temporary = temporary
        root = Path(temporary.name)
        self.source_directory = root / "source"
        self.target_directory = root / "target"
        self.source_directory.mkdir()
        self.target_directory.mkdir()
        selected = [
            item
            for item in self.fixture.inputs.inputs
            if item.workbook_path == self.workbook_path
        ]
        for item in selected:
            directory = (
                self.source_directory if item.side == "source" else self.target_directory
            )
            blob = self.fixture.members[f"blobs/{item.blob_sha256}"]
            (directory / item.filename).write_bytes(blob)
        workbook_name = PurePosixPath(self.workbook_path).name
        for directory in (self.source_directory, self.target_directory):
            if not (directory / workbook_name).is_file():
                self.__exit__(None, None, None)
                raise OfflineFixtureError(
                    "FIXTURE_INPUT_INCOMPLETE",
                    "离线夹具缺少工作簿原始文件",
                )
        return self

    def __exit__(self, exc_type, exc, traceback) -> None:
        if self._temporary is not None:
            self._temporary.cleanup()
            self._temporary = None


def _json_bytes(value: Any) -> bytes:
    return (
        json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            indent=2,
            separators=(",", ": "),
        )
        + "\n"
    ).encode("utf-8")


def _hash(raw: bytes) -> str:
    return sha256(raw).hexdigest()


def _member_metadata(raw: bytes, kind: str) -> dict[str, Any]:
    return {"sha256": _hash(raw), "size_bytes": len(raw), "kind": kind}


def _result_path(item_id: UUID | str) -> str:
    return f"expected/results/{item_id}.json"


def create_offline_fixture_bytes(
    *,
    task: BatchTaskPayload,
    dataset_layout: Mapping[str, Any],
    input_files: Sequence[FixtureInputData],
    missing_files: Sequence[FixtureMissingFilePayload],
    golden_results: Mapping[str, bytes],
) -> bytes:
    """Build a byte-for-byte deterministic fixture archive."""
    DatasetLayout.from_config(dataset_layout)
    expected_result_ids = {
        str(item.item_id) for item in task.items if item.result_ref is not None
    }
    if set(golden_results) != expected_result_ids:
        raise OfflineFixtureError(
            "FIXTURE_RESULT_SET_INVALID",
            "黄金结果集合与任务结果项不一致",
        )

    blobs: dict[str, bytes] = {}
    input_payloads: list[FixtureInputPayload] = []
    seen_inputs: set[tuple[str, str, str]] = set()
    for item in sorted(
        input_files,
        key=lambda value: (
            value.workbook_path.casefold(),
            value.side,
            value.filename.casefold(),
        ),
    ):
        digest = _hash(item.content)
        key = (item.side, item.workbook_path.casefold(), item.filename.casefold())
        if key in seen_inputs:
            raise OfflineFixtureError("FIXTURE_INPUT_DUPLICATE", "离线输入文件重复")
        seen_inputs.add(key)
        existing = blobs.get(digest)
        if existing is not None and existing != item.content:
            raise OfflineFixtureError("FIXTURE_HASH_COLLISION", "输入文件哈希冲突")
        blobs[digest] = item.content
        input_payloads.append(
            FixtureInputPayload(
                side=item.side,
                workbook_path=item.workbook_path,
                filename=item.filename,
                kind=item.kind,
                blob_sha256=digest,
                size_bytes=len(item.content),
            )
        )

    normalized_missing = sorted(
        [FixtureMissingFilePayload.model_validate(item) for item in missing_files],
        key=lambda value: (
            value.workbook_path.casefold(),
            value.side,
            value.filename.casefold(),
        ),
    )
    inputs_payload = FixtureInputsPayload(
        schema_version=INPUTS_SCHEMA_VERSION,
        inputs=input_payloads,
    )
    missing_payload = FixtureMissingFilesPayload(
        schema_version=MISSING_SCHEMA_VERSION,
        missing_files=normalized_missing,
    )
    captured_at = task.finished_at or task.updated_at
    audit_payload = FixtureAuditPayload(
        schema_version=AUDIT_SCHEMA_VERSION,
        task_id=task.task_id,
        task_status=task.status,
        captured_at=captured_at,
        items=[
            FixtureAuditItemPayload(
                item_id=item.item_id,
                ordinal=item.ordinal,
                workbook_path=item.candidate.path,
                status=item.status,
                diff_status=item.diff_status,
                diff_error_count=item.diff_error_count,
                attempt_count=item.attempt_count,
                recovery_count=item.recovery_count,
                created_at=item.created_at,
                started_at=item.started_at,
                finished_at=item.finished_at,
                result_sha256=item.result_sha256,
                result_size_bytes=item.result_size_bytes,
            )
            for item in task.items
        ],
    )

    members: dict[str, bytes] = {
        "expected/task.json": _json_bytes(task.model_dump(mode="json")),
        "inputs.json": _json_bytes(inputs_payload.model_dump(mode="json")),
        "missing-files.json": _json_bytes(missing_payload.model_dump(mode="json")),
        "audit/task-items.json": _json_bytes(audit_payload.model_dump(mode="json")),
    }
    kinds = {
        "expected/task.json": "metadata",
        "inputs.json": "metadata",
        "missing-files.json": "metadata",
        "audit/task-items.json": "audit",
    }
    for digest, content in blobs.items():
        path = f"blobs/{digest}"
        members[path] = content
        kinds[path] = "input_blob"

    results: list[FixtureResultPayload] = []
    items_by_id = {str(item.item_id): item for item in task.items}
    for item_id in sorted(golden_results, key=lambda value: items_by_id[value].ordinal):
        raw = golden_results[item_id]
        item = items_by_id[item_id]
        try:
            parsed = DiffResultPayload.model_validate_json(raw)
        except Exception as exc:
            raise OfflineFixtureError(
                "FIXTURE_RESULT_INVALID",
                "黄金 Diff 不符合 m2.diff.v1 契约",
            ) from exc
        if parsed.schema_version != "m2.diff.v1" or parsed.workbook.name != PurePosixPath(item.candidate.path).name:
            raise OfflineFixtureError(
                "FIXTURE_RESULT_IDENTITY_MISMATCH",
                "黄金 Diff 与任务工作簿身份不一致",
            )
        digest = _hash(raw)
        if item.result_sha256 != digest or item.result_size_bytes != len(raw):
            raise OfflineFixtureError(
                "FIXTURE_RESULT_HASH_MISMATCH",
                "黄金 Diff 与任务记录的哈希或大小不一致",
            )
        path = _result_path(item_id)
        members[path] = raw
        kinds[path] = "golden_result"
        results.append(
            FixtureResultPayload(
                item_id=item.item_id,
                ordinal=item.ordinal,
                workbook_path=item.candidate.path,
                result_path=path,
                sha256=digest,
                size_bytes=len(raw),
            )
        )

    manifest = OfflineFixtureManifest(
        schema_version=FIXTURE_SCHEMA_VERSION,
        fixture_id=task.task_id,
        task_id=task.task_id,
        captured_at=captured_at,
        source=task.source,
        target=task.target,
        dataset_layout=dict(dataset_layout),
        results=results,
        files={
            path: FixtureFilePayload.model_validate(
                _member_metadata(raw, kinds[path])
            )
            for path, raw in sorted(members.items())
        },
    )
    manifest_raw = _json_bytes(manifest.model_dump(mode="json"))

    output = BytesIO()
    with ZipFile(output, "w", compression=ZIP_DEFLATED, compresslevel=9) as archive:
        for path, content in [("manifest.json", manifest_raw), *sorted(members.items())]:
            info = ZipInfo(path, date_time=(1980, 1, 1, 0, 0, 0))
            info.compress_type = ZIP_DEFLATED
            info.create_system = 3
            info.external_attr = 0o100644 << 16
            archive.writestr(info, content, compress_type=ZIP_DEFLATED, compresslevel=9)
    return output.getvalue()


def _read_json_model(
    members: Mapping[str, bytes],
    path: str,
    model: type[_StrictModel],
):
    try:
        return model.model_validate_json(members[path])
    except Exception as exc:
        raise OfflineFixtureError(
            "FIXTURE_CONTRACT_INVALID",
            f"离线夹具契约文件无效：{path}",
        ) from exc


def _validate_zip_info(info: ZipInfo, limits: FixtureLimits) -> None:
    try:
        _portable_relative_path(info.filename)
    except ValueError as exc:
        raise OfflineFixtureError(
            "FIXTURE_ARCHIVE_PATH_INVALID",
            "离线夹具包含不安全路径",
        ) from exc
    if info.is_dir():
        raise OfflineFixtureError(
            "FIXTURE_ARCHIVE_ENTRY_INVALID",
            "离线夹具不能包含目录项",
        )
    if info.flag_bits & 0x1:
        raise OfflineFixtureError(
            "FIXTURE_ARCHIVE_ENTRY_INVALID",
            "离线夹具不能包含加密成员",
        )
    if info.compress_type not in {ZIP_STORED, ZIP_DEFLATED}:
        raise OfflineFixtureError(
            "FIXTURE_ARCHIVE_ENTRY_INVALID",
            "离线夹具包含不支持的压缩方式",
        )
    if info.file_size > limits.max_member_bytes:
        raise OfflineFixtureError(
            "FIXTURE_ARCHIVE_TOO_LARGE",
            "离线夹具的单个成员超过大小限制",
            status_code=413,
        )
    unix_type = (info.external_attr >> 16) & 0o170000
    if unix_type not in {0, 0o100000}:
        raise OfflineFixtureError(
            "FIXTURE_ARCHIVE_ENTRY_INVALID",
            "离线夹具只能包含普通文件",
        )


def load_offline_fixture(
    raw: bytes,
    *,
    limits: FixtureLimits = FixtureLimits(),
) -> LoadedOfflineFixture:
    if not raw:
        raise OfflineFixtureError("FIXTURE_EMPTY", "离线夹具为空")
    if len(raw) > limits.max_archive_bytes:
        raise OfflineFixtureError(
            "FIXTURE_ARCHIVE_TOO_LARGE",
            "离线夹具超过上传大小限制",
            status_code=413,
        )
    try:
        archive = ZipFile(BytesIO(raw), "r")
    except BadZipFile as exc:
        raise OfflineFixtureError("FIXTURE_ARCHIVE_INVALID", "离线夹具不是有效 ZIP") from exc

    with archive:
        infos = archive.infolist()
        if not infos or len(infos) > limits.max_entries:
            raise OfflineFixtureError(
                "FIXTURE_ARCHIVE_ENTRY_LIMIT",
                "离线夹具成员数量无效",
                status_code=413,
            )
        names: set[str] = set()
        folded_names: set[str] = set()
        total_size = 0
        for info in infos:
            _validate_zip_info(info, limits)
            folded = info.filename.casefold()
            if info.filename in names or folded in folded_names:
                raise OfflineFixtureError(
                    "FIXTURE_ARCHIVE_DUPLICATE",
                    "离线夹具包含重复成员",
                )
            names.add(info.filename)
            folded_names.add(folded)
            total_size += info.file_size
            if total_size > limits.max_uncompressed_bytes:
                raise OfflineFixtureError(
                    "FIXTURE_ARCHIVE_TOO_LARGE",
                    "离线夹具解压后超过大小限制",
                    status_code=413,
                )
        if "manifest.json" not in names:
            raise OfflineFixtureError(
                "FIXTURE_MANIFEST_MISSING",
                "离线夹具缺少 manifest.json",
            )
        try:
            manifest = OfflineFixtureManifest.model_validate_json(
                archive.read("manifest.json")
            )
        except Exception as exc:
            raise OfflineFixtureError(
                "FIXTURE_MANIFEST_INVALID",
                "离线夹具 manifest.json 无效",
            ) from exc
        expected_names = {"manifest.json", *manifest.files.keys()}
        if names != expected_names:
            raise OfflineFixtureError(
                "FIXTURE_ARCHIVE_UNDECLARED_ENTRY",
                "离线夹具成员与 manifest 声明不一致",
            )
        members: dict[str, bytes] = {}
        for path, metadata in manifest.files.items():
            content = archive.read(path)
            if len(content) != metadata.size_bytes or _hash(content) != metadata.sha256:
                raise OfflineFixtureError(
                    "FIXTURE_MEMBER_HASH_MISMATCH",
                    "离线夹具成员哈希或大小校验失败",
                )
            members[path] = content

    task = _read_json_model(members, "expected/task.json", BatchTaskPayload)
    inputs = _read_json_model(members, "inputs.json", FixtureInputsPayload)
    missing = _read_json_model(
        members,
        "missing-files.json",
        FixtureMissingFilesPayload,
    )
    audit = _read_json_model(members, "audit/task-items.json", FixtureAuditPayload)
    if (
        task.task_id != manifest.task_id
        or audit.task_id != manifest.task_id
        or task.source != manifest.source
        or task.target != manifest.target
    ):
        raise OfflineFixtureError(
            "FIXTURE_IDENTITY_MISMATCH",
            "离线夹具任务身份不一致",
        )

    task_items = {str(item.item_id): item for item in task.items}
    result_ids = {str(result.item_id) for result in manifest.results}
    expected_result_ids = {
        str(item.item_id) for item in task.items if item.result_ref is not None
    }
    if result_ids != expected_result_ids or len(result_ids) != len(manifest.results):
        raise OfflineFixtureError(
            "FIXTURE_RESULT_SET_INVALID",
            "离线夹具黄金结果集合与任务不一致",
        )

    golden_results: dict[str, bytes] = {}
    for result in manifest.results:
        item_id = str(result.item_id)
        item = task_items[item_id]
        content = members.get(result.result_path)
        if content is None:
            raise OfflineFixtureError(
                "FIXTURE_RESULT_MISSING",
                "离线夹具缺少黄金结果",
            )
        if (
            _hash(content) != result.sha256
            or len(content) != result.size_bytes
            or item.result_sha256 != result.sha256
            or item.result_size_bytes != result.size_bytes
            or result.ordinal != item.ordinal
            or result.workbook_path != item.candidate.path
        ):
            raise OfflineFixtureError(
                "FIXTURE_RESULT_HASH_MISMATCH",
                "黄金结果身份、哈希或大小不一致",
            )
        try:
            parsed = DiffResultPayload.model_validate_json(content)
        except Exception as exc:
            raise OfflineFixtureError(
                "FIXTURE_RESULT_INVALID",
                "黄金结果不符合 m2.diff.v1 契约",
            ) from exc
        if parsed.schema_version != "m2.diff.v1" or parsed.workbook.name != PurePosixPath(result.workbook_path).name:
            raise OfflineFixtureError(
                "FIXTURE_RESULT_IDENTITY_MISMATCH",
                "黄金结果工作簿身份不一致",
            )
        golden_results[item_id] = content

    known_workbooks = {item.candidate.path for item in task.items}
    seen_inputs: set[tuple[str, str, str]] = set()
    referenced_blobs: set[str] = set()
    present: set[tuple[str, str, str]] = set()
    for item in inputs.inputs:
        if item.workbook_path not in known_workbooks:
            raise OfflineFixtureError(
                "FIXTURE_INPUT_IDENTITY_MISMATCH",
                "输入文件引用了任务外工作簿",
            )
        key = (item.side, item.workbook_path, item.filename.casefold())
        if key in seen_inputs:
            raise OfflineFixtureError("FIXTURE_INPUT_DUPLICATE", "离线输入文件重复")
        seen_inputs.add(key)
        present.add(key)
        blob_path = f"blobs/{item.blob_sha256}"
        blob = members.get(blob_path)
        if blob is None or len(blob) != item.size_bytes or _hash(blob) != item.blob_sha256:
            raise OfflineFixtureError(
                "FIXTURE_INPUT_HASH_MISMATCH",
                "离线输入文件哈希或大小不一致",
            )
        referenced_blobs.add(blob_path)

    missing_keys: set[tuple[str, str, str]] = set()
    for item in missing.missing_files:
        if item.workbook_path not in known_workbooks:
            raise OfflineFixtureError(
                "FIXTURE_INPUT_IDENTITY_MISMATCH",
                "缺失清单引用了任务外工作簿",
            )
        key = (item.side, item.workbook_path, item.filename.casefold())
        if key in missing_keys or key in present:
            raise OfflineFixtureError(
                "FIXTURE_MISSING_SET_INVALID",
                "缺失清单重复或与现有输入冲突",
            )
        missing_keys.add(key)

    declared_blobs = {
        path for path, metadata in manifest.files.items() if metadata.kind == "input_blob"
    }
    if declared_blobs != referenced_blobs:
        raise OfflineFixtureError(
            "FIXTURE_BLOB_SET_INVALID",
            "离线夹具包含未引用或未声明的输入 blob",
        )
    declared_results = {
        path
        for path, metadata in manifest.files.items()
        if metadata.kind == "golden_result"
    }
    if declared_results != {result.result_path for result in manifest.results}:
        raise OfflineFixtureError(
            "FIXTURE_RESULT_SET_INVALID",
            "离线夹具包含未引用或未声明的黄金结果",
        )
    if not _CONTROL_PATHS.issubset(manifest.files):
        raise OfflineFixtureError(
            "FIXTURE_CONTROL_FILE_MISSING",
            "离线夹具控制文件不完整",
        )

    for result in manifest.results:
        workbook_filename = PurePosixPath(result.workbook_path).name
        workbook_name = workbook_filename.casefold()
        for side in ("source", "target"):
            workbook_inputs = [
                item
                for item in inputs.inputs
                if item.side == side
                and item.workbook_path == result.workbook_path
                and item.kind == "workbook"
                and item.filename.casefold() == workbook_name
            ]
            if len(workbook_inputs) != 1:
                raise OfflineFixtureError(
                    "FIXTURE_INPUT_INCOMPLETE",
                    "每个黄金结果必须包含双侧原始工作簿",
                )
            workbook_raw = members[f"blobs/{workbook_inputs[0].blob_sha256}"]
            manifest_config = manifest.dataset_layout["manifest"]
            csv_config = manifest.dataset_layout["csv_export"]
            try:
                workbook_manifest = parse_workbook_manifest(
                    workbook_raw,
                    sheet_name=str(manifest_config["sheet_name"]),
                    sheet_field=str(manifest_config["sheet_field"]),
                    csv_name_field=str(manifest_config["csv_name_field"]),
                    export_flag_field=str(manifest_config["export_flag_field"]),
                )
            except M2ProcessingError:
                workbook_manifest = None
            expected_csv: set[str] = set()
            if workbook_manifest is not None:
                template = str(csv_config["filename_template"])
                expected_csv = {
                    template.format(tbxName=entry.tbx_name).casefold()
                    for entry in workbook_manifest.entries
                }
            accounted_csv = {
                item.filename.casefold()
                for item in inputs.inputs
                if item.side == side
                and item.workbook_path == result.workbook_path
                and item.kind == "csv"
            }
            accounted_csv.update(
                item.filename.casefold()
                for item in missing.missing_files
                if item.side == side
                and item.workbook_path == result.workbook_path
            )
            if accounted_csv != expected_csv:
                raise OfflineFixtureError(
                    "FIXTURE_INPUT_SET_INVALID",
                    "原始 CSV 与显式缺失清单未完整覆盖工作簿导出清单",
                )

    return LoadedOfflineFixture(
        archive_sha256=_hash(raw),
        manifest=manifest,
        task=task,
        inputs=inputs,
        missing_files=missing,
        audit=audit,
        members=members,
        golden_results=golden_results,
    )


def export_task_fixture(
    *,
    store: BatchStore,
    resolver: WorkbookDatasetResolver,
    task: BatchTaskPayload,
    dataset_layout: Mapping[str, Any],
) -> bytes:
    """Read a completed task and its frozen SVN inputs without any write operation."""
    manifest_config = dict(dataset_layout["manifest"])
    csv_config = dict(dataset_layout["csv_export"])
    input_files: list[FixtureInputData] = []
    missing_files: list[FixtureMissingFilePayload] = []
    golden_results: dict[str, bytes] = {}

    for item in task.items:
        if item.result_ref is None:
            continue
        result_raw, _ = store.load_result(item.result_ref)
        golden_results[str(item.item_id)] = result_raw
        payload = WorkbookCompareRequestPayload(
            schema_version="m2.workbook-compare.request.v1",
            request_id=item.item_id,
            source=task.source.model_dump(),
            target=task.target.model_dump(),
            workbook_path=item.candidate.path,
        )
        workbook_name = PurePosixPath(item.candidate.path).name
        with resolver.resolve(payload) as dataset:
            for side, directory in (
                ("source", dataset.source_directory),
                ("target", dataset.target_directory),
            ):
                files = {path.name: path.read_bytes() for path in directory.iterdir() if path.is_file()}
                workbook_raw = files.get(workbook_name)
                if workbook_raw is None:
                    raise OfflineFixtureError(
                        "FIXTURE_INPUT_INCOMPLETE",
                        "数据集物化后缺少工作簿",
                    )
                input_files.append(
                    FixtureInputData(
                        side=side,
                        workbook_path=item.candidate.path,
                        filename=workbook_name,
                        kind="workbook",
                        content=workbook_raw,
                    )
                )
                try:
                    workbook_manifest = parse_workbook_manifest(
                        workbook_raw,
                        sheet_name=str(manifest_config["sheet_name"]),
                        sheet_field=str(manifest_config["sheet_field"]),
                        csv_name_field=str(manifest_config["csv_name_field"]),
                        export_flag_field=str(manifest_config["export_flag_field"]),
                    )
                except M2ProcessingError:
                    workbook_manifest = None
                expected_csv = []
                if workbook_manifest is not None:
                    template = str(csv_config["filename_template"])
                    expected_csv = sorted(
                        {template.format(tbxName=entry.tbx_name) for entry in workbook_manifest.entries},
                        key=str.casefold,
                    )
                for filename in expected_csv:
                    csv_raw = files.get(filename)
                    if csv_raw is None:
                        missing_files.append(
                            FixtureMissingFilePayload(
                                side=side,
                                workbook_path=item.candidate.path,
                                filename=filename,
                            )
                        )
                        continue
                    input_files.append(
                        FixtureInputData(
                            side=side,
                            workbook_path=item.candidate.path,
                            filename=filename,
                            kind="csv",
                            content=csv_raw,
                        )
                    )

    return create_offline_fixture_bytes(
        task=task,
        dataset_layout=dataset_layout,
        input_files=input_files,
        missing_files=missing_files,
        golden_results=golden_results,
    )


class OfflineFixtureService:
    """In-memory replay service. It has no provider or BatchStore dependency."""

    def __init__(self):
        self._lock = RLock()
        self._fixture: LoadedOfflineFixture | None = None
        self._current_results: dict[str, bytes] = {}

    def _loaded(self) -> LoadedOfflineFixture:
        if self._fixture is None:
            raise OfflineFixtureError(
                "FIXTURE_NOT_LOADED",
                "尚未加载离线夹具",
                status_code=404,
            )
        return self._fixture

    def load(self, raw: bytes) -> dict[str, Any]:
        fixture = load_offline_fixture(raw)
        with self._lock:
            self._fixture = fixture
            self._current_results = {}
            return self.session()

    def session(self) -> dict[str, Any]:
        with self._lock:
            fixture = self._loaded()
            result_items = {str(result.item_id): result for result in fixture.manifest.results}
            comparisons: dict[str, dict[str, Any]] = {}
            matched = 0
            mismatched = 0
            for item_id, result in result_items.items():
                current = self._current_results.get(item_id)
                is_match = current == fixture.golden_results[item_id] if current is not None else None
                if is_match is True:
                    matched += 1
                elif is_match is False:
                    mismatched += 1
                comparisons[item_id] = {
                    "available": current is not None,
                    "matches_golden": is_match,
                    "golden_sha256": result.sha256,
                    "current_sha256": _hash(current) if current is not None else None,
                }
            return {
                "schema_version": SESSION_SCHEMA_VERSION,
                "fixture": {
                    "fixture_id": str(fixture.manifest.fixture_id),
                    "archive_sha256": fixture.archive_sha256,
                    "captured_at": fixture.manifest.captured_at.isoformat(),
                    "input_file_count": len(fixture.inputs.inputs),
                    "missing_file_count": len(fixture.missing_files.missing_files),
                    "golden_result_count": len(fixture.golden_results),
                },
                "task": fixture.task.model_dump(mode="json"),
                "current": {
                    "available_count": len(self._current_results),
                    "matched_count": matched,
                    "mismatched_count": mismatched,
                    "comparisons": comparisons,
                },
            }

    def _recompute_item(self, fixture: LoadedOfflineFixture, item_id: str) -> bytes:
        item = next(
            (item for item in fixture.task.items if str(item.item_id) == item_id),
            None,
        )
        if item is None or item_id not in fixture.golden_results:
            raise OfflineFixtureError(
                "FIXTURE_RESULT_NOT_FOUND",
                "离线夹具中不存在该结果项",
                status_code=404,
            )
        service = WorkbookDiffService(
            DatasetLayout.from_config(fixture.manifest.dataset_layout)
        )
        workbook_name = PurePosixPath(item.candidate.path).name
        with fixture.materialize(item.candidate.path) as dataset:
            assert dataset.source_directory is not None
            assert dataset.target_directory is not None
            result = service.compare_local(
                dataset.source_directory,
                dataset.target_directory,
                workbook_name,
            )
        return serialize_diff_json(result)

    def recompute_all(self) -> dict[str, Any]:
        with self._lock:
            fixture = self._loaded()
            current = {
                item_id: self._recompute_item(fixture, item_id)
                for item_id in fixture.golden_results
            }
            self._current_results = current
            return self.session()

    def recompute_item(self, item_id: UUID | str) -> dict[str, Any]:
        key = str(item_id)
        with self._lock:
            fixture = self._loaded()
            self._current_results[key] = self._recompute_item(fixture, key)
            return self.session()

    def result(
        self,
        item_id: UUID | str,
        *,
        mode: Literal["golden", "current"],
    ) -> tuple[bytes, str, bool | None]:
        key = str(item_id)
        with self._lock:
            fixture = self._loaded()
            golden = fixture.golden_results.get(key)
            if golden is None:
                raise OfflineFixtureError(
                    "FIXTURE_RESULT_NOT_FOUND",
                    "离线夹具中不存在该结果项",
                    status_code=404,
                )
            if mode == "golden":
                return golden, _hash(golden), None
            current = self._current_results.get(key)
            if current is None:
                raise OfflineFixtureError(
                    "FIXTURE_CURRENT_RESULT_MISSING",
                    "该工作簿尚未使用当前代码离线重算",
                    status_code=409,
                )
            return current, _hash(current), current == golden
