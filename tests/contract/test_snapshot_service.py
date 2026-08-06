from __future__ import annotations

from core.svn_provider import MockSVNProvider, SVNProviderError
from app.services.snapshot_service import SnapshotService


class CountingProvider(MockSVNProvider):
    def __init__(self, fixture):
        super().__init__(fixture)
        self.info_calls = []
        self.read_calls = []

    def info(self, endpoint):
        self.info_calls.append((endpoint.url, endpoint.revision))
        return super().info(endpoint)

    def read_bytes(self, endpoint, path):
        self.read_calls.append((endpoint.url, path, endpoint.revision))
        if path.casefold().endswith("bad.xls"):
            raise SVNProviderError("SVN_PATH_NOT_FOUND", "模拟单文件失败")
        return super().read_bytes(endpoint, path)


def fixture():
    return {
        "info": {
            "repository_root": "https://mock.local/repo",
            "repository_uuid": "fixture-repository-uuid",
            "revision": "210",
            "last_changed_revision": "210",
            "last_changed_author": "tester",
            "last_changed_date": "2026-08-04T09:00:00Z",
        },
        "tree": [
            {"path": "Resource/table/Arena.xlsx", "kind": "file", "size": 4, "revision": "210", "author": "alice", "date": "2026-08-04T09:00:00Z"},
            {"path": "Resource/table/nested/Balance.XLSM", "kind": "file", "size": 4, "revision": "209", "author": "bob", "date": "2026-08-03T09:00:00Z"},
            {"path": "Resource/Table/Bad.xls", "kind": "file", "size": 4, "revision": "208", "author": "carol", "date": "2026-08-02T09:00:00Z"},
            {"path": "Resource/Table/Ignore.csv", "kind": "file", "size": 4, "revision": "207", "author": "skip", "date": "2026-08-02T08:00:00Z"},
            {"path": "Resource/CONFIG/Ignore.xlsx", "kind": "file", "size": 4, "revision": "206", "author": "skip", "date": "2026-08-02T07:00:00Z"},
            {"path": "Resource/TABLECSV/Ignore.xlsx", "kind": "file", "size": 4, "revision": "205", "author": "skip", "date": "2026-08-02T06:00:00Z"},
            {"path": "Resource/Other/Ignore.xlsx", "kind": "file", "size": 4, "revision": "204", "author": "skip", "date": "2026-08-02T05:00:00Z"},
            {"path": "Resource/Table", "kind": "dir", "revision": "210"},
        ],
        "content": {
            "Resource/table/Arena.xlsx": {"210": b"xlsx"},
            "Resource/table/nested/Balance.XLSM": {"210": b"xlsm"},
            "Resource/Table/Bad.xls": {"210": b"xls"},
        },
    }


def records():
    return [
        {
            "id": "KR_FIX_1_1_0",
            "region": "KR",
            "track": "FIX",
            "label": "FIX1.1.0",
            "url": "https://mock.local/repo/Resource",
            "logical_scopes": ["TABLE"],
            "physical_path_filters": {"TABLE": "resource/TABLE"},
            "enabled": True,
        },
        {
            "id": "KR_FIX_1_0_0",
            "region": "KR",
            "track": "FIX",
            "label": "fix1.0.0",
            "url": "https://mock.local/repo/Resource",
            "logical_scopes": ["TABLE"],
            "physical_path_filters": {},
            "enabled": True,
        },
    ]


def service(provider):
    return SnapshotService(provider, allowed_schemes=("http", "https", "svn", "svn+ssh", "file"))


def test_snapshot_freezes_each_head_once_and_reads_only_table_excel():
    provider = CountingProvider(fixture())
    result = service(provider).create_snapshot(records(), source_id="KR_FIX_1_1_0", target_id="KR_FIX_1_0_0")

    assert result.logical_scopes == ["TABLE"]
    assert result.source.resolved_revision == 210
    assert result.target.resolved_revision == 210
    assert len(provider.info_calls) == 2
    assert all(revision == "HEAD" for _, revision in provider.info_calls)
    assert [item.path for item in result.source.files] == [
        "Resource/table/Arena.xlsx",
        "Resource/Table/Bad.xls",
        "Resource/table/nested/Balance.XLSM",
    ]
    assert all(item.logical_scope == "TABLE" for item in result.source.files)
    assert not any("CONFIG" in item.path.upper() or "TABLECSV" in item.path.upper() for item in result.source.files)
    assert not any(item.path.casefold().endswith(".csv") for item in result.source.files)
    assert result.source.stats.file_count == 3
    assert result.source.stats.failed_count == 1
    failed = next(item for item in result.source.files if item.path.casefold().endswith("bad.xls"))
    assert failed.error.code == "SVN_PATH_NOT_FOUND"
    assert all(revision == 210 for _, _, revision in provider.read_calls)


def test_endpoint_registry_accepts_multiple_concrete_fix_records_and_rejects_disabled():
    provider = CountingProvider(fixture())
    snapshot = service(provider)
    normalized = snapshot.normalize_registry(records())
    assert [item["id"] for item in normalized] == ["KR_FIX_1_1_0", "KR_FIX_1_0_0"]
    disabled = [dict(item, enabled=False) if item["id"] == "KR_FIX_1_0_0" else item for item in normalized]
    try:
        snapshot.create_snapshot(disabled, source_id="KR_FIX_1_1_0", target_id="KR_FIX_1_0_0")
    except SVNProviderError as exc:
        assert exc.code == "SVN_ENDPOINT_DISABLED"
    else:
        raise AssertionError("disabled endpoint should fail")