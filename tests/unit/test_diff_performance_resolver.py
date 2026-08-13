from __future__ import annotations

from copy import deepcopy
import json
from pathlib import Path

from app.schemas.workbook_compare import WorkbookCompareRequestPayload
from app.services.diff_performance import DiffPerformanceRecorder
from app.services.diff_performance_adapters import TimedSVNWorkbookDatasetResolver
from core.svn_provider import MockSVNProvider


PROJECT_ROOT = Path(__file__).resolve().parents[2]
SOURCE_DIR = PROJECT_ROOT / "tests" / "excel" / "left"
TARGET_DIR = PROJECT_ROOT / "tests" / "excel" / "right"
WORKBOOK = "AtlasConfig.xlsm"
CONFIG = json.loads(
    (PROJECT_ROOT / "config" / "settings.json").read_text(encoding="utf-8")
)


def _fixture() -> dict:
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
            raw = path.read_bytes()
            tree.append(
                {
                    "path": svn_path,
                    "kind": "file",
                    "size": len(raw),
                    "revision": str(revision),
                }
            )
            content[svn_path] = {str(revision): raw}
    return {
        "info": {
            "repository_root": "mock://repository",
            "repository_uuid": "performance-fixture",
            "revision": "202",
        },
        "tree": tree,
        "children": children,
        "content": content,
    }


def test_timed_resolver_records_svn_and_materialization_and_cleans_up():
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
    recorder = DiffPerformanceRecorder(enabled=True)
    resolver = TimedSVNWorkbookDatasetResolver(
        MockSVNProvider(deepcopy(_fixture())),
        lambda: records,
        CONFIG["dataset_layout"],
        allowed_schemes=("mock",),
        performance=recorder,
    )
    payload = WorkbookCompareRequestPayload.model_validate(
        {
            "schema_version": "m2.workbook-compare.request.v1",
            "request_id": "a7e47a49-3308-4d10-936c-bbb80e4547b3",
            "source": {"endpoint_id": "LEFT", "revision": 101},
            "target": {"endpoint_id": "RIGHT", "revision": 202},
            "workbook_path": WORKBOOK,
        }
    )

    dataset = resolver.resolve(payload)
    root = dataset.source_directory.parent
    assert root.is_dir()
    dataset.close()
    assert not root.exists()

    snapshot = recorder.snapshot()
    assert snapshot["counters"]["svn.list_tree_calls"] == 2
    assert snapshot["counters"]["svn.list_children_calls"] == 2
    assert snapshot["counters"]["svn.workbook_read_calls"] == 2
    assert snapshot["counters"]["svn.csv_read_calls"] == 32
    assert snapshot["counters"]["resolver.materialize_bytes"] > 0
    assert snapshot["phases"]["resolver.manifest_parse"]["calls"] == 2
    assert snapshot["phases"]["resolver.csv_fetch"]["calls"] == 2
    assert snapshot["phases"]["resolver.materialize"]["calls"] == 2
    assert snapshot["phases"]["resolver.total"]["calls"] == 1
