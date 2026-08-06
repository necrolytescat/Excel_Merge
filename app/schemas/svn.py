"""SVN API 的 Pydantic 请求/响应模型。"""
from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field


class EndpointPayload(BaseModel):
    url: str
    revision: int | str = "HEAD"
    path_filter: list[str] = Field(default_factory=list)
    label: str = ""


class SVNConfigUpdatePayload(BaseModel):
    server_url: str = Field(..., min_length=1, max_length=2048)


class SVNConfigPayload(BaseModel):
    provider: str
    server_url: str
    credential_source: str

class EndpointRegionConfig(BaseModel):
    display_name: str
    trunk_branch: str = ""
    fix_pattern: str = ""


class EndpointCatalogPayload(BaseModel):
    regions: dict[str, EndpointRegionConfig] = Field(default_factory=dict)

class ErrorPayload(BaseModel):
    code: str
    message: str


class HealthPayload(BaseModel):
    status: str
    provider: str
    svn_cli_available: bool | None = None
    credential_source: str = "svn_cli_cache"


class InfoPayload(BaseModel):
    url: str
    repository_root: str
    repository_uuid: str
    revision: str
    last_changed_revision: str
    last_changed_author: str
    last_changed_date: str


class TreeEntryPayload(BaseModel):
    path: str
    kind: str
    size: int | None = None
    revision: str = ""
    author: str = ""
    date: str = ""


class ChangedPathPayload(BaseModel):
    path: str
    action: str
    copyfrom_path: str | None = None
    copyfrom_revision: str | None = None


class CommitPayload(BaseModel):
    revision: int | str
    author: str = ""
    date: str = ""
    message: str = ""
    changed_paths: list[ChangedPathPayload] = Field(default_factory=list)


class ContentPayload(BaseModel):
    path: str
    revision: int | str
    encoding: str
    size: int
    truncated: bool
    text: str


class BranchMatchPayload(BaseModel):
    region: str
    track: str
    label: str
    branch: str
    url: str
    match_type: str


class BranchCandidatesPayload(BaseModel):
    base_url: str
    revision: int | str
    trunk_branches: list[str] = Field(default_factory=list)
    fix_branches: list[str] = Field(default_factory=list)
    matches: list[BranchMatchPayload] = Field(default_factory=list)


class ListPayload(BaseModel):
    entries: list[TreeEntryPayload]


class LogPayload(BaseModel):
    commits: list[CommitPayload]


class ErrorResponse(BaseModel):
    error: ErrorPayload


class EndpointRecordPayload(BaseModel):
    id: str = Field(..., min_length=1, max_length=128, pattern=r"^[A-Za-z0-9._-]+$")
    region: str = Field(..., min_length=1, max_length=16)
    track: str = Field(..., min_length=1, max_length=16)
    label: str = Field(..., min_length=1, max_length=256)
    url: str = Field(..., min_length=1, max_length=2048)
    logical_scopes: list[str] = Field(default_factory=lambda: ["TABLE"])
    physical_path_filters: dict[str, str] = Field(default_factory=dict)
    enabled: bool = True


class EndpointRegistryPayload(BaseModel):
    endpoints: list[EndpointRecordPayload] = Field(default_factory=list)


class SnapshotEndpointRefPayload(BaseModel):
    endpoint_id: str = Field(..., min_length=1, max_length=128)


class SnapshotRequestPayload(BaseModel):
    source: SnapshotEndpointRefPayload
    target: SnapshotEndpointRefPayload


class SnapshotFilePayload(BaseModel):
    path: str
    logical_scope: str
    size: int | None = None
    revision: int | str = ""
    author: str = ""
    date: str = ""
    encoding: str = ""
    content: str | None = None
    content_ref: str | None = None
    content_hash: str | None = None
    error: ErrorPayload | None = None


class SnapshotStatsPayload(BaseModel):
    file_count: int
    total_size: int
    failed_count: int


class SnapshotEndpointPayload(BaseModel):
    endpoint_id: str
    label: str
    url: str
    resolved_revision: int | str
    physical_path_filters: dict[str, str]
    files: list[SnapshotFilePayload]
    stats: SnapshotStatsPayload


class SnapshotResponsePayload(BaseModel):
    captured_at: str
    logical_scopes: list[str]
    source: SnapshotEndpointPayload
    target: SnapshotEndpointPayload