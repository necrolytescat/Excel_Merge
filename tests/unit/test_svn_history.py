from __future__ import annotations

from datetime import datetime, timezone
import xml.etree.ElementTree as ET

import pytest

from app.services.branch_history_service import BranchHistoryService
from core.models import EndpointSpec
from core.svn_history import (
    BranchIdentity,
    append_url_path,
    branch_relative_path,
    path_is_within_branch,
    repository_path_from_urls,
)
from core.svn_provider import CLISVNProvider, SVNProviderError


def _identity(path: str = "branches/foo") -> BranchIdentity:
    return BranchIdentity(
        canonical_url="https://svn.example/repo/" + path,
        repository_root="https://svn.example/repo",
        repository_uuid="20000000-0000-4000-8000-000000000001",
        repository_relative_path=path,
        bound_revision=110,
    )


def _log_xml(entries: str) -> ET.Element:
    return ET.fromstring(f"<log>{entries}</log>")


def _entry(revision: int, date: str, paths: str, *, author: str = "alice") -> str:
    return (
        f'<logentry revision="{revision}"><author>{author}</author>'
        f"<date>{date}</date><paths>{paths}</paths><msg>r{revision}</msg></logentry>"
    )


def _path(value: str, action: str = "M", copy: str = "") -> str:
    return f'<path action="{action}"{copy}>{value}</path>'


def test_repository_paths_use_segment_boundaries_and_decode_urls():
    assert path_is_within_branch("/branches/foo/TableCsv/A.csv", "branches/foo")
    assert not path_is_within_branch("/branches/foobar/TableCsv/A.csv", "branches/foo")
    assert branch_relative_path("/branches/foo/TableCsv/A.csv", "branches/foo") == (
        "TableCsv/A.csv"
    )
    assert branch_relative_path("/branches/foobar/A.csv", "branches/foo") is None
    assert repository_path_from_urls(
        "https://svn.example/repo",
        "https://svn.example/repo/branches/Fix%201.0",
    ) == "branches/Fix 1.0"
    assert append_url_path(
        "https://svn.example/repo/branches/Fix%201.0",
        "TableCsv/角色 配置.csv",
    ).endswith("/branches/Fix%201.0/TableCsv/%E8%A7%92%E8%89%B2%20%E9%85%8D%E7%BD%AE.csv")


def test_cli_history_resolves_canonical_branch_identity(monkeypatch):
    provider = CLISVNProvider(cache_dir=None)
    monkeypatch.setattr(provider, "_validate", lambda endpoint: endpoint)
    info = ET.fromstring(
        """
        <info><entry revision="110">
          <url>https://svn.example/repo/branches/Fix%201.0</url>
          <repository>
            <root>https://svn.example/repo</root>
            <uuid>20000000-0000-4000-8000-000000000001</uuid>
          </repository>
        </entry></info>
        """
    )
    monkeypatch.setattr(provider, "_run_xml", lambda *args, **kwargs: info)

    identity = provider.resolve_branch_identity(
        EndpointSpec(url="https://svn.example/repo/branches/Fix 1.0")
    )

    assert identity.canonical_url.endswith("/branches/Fix%201.0")
    assert identity.repository_relative_path == "branches/Fix 1.0"
    assert identity.bound_revision == 110


def test_cli_history_reads_encoded_path_at_exact_revision(monkeypatch):
    provider = CLISVNProvider(cache_dir=None)
    calls = []

    def read(target, revision, peg):
        calls.append((target, revision, peg))
        return b"content"

    monkeypatch.setattr(provider.client, "_cat_cached", read)

    raw = provider.read_path_bytes_at_revision(
        _identity(),
        "TableCsv/角色 配置.csv",
        103,
    )

    assert raw == b"content"
    assert calls == [
        (
            "https://svn.example/repo/branches/foo/TableCsv/%E8%A7%92%E8%89%B2%20%E9%85%8D%E7%BD%AE.csv",
            103,
            103,
        )
    ]


