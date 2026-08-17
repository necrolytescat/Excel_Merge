from __future__ import annotations

from copy import deepcopy
import csv
import hashlib
from io import StringIO
import json
from pathlib import Path

from fastapi.testclient import TestClient
import pytest

from app.main import create_app
from app.schemas.batch import BatchEndpointPayload
from app.services.workbook_diff_service import DatasetLayout, WorkbookDiffService
from core.svn_provider import (
    MockSVNProvider,
    SVNProviderError,
    normalize_relative_path,
)


PROJECT_ROOT = Path(__file__).resolve().parents[2]
SOURCE_DIR = PROJECT_ROOT / "tests" / "excel" / "left"
TARGET_DIR = PROJECT_ROOT / "tests" / "excel" / "right"
CONFIG = json.loads(
    (PROJECT_ROOT / "config" / "settings.json").read_text(encoding="utf-8")
)
SOURCE_ENDPOINT_ID = "LEFT"
TARGET_ENDPOINT_ID = "RIGHT"
SOURCE_REVISION = 101
TARGET_REVISION = 202
WORKBOOK_NAME = "AtlasConfig.xlsm"


class CountingProvider(MockSVNProvider):
    def __init__(self, fixture):
        super().__init__(fixture)
        self.info_calls = []
        self.list_tree_calls = []
        self.list_children_calls = []
        self.read_calls = []
        self.failure_path: str | None = None

    def info(self, endpoint):
        self.info_calls.append(endpoint)
        raise AssertionError("阶段 C 不得重新解析 HEAD")

    def list_tree(self, endpoint, prefix=""):
        self.list_tree_calls.append((endpoint, prefix))
        return super().list_tree(endpoint, prefix)

    def list_children(self, endpoint, prefix=""):
        self.list_children_calls.append((endpoint, prefix))
        return super().list_children(endpoint, prefix)

    def read_bytes(self, endpoint, path):
        clean_path = normalize_relative_path(path)
        self.read_calls.append((endpoint, clean_path))
        if clean_path == self.failure_path:
            raise SVNProviderError("SVN_TIMEOUT", "internal svn target")
        return super().read_bytes(endpoint, clean_path)


class RecordingDiffService:
    def __init__(self):
        self.inner = WorkbookDiffService(
            DatasetLayout.from_config(CONFIG["dataset_layout"])
        )
        self.directories: tuple[Path, Path] | None = None

    def compare_local(self, source_directory, target_directory, workbook_name):
        self.directories = (Path(source_directory), Path(target_directory))
        assert self.directories[0].exists()
        assert self.directories[1].exists()
        return self.inner.compare_local(
            source_directory,
            target_directory,
            workbook_name,
        )


class RaisingDiffService:
    def __init__(self):
        self.directories: tuple[Path, Path] | None = None

    def compare_local(self, source_directory, target_directory, workbook_name):
        self.directories = (Path(source_directory), Path(target_directory))
        raise RuntimeError("temporary dataset path must not leak")


def _endpoint_records():
    return [
        {
            "id": SOURCE_ENDPOINT_ID,
            "region": "KR",
            "track": "FIX",
            "label": "Left",
            "url": "mock://left",
            "logical_scopes": ["TABLE"],
            "enabled": True,
            "physical_path_filters": {"TABLE": "left/Table"},
        },
        {
            "id": TARGET_ENDPOINT_ID,
            "region": "KR",
            "track": "FIX",
            "label": "Right",
            "url": "mock://right",
            "logical_scopes": ["TABLE"],
            "enabled": True,
            "physical_path_filters": {"TABLE": "right/Table"},
        },
    ]


