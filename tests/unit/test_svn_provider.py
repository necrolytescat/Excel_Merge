import json
from pathlib import Path
import xml.etree.ElementTree as ET

import pytest

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
