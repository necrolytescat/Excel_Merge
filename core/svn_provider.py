"""统一的 SVN 只读 Provider。

M0 提供两个实现：

* ``MockSVNProvider``：不访问网络，用于页面演示和稳定测试；
* ``CLISVNProvider``：复用 ``core.svn_client`` 的 subprocess、编码、peg 和缓存能力。

Provider 层不依赖 FastAPI，后续 CLI、Web 和 Diff 编排均可复用。
"""
from __future__ import annotations

import copy
from datetime import datetime
import hashlib
import os
import urllib.parse
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Any, Protocol

from . import svn_client
from .models import (
    ChangedPath,
    CommitInfo,
    ContentPreview,
    EndpointSpec,
    Revision,
    SvnInfo,
    TreeEntry,
)
from .svn_history import (
    BranchChangedPath,
    BranchCommit,
    BranchCopyBoundary,
    BranchIdentity,
    append_url_path,
    branch_relative_path,
    canonicalize_svn_url,
    normalize_repository_path,
    parse_svn_datetime,
    repository_path_from_urls,
    require_utc_instant,
    svn_date_revision,
)


DEFAULT_ALLOWED_SCHEMES = ("http", "https", "svn", "svn+ssh", "file")


class SVNProviderError(RuntimeError):
    """可安全展示给用户的 SVN 错误。"""

    def __init__(self, code: str, message: str, *, detail: str = ""):
        super().__init__(message)
        self.code = code
        self.message = message
        self.detail = detail


class SVNProvider(Protocol):
    def info(self, endpoint: EndpointSpec) -> SvnInfo: ...

    def list_tree(self, endpoint: EndpointSpec, prefix: str = "") -> list[TreeEntry]: ...
    def list_children(self, endpoint: EndpointSpec, prefix: str = "") -> list[TreeEntry]: ...

    def log(
        self,
        endpoint: EndpointSpec,
        rev_from: Revision | None = None,
        rev_to: Revision | None = None,
    ) -> list[CommitInfo]: ...

    def branch_log_page(
        self,
        endpoint: EndpointSpec,
        upper_revision: int | str,
        limit: int,
    ) -> list[CommitInfo]: ...

    def read_bytes(self, endpoint: EndpointSpec, path: str) -> bytes: ...

    def read_content(
        self,
        endpoint: EndpointSpec,
        path: str,
        preview_limit: int = 262144,
    ) -> ContentPreview: ...


def normalize_relative_path(path: str) -> str:
    """规范化相对路径并拒绝路径穿越。"""
    value = (path or "").replace("\\", "/").strip()
    while value.startswith("/"):
        value = value[1:]
    parts = [p for p in value.split("/") if p not in ("", ".")]
    if any(part == ".." for part in parts):
        raise SVNProviderError("SVN_PATH_NOT_FOUND", "路径不允许包含 '..'")
    return "/".join(parts)


def validate_endpoint(
    endpoint: EndpointSpec,
    allowed_schemes: tuple[str, ...] = DEFAULT_ALLOWED_SCHEMES,
) -> EndpointSpec:
    """校验 URL、revision 和路径过滤，不保存或解析认证信息。"""
    url = (endpoint.url or "").strip()
    parsed = urllib.parse.urlparse(url)
    if not parsed.scheme or not parsed.netloc and parsed.scheme != "file":
        raise SVNProviderError("SVN_NOT_FOUND", "SVN URL 格式无效")
    if parsed.username or parsed.password:
        raise SVNProviderError("SVN_AUTH_FAILED", "URL 中不允许携带账号或密码")
    if parsed.scheme.lower() not in {s.lower() for s in allowed_schemes}:
        raise SVNProviderError("SVN_NOT_FOUND", f"不允许的 SVN URL 协议: {parsed.scheme}")

    revision = endpoint.revision
    if isinstance(revision, str):
        token = revision.strip()
        if token and token.upper() != "HEAD" and not token.isdigit():
            # 日期由 SVN CLI 解析；只限制空白和明显的命令注入字符。
            if any(ch in token for ch in ("\x00", "\r", "\n")):
                raise SVNProviderError("SVN_INVALID_REVISION", "revision 格式无效")
        revision = token or "HEAD"
    elif not isinstance(revision, int) or revision < 0:
        raise SVNProviderError("SVN_INVALID_REVISION", "revision 必须是正整数、HEAD 或日期")

    filters = tuple(normalize_relative_path(p) for p in endpoint.path_filter if p)
    return EndpointSpec(url=url, revision=revision, path_filter=filters, label=endpoint.label)


