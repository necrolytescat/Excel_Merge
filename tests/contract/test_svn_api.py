import json
from pathlib import Path

from fastapi.testclient import TestClient

from app.main import create_app
from core.svn_provider import MockSVNProvider


FIXTURE = Path(__file__).parents[1] / "fixtures" / "mock_svn" / "repository.json"


def client():
    fixture = json.loads(FIXTURE.read_text(encoding="utf-8"))
    config = {
        "svn": {
            "provider": "mock",
            "allowed_schemes": ["http", "https", "svn", "svn+ssh", "file"],
            "content_preview_max_bytes": 64,
        }
    }
    return TestClient(create_app(config=config, provider=MockSVNProvider(fixture)))


def test_health_and_probe_contract():
    api = client()
    health = api.get("/api/health")
    assert health.status_code == 200
    assert health.json()["provider"] == "mock"

    probe = api.post("/api/svn/probe", json={"url": "https://mock.local/repo", "revision": "HEAD"})
    assert probe.status_code == 200
    assert probe.json()["repository_uuid"] == "fixture-repository-uuid"


def test_tree_log_and_content_contract():
    api = client()
    tree = api.get("/api/svn/tree", params={"url": "https://mock.local/repo", "revision": "HEAD"})
    assert tree.status_code == 200
    assert tree.json()["entries"][0]["path"] == "table"

    logs = api.get("/api/svn/log", params={"url": "https://mock.local/repo", "revision": "HEAD"})
    assert logs.status_code == 200
    assert logs.json()["commits"][0]["revision"] == 105

    content = api.get("/api/svn/content", params={"url": "https://mock.local/repo", "revision": 105, "path": "table/Test.csv"})
    assert content.status_code == 200
    assert "new" in content.json()["text"]


def test_api_returns_stable_error_shape():
    api = client()
    response = api.get("/api/svn/content", params={"url": "https://mock.local/repo", "revision": 105, "path": "../secret.csv"})
    assert response.status_code == 400
    assert response.json()["error"]["code"] == "SVN_PATH_NOT_FOUND"


def test_branch_candidates_follow_endpoint_catalog_patterns():
    fixture = json.loads(FIXTURE.read_text(encoding="utf-8"))
    fixture["children"] = [
        {"path": "Trunk_KR", "kind": "dir"},
        {"path": "Trunk_Tc", "kind": "dir"},
        {"path": "branches", "kind": "dir"},
        {"path": "branches/KR-Fix-1.0.0.0", "kind": "dir"},
        {"path": "branches/TC-Fix-5.0.0.0", "kind": "dir"},
        {"path": "branches/feature-a8", "kind": "dir"},
        {"path": "branches/reborn-cn-dev-3.1.0.0", "kind": "dir"},
    ]
    api = TestClient(
        create_app(
            config={"svn": {"provider": "mock"}},
            provider=MockSVNProvider(fixture),
        )
    )

    kr = api.get("/api/svn/branch-candidates", params={"url": "https://mock.local/repo", "region": "KR"})
    assert kr.status_code == 200
    assert kr.json()["trunk_branches"] == ["Trunk_KR"]
    assert kr.json()["fix_branches"] == ["KR-Fix-1.0.0.0"]
    assert [item["match_type"] for item in kr.json()["matches"]] == ["FIX", "TRUNK"]

    all_regions = api.get("/api/svn/branch-candidates", params={"url": "https://mock.local/repo"})
    assert all_regions.status_code == 200
    assert all_regions.json()["fix_branches"] == ["KR-Fix-1.0.0.0", "TC-Fix-5.0.0.0"]
    assert "feature-a8" not in all_regions.json()["fix_branches"]
    assert "reborn-cn-dev-3.1.0.0" not in all_regions.json()["fix_branches"]