"""M3 fixed-branch history service with binding validation."""
from __future__ import annotations

from datetime import datetime

from core.models import EndpointSpec, TreeEntry
from core.svn_history import (
    BranchCommit,
    BranchCopyBoundary,
    BranchIdentity,
    SVNHistoryProvider,
    canonicalize_svn_url,
)
from core.svn_provider import SVNProviderError


class BranchHistoryService:
    def __init__(self, provider: SVNHistoryProvider):
        self.provider = provider

    def resolve_branch_identity(self, endpoint: EndpointSpec) -> BranchIdentity:
        return self.provider.resolve_branch_identity(endpoint)

    def verify_branch_identity(
        self,
        endpoint: EndpointSpec,
        expected: BranchIdentity,
    ) -> BranchIdentity:
        actual = self.provider.resolve_branch_identity(endpoint)
        if (
            actual.repository_uuid != expected.repository_uuid
            or actual.repository_relative_path != expected.repository_relative_path
            or canonicalize_svn_url(actual.canonical_url)
            != canonicalize_svn_url(expected.canonical_url)
        ):
            raise SVNProviderError(
                "SVN_BRANCH_BINDING_INVALID",
                "固定 SVN 分支身份已变化",
            )
        return actual

    def resolve_revision_at(self, identity: BranchIdentity, instant: datetime) -> int:
        return self.provider.resolve_revision_at(identity, instant)

    def list_branch_commits(
        self,
        identity: BranchIdentity,
        start: datetime,
        end: datetime,
    ) -> list[BranchCommit]:
        return self.provider.list_branch_commits(identity, start, end)

    def read_path_bytes_at_revision(
        self,
        identity: BranchIdentity,
        path: str,
        revision: int,
    ) -> bytes:
        return self.provider.read_path_bytes_at_revision(identity, path, revision)

    def list_paths_at_revision(
        self,
        identity: BranchIdentity,
        revision: int,
    ) -> list[TreeEntry]:
        return self.provider.list_paths_at_revision(identity, revision)

    def resolve_copy_boundary(self, identity: BranchIdentity) -> BranchCopyBoundary:
        return self.provider.resolve_copy_boundary(identity)