def _revision_number(value: Revision, fallback: int = 105) -> int:
    if isinstance(value, int):
        return value
    token = str(value).strip().upper()
    if token == "HEAD" or not token:
        return fallback
    if token.isdigit():
        return int(token)
    raise SVNProviderError("SVN_INVALID_REVISION", "Mock Provider 只支持 HEAD 或整数 revision")


def _decode_preview(raw: bytes, limit: int) -> tuple[str, str, bool]:
    truncated = len(raw) > limit
    sample = raw[: max(0, limit)]
    if sample.startswith(b"\xef\xbb\xbf"):
        return sample[3:].decode("utf-8", errors="replace"), "utf-8-sig", truncated
    for encoding in ("utf-8", "gbk"):
        try:
            return sample.decode(encoding), encoding, truncated
        except UnicodeDecodeError:
            continue
    return sample.decode("utf-8", errors="replace"), "utf-8-replace", truncated


class MockSVNProvider:
    """内置小型仓库快照，支持页面演示和可控失败场景。"""

    def __init__(self, fixture: dict[str, Any] | None = None):
        self.fixture = copy.deepcopy(fixture or self._default_fixture())

    @staticmethod
    def _default_fixture() -> dict[str, Any]:
        return {
            "info": {
                "repository_root": "https://mock.local/repo",
                "repository_uuid": "mock-repository-uuid",
                "revision": "105",
                "last_changed_revision": "105",
                "last_changed_author": "策划B",
                "last_changed_date": "2026-08-04T09:00:00Z",
            },
            "tree": [
                {"path": "table/ArenaPeak_Base.csv", "kind": "file", "size": 49, "revision": "105", "author": "策划B", "date": "2026-08-04T09:00:00Z"},
                {"path": "table/ArenaPeak_Banner.csv", "kind": "file", "size": 84, "revision": "101", "author": "策划A", "date": "2026-08-03T09:00:00Z"},
                {"path": "table", "kind": "dir", "size": None, "revision": "105", "author": "策划B", "date": "2026-08-04T09:00:00Z"},
                {"path": "README.txt", "kind": "file", "size": 25, "revision": "100", "author": "system", "date": "2026-08-01T09:00:00Z"},
            ],
            "children": [
                {"path": "Trunk_KR", "kind": "dir"},
                {"path": "Trunk_TC", "kind": "dir"},
                {"path": "branches", "kind": "dir"},
                {"path": "branches/KR-fix-1.2.3.4", "kind": "dir"},
                {"path": "branches/TC-fix-1.2.3.4", "kind": "dir"},
            ],            "logs": [
                {
                    "revision": 101,
                    "author": "策划A",
                    "date": "2026-08-03T10:00:00Z",
                    "message": "调整 ArenaPeak 配置",
                    "changed_paths": [{"path": "/repo/table/ArenaPeak_Banner.csv", "action": "M"}],
                },
                {
                    "revision": 105,
                    "author": "策划B",
                    "date": "2026-08-04T09:00:00Z",
                    "message": "修正基础数值",
                    "changed_paths": [{"path": "/repo/table/ArenaPeak_Base.csv", "action": "M"}],
                },
            ],
            "content": {
                "table/ArenaPeak_Base.csv": {
                    "100": "Id,Name,HP\n1,Alpha,100\n2,Beta,200\n",
                    "105": "Id,Name,HP\n1,Alpha,120\n2,Beta,200\n3,Gamma,300\n",
                },
                "table/ArenaPeak_Banner.csv": {
                    "100": "Id,Name,Resource\n1,开场,ui/banner/start\n",
                    "101": "Id,Name,Resource\n1,开场,ui/banner/arena\n",
                },
                "README.txt": {"100": "Mock SVN repository\n"},
            },
        }

    def _scenario(self, endpoint: EndpointSpec) -> None:
        lowered = endpoint.url.lower()
        if "auth-fail" in lowered:
            raise SVNProviderError("SVN_AUTH_FAILED", "Mock：SVN 认证失败")
        if "timeout" in lowered:
            raise SVNProviderError("SVN_TIMEOUT", "Mock：SVN 请求超时")
        if "missing" in lowered:
            raise SVNProviderError("SVN_NOT_FOUND", "Mock：SVN 路径不存在")

    def info(self, endpoint: EndpointSpec) -> SvnInfo:
        endpoint = validate_endpoint(endpoint, DEFAULT_ALLOWED_SCHEMES + ("mock",))
        self._scenario(endpoint)
        data = self.fixture["info"]
        return SvnInfo(url=endpoint.url, **data)

    def list_tree(self, endpoint: EndpointSpec, prefix: str = "") -> list[TreeEntry]:
        endpoint = validate_endpoint(endpoint, DEFAULT_ALLOWED_SCHEMES + ("mock",))
        self._scenario(endpoint)
        clean_prefix = normalize_relative_path(prefix)
        rows = []
        for row in self.fixture.get("tree", []):
            path = normalize_relative_path(row["path"])
            if clean_prefix and not (path == clean_prefix or path.startswith(clean_prefix + "/")):
                continue
            if endpoint.path_filter and not any(
                path.casefold() == f.casefold() or path.casefold().startswith(f.casefold() + '/')
                for f in endpoint.path_filter
            ):
                continue
            rows.append(TreeEntry(path=path, kind=row.get("kind", "file"), size=row.get("size"), revision=str(row.get("revision", "")), author=row.get("author", ""), date=row.get("date", "")))
        return sorted(rows, key=lambda item: (item.kind != "dir", item.path))

    def list_children(self, endpoint: EndpointSpec, prefix: str = "") -> list[TreeEntry]:
        endpoint = validate_endpoint(endpoint, DEFAULT_ALLOWED_SCHEMES + ("mock",))
        self._scenario(endpoint)
        clean_prefix = normalize_relative_path(prefix)
        configured = self.fixture.get("children")
        rows = []
        if configured is not None:
            for row in configured:
                path = normalize_relative_path(row.get("path", ""))
                if clean_prefix:
                    if path == clean_prefix:
                        continue
                    if not path.startswith(clean_prefix + "/"):
                        continue
                    relative = path[len(clean_prefix):].lstrip("/")
                    if "/" in relative:
                        continue
                elif "/" in path:
                    continue
                rows.append(TreeEntry(path=path, kind=row.get("kind", "dir"), size=row.get("size"), revision=str(row.get("revision", "")), author=row.get("author", ""), date=row.get("date", "")))
            return sorted(rows, key=lambda item: item.path)

        # 外部 fixture 未提供 children 时，从递归树中推导直接子项。
        seen: dict[str, TreeEntry] = {}
        for row in self.fixture.get("tree", []):
            path = normalize_relative_path(row.get("path", ""))
            if clean_prefix:
                if not (path == clean_prefix or path.startswith(clean_prefix + "/")):
                    continue
                relative = path[len(clean_prefix):].lstrip("/")
                child = relative.split("/", 1)[0] if relative else ""
                child_path = f"{clean_prefix}/{child}" if child else clean_prefix
                kind = "dir" if "/" in relative else row.get("kind", "file")
            else:
                child = path.split("/", 1)[0] if path else ""
                child_path = child
                kind = "dir" if "/" in path else row.get("kind", "file")
            if child and child_path not in seen:
                seen[child_path] = TreeEntry(path=child_path, kind=kind, size=None if kind == "dir" else row.get("size"), revision=str(row.get("revision", "")), author=row.get("author", ""), date=row.get("date", ""))
        return sorted(seen.values(), key=lambda item: item.path)
    def log(self, endpoint: EndpointSpec, rev_from: Revision | None = None, rev_to: Revision | None = None) -> list[CommitInfo]:
        endpoint = validate_endpoint(endpoint, DEFAULT_ALLOWED_SCHEMES + ("mock",))
        self._scenario(endpoint)
        start = _revision_number(rev_from if rev_from is not None else 0, 0)
        end = _revision_number(rev_to if rev_to is not None else endpoint.revision)
        result = []
        for row in self.fixture.get("logs", []):
            if start <= int(row["revision"]) <= end:
                paths = tuple(ChangedPath(**path) for path in row.get("changed_paths", []))
                result.append(CommitInfo(revision=row["revision"], author=row.get("author", ""), date=row.get("date", ""), message=row.get("message", ""), changed_paths=paths))
        return result

    def branch_log_page(
        self,
        endpoint: EndpointSpec,
        upper_revision: int | str,
        limit: int,
    ) -> list[CommitInfo]:
        endpoint = validate_endpoint(endpoint, DEFAULT_ALLOWED_SCHEMES + ("mock",))
        self._scenario(endpoint)
        upper = (
            int(self.fixture["info"]["revision"])
            if str(upper_revision).strip().upper() == "HEAD"
            else _revision_number(upper_revision)
        )
        result = []
        for row in self.fixture.get("logs", []):
            revision = int(row["revision"])
            if revision > upper:
                continue
            paths = tuple(ChangedPath(**path) for path in row.get("changed_paths", []))
            result.append(
                CommitInfo(
                    revision=revision,
                    author=row.get("author", ""),
                    date=row.get("date", ""),
                    message=row.get("message", ""),
                    changed_paths=paths,
                )
            )
        result.sort(key=lambda item: int(item.revision), reverse=True)
        return result[: max(1, int(limit))]

    def read_bytes(self, endpoint: EndpointSpec, path: str) -> bytes:
        endpoint = validate_endpoint(endpoint, DEFAULT_ALLOWED_SCHEMES + ("mock",))
        self._scenario(endpoint)
        clean_path = normalize_relative_path(path)
        versions = self.fixture.get("content", {}).get(clean_path)
        if not versions:
            raise SVNProviderError("SVN_PATH_NOT_FOUND", f"Mock：文件不存在：{clean_path}")
        revision = _revision_number(endpoint.revision)
        available = sorted((int(k), v) for k, v in versions.items())
        selected = next((value for number, value in reversed(available) if number <= revision), None)
        if selected is None:
            raise SVNProviderError("SVN_PATH_NOT_FOUND", f"Mock：revision {revision} 没有该文件")
        return selected if isinstance(selected, bytes) else str(selected).encode("utf-8")
    def read_content(self, endpoint: EndpointSpec, path: str, preview_limit: int = 262144) -> ContentPreview:
        endpoint = validate_endpoint(endpoint, DEFAULT_ALLOWED_SCHEMES + ("mock",))
        self._scenario(endpoint)
        clean_path = normalize_relative_path(path)
        versions = self.fixture.get("content", {}).get(clean_path)
        if not versions:
            raise SVNProviderError("SVN_PATH_NOT_FOUND", f"Mock：文件不存在：{clean_path}")
        revision = _revision_number(endpoint.revision)
        available = sorted((int(k), v) for k, v in versions.items())
        selected = next((text for number, text in reversed(available) if number <= revision), None)
        if selected is None:
            raise SVNProviderError("SVN_PATH_NOT_FOUND", f"Mock：revision {revision} 没有该文件")
        raw = selected if isinstance(selected, bytes) else str(selected).encode("utf-8")
        text, encoding, truncated = _decode_preview(raw, preview_limit)
        return ContentPreview(path=clean_path, revision=endpoint.revision, encoding=encoding, size=len(raw), truncated=truncated, text=text)