def _atlas_fixture():
    paths = {
        "source": {
            "directory": SOURCE_DIR,
            "table": "left/Table",
            "csv": "left/TaBlEcSv",
            "revision": SOURCE_REVISION,
        },
        "target": {
            "directory": TARGET_DIR,
            "table": "right/Table",
            "csv": "right/tablecsv",
            "revision": TARGET_REVISION,
        },
    }
    tree = []
    children = []
    content = {}
    for item in paths.values():
        tree.extend(
            [
                {"path": item["table"], "kind": "dir"},
                {"path": item["csv"], "kind": "dir"},
            ]
        )
        children.extend(
            [
                {"path": item["table"], "kind": "dir"},
                {"path": item["csv"], "kind": "dir"},
            ]
        )
        for local_path in sorted(item["directory"].iterdir()):
            parent = item["table"] if local_path.suffix.casefold() in {
                ".xlsx",
                ".xlsm",
                ".xls",
            } else item["csv"]
            svn_path = f"{parent}/{local_path.name}"
            raw = local_path.read_bytes()
            tree.append(
                {
                    "path": svn_path,
                    "kind": "file",
                    "size": len(raw),
                    "revision": str(item["revision"]),
                }
            )
            content[svn_path] = {str(item["revision"]): raw}
    return {
        "info": {
            "repository_root": "mock://repository",
            "repository_uuid": "atlas-fixture",
            "revision": str(TARGET_REVISION),
        },
        "tree": tree,
        "children": children,
        "content": content,
    }


def _request_payload(*, workbook_path=WORKBOOK_NAME):
    return {
        "schema_version": "m2.workbook-compare.request.v1",
        "request_id": "a7e47a49-3308-4d10-936c-bbb80e4547b3",
        "source": {
            "endpoint_id": SOURCE_ENDPOINT_ID,
            "revision": SOURCE_REVISION,
        },
        "target": {
            "endpoint_id": TARGET_ENDPOINT_ID,
            "revision": TARGET_REVISION,
        },
        "workbook_path": workbook_path,
    }


def _create_client(fixture, *, provider=None, service=None, records=None):
    provider = provider or CountingProvider(fixture)
    config = {
        "dataset_layout": deepcopy(CONFIG["dataset_layout"]),
        "svn": {
            "provider": "mock",
            "allowed_schemes": ["mock"],
            "endpoint_registry": records or _endpoint_records(),
        },
    }
    app = create_app(
        config=config,
        provider=provider,
        workbook_diff_service=service,
    )
    return app, TestClient(app), provider


def _fixed_dataset_hashes():
    return {
        str(path.relative_to(PROJECT_ROOT)): hashlib.sha256(path.read_bytes()).hexdigest()
        for directory in (SOURCE_DIR, TARGET_DIR)
        for path in sorted(directory.iterdir())
    }


def _duplicate_key_csv():
    buffer = StringIO(newline="")
    writer = csv.writer(buffer, lineterminator="\n")
    writer.writerows(
        [
            ["显示名", "描述"],
            ["Id", "Name"],
            ["uint32", "string"],
            ["All", "Client"],
            ["meta", ""],
            ["meta", ""],
            ["meta", ""],
            ["1", "Alpha"],
            ["1", "Beta"],
        ]
    )
    return b"\xef\xbb\xbf" + buffer.getvalue().encode("utf-8")


def _rename_csv_case(fixture, old_path, new_path):
    fixture["content"][new_path] = fixture["content"].pop(old_path)
    for entry in fixture["tree"]:
        if entry["path"] == old_path:
            entry["path"] = new_path
            break
    fixture["children"].append({"path": new_path, "kind": "file"})


def test_svn_api_uses_frozen_revisions_exact_manifest_files_and_cleans_up():
    fixture = _atlas_fixture()
    provider = CountingProvider(fixture)
    provider_before = deepcopy(provider.fixture)
    fixed_before = _fixed_dataset_hashes()
    service = RecordingDiffService()
    _, client, _ = _create_client(fixture, provider=provider, service=service)

    response = client.post(
        "/api/diff/workbooks/compare",
        json=_request_payload(),
    )

    assert response.status_code == 200
    result = response.json()
    assert result["schema_version"] == "m2.diff.v1"
    assert result["direction"] == {"source": "left", "target": "right"}
    assert result["workbook"]["status"] == "modified"
    assert result["summary"]["total_sheets"] == 16
    assert result["summary"]["modified_rows"] == 273
    assert result["summary"]["modified_fields"] == 375
    assert provider.info_calls == []
    assert {
        (endpoint.url, endpoint.revision, prefix)
        for endpoint, prefix in provider.list_tree_calls
    } == {
        ("mock://left", SOURCE_REVISION, ""),
        ("mock://right", TARGET_REVISION, ""),
    }
    assert {
        (call[0].url, call[0].revision, call[1])
        for call in provider.read_calls
    } == {
        (
            "mock://left",
            SOURCE_REVISION,
            f"left/Table/{WORKBOOK_NAME}",
        ),
        (
            "mock://right",
            TARGET_REVISION,
            f"right/Table/{WORKBOOK_NAME}",
        ),
        *{
            (
                "mock://left",
                SOURCE_REVISION,
                f"left/TaBlEcSv/{path.name}",
            )
            for path in SOURCE_DIR.glob("*.csv")
        },
        *{
            (
                "mock://right",
                TARGET_REVISION,
                f"right/tablecsv/{path.name}",
            )
            for path in TARGET_DIR.glob("*.csv")
        },
    }
    assert all(call[0].revision != "HEAD" for call in provider.read_calls)
    assert all(call[0].revision != "HEAD" for call in provider.list_children_calls)
    assert service.directories is not None
    assert not service.directories[0].exists()
    assert not service.directories[1].exists()
    assert provider.fixture == provider_before
    assert _fixed_dataset_hashes() == fixed_before


