import json
from pathlib import Path
import xml.etree.ElementTree as ET

import pytest

from core.svn_history import BranchIdentity
from core.models import EndpointSpec
from core.svn_provider import CLISVNProvider, MockSVNProvider, SVNProviderError, normalize_relative_path, validate_endpoint


FIXTURE = Path(__file__).parents[1] / "fixtures" / "mock_svn" / "repository.json"


def provider():
    return MockSVNProvider(json.loads(FIXTURE.read_text(encoding="utf-8")))


def endpoint(revision="HEAD", url="https://mock.local/repo"):
    return EndpointSpec(url=url, revision=revision)


def test_mock_info_tree_log_and_content():
    p = provider()
    info = p.info(endpoint())
    assert info.repository_uuid == "fixture-repository-uuid"
    assert [item.path for item in p.list_tree(endpoint())] == ["table", "table/Test.csv"]
    assert p.log(endpoint()).pop().revision == 105
    content = p.read_content(endpoint(105), "table/Test.csv")
    assert content.encoding == "utf-8"
    assert "new" in content.text


@pytest.mark.parametrize(
    "upper_revision,expected_args",
    [
        (
            "HEAD",
            [
                "log",
                "--xml",
                "--stop-on-copy",
                "--limit",
                "31",
                "https://svn.example/repo/branches/foo",
            ],
        ),
        (
            105,
            [
                "log",
                "--xml",
                "--stop-on-copy",
                "--limit",
                "31",
                "--revision",
                "105:1",
                "https://svn.example/repo/branches/foo",
            ],
        ),
    ],
)
def test_cli_branch_log_page_uses_native_limit_and_branch_url(
    monkeypatch,
    upper_revision,
    expected_args,
):
    p = CLISVNProvider(cache_dir=None)
    calls = []
    root = ET.fromstring(
        """
        <log>
          <logentry revision="105"><author>alice</author><date>2026-08-12T05:30:00Z</date><msg>new</msg></logentry>
          <logentry revision="103"><author>bob</author><date>2026-08-11T04:20:00Z</date><msg>old</msg></logentry>
        </log>
        """
    )
    monkeypatch.setattr(p, "_validate", lambda value: value)

    def run(args, **kwargs):
        calls.append(args)
        return root

    monkeypatch.setattr(p, "_run_xml", run)
    branch_url = "https://svn.example/repo/branches/foo"

    commits = p.branch_log_page(endpoint(url=branch_url), upper_revision, 31)

    assert [item.revision for item in commits] == [105, 103]
    assert [item.date for item in commits] == [
        "2026-08-12T05:30:00Z",
        "2026-08-11T04:20:00Z",
    ]
    assert calls == [expected_args]


@pytest.mark.parametrize(
    "stderr",
    [
        "svn: E170013: Unable to connect to a repository at URL 'https://svn.example/repo'",
        "svn: E730053: Error running context: 中止了一个已建立的连接。",
    ],
)
def test_cli_maps_connection_abort_to_not_reachable(stderr):
    error = CLISVNProvider._error_from_text(stderr, "SVN_NOT_REACHABLE")

    assert error.code == "SVN_NOT_REACHABLE"
    assert error.message == "SVN 连接中断"


def test_mock_preview_is_truncated():
    p = provider()
    content = p.read_content(endpoint(105), "table/Test.csv", preview_limit=4)
    assert content.truncated is True
    assert content.size > len(content.text.encode("utf-8"))


@pytest.mark.parametrize(
    "url,code",
    [
        ("https://mock.local/auth-fail", "SVN_AUTH_FAILED"),
        ("https://mock.local/timeout", "SVN_TIMEOUT"),
        ("https://mock.local/missing", "SVN_NOT_FOUND"),
    ],
)
def test_mock_failure_scenarios(url, code):
    with pytest.raises(SVNProviderError) as error:
        provider().info(endpoint(url=url))
    assert error.value.code == code


def test_path_traversal_is_rejected():
    with pytest.raises(SVNProviderError):
        normalize_relative_path("table/../secret.csv")


def test_endpoint_rejects_credentials_and_invalid_scheme():
    with pytest.raises(SVNProviderError) as credential_error:
        validate_endpoint(endpoint(url="https://user:password@example/repo"))
    assert credential_error.value.code == "SVN_AUTH_FAILED"
    with pytest.raises(SVNProviderError) as scheme_error:
        validate_endpoint(endpoint(url="ftp://example/repo"))
    assert scheme_error.value.code == "SVN_NOT_FOUND"


def branch_identity(
    branch: str,
    revision: int,
    *,
    repository_uuid: str = "repo-uuid",
    repository_root: str = "https://svn.example/repo",
) -> BranchIdentity:
    return BranchIdentity(
        canonical_url=f"https://svn.example/repo/branches/{branch}",
        repository_root=repository_root,
        repository_uuid=repository_uuid,
        repository_relative_path=f"branches/{branch}",
        bound_revision=revision,
    )


