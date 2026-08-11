from __future__ import annotations

from types import SimpleNamespace

from app.services.monitor_endpoint_catalog import (
    MonitorEndpointCatalog,
    project_root_url,
)
from core.svn_provider import SVNProviderError


CATALOG = {
    "KR": {
        "display_name": "韩国 KR",
        "trunk_branch": "Trunk_KR",
        "fix_pattern": "KR-fix-x.x.x.x",
    },
    "TC": {
        "display_name": "港台 TC",
        "trunk_branch": "Trunk_Tc",
        "fix_pattern": "TC-fix-x.x.x.x",
    },
}


def _record(endpoint_id, label, url, *, enabled=True):
    return {
        "id": endpoint_id,
        "region": endpoint_id[:2],
        "track": "FIX",
        "label": label,
        "url": url,
        "logical_scopes": ["TABLE"],
        "physical_path_filters": {},
        "enabled": enabled,
    }


class DirectoryService:
    def __init__(self):
        self.calls = []

    def children(self, payload, prefix=""):
        self.calls.append((payload.url, payload.revision, prefix))
        paths = (
            ["Trunk_KR", "Trunk_Tc", "branches"]
            if not prefix
            else [
                "branches/KR-Fix-1.0.0.0",
                "branches/KR-Fix-1.0.1.0",
                "branches/TC-Fix-5.0.0.0",
                "branches/feature-not-configured",
            ]
        )
        return [SimpleNamespace(path=path, kind="dir") for path in paths]


def test_monitor_catalog_merges_all_matches_and_caches_directory_reads():
    service = DirectoryService()
    configured = [
        _record(
            "KR_FIX_KR-Fix-1.0.0.0",
            "韩国 KR · KR-Fix-1.0.0.0",
            "https://mock.local/repo/branches/KR-Fix-1.0.0.0",
        ),
        _record(
            "TC_FIX_TC-Fix-5.0.0.0",
            "港台 TC · TC-Fix-5.0.0.0",
            "https://mock.local/repo/branches/TC-Fix-5.0.0.0",
            enabled=False,
        ),
    ]
    catalog = MonitorEndpointCatalog(
        service,
        server_url=lambda: "https://mock.local/repo",
        endpoint_catalog=lambda: CATALOG,
        endpoint_registry=lambda: configured,
    )

    first = catalog.records()
    second = catalog.records()

    assert {record["id"] for record in first} == {
        "KR_FIX_KR-Fix-1.0.0.0",
        "KR_FIX_KR-Fix-1.0.1.0",
        "KR_DEV_Trunk_KR",
        "TC_FIX_TC-Fix-5.0.0.0",
        "TC_DEV_Trunk_Tc",
    }
    assert second == first
    assert [call[2] for call in service.calls] == ["", "branches"]
    assert not next(
        record
        for record in first
        if record["id"] == "TC_FIX_TC-Fix-5.0.0.0"
    )["enabled"]


def test_monitor_catalog_falls_back_to_registered_endpoints_on_svn_error():
    configured = [
        _record(
            "KR_FIX_KR-Fix-1.0.0.0",
            "韩国 KR · KR-Fix-1.0.0.0",
            "https://mock.local/repo/branches/KR-Fix-1.0.0.0",
        )
    ]

    class FailedService:
        def children(self, payload, prefix=""):
            raise SVNProviderError("SVN_TIMEOUT", "timeout")

    catalog = MonitorEndpointCatalog(
        FailedService(),
        server_url=lambda: "https://mock.local/repo",
        endpoint_catalog=lambda: CATALOG,
        endpoint_registry=lambda: configured,
    )

    assert catalog.records() == configured


def test_monitor_catalog_keeps_stale_discovery_when_refresh_fails():
    service = DirectoryService()
    catalog = MonitorEndpointCatalog(
        service,
        server_url=lambda: "https://mock.local/repo",
        endpoint_catalog=lambda: CATALOG,
        endpoint_registry=lambda: [],
        cache_seconds=0,
    )
    expected = catalog.records()

    def fail(payload, prefix=""):
        raise SVNProviderError("SVN_TIMEOUT", "timeout")

    service.children = fail
    assert catalog.records() == expected


def test_project_root_url_accepts_root_or_trunk_url():
    assert project_root_url("https://mock.local/repo") == "https://mock.local/repo"
    assert (
        project_root_url("https://mock.local/repo/Trunk_KR/")
        == "https://mock.local/repo"
    )
