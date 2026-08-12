"""将 API 请求转换为 Provider 调用，并统一校验和序列化。"""
from __future__ import annotations

import base64
import binascii
import hashlib
import json
from typing import Any

from core.models import EndpointSpec
from core.svn_history import canonicalize_svn_url
from core.svn_provider import SVNProvider, SVNProviderError, validate_endpoint

from app.schemas.svn import (
    BranchLogCommitPayload,
    BranchLogPagePayload,
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

    @staticmethod
    def _branch_cursor_url_hash(url: str) -> str:
        try:
            canonical = canonicalize_svn_url(url)
        except ValueError as exc:
            raise SVNProviderError("SVN_NOT_FOUND", "SVN URL 格式无效") from exc
        return hashlib.sha256(canonical.encode("utf-8")).hexdigest()

    @classmethod
    def _encode_branch_cursor(cls, url: str, revision: int) -> str:
        raw = json.dumps(
            {"v": 1, "url": cls._branch_cursor_url_hash(url), "revision": revision},
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")

    @classmethod
    def _decode_branch_cursor(cls, url: str, cursor: str) -> int:
        try:
            padding = "=" * (-len(cursor) % 4)
            payload = json.loads(base64.urlsafe_b64decode(cursor + padding))
            revision = payload["revision"]
            if (
                payload.get("v") != 1
                or payload.get("url") != cls._branch_cursor_url_hash(url)
                or not isinstance(revision, int)
                or isinstance(revision, bool)
                or revision <= 0
            ):
                raise ValueError
            return revision
        except (
            KeyError,
            TypeError,
            ValueError,
            UnicodeDecodeError,
            binascii.Error,
            json.JSONDecodeError,
        ):
            raise SVNProviderError("SVN_INVALID_CURSOR", "分支提交游标无效") from None

    def branch_logs(
        self,
        *,
        url: str,
        limit: int,
        cursor: str | None,
    ) -> BranchLogPagePayload:
        endpoint = self.endpoint(EndpointPayload(url=url, revision="HEAD"))
        upper_revision: int | str = (
            self._decode_branch_cursor(endpoint.url, cursor) if cursor else "HEAD"
        )
        reader = getattr(self.provider, "branch_log_page", None)
        if reader is None:
            raise SVNProviderError(
                "SVN_HISTORY_UNAVAILABLE",
                "当前 SVN Provider 不支持分支历史",
            )
        rows = sorted(
            reader(endpoint, upper_revision, limit + 1),
            key=lambda item: int(item.revision),
            reverse=True,
        )
        has_more = len(rows) > limit
        visible = rows[:limit]
        next_cursor = (
            self._encode_branch_cursor(endpoint.url, int(rows[limit].revision))
            if has_more
            else None
        )
        return BranchLogPagePayload(
            commits=[
                BranchLogCommitPayload(
                    revision=int(item.revision),
                    author=item.author,
                    date=item.date,
                    message=item.message,
                )
                for item in visible
            ],
            next_cursor=next_cursor,
            has_more=has_more,
        )

    def content(self, payload: EndpointPayload, path: str, preview_limit: int | None = None) -> ContentPayload:
        endpoint = self.endpoint(payload)
        limit = self.preview_limit if preview_limit is None else max(1, min(int(preview_limit), self.preview_limit))
        content = self.provider.read_content(endpoint, path, limit)
        return ContentPayload(path=content.path, revision=content.revision, encoding=content.encoding, size=content.size, truncated=content.truncated, text=content.text)

