import json
from pathlib import Path

import pytest

from core.models import EndpointSpec
from core.svn_provider import MockSVNProvider, SVNProviderError, normalize_relative_path, validate_endpoint


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
