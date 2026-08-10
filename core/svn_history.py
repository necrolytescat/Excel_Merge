"""Fixed-branch SVN history contracts used by M3 monitoring."""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Protocol
import urllib.parse

from core.models import EndpointSpec, TreeEntry


@dataclass(frozen=True)
class BranchIdentity:
    canonical_url: str
    repository_root: str
    repository_uuid: str
    repository_relative_path: str
    bound_revision: int


@dataclass(frozen=True)
class BranchCopyBoundary:
    revision: int
    copied_from_path: str | None = None
    copied_from_revision: int | None = None


@dataclass(frozen=True)
class BranchChangedPath:
    repository_path: str
    branch_relative_path: str
    action: str
    copied_from_path: str | None = None
    copied_from_revision: int | None = None


@dataclass(frozen=True)
class BranchCommit:
    revision: int
    author: str | None
    changed_at: datetime
    message: str
    changed_paths: tuple[BranchChangedPath, ...] = field(default_factory=tuple)


class SVNHistoryProvider(Protocol):
    def resolve_branch_identity(self, endpoint: EndpointSpec) -> BranchIdentity: ...

    def resolve_revision_at(
        self,
        identity: BranchIdentity,
        instant: datetime,
    ) -> int: ...

    def list_branch_commits(
        self,
        identity: BranchIdentity,
        start: datetime,
        end: datetime,
    ) -> list[BranchCommit]: ...

    def read_path_bytes_at_revision(
        self,
        identity: BranchIdentity,
        path: str,
        revision: int,
    ) -> bytes: ...

    def list_paths_at_revision(
        self,
        identity: BranchIdentity,
        revision: int,
    ) -> list[TreeEntry]: ...

    def resolve_copy_boundary(
        self,
        identity: BranchIdentity,
    ) -> BranchCopyBoundary: ...


def require_utc_instant(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("history instant must include a UTC offset")
    return value.astimezone(timezone.utc)


def parse_svn_datetime(value: str) -> datetime:
    token = value.strip()
    if token.endswith("Z"):
        token = token[:-1] + "+00:00"
    parsed = datetime.fromisoformat(token)
    return require_utc_instant(parsed)


def svn_date_revision(value: datetime) -> str:
    utc = require_utc_instant(value)
    return "{" + utc.isoformat(timespec="microseconds").replace("+00:00", "Z") + "}"


def normalize_repository_path(path: str) -> str:
    value = urllib.parse.unquote(path or "").replace("\\", "/").strip("/")
    segments = value.split("/") if value else []
    if any(segment in {"", ".", ".."} for segment in segments):
        raise ValueError("repository path contains an invalid segment")
    return "/".join(segments)


def path_is_within_branch(path: str, branch_path: str) -> bool:
    path_segments = normalize_repository_path(path).split("/")
    branch_segments = normalize_repository_path(branch_path).split("/")
    return path_segments[: len(branch_segments)] == branch_segments


def branch_relative_path(path: str, branch_path: str) -> str | None:
    normalized = normalize_repository_path(path)
    branch = normalize_repository_path(branch_path)
    if not path_is_within_branch(normalized, branch):
        return None
    if normalized == branch:
        return ""
    return normalized[len(branch) + 1 :]


def canonicalize_svn_url(url: str) -> str:
    parsed = urllib.parse.urlsplit(url.strip())
    if not parsed.scheme or (not parsed.netloc and parsed.scheme != "file"):
        raise ValueError("SVN URL is not absolute")
    path = urllib.parse.quote(urllib.parse.unquote(parsed.path), safe="/~:@-._")
    return urllib.parse.urlunsplit(
        (parsed.scheme.lower(), parsed.netloc, path.rstrip("/"), parsed.query, "")
    )


def repository_path_from_urls(repository_root: str, canonical_url: str) -> str:
    root = urllib.parse.urlsplit(canonicalize_svn_url(repository_root))
    branch = urllib.parse.urlsplit(canonicalize_svn_url(canonical_url))
    if root.scheme != branch.scheme or root.netloc.casefold() != branch.netloc.casefold():
        raise ValueError("branch URL belongs to a different repository root")
    root_path = normalize_repository_path(root.path)
    branch_path = normalize_repository_path(branch.path)
    if root_path:
        if not path_is_within_branch(branch_path, root_path) or branch_path == root_path:
            raise ValueError("branch URL is outside the repository root")
        return branch_path[len(root_path) + 1 :]
    if not branch_path:
        raise ValueError("repository root cannot be monitored as a branch")
    return branch_path


def append_url_path(url: str, path: str) -> str:
    relative = normalize_repository_path(path)
    parsed = urllib.parse.urlsplit(canonicalize_svn_url(url))
    encoded = urllib.parse.quote(relative, safe="/~:@-._")
    joined = parsed.path.rstrip("/") + ("/" + encoded if encoded else "")
    return urllib.parse.urlunsplit(
        (parsed.scheme, parsed.netloc, joined, parsed.query, "")
    )
