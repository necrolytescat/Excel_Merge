from __future__ import annotations

from copy import deepcopy
import json
from pathlib import Path

import pytest

from app.services.workbook_dataset_service import (
    SVNWorkbookDatasetResolver,
    WorkbookCompareError,
)
from core.models import EndpointSpec, TreeEntry
from core.svn_provider import SVNProviderError


PROJECT_ROOT = Path(__file__).resolve().parents[2]
CONFIG = json.loads(
    (PROJECT_ROOT / "config" / "settings.json").read_text(encoding="utf-8")
)


class DirectoryProvider:
    def __init__(self):
        self.list_tree_calls = 0
        self.list_children_calls = 0
        self.fail_tree_once = False
        self.table_exists = True
        self.csv_exists = True

    def list_tree(self, endpoint, prefix=""):
        self.list_tree_calls += 1
        if self.fail_tree_once:
            self.fail_tree_once = False
            raise SVNProviderError("SVN_TIMEOUT", "timeout")
        if not self.table_exists:
            return []
        return [TreeEntry(path="branch/Table", kind="dir")]

    def list_children(self, endpoint, prefix=""):
        self.list_children_calls += 1
        if not self.csv_exists:
            return []
        return [TreeEntry(path="branch/TableCsv", kind="dir")]


def _record():
    return {
        "id": "LEFT",
        "url": "mock://repository/branch",
        "enabled": True,
        "physical_path_filters": {"TABLE": "branch/Table"},
    }


def _resolver(provider, records):
    return SVNWorkbookDatasetResolver(
        provider,
        lambda: records,
        CONFIG["dataset_layout"],
        allowed_schemes=("mock",),
    )


def test_resolver_caches_table_and_csv_facts_for_same_frozen_identity():
    provider = DirectoryProvider()
    record = _record()
    resolver = _resolver(provider, [record])
    endpoint = EndpointSpec(url=record["url"], revision=101)

    assert resolver._cached_table_directory(record, endpoint) == "branch/Table"
    assert resolver._cached_table_directory(record, endpoint) == "branch/Table"
    assert resolver._cached_csv_directory(record, endpoint, "branch/Table") == "branch/TableCsv"
    assert resolver._cached_csv_directory(record, endpoint, "branch/Table") == "branch/TableCsv"
    assert provider.list_tree_calls == 1
    assert provider.list_children_calls == 1


def test_resolver_cache_key_separates_revision_url_and_physical_configuration():
    provider = DirectoryProvider()
    record = _record()
    resolver = _resolver(provider, [record])

    endpoint = EndpointSpec(url="mock://repository/branch/", revision=101)
    resolver._cached_table_directory(record, endpoint)
    resolver._cached_table_directory(
        record,
        EndpointSpec(url="mock://repository/branch", revision=101),
    )
    resolver._cached_table_directory(
        record,
        EndpointSpec(url="mock://repository/branch", revision=102),
    )
    changed_record = deepcopy(record)
    changed_record["physical_path_filters"]["TABLE"] = "other/Table"
    resolver._cached_table_directory(
        changed_record,
        EndpointSpec(url="mock://repository/branch", revision=102),
    )
    resolver._cached_table_directory(
        changed_record,
        EndpointSpec(url="mock://other/branch", revision=102),
    )

    assert provider.list_tree_calls == 4


def test_resolver_caches_successful_missing_directory_facts():
    provider = DirectoryProvider()
    provider.table_exists = False
    record = _record()
    resolver = _resolver(provider, [record])
    endpoint = EndpointSpec(url=record["url"], revision=101)

    for _ in range(2):
        with pytest.raises(WorkbookCompareError) as caught:
            resolver._cached_table_directory(record, endpoint)
        assert caught.value.code == "DIFF_WORKBOOK_NOT_FOUND"
    assert provider.list_tree_calls == 1

    provider.csv_exists = False
    assert resolver._cached_csv_directory(record, endpoint, "branch/Table") is None
    assert resolver._cached_csv_directory(record, endpoint, "branch/Table") is None
    assert provider.list_children_calls == 1


def test_resolver_does_not_cache_provider_failure():
    provider = DirectoryProvider()
    provider.fail_tree_once = True
    record = _record()
    resolver = _resolver(provider, [record])
    endpoint = EndpointSpec(url=record["url"], revision=101)

    with pytest.raises(WorkbookCompareError) as caught:
        resolver._cached_table_directory(record, endpoint)
    assert caught.value.code == "DIFF_DATASET_READ_FAILED"

    assert resolver._cached_table_directory(record, endpoint) == "branch/Table"
    assert provider.list_tree_calls == 2
