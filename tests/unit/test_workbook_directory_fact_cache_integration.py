from __future__ import annotations

from copy import deepcopy
import json
from pathlib import Path
from uuid import uuid4

from app.schemas.workbook_compare import WorkbookCompareRequestPayload
from app.services.workbook_dataset_service import SVNWorkbookDatasetResolver
from core.svn_provider import MockSVNProvider


PROJECT_ROOT = Path(__file__).resolve().parents[2]
SOURCE_DIR = PROJECT_ROOT / "tests" / "excel" / "left"
TARGET_DIR = PROJECT_ROOT / "tests" / "excel" / "right"
CONFIG = json.loads(
    (PROJECT_ROOT / "config" / "settings.json").read_text(encoding="utf-8")
)


class CountingProvider(MockSVNProvider):
    def __init__(self, fixture):
        super().__init__(fixture)
        self.list_tree_calls = 0
        self.list_children_calls = 0

    def list_tree(self, endpoint, prefix=""):
        self.list_tree_calls += 1
        return super().list_tree(endpoint, prefix)

    def list_children(self, endpoint, prefix=""):
        self.list_children_calls += 1
        return super().list_children(endpoint, prefix)


def _fixture():
    tree = []
    children = []
    content = {}
    for directory, table, csv_directory, revision in (
        (SOURCE_DIR, "left/Table", "left/TableCsv", 101),
        (TARGET_DIR, "right/Table", "right/TableCsv", 202),
    ):
        tree.extend(
            [
                {"path": table, "kind": "dir"},
                {"path": csv_directory, "kind": "dir"},
            ]
        )
        children.extend(
            [
                {"path": table, "kind": "dir"},
                {"path": csv_directory, "kind": "dir"},
            ]
        )
        for path in directory.iterdir():
            parent = table if path.suffix.casefold() in {".xls", ".xlsx", ".xlsm"} else csv_directory
            svn_path = f"{parent}/{path.name}"
            tree.append({"path": svn_path, "kind": "file"})
            content[svn_path] = {str(revision): path.read_bytes()}
    return {
        "info": {
            "repository_root": "mock://repository",
            "repository_uuid": "directory-cache-integration",
            "revision": "202",
        },
        "tree": tree,
        "children": children,
        "content": content,
    }


def test_repeated_full_resolve_reuses_directory_facts_and_cleans_datasets():
    records = [
        {
            "id": "LEFT",
            "url": "mock://left",
            "enabled": True,
            "physical_path_filters": {"TABLE": "left/Table"},
        },
        {
            "id": "RIGHT",
            "url": "mock://right",
            "enabled": True,
            "physical_path_filters": {"TABLE": "right/Table"},
        },
    ]
    provider = CountingProvider(deepcopy(_fixture()))
    resolver = SVNWorkbookDatasetResolver(
        provider,
        lambda: records,
        CONFIG["dataset_layout"],
        allowed_schemes=("mock",),
    )

    for _ in range(2):
        payload = WorkbookCompareRequestPayload.model_validate(
            {
                "schema_version": "m2.workbook-compare.request.v1",
                "request_id": str(uuid4()),
                "source": {"endpoint_id": "LEFT", "revision": 101},
                "target": {"endpoint_id": "RIGHT", "revision": 202},
                "workbook_path": "AtlasConfig.xlsm",
            }
        )
        dataset = resolver.resolve(payload)
        root = dataset.source_directory.parent
        assert root.is_dir()
        dataset.close()
        assert not root.exists()

    assert provider.list_tree_calls == 2
    assert provider.list_children_calls == 2