def test_cli_history_filters_mixed_revisions_prefix_collisions_gaps_and_dates(monkeypatch):
    provider = CLISVNProvider(cache_dir=None)
    identity = _identity()
    start = datetime.fromisoformat("2026-08-10T16:00:00+08:00")
    end = datetime.fromisoformat("2026-08-10T18:00:00+08:00")
    xml = _log_xml(
        _entry(
            101,
            "2026-08-10T08:00:00Z",
            _path("/branches/foo/TableCsv/AtStart.csv"),
        )
        + _entry(
            103,
            "2026-08-10T09:00:00+00:00",
            _path("/branches/foo/TableCsv/Role.csv")
            + _path("/branches/other/TableCsv/Role.csv")
            + _path("/branches/foobar/TableCsv/Role.csv"),
        )
        + _entry(
            110,
            "2026-08-10T10:00:00Z",
            _path("/branches/foo/TableCsv/End.csv"),
            author="bob",
        )
        + _entry(
            111,
            "2026-08-10T10:00:00.001Z",
            _path("/branches/foo/TableCsv/TooLate.csv"),
        )
    )
    monkeypatch.setattr(provider, "resolve_revision_at", lambda identity, value: 100 if value == start else 111)
    monkeypatch.setattr(provider, "_run_xml", lambda *args, **kwargs: xml)

    commits = provider.list_branch_commits(identity, start, end)

    assert [commit.revision for commit in commits] == [103, 110]
    assert [path.branch_relative_path for path in commits[0].changed_paths] == [
        "TableCsv/Role.csv"
    ]
    assert commits[1].author == "bob"


def test_cli_history_resolves_copy_boundary_without_following_source(monkeypatch):
    provider = CLISVNProvider(cache_dir=None)
    identity = _identity()
    xml = _log_xml(
        _entry(110, "2026-08-10T10:00:00Z", _path("/branches/foo/A.csv"))
        + _entry(
            50,
            "2026-08-01T00:00:00Z",
            _path(
                "/branches/foo",
                action="A",
                copy=' copyfrom-path="/trunk" copyfrom-rev="49"',
            ),
        )
    )
    monkeypatch.setattr(provider, "_run_xml", lambda *args, **kwargs: xml)

    boundary = provider.resolve_copy_boundary(identity)

    assert boundary.revision == 50
    assert boundary.copied_from_path == "trunk"
    assert boundary.copied_from_revision == 49


def test_cli_history_returns_stable_error_when_branch_did_not_exist(monkeypatch):
    provider = CLISVNProvider(cache_dir=None)
    monkeypatch.setattr(provider, "_run_xml", lambda *args, **kwargs: _log_xml(""))

    with pytest.raises(SVNProviderError) as captured:
        provider.resolve_revision_at(
            _identity(),
            datetime(2026, 7, 1, tzinfo=timezone.utc),
        )

    assert captured.value.code == "SVN_BRANCH_NOT_FOUND_AT_BOUNDARY"


def test_revision_at_ignores_non_target_paths_in_global_history(monkeypatch):
    provider = CLISVNProvider(cache_dir=None)
    xml = _log_xml(
        _entry(105, "2026-08-10T09:00:00Z", _path("/branches/other/A.csv"))
        + _entry(103, "2026-08-10T08:00:00Z", _path("/branches/foo/A.csv"))
    )
    monkeypatch.setattr(provider, "_run_xml", lambda *args, **kwargs: xml)

    revision = provider.resolve_revision_at(
        _identity(),
        datetime(2026, 8, 10, 10, 0, tzinfo=timezone.utc),
    )

    assert revision == 103


def test_branch_history_service_rejects_binding_drift():
    class Provider:
        def resolve_branch_identity(self, endpoint):
            return _identity("branches/other")

    service = BranchHistoryService(Provider())

    with pytest.raises(SVNProviderError) as captured:
        service.verify_branch_identity(
            EndpointSpec(url="https://svn.example/repo/branches/foo"),
            _identity(),
        )

    assert captured.value.code == "SVN_BRANCH_BINDING_INVALID"
