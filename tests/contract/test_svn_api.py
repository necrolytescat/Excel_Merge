import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.main import create_app
from app.services.config_service import ConfigStore
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


def snapshot_client(tmp_path):
    fixture = json.loads(FIXTURE.read_text(encoding="utf-8"))
    records = [
        {
            "id": endpoint_id,
            "region": "KR",
            "track": "FIX",
            "label": endpoint_id,
            "url": "https://mock.local/repo/branches/" + endpoint_id,
            "logical_scopes": ["TABLE"],
            "physical_path_filters": {},
            "enabled": True,
        }
        for endpoint_id in ("LEFT", "RIGHT")
    ]
    app = create_app(
        config={"svn": {"provider": "mock", "endpoint_registry": records}},
        provider=MockSVNProvider(fixture),
    )
    app.state.config_store = ConfigStore(tmp_path / "settings.json")
    return TestClient(app)


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


def test_branch_logs_are_branch_scoped_cursor_paginated_and_minimal():
    fixture = json.loads(FIXTURE.read_text(encoding="utf-8"))
    fixture["info"]["revision"] = "165"
    fixture["info"]["last_changed_revision"] = "165"
    fixture["logs"] = [
        {
            "revision": revision,
            "author": "author-" + str(revision),
            "date": "2026-08-04T09:00:00Z",
            "message": "message-" + str(revision),
            "changed_paths": [{"path": "/repo/branches/foo/Table/A.xlsx", "action": "M"}],
        }
        for revision in range(101, 166)
    ]
    api = TestClient(
        create_app(
            config={"svn": {"provider": "mock"}},
            provider=MockSVNProvider(fixture),
        )
    )
    branch_url = "https://mock.local/repo/branches/foo"

    first = api.get("/api/svn/branch-logs", params={"url": branch_url})

    assert first.status_code == 200
    body = first.json()
    assert body["schema_version"] == "m2.svn-branch-log.v1"
    assert len(body["commits"]) == 30
    assert [item["revision"] for item in body["commits"]] == list(range(165, 135, -1))
    assert set(body["commits"][0]) == {"revision", "author", "date", "message"}
    assert body["commits"][0]["date"] == "2026-08-04T09:00:00Z"
    assert body["has_more"] is True
    assert body["next_cursor"]

    second = api.get(
        "/api/svn/branch-logs",
        params={"url": branch_url, "cursor": body["next_cursor"]},
    )
    assert second.status_code == 200
    second_body = second.json()
    assert [item["revision"] for item in second_body["commits"]] == list(range(135, 105, -1))
    assert not (
        {item["revision"] for item in body["commits"]}
        & {item["revision"] for item in second_body["commits"]}
    )

    final = api.get(
        "/api/svn/branch-logs",
        params={"url": branch_url, "cursor": second_body["next_cursor"]},
    ).json()
    assert [item["revision"] for item in final["commits"]] == list(range(105, 100, -1))
    assert final["has_more"] is False
    assert final["next_cursor"] is None

    wrong_branch = api.get(
        "/api/svn/branch-logs",
        params={
            "url": "https://mock.local/repo/branches/other",
            "cursor": body["next_cursor"],
        },
    )
    assert wrong_branch.status_code == 400
    assert wrong_branch.json()["error"]["code"] == "SVN_INVALID_CURSOR"
    malformed = api.get(
        "/api/svn/branch-logs",
        params={"url": branch_url, "cursor": "not-a-cursor"},
    )
    assert malformed.status_code == 400
    assert malformed.json()["error"]["code"] == "SVN_INVALID_CURSOR"


def test_snapshot_api_defaults_to_head_and_accepts_fixed_revisions(tmp_path):
    api = snapshot_client(tmp_path)

    default_response = api.post(
        "/api/svn/snapshots",
        json={
            "source": {"endpoint_id": "LEFT"},
            "target": {"endpoint_id": "RIGHT"},
        },
    )
    assert default_response.status_code == 200
    assert default_response.json()["source"]["resolved_revision"] == 105
    assert default_response.json()["target"]["resolved_revision"] == 105

    fixed_response = api.post(
        "/api/svn/snapshots",
        json={
            "source": {"endpoint_id": "LEFT", "revision": 100},
            "target": {"endpoint_id": "LEFT", "revision": 105},
        },
    )
    assert fixed_response.status_code == 200
    assert fixed_response.json()["source"]["resolved_revision"] == 100
    assert fixed_response.json()["target"]["resolved_revision"] == 105


@pytest.mark.parametrize("revision", [0, -1, True, "100", "head"])
def test_snapshot_api_rejects_invalid_revision_values(tmp_path, revision):
    api = snapshot_client(tmp_path)
    response = api.post(
        "/api/svn/snapshots",
        json={
            "source": {"endpoint_id": "LEFT", "revision": revision},
            "target": {"endpoint_id": "RIGHT", "revision": "HEAD"},
        },
    )

    assert response.status_code == 422


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