def test_cli_summarizes_frozen_tree_diff_with_exact_read_only_command(monkeypatch):
    p = CLISVNProvider(cache_dir=None)
    calls = []
    root = ET.fromstring(
        """
        <diff>
          <paths>
            <path kind="file" item="modified">https://svn.example/repo/branches/target/Source/Table/Changed.xlsx</path>
            <path kind="file" item="added">https://svn.example/repo/branches/target/Source/Table/New%20Book.xlsx</path>
            <path kind="file" item="deleted">https://svn.example/repo/branches/source/Source/Table/Removed.xlsx</path>
            <path kind="dir" item="replaced">https://svn.example/repo/branches/target/Source/Table/Nested</path>
          </paths>
        </diff>
        """
    )

    def run(args, **kwargs):
        calls.append((args, kwargs))
        return root

    monkeypatch.setattr(p, "_run_xml", run)
    source = branch_identity("source", 101)
    target = branch_identity("target", 205)

    result = p.summarize_frozen_tree_diff(
        source,
        "Source/Table",
        target,
        "Source/Table",
    )

    assert calls == [
        (
            [
                "diff",
                "--summarize",
                "--xml",
                "--notice-ancestry",
                "--ignore-properties",
                "--depth",
                "infinity",
                "--old",
                "https://svn.example/repo/branches/source/Source/Table@101",
                "--new",
                "https://svn.example/repo/branches/target/Source/Table@205",
            ],
            {
                "fallback": "SVN_TREE_DIFF_UNAVAILABLE",
                "reject_stderr": True,
            },
        )
    ]
    assert [
        (change.relative_path, change.action, change.kind)
        for change in result.changes
    ] == [
        ("Changed.xlsx", "M", "file"),
        ("New Book.xlsx", "A", "file"),
        ("Removed.xlsx", "D", "file"),
        ("Nested", "R", "dir"),
    ]
    assert result.repository_uuid == "repo-uuid"
    assert result.source_canonical_url == source.canonical_url
    assert result.source_revision == 101
    assert result.source_root == "Source/Table"
    assert result.target_canonical_url == target.canonical_url
    assert result.target_revision == 205
    assert result.target_root == "Source/Table"


@pytest.mark.parametrize(
    "xml",
    [
        "<log />",
        "<diff />",
        "<diff><paths /><paths /></diff>",
        (
            "<diff><paths><path kind='file' item='conflicted'>"
            "https://svn.example/repo/branches/target/Source/Table/A.xlsx"
            "</path></paths></diff>"
        ),
        (
            "<diff><paths><path kind='symlink' item='modified'>"
            "https://svn.example/repo/branches/target/Source/Table/A.xlsx"
            "</path></paths></diff>"
        ),
        (
            "<diff><paths><path kind='file' item='modified'>"
            "https://svn.example/repo/branches/target/Outside/A.xlsx"
            "</path></paths></diff>"
        ),
    ],
)
def test_cli_rejects_incomplete_or_ambiguous_tree_diff_xml(monkeypatch, xml):
    p = CLISVNProvider(cache_dir=None)
    monkeypatch.setattr(p, "_run_xml", lambda *args, **kwargs: ET.fromstring(xml))

    with pytest.raises(SVNProviderError) as error:
        p.summarize_frozen_tree_diff(
            branch_identity("source", 101),
            "Source/Table",
            branch_identity("target", 205),
            "Source/Table",
        )

    assert error.value.code == "SVN_TREE_DIFF_INVALID"


@pytest.mark.parametrize(
    "source,target",
    [
        (
            branch_identity("source", 101, repository_uuid="repo-a"),
            branch_identity("target", 205, repository_uuid="repo-b"),
        ),
        (
            branch_identity("source", 101),
            branch_identity(
                "target",
                205,
                repository_root="https://svn.example/other",
            ),
        ),
    ],
)
def test_cli_rejects_tree_diff_across_repository_identity(monkeypatch, source, target):
    p = CLISVNProvider(cache_dir=None)
    called = False

    def run(*args, **kwargs):
        nonlocal called
        called = True
        return ET.fromstring("<diff><paths /></diff>")

    monkeypatch.setattr(p, "_run_xml", run)

    with pytest.raises(SVNProviderError) as error:
        p.summarize_frozen_tree_diff(
            source,
            "Source/Table",
            target,
            "Source/Table",
        )

    assert error.value.code == "SVN_TREE_DIFF_REPOSITORY_MISMATCH"
    assert called is False


@pytest.mark.parametrize(
    "stderr,expected",
    [
        ("svn: E170001: Authorization failed", "SVN_AUTH_FAILED"),
        ("svn: E175012: Connection timed out", "SVN_TIMEOUT"),
        (
            "svn: E205000: invalid option: --notice-ancestry",
            "SVN_TREE_DIFF_UNAVAILABLE",
        ),
    ],
)
def test_cli_maps_tree_diff_command_failures(stderr, expected):
    error = CLISVNProvider._error_from_text(stderr, "SVN_TREE_DIFF_UNAVAILABLE")

    assert error.code == expected


def test_cli_maps_tree_diff_invalid_xml(monkeypatch):
    p = CLISVNProvider(cache_dir=None)
    monkeypatch.setattr(
        "core.svn_provider.svn_client._run",
        lambda *args, **kwargs: (0, "<diff>", ""),
    )

    with pytest.raises(SVNProviderError) as error:
        p.summarize_frozen_tree_diff(
            branch_identity("source", 101),
            "Source/Table",
            branch_identity("target", 205),
            "Source/Table",
        )

    assert error.value.code == "SVN_DECODE_ERROR"


def test_cli_rejects_successful_tree_diff_with_stderr_warning(monkeypatch):
    p = CLISVNProvider(cache_dir=None)
    monkeypatch.setattr(
        "core.svn_provider.svn_client._run",
        lambda *args, **kwargs: (
            0,
            "<diff><paths /></diff>",
            "svn: warning: W170001: authorization filtered a path",
        ),
    )

    with pytest.raises(SVNProviderError) as error:
        p.summarize_frozen_tree_diff(
            branch_identity("source", 101),
            "Source/Table",
            branch_identity("target", 205),
            "Source/Table",
        )

    assert error.value.code == "SVN_TREE_DIFF_UNAVAILABLE"
