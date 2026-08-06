import json

from fastapi.testclient import TestClient

from app.main import create_app
from app.services.config_service import ConfigStore
from core.svn_provider import MockSVNProvider


def test_config_save_persists_only_server_url(tmp_path):
    config_path = tmp_path / "settings.json"
    config_path.write_text(
        json.dumps({"branch": "fix", "svn": {"provider": "mock", "timeout_seconds": 30}}),
        encoding="utf-8",
    )
    app = create_app(
        config={"svn": {"provider": "mock", "allowed_schemes": ["http", "https", "svn", "svn+ssh", "file"]}},
        provider=MockSVNProvider(),
    )
    app.state.config_store = ConfigStore(config_path)
    api = TestClient(app)

    response = api.post("/api/svn/config", json={"server_url": "https://mock.local/repo"})
    assert response.status_code == 200
    assert response.json() == {
        "provider": "mock",
        "server_url": "https://mock.local/repo",
        "credential_source": "svn_cli_cache",
    }

    saved = json.loads(config_path.read_text(encoding="utf-8"))
    assert saved["branch"] == "fix"
    assert saved["svn"]["provider"] == "mock"
    assert saved["svn"]["timeout_seconds"] == 30
    assert saved["svn"]["server_url"] == "https://mock.local/repo"


def test_config_save_rejects_credentials(tmp_path):
    config_path = tmp_path / "settings.json"
    config_path.write_text(json.dumps({"svn": {"provider": "cli"}}), encoding="utf-8")
    app = create_app(
        config={"svn": {"provider": "cli", "allowed_schemes": ["http", "https", "svn", "svn+ssh", "file"]}},
        provider=MockSVNProvider(),
    )
    app.state.config_store = ConfigStore(config_path)
    api = TestClient(app)

    response = api.post(
        "/api/svn/config",
        json={"server_url": "https://user:password@example/repo"},
    )
    assert response.status_code == 400
    assert response.json()["error"]["code"] == "SVN_AUTH_FAILED"
def test_endpoint_catalog_save_and_validate(tmp_path):
    config_path = tmp_path / "settings.json"
    config_path.write_text(json.dumps({"svn": {"provider": "mock"}}), encoding="utf-8")
    app = create_app(
        config={"svn": {"provider": "mock", "allowed_schemes": ["http", "https", "svn", "svn+ssh", "file"]}},
        provider=MockSVNProvider(),
    )
    app.state.config_store = ConfigStore(config_path)
    api = TestClient(app)

    catalog = {
        "regions": {
            "TC": {"display_name": "港台 TC", "trunk_branch": "Trunk_TC", "fix_pattern": "TC-fix-x.x.x.x"},
            "KR": {"display_name": "韩国 KR", "trunk_branch": "Trunk_KR", "fix_pattern": "KR-fix-x.x.x.x"},
            "BT": {"display_name": "折扣 BT", "trunk_branch": "", "fix_pattern": ""},
            "JP": {"display_name": "日本 JP", "trunk_branch": "", "fix_pattern": ""},
        }
    }
    response = api.post("/api/svn/endpoint-catalog", json=catalog)
    assert response.status_code == 200
    saved = json.loads(config_path.read_text(encoding="utf-8"))
    assert saved["svn"]["endpoint_catalog"]["KR"]["fix_pattern"] == "KR-fix-x.x.x.x"

    invalid = dict(catalog)
    invalid["regions"] = dict(catalog["regions"])
    invalid["regions"]["KR"] = dict(catalog["regions"]["KR"])
    invalid["regions"]["KR"]["fix_pattern"] = "KR-fix-../secret"
    response = api.post("/api/svn/endpoint-catalog", json=invalid)
    assert response.status_code == 400
    assert response.json()["error"]["code"] == "SVN_INVALID_ENDPOINT_CONFIG"

def test_endpoint_registry_save_supports_multiple_fix_endpoints(tmp_path):
    config_path = tmp_path / "settings.json"
    config_path.write_text(json.dumps({"svn": {"provider": "mock"}}), encoding="utf-8")
    app = create_app(
        config={"svn": {"provider": "mock", "allowed_schemes": ["http", "https", "svn", "svn+ssh", "file"]}},
        provider=MockSVNProvider(),
    )
    app.state.config_store = ConfigStore(config_path)
    api = TestClient(app)
    payload = {
        "endpoints": [
            {
                "id": "KR_FIX_1_1_0",
                "region": "KR",
                "track": "FIX",
                "label": "FIX1.1.0",
                "url": "https://mock.local/repo/branches/KR-fix-1.1.0",
                "logical_scopes": ["Table"],
                "physical_path_filters": {"Table": "Resource/table"},
                "enabled": True,
            },
            {
                "id": "KR_FIX_1_0_0",
                "region": "KR",
                "track": "FIX",
                "label": "fix1.0.0",
                "url": "https://mock.local/repo/branches/KR-fix-1.0.0",
                "logical_scopes": ["TABLE"],
                "physical_path_filters": {"TABLE": "Resource/Table"},
                "enabled": True,
            },
        ]
    }
    response = api.post("/api/svn/endpoints", json=payload)
    assert response.status_code == 200
    assert [item["id"] for item in response.json()["endpoints"]] == ["KR_FIX_1_1_0", "KR_FIX_1_0_0"]
    assert response.json()["endpoints"][0]["logical_scopes"] == ["TABLE"]
    saved = json.loads(config_path.read_text(encoding="utf-8"))
    assert saved["svn"]["endpoint_registry"][0]["physical_path_filters"] == {"TABLE": "Resource/table"}