def test_missing_table_binding_is_discovered_at_request_revision():
    fixture = _atlas_fixture()
    records = _endpoint_records()
    records[0]["physical_path_filters"] = {}
    _, client, provider = _create_client(fixture, records=records)

    response = client.post(
        "/api/diff/workbooks/compare",
        json=_request_payload(),
    )

    assert response.status_code == 200
    assert provider.info_calls == []
    assert {
        (endpoint.url, endpoint.revision, prefix)
        for endpoint, prefix in provider.list_tree_calls
    } == {
        ("mock://left", SOURCE_REVISION, ""),
        ("mock://right", TARGET_REVISION, ""),
    }


def test_table_binding_uses_actual_path_case_at_request_revision():
    fixture = _atlas_fixture()
    records = _endpoint_records()
    records[0]["physical_path_filters"] = {"TABLE": "LEFT/TABLE"}
    _, client, provider = _create_client(fixture, records=records)

    response = client.post(
        "/api/diff/workbooks/compare",
        json=_request_payload(),
    )

    assert response.status_code == 200
    source_paths = {
        path
        for endpoint, path in provider.read_calls
        if endpoint.url == "mock://left"
    }
    assert f"left/Table/{WORKBOOK_NAME}" in source_paths
    assert not any(path.startswith("LEFT/TABLE/") for path in source_paths)


@pytest.mark.parametrize(
    ("mutation", "status_code", "error_code"),
    [
        (
            lambda fixture: (
                fixture["content"].pop(f"left/Table/{WORKBOOK_NAME}"),
                fixture["content"].pop(f"right/Table/{WORKBOOK_NAME}"),
            ),
            404,
            "DIFF_WORKBOOK_NOT_FOUND",
        ),
        (
            lambda fixture: fixture["content"].pop(
                f"right/Table/{WORKBOOK_NAME}"
            ),
            422,
            "DIFF_CANDIDATE_NOT_COMPARABLE",
        ),
        (
            lambda fixture: fixture["content"].update(
                {
                    f"right/Table/{WORKBOOK_NAME}": {
                        str(TARGET_REVISION): SOURCE_DIR.joinpath(
                            WORKBOOK_NAME
                        ).read_bytes()
                    }
                }
            ),
            422,
            "DIFF_CANDIDATE_NOT_COMPARABLE",
        ),
    ],
)
def test_missing_or_non_modified_workbooks_are_orchestration_errors(
    mutation,
    status_code,
    error_code,
):
    fixture = _atlas_fixture()
    mutation(fixture)
    _, client, provider = _create_client(fixture)

    response = client.post(
        "/api/diff/workbooks/compare",
        json=_request_payload(),
    )

    assert response.status_code == status_code
    assert response.json()["error"]["code"] == error_code
    assert provider.info_calls == []


