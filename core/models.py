"""M0 SVN 领域模型。

这些模型不依赖 Web 框架，供 Mock Provider、CLI Provider 和后续 Diff 任务共用。
"""
from dataclasses import dataclass, field
from typing import Union


Revision = Union[int, str]


@dataclass(frozen=True)
class EndpointSpec:
    url: str
    revision: Revision = "HEAD"
    path_filter: tuple[str, ...] = ()
    label: str = ""


@dataclass(frozen=True)
class SvnInfo:
    url: str
    repository_root: str = ""
    repository_uuid: str = ""
    revision: str = ""
    last_changed_revision: str = ""
    last_changed_author: str = ""
    last_changed_date: str = ""


@dataclass(frozen=True)
class TreeEntry:
    path: str
    kind: str = "file"
    size: int | None = None
    revision: str = ""
    author: str = ""
    date: str = ""


@dataclass(frozen=True)
class ChangedPath:
    path: str
    action: str
    copyfrom_path: str | None = None
    copyfrom_revision: str | None = None


@dataclass(frozen=True)
class CommitInfo:
    revision: int | str
    author: str = ""
    date: str = ""
    message: str = ""
    changed_paths: tuple[ChangedPath, ...] = field(default_factory=tuple)


@dataclass(frozen=True)
class ContentPreview:
    path: str
    revision: Revision
    encoding: str
    size: int
    truncated: bool
    text: str