class CLISVNProvider:
    """基于 SVN CLI 的只读 Provider。"""

    def __init__(self, *, timeout: int = 30, cache_dir: str | None = ".cache/svn", allowed_schemes: tuple[str, ...] = DEFAULT_ALLOWED_SCHEMES):
        self.timeout = timeout
        self.allowed_schemes = allowed_schemes
        self.client = svn_client.SVNClient(cache_dir=cache_dir, timeout=timeout)

    def _validate(self, endpoint: EndpointSpec) -> EndpointSpec:
        endpoint = validate_endpoint(endpoint, self.allowed_schemes)
        if not self.client.available():
            raise SVNProviderError("SVN_CLI_NOT_FOUND", "未检测到 SVN CLI 或 TortoiseSVN")
        return endpoint

    @staticmethod
    def _error_from_text(stderr: str, fallback: str) -> SVNProviderError:
        text = (stderr or "").lower()
        if "authorization failed" in text or "authentication" in text or "forbidden" in text:
            return SVNProviderError("SVN_AUTH_FAILED", "SVN 认证失败", detail=stderr)
        if "timed out" in text or "timeout" in text:
            return SVNProviderError("SVN_TIMEOUT", "SVN 请求超时", detail=stderr)
        if (
            "e170013" in text
            or "e730053" in text
            or "unable to connect" in text
            or "connection was aborted" in text
            or "连接被中止" in text
            or "中止了一个已建立的连接" in text
        ):
            return SVNProviderError("SVN_NOT_REACHABLE", "SVN 连接中断", detail=stderr)
        if "not found" in text or "does not exist" in text or "path not found" in text:
            return SVNProviderError("SVN_PATH_NOT_FOUND", "SVN 路径不存在", detail=stderr)
        return SVNProviderError(fallback, "SVN 请求失败", detail=stderr)

    @staticmethod
    def _target(url: str, revision: Revision) -> str:
        return f"{url.rstrip('/')}@{revision}"

    def _run_xml(self, args: list[str], *, fallback: str = "SVN_NOT_REACHABLE") -> ET.Element:
        rc, out, stderr = svn_client._run(*args, timeout=self.timeout)
        if rc != 0:
            raise self._error_from_text(stderr, fallback)
        try:
            return ET.fromstring(out)
        except ET.ParseError as exc:
            raise SVNProviderError("SVN_DECODE_ERROR", "SVN XML 响应解析失败", detail=str(exc)) from exc

    @staticmethod
    def _positive_revision(value: str | int, *, code: str) -> int:
        token = str(value)
        if not token.isdigit() or int(token) <= 0:
            raise SVNProviderError(code, "SVN 未返回有效 Revision")
        return int(token)

    @staticmethod
    def _history_commits(root: ET.Element, identity: BranchIdentity) -> list[BranchCommit]:
        commits: list[BranchCommit] = []
        for logentry in root.iter("logentry"):
            revision = CLISVNProvider._positive_revision(
                logentry.get("revision", ""),
                code="SVN_HISTORY_INVALID",
            )
            try:
                changed_at = parse_svn_datetime(logentry.findtext("date", "") or "")
            except ValueError as exc:
                raise SVNProviderError(
                    "SVN_HISTORY_INVALID",
                    "SVN 日志提交时间无效",
                ) from exc
            changed_paths: list[BranchChangedPath] = []
            paths_node = logentry.find("paths")
            if paths_node is not None:
                for node in paths_node.findall("path"):
                    repository_path = node.text or ""
                    relative = branch_relative_path(
                        repository_path,
                        identity.repository_relative_path,
                    )
                    if relative is None:
                        continue
                    copied_from_revision = node.get("copyfrom-rev")
                    changed_paths.append(
                        BranchChangedPath(
                            repository_path=normalize_repository_path(repository_path),
                            branch_relative_path=relative,
                            action=node.get("action", ""),
                            copied_from_path=(
                                normalize_repository_path(node.get("copyfrom-path", ""))
                                if node.get("copyfrom-path")
                                else None
                            ),
                            copied_from_revision=(
                                int(copied_from_revision)
                                if copied_from_revision and copied_from_revision.isdigit()
                                else None
                            ),
                        )
                    )
            if not changed_paths:
                continue
            author_node = logentry.find("author")
            author = author_node.text if author_node is not None else None
            commits.append(
                BranchCommit(
                    revision=revision,
                    author=author or None,
                    changed_at=changed_at,
                    message=(logentry.findtext("msg", "") or "").strip(),
                    changed_paths=tuple(changed_paths),
                )
            )
        return sorted(commits, key=lambda commit: commit.revision)

    def resolve_branch_identity(self, endpoint: EndpointSpec) -> BranchIdentity:
        endpoint = self._validate(endpoint)
        args = ["info", "--xml"]
        if endpoint.revision != "HEAD":
            args.extend(["-r", str(endpoint.revision)])
        args.append(endpoint.url)
        root = self._run_xml(args, fallback="SVN_BRANCH_NOT_FOUND")
        entry = root.find(".//entry")
        if entry is None:
            raise SVNProviderError("SVN_BRANCH_NOT_FOUND", "SVN 分支不存在")
        canonical_url = canonicalize_svn_url(entry.findtext("url", "") or endpoint.url)
        repository_root = canonicalize_svn_url(entry.findtext("repository/root", "") or "")
        repository_uuid = entry.findtext("repository/uuid", "") or ""
        if not repository_uuid:
            raise SVNProviderError("SVN_HISTORY_INVALID", "SVN 未返回仓库 UUID")
        try:
            repository_path = repository_path_from_urls(repository_root, canonical_url)
        except ValueError as exc:
            raise SVNProviderError("SVN_HISTORY_INVALID", "SVN 分支身份无效") from exc
        return BranchIdentity(
            canonical_url=canonical_url,
            repository_root=repository_root,
            repository_uuid=repository_uuid,
            repository_relative_path=repository_path,
            bound_revision=self._positive_revision(
                entry.get("revision", ""),
                code="SVN_HISTORY_INVALID",
            ),
        )

    def resolve_revision_at(self, identity: BranchIdentity, instant: datetime) -> int:
        instant = require_utc_instant(instant)
        root = self._run_xml(
            [
                "log",
                "-v",
                "--xml",
                "--stop-on-copy",
                "-l",
                "1",
                "-r",
                f"{svn_date_revision(instant)}:1",
                identity.canonical_url,
            ],
            fallback="SVN_BRANCH_NOT_FOUND_AT_BOUNDARY",
        )
        commits = [
            commit
            for commit in self._history_commits(root, identity)
            if commit.changed_at <= instant
        ]
        if not commits:
            raise SVNProviderError(
                "SVN_BRANCH_NOT_FOUND_AT_BOUNDARY",
                "SVN 分支在时间边界处不存在",
            )
        return max(commits, key=lambda commit: commit.revision).revision

    def list_branch_commits(
        self,
        identity: BranchIdentity,
        start: datetime,
        end: datetime,
    ) -> list[BranchCommit]:
        start = require_utc_instant(start)
        end = require_utc_instant(end)
        if start >= end:
            raise ValueError("history interval must have positive duration")
        start_revision = self.resolve_revision_at(identity, start)
        end_revision = self.resolve_revision_at(identity, end)
        root = self._run_xml(
            [
                "log",
                "-v",
                "--xml",
                "--stop-on-copy",
                "-r",
                f"{start_revision}:{end_revision}",
                identity.canonical_url,
            ]
        )
        return [
            commit
            for commit in self._history_commits(root, identity)
            if start < commit.changed_at <= end
        ]

    def read_path_bytes_at_revision(
        self,
        identity: BranchIdentity,
        path: str,
        revision: int,
    ) -> bytes:
        if not isinstance(revision, int) or isinstance(revision, bool) or revision <= 0:
            raise SVNProviderError("SVN_INVALID_REVISION", "revision 必须是正整数")
        target = append_url_path(identity.canonical_url, path)
        raw = self.client._cat_cached(target, revision, revision)
        if raw is None:
            raise SVNProviderError("SVN_PATH_NOT_FOUND", "固定 Revision 下路径不存在")
        return raw

    def list_paths_at_revision(
        self,
        identity: BranchIdentity,
        revision: int,
    ) -> list[TreeEntry]:
        return self.list_tree(EndpointSpec(url=identity.canonical_url, revision=revision))

    def resolve_copy_boundary(self, identity: BranchIdentity) -> BranchCopyBoundary:
        root = self._run_xml(
            [
                "log",
                "-v",
                "--xml",
                "--stop-on-copy",
                "-r",
                f"{identity.bound_revision}:1",
                identity.canonical_url,
            ],
            fallback="SVN_BRANCH_NOT_FOUND",
        )
        commits = self._history_commits(root, identity)
        if not commits:
            raise SVNProviderError("SVN_BRANCH_NOT_FOUND", "SVN 分支没有可读取历史")
        boundary_commit = min(commits, key=lambda commit: commit.revision)
        branch_creation = next(
            (
                path
                for path in boundary_commit.changed_paths
                if path.branch_relative_path == "" and path.action == "A"
            ),
            None,
        )
        return BranchCopyBoundary(
            revision=boundary_commit.revision,
            copied_from_path=(
                branch_creation.copied_from_path if branch_creation is not None else None
            ),
            copied_from_revision=(
                branch_creation.copied_from_revision if branch_creation is not None else None
            ),
        )

    def info(self, endpoint: EndpointSpec) -> SvnInfo:
        endpoint = self._validate(endpoint)
        args = ["info", "--xml"]
        if endpoint.revision != "HEAD":
            args.extend(["-r", str(endpoint.revision)])
        args.append(endpoint.url)
        root = self._run_xml(args)
        entry = root.find(".//entry")
        if entry is None:
            raise SVNProviderError("SVN_NOT_FOUND", "SVN 未返回仓库信息")
        commit = entry.find("commit")
        return SvnInfo(
            url=entry.findtext("url", endpoint.url) or endpoint.url,
            repository_root=entry.findtext("repository/root", "") or "",
            repository_uuid=entry.findtext("repository/uuid", "") or "",
            revision=entry.get("revision", "") or "",
            last_changed_revision=commit.get("revision", "") if commit is not None else "",
            last_changed_author=commit.findtext("author", "") if commit is not None else "",
            last_changed_date=commit.findtext("date", "") if commit is not None else "",
        )

    def list_tree(self, endpoint: EndpointSpec, prefix: str = "") -> list[TreeEntry]:
        endpoint = self._validate(endpoint)
        clean_prefix = normalize_relative_path(prefix)
        target = endpoint.url.rstrip("/")
        if clean_prefix:
            target += "/" + urllib.parse.quote(clean_prefix, safe="/~:@-_.")
        target = self._target(target, endpoint.revision)
        root = self._run_xml(["list", "-R", "--xml", target])
        result = []
        for entry in root.iter("entry"):
            if entry.get("kind") not in ("file", "dir"):
                continue
            path = entry.get("path") or entry.findtext("name") or ""
            path = normalize_relative_path(path)
            if endpoint.path_filter and not any(path.startswith(item) for item in endpoint.path_filter):
                continue
            commit = entry.find("commit")
            result.append(TreeEntry(path=path, kind=entry.get("kind", "file"), size=int(entry.get("size")) if (entry.get("size") or "").isdigit() else None, revision=commit.get("revision", "") if commit is not None else "", author=commit.findtext("author", "") if commit is not None else "", date=commit.findtext("date", "") if commit is not None else ""))
        return sorted(result, key=lambda item: (item.kind != "dir", item.path))

    def list_children(self, endpoint: EndpointSpec, prefix: str = "") -> list[TreeEntry]:
        endpoint = self._validate(endpoint)
        clean_prefix = normalize_relative_path(prefix)
        target = endpoint.url.rstrip("/")
        if clean_prefix:
            target += "/" + urllib.parse.quote(clean_prefix, safe="/~:@-_.")
        target = self._target(target, endpoint.revision)
        root = self._run_xml(["list", "--xml", target], fallback="SVN_NOT_REACHABLE")
        result = []
        for entry in root.iter("entry"):
            if entry.get("kind") not in ("file", "dir"):
                continue
            name = entry.findtext("name") or entry.get("path") or ""
            name = normalize_relative_path(name)
            if not name:
                continue
            path = f"{clean_prefix}/{name}" if clean_prefix else name
            commit = entry.find("commit")
            result.append(TreeEntry(path=path, kind=entry.get("kind", "file"), size=int(entry.get("size")) if (entry.get("size") or "").isdigit() else None, revision=commit.get("revision", "") if commit is not None else "", author=commit.findtext("author", "") if commit is not None else "", date=commit.findtext("date", "") if commit is not None else ""))
        return sorted(result, key=lambda item: (item.kind != "dir", item.path))
    def log(self, endpoint: EndpointSpec, rev_from: Revision | None = None, rev_to: Revision | None = None) -> list[CommitInfo]:
        endpoint = self._validate(endpoint)
        end = rev_to if rev_to is not None else endpoint.revision
        if end == "HEAD":
            head = self.client.head_revision(endpoint.url)
            end = head if head is not None else "HEAD"
        start = rev_from if rev_from is not None else 1
        root = self._run_xml(["log", "-v", "--xml", "-r", f"{start}:{end}", endpoint.url], fallback="SVN_NOT_REACHABLE")
        result = []
        for logentry in root.iter("logentry"):
            paths = []
            paths_node = logentry.find("paths")
            if paths_node is not None:
                for path in paths_node.findall("path"):
                    paths.append(ChangedPath(path=path.text or "", action=path.get("action", ""), copyfrom_path=path.get("copyfrom-path"), copyfrom_revision=path.get("copyfrom-rev")))
            revision = logentry.get("revision", "")
            result.append(CommitInfo(revision=int(revision) if revision.isdigit() else revision, author=logentry.findtext("author", "") or "", date=logentry.findtext("date", "") or "", message=(logentry.findtext("msg", "") or "").strip(), changed_paths=tuple(paths)))
        return sorted(result, key=lambda item: int(item.revision) if str(item.revision).isdigit() else 0)

    def branch_log_page(
        self,
        endpoint: EndpointSpec,
        upper_revision: int | str,
        limit: int,
    ) -> list[CommitInfo]:
        endpoint = self._validate(endpoint)
        page_limit = max(1, int(limit))
        upper = str(upper_revision).strip().upper()
        if upper != "HEAD" and (not upper.isdigit() or int(upper) <= 0):
            raise SVNProviderError("SVN_INVALID_REVISION", "revision 必须是正整数或 HEAD")
        args = [
            "log",
            "--xml",
            "--stop-on-copy",
            "--limit",
            str(page_limit),
        ]
        # URL 未指定 revision 时，SVN CLI 原生使用 HEAD:1；翻页才覆盖数字上界。
        if upper != "HEAD":
            args.extend(["--revision", f"{upper}:1"])
        args.append(endpoint.url)
        root = self._run_xml(args, fallback="SVN_NOT_REACHABLE")
        commits = []
        for logentry in root.iter("logentry"):
            revision = logentry.get("revision", "")
            if not revision.isdigit() or int(revision) <= 0:
                raise SVNProviderError("SVN_HISTORY_INVALID", "SVN 日志包含无效 Revision")
            commits.append(
                CommitInfo(
                    revision=int(revision),
                    author=logentry.findtext("author", "") or "",
                    date=logentry.findtext("date", "") or "",
                    message=(logentry.findtext("msg", "") or "").strip(),
                )
            )
        commits.sort(key=lambda item: int(item.revision), reverse=True)
        return commits[:page_limit]

    def read_bytes(self, endpoint: EndpointSpec, path: str) -> bytes:
        endpoint = self._validate(endpoint)
        clean_path = normalize_relative_path(path)
        target = endpoint.url.rstrip("/") + "/" + urllib.parse.quote(clean_path, safe="/~:@-_.")
        raw = self.client._cat_cached(target, endpoint.revision, endpoint.revision)
        if raw is None:
            raise SVNProviderError("SVN_PATH_NOT_FOUND", f"无法读取 SVN 文件：{clean_path}")
        return raw
    def read_content(self, endpoint: EndpointSpec, path: str, preview_limit: int = 262144) -> ContentPreview:
        endpoint = self._validate(endpoint)
        clean_path = normalize_relative_path(path)
        target = endpoint.url.rstrip("/") + "/" + urllib.parse.quote(clean_path, safe="/~:@-_.")
        peg = endpoint.revision
        raw = self.client._cat_cached(target, endpoint.revision, peg)
        if raw is None:
            raise SVNProviderError("SVN_PATH_NOT_FOUND", f"无法读取 SVN 文件：{clean_path}")
        text, encoding, truncated = _decode_preview(raw, preview_limit)
        return ContentPreview(path=clean_path, revision=endpoint.revision, encoding=encoding, size=len(raw), truncated=truncated, text=text)


def provider_from_config(config: dict[str, Any]) -> SVNProvider:
    svn_cfg = config.get("svn", {}) if isinstance(config, dict) else {}
    provider_name = str(svn_cfg.get("provider", "mock")).lower()
    allowed = tuple(svn_cfg.get("allowed_schemes", DEFAULT_ALLOWED_SCHEMES))
    if provider_name == "mock":
        return MockSVNProvider()
    if provider_name == "cli":
        return CLISVNProvider(timeout=int(svn_cfg.get("timeout_seconds", 30)), cache_dir=svn_cfg.get("cache_dir", ".cache/svn"), allowed_schemes=allowed)
    raise SVNProviderError("SVN_NOT_FOUND", f"未知 SVN Provider：{provider_name}")





