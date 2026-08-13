from __future__ import annotations

import json
from pathlib import Path

from app.services.workbook_dataset_service import SVNWorkbookDatasetResolver
from core.models import EndpointSpec, TreeEntry
from core.svn_provider import SVNProviderError


PROJECT_ROOT = Path(__file__).resolve().parents[2]
CONFIG = json.loads(
    (PROJECT_ROOT / "config" / "settings.json").read_text(encoding="utf-8")
)


class MissingOnceProvider:
    def __init__(self):
        self.calls = 0

    def list_children(self, endpoint, prefix=""):
        self.calls += 1
        if self.calls == 1:
            raise SVNProviderError("SVN_PATH_NOT_FOUND", "missing parent")
        return [TreeEntry(path="branch/TableCsv", kind="dir")]


def test_csv_missing_path_provider_exception_is_not_cached():
    record = {
        "id": "LEFT",
        "url": "mock://repository/branch",
        "physical_path_filters": {"TABLE": "branch/Table"},
    }
    provider = MissingOnceProvider()
    resolver = SVNWorkbookDatasetResolver(
        provider,
        lambda: [record],
        CONFIG["dataset_layout"],
        allowed_schemes=("mock",),
    )
    endpoint = EndpointSpec(url=record["url"], revision=101)

    assert resolver._cached_csv_directory(record, endpoint, "branch/Table") is None
    assert (
        resolver._cached_csv_directory(record, endpoint, "branch/Table")
        == "branch/TableCsv"
    )
    assert provider.calls == 2