def test_missing_csv_remains_http_200_partial_with_available_sheets():
    fixture = _atlas_fixture()
    fixture["content"].pop("right/tablecsv/AtlasConfig_Base.csv")
    _, client, _ = _create_client(fixture)

    response = client.post(
        "/api/diff/workbooks/compare",
        json=_request_payload(),
    )

    assert response.status_code == 200
    result = response.json()
    assert result["workbook"]["status"] == "partial"
    assert result["summary"]["failed_sheets"] == 1
    assert len(result["sheets"]) == 16
    assert any(sheet["status"] != "failed" for sheet in result["sheets"])
    assert "M2_CSV_MISSING" in {error["code"] for error in result["errors"]}


def test_unique_casefold_csv_filename_match_uses_frozen_revision():
    fixture = _atlas_fixture()
    _rename_csv_case(
        fixture,
        "left/TaBlEcSv/AtlasConfig_Base.csv",
        "left/TaBlEcSv/AtlasConfig_bASe.csv",
    )
    _rename_csv_case(
        fixture,
        "right/tablecsv/AtlasConfig_Base.csv",
        "right/tablecsv/AtlasConfig_bASe.csv",
    )
    _, client, provider = _create_client(fixture)

    response = client.post(
        "/api/diff/workbooks/compare",
        json=_request_payload(),
    )

    assert response.status_code == 200
    assert response.json()["workbook"]["status"] == "modified"
    assert {
        (call[0].url, call[0].revision, call[1])
        for call in provider.read_calls
        if call[1].endswith("AtlasConfig_bASe.csv")
    } == {
        ("mock://left", SOURCE_REVISION, "left/TaBlEcSv/AtlasConfig_bASe.csv"),
        ("mock://right", TARGET_REVISION, "right/tablecsv/AtlasConfig_bASe.csv"),
    }
    assert {
        (call[0].url, call[0].revision, call[1])
        for call in provider.list_children_calls
        if call[1].casefold().endswith("tablecsv")
    } == {
        ("mock://left", SOURCE_REVISION, "left/TaBlEcSv"),
        ("mock://right", TARGET_REVISION, "right/tablecsv"),
    }


def test_casefold_csv_filename_collision_is_rejected():
    fixture = _atlas_fixture()
    original_csv = fixture["content"].pop(
        "left/TaBlEcSv/AtlasConfig_Base.csv"
    )
    fixture["content"]["left/TaBlEcSv/AtlasConfig_bASe.csv"] = original_csv
    fixture["content"]["left/TaBlEcSv/AtlasConfig_BasE.csv"] = original_csv
    fixture["children"].extend(
        [
            {"path": "left/TaBlEcSv/AtlasConfig_bASe.csv", "kind": "file"},
            {"path": "left/TaBlEcSv/AtlasConfig_BasE.csv", "kind": "file"},
        ]
    )
    _, client, provider = _create_client(fixture)

    response = client.post(
        "/api/diff/workbooks/compare",
        json=_request_payload(),
    )

    assert response.status_code == 500
    assert response.json() == {
        "error": {
            "code": "DIFF_DATASET_CONFIG_INVALID",
            "message": "冻结 Revision 的 TableCsv 文件名大小写匹配不唯一",
        }
    }
    assert provider.info_calls == []


def test_invalid_manifest_remains_http_200_failed():
    fixture = _atlas_fixture()
    fixture["content"][f"left/Table/{WORKBOOK_NAME}"] = {
        str(SOURCE_REVISION): b"not-an-excel-workbook"
    }
    _, client, provider = _create_client(fixture)

    response = client.post(
        "/api/diff/workbooks/compare",
        json=_request_payload(),
    )

    assert response.status_code == 200
    result = response.json()
    assert result["workbook"]["status"] == "failed"
    assert "M2_WORKBOOK_PARSE_FAILED" in {
        error["code"] for error in result["errors"]
    }
    assert len(provider.read_calls) == 2


def test_duplicate_primary_key_remains_http_200_partial():
    fixture = _atlas_fixture()
    fixture["content"]["right/tablecsv/AtlasConfig_Base.csv"] = {
        str(TARGET_REVISION): _duplicate_key_csv()
    }
    _, client, _ = _create_client(fixture)

    response = client.post(
        "/api/diff/workbooks/compare",
        json=_request_payload(),
    )

    assert response.status_code == 200
    result = response.json()
    assert result["workbook"]["status"] == "partial"
    assert "M2_CSV_DUPLICATE_KEY" in {
        error["code"] for error in result["errors"]
    }


