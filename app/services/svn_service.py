"""将 API 请求转换为 Provider 调用，并统一校验和序列化。"""
from __future__ import annotations

from typing import Any

from core.models import EndpointSpec
from core.svn_provider import SVNProvider, validate_endpoint

from app.schemas.svn import (
    ChangedPathPayload,
    CommitPayload,
    ContentPayload,
    EndpointPayload,
    InfoPayload,
    TreeEntryPayload,
)


class SVNService:
    def __init__(self, provider: SVNProvider, *, allowed_schemes: tuple[str, ...] = ("http", "https", "svn", "svn+ssh", "file"), preview_limit: int = 262144):
        self.provider = provider
        self.allowed_schemes = allowed_schemes
        self.preview_limit = preview_limit

    def endpoint(self, payload: EndpointPayload) -> EndpointSpec:
        return validate_endpoint(
            EndpointSpec(
                url=payload.url,
                revision=payload.revision,
                path_filter=tuple(payload.path_filter),
                label=payload.label,
            ),
            self.allowed_schemes,
        )

    def probe(self, payload: EndpointPayload) -> InfoPayload:
        endpoint = self.endpoint(payload)
        info = self.provider.info(endpoint)
        return InfoPayload(
            url=info.url,
            repository_root=info.repository_root,
            repository_uuid=info.repository_uuid,
            revision=info.revision,
            last_changed_revision=info.last_changed_revision,
            last_changed_author=info.last_changed_author,
            last_changed_date=info.last_changed_date,
        )

    def tree(self, payload: EndpointPayload, prefix: str = "") -> list[TreeEntryPayload]:
        endpoint = self.endpoint(payload)
        return [
            TreeEntryPayload(path=row.path, kind=row.kind, size=row.size, revision=row.revision, author=row.author, date=row.date)
            for row in self.provider.list_tree(endpoint, prefix)
        ]

    def children(self, payload: EndpointPayload, prefix: str = "") -> list[TreeEntryPayload]:
        endpoint = self.endpoint(payload)
        return [
            TreeEntryPayload(path=row.path, kind=row.kind, size=row.size, revision=row.revision, author=row.author, date=row.date)
            for row in self.provider.list_children(endpoint, prefix)
        ]
    def logs(self, payload: EndpointPayload, rev_from: int | str | None = None, rev_to: int | str | None = None) -> list[CommitPayload]:
        endpoint = self.endpoint(payload)
        commits = self.provider.log(endpoint, rev_from, rev_to)
        return [
            CommitPayload(
                revision=commit.revision,
                author=commit.author,
                date=commit.date,
                message=commit.message,
                changed_paths=[
                    ChangedPathPayload(
                        path=path.path,
                        action=path.action,
                        copyfrom_path=path.copyfrom_path,
                        copyfrom_revision=path.copyfrom_revision,
                    )
                    for path in commit.changed_paths
                ],
            )
            for commit in commits
        ]

    def content(self, payload: EndpointPayload, path: str, preview_limit: int | None = None) -> ContentPayload:
        endpoint = self.endpoint(payload)
        limit = self.preview_limit if preview_limit is None else max(1, min(int(preview_limit), self.preview_limit))
        content = self.provider.read_content(endpoint, path, limit)
        return ContentPayload(path=content.path, revision=content.revision, encoding=content.encoding, size=content.size, truncated=content.truncated, text=content.text)