def test_svn_read_failure_is_stable_orchestration_error():
    fixture = _atlas_fixture()
    provider = CountingProvider(fixture)
    provider.failure_path = f"left/Table/{WORKBOOK_NAME}"
    _, client, _ = _create_client(fixture, provider=provider)

    response = client.post(
        "/api/diff/workbooks/compare",
        json=_request_payload(),
    )

    assert response.status_code == 500
    assert response.json() == {
        "error": {
            "code": "DIFF_DATASET_READ_FAILED",
            "message": "无法读取冻结 Revision 数据集",
        }
    }
    assert "internal svn target" not in response.text


def test_temporary_dataset_is_cleaned_when_diff_service_raises():
    fixture = _atlas_fixture()
    service = RaisingDiffService()
    _, client, _ = _create_client(fixture, service=service)

    response = client.post(
        "/api/diff/workbooks/compare",
        json=_request_payload(),
    )

    assert response.status_code == 500
    assert response.json()["error"]["code"] == "DIFF_ORCHESTRATION_FAILED"
    assert service.directories is not None
    assert not service.directories[0].exists()
    assert not service.directories[1].exists()


def test_endpoint_registry_is_read_dynamically_for_each_request():
    fixture = _atlas_fixture()
    app, client, provider = _create_client(fixture)
    app.state.endpoint_registry[0]["enabled"] = False

    response = client.post(
        "/api/diff/workbooks/compare",
        json=_request_payload(),
    )

    assert response.status_code == 404
    assert response.json()["error"]["code"] == "DIFF_ENDPOINT_NOT_FOUND"
    assert provider.read_calls == []


def test_real_svn_api_reuses_resolver_manifests_in_diff(monkeypatch):
    fixture = _atlas_fixture()
    service = WorkbookDiffService(
        DatasetLayout.from_config(CONFIG["dataset_layout"])
    )
    app, client, _ = _create_client(fixture, service=service)
    resolver = app.state.workbook_dataset_resolver
    resolver_calls = 0
    diff_calls = 0
    original_resolver_manifest = resolver._manifest
    original_diff_manifest = service._manifest

    def count_resolver_manifest(raw):
        nonlocal resolver_calls
        resolver_calls += 1
        return original_resolver_manifest(raw)

    def count_diff_manifest(raw):
        nonlocal diff_calls
        diff_calls += 1
        return original_diff_manifest(raw)

    monkeypatch.setattr(resolver, "_manifest", count_resolver_manifest)
    monkeypatch.setattr(service, "_manifest", count_diff_manifest)

    response = client.post(
        "/api/diff/workbooks/compare",
        json=_request_payload(),
    )

    assert response.status_code == 200
    result = response.json()
    assert result["schema_version"] == "m2.diff.v1"
    assert result["direction"] == {"source": "left", "target": "right"}
    assert result["summary"]["modified_rows"] == 273
    assert resolver_calls == 2
    assert diff_calls == 0


def test_diff_materialization_reuses_persisted_snapshot_workbook_bytes(tmp_path):
    class PersistentProvider(CountingProvider):
        def info(self, endpoint):
            self.info_calls.append((endpoint.url, endpoint.revision))
            return MockSVNProvider.info(self, endpoint)

    provider = PersistentProvider(_atlas_fixture())
    config = {
        "dataset_layout": deepcopy(CONFIG["dataset_layout"]),
        "svn": {
            "provider": "mock",
            "allowed_schemes": ["mock"],
            "endpoint_registry": _endpoint_records(),
        },
        "snapshot_reuse": {
            "content_read_workers": 7,
            "bulk_export_enabled": False,
            "bulk_export_min_files": 9,
            "persistent_cache": {
                "enabled": True,
                "directory": str(tmp_path / ".cache" / "snapshot"),
                "max_bytes": 64 * 1024 * 1024,
                "max_file_entries": 128,
                "max_tree_entries": 8,
            }
        },
        "batch_diff": {
            "state_directory": str(tmp_path / "batch-state"),
        },
    }
    app = create_app(
        config=config,
        provider=provider,
        workbook_diff_service=RecordingDiffService(),
    )
    assert app.state.snapshot_service.content_read_workers == 7
    assert app.state.snapshot_service.bulk_export_enabled is False
    assert app.state.snapshot_service.bulk_export_min_files == 9
    app.state.snapshot_service.create_snapshot_at_revisions(
        app.state.endpoint_registry,
        source_id=SOURCE_ENDPOINT_ID,
        source_revision=SOURCE_REVISION,
        target_id=TARGET_ENDPOINT_ID,
        target_revision=TARGET_REVISION,
    )
    workbook_reads = sum(
        path.casefold().endswith((".xlsx", ".xlsm", ".xls"))
        for _, path in provider.read_calls
    )

    with TestClient(app) as client:
        response = client.post(
            "/api/diff/workbooks/compare",
            json=_request_payload(),
        )

    assert response.status_code == 200
    assert response.json()["schema_version"] == "m2.diff.v1"
    assert sum(
        path.casefold().endswith((".xlsx", ".xlsm", ".xls"))
        for _, path in provider.read_calls
    ) == workbook_reads
    assert app.state.snapshot_service.snapshot_reuse_metrics()[
        "disk_byte_hits"
    ] >= 2


@pytest.mark.parametrize("missing_csv", [False, True])
def test_frozen_dataset_item_stage_uses_no_svn_content_or_tree_calls(
    tmp_path, missing_csv
):
    fixture = _atlas_fixture()
    if missing_csv:
        missing_path = next(
            path for path in fixture["content"] if path.startswith("left/TaBlEcSv/")
        )
        fixture["content"].pop(missing_path)
        fixture["tree"] = [
            item for item in fixture["tree"] if item["path"] != missing_path
        ]
    class PersistentProvider(CountingProvider):
        def info(self, endpoint):
            self.info_calls.append((endpoint.url, endpoint.revision))
            return MockSVNProvider.info(self, endpoint)

    provider = PersistentProvider(fixture)
    config = {
        "dataset_layout": deepcopy(CONFIG["dataset_layout"]),
        "svn": {
            "provider": "mock",
            "allowed_schemes": ["mock"],
            "endpoint_registry": _endpoint_records(),
        },
        "snapshot_reuse": {
            "frozen_dataset_enabled": True,
            "cross_branch_csv_reuse_enabled": False,
            "bulk_export_enabled": False,
            "persistent_cache": {
                "enabled": True,
                "directory": str(tmp_path / "snapshot-cache"),
            },
        },
        "batch_diff": {
            "state_directory": str(tmp_path / "batch-state"),
        },
        "diff_plan": {
            "database_path": str(tmp_path / "m4" / "diff-plan.sqlite3"),
        },
    }
    app = create_app(config=config, provider=provider)
    resolver = app.state.batch_diff_service.candidate_resolver
    candidates = resolver.prepare(
        BatchEndpointPayload(
            endpoint_id=SOURCE_ENDPOINT_ID,
            revision=SOURCE_REVISION,
        ),
        BatchEndpointPayload(
            endpoint_id=TARGET_ENDPOINT_ID,
            revision=TARGET_REVISION,
        ),
    )
    assert [(item.path, item.status) for item in candidates] == [
        (WORKBOOK_NAME, "modified")
    ]
    phase_events = []
    app.state.workbook_dataset_resolver.prepare_frozen_pair(
        BatchEndpointPayload(
            endpoint_id=SOURCE_ENDPOINT_ID,
            revision=SOURCE_REVISION,
        ),
        BatchEndpointPayload(
            endpoint_id=TARGET_ENDPOINT_ID,
            revision=TARGET_REVISION,
        ),
        candidates,
        phase_sink=lambda phase, wall_ns, metrics: phase_events.append(
            (phase, wall_ns, metrics)
        ),
    )
    phase_names = {phase for phase, _, _ in phase_events}
    assert {
        "parse_manifests",
        "enumerate_dataset",
        "reuse_evidence",
        "fetch_dataset",
        "publish_dataset",
    } <= phase_names
    assert all(wall_ns >= 0 for _, wall_ns, _ in phase_events)

    provider.read_calls.clear()
    provider.list_tree_calls.clear()
    provider.list_children_calls.clear()
    with TestClient(app) as client:
        response = client.post(
            "/api/diff/workbooks/compare",
            json=_request_payload(),
        )
