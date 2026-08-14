from __future__ import annotations

import threading
from pathlib import Path

import pytest

from app.services.snapshot_content_cache import PersistentSnapshotContentCache
from app.services.snapshot_service import SnapshotService
from core.models import EndpointSpec, SvnInfo, TreeEntry
from core.svn_provider import (
    CLISVNProvider,
    ExportedFileSet,
    SVNProviderError,
)


class BulkExportProvider:
    def __init__(
        self,
        *,
        file_count: int = 4,
        export_failure: bool = False,
        omit_one: bool = False,
    ) -> None:
        self.file_count = file_count
        self.export_failure = export_failure
        self.omit_one = omit_one
        self.export_calls: list[tuple[str, int, str, tuple[str, ...]]] = []
        self.cat_calls: list[tuple[str, str]] = []
        self._lock = threading.Lock()

    @staticmethod
    def _raw(endpoint: EndpointSpec, path: str) -> bytes:
        return f"{endpoint.url}|{endpoint.revision}|{path}".encode("utf-8")

    def info(self, endpoint: EndpointSpec) -> SvnInfo:
        return SvnInfo(
            url=endpoint.url,
            repository_root="mock://repository",
            repository_uuid="repository-uuid",
            revision=str(endpoint.revision),
        )

    def list_tree(self, endpoint: EndpointSpec, prefix: str = "") -> list[TreeEntry]:
        paths = [f"Source/Table/Book{index}.xlsx" for index in range(self.file_count)]
        return [TreeEntry(path="Source/Table", kind="dir")] + [
            TreeEntry(
                path=path,
                kind="file",
                size=len(self._raw(endpoint, path)),
                revision=str(endpoint.revision),
            )
            for path in paths
        ]

    def export_files(
        self,
        endpoint: EndpointSpec,
        prefix: str,
        paths: list[str],
    ) -> ExportedFileSet:
        with self._lock:
            self.export_calls.append(
                (endpoint.url, int(endpoint.revision), prefix, tuple(paths))
            )
        if self.export_failure:
            raise SVNProviderError("SVN_EXPORT_FAILED", "export failed")
        selected = list(paths)
        if self.omit_one and selected:
            selected = selected[1:]
        files = {path: self._raw(endpoint, path) for path in selected}
        return ExportedFileSet(
            files=files,
            exported_file_count=self.file_count,
            exported_bytes=sum(len(raw) for raw in files.values()),
        )

    def read_bytes_with_source(
        self,
        endpoint: EndpointSpec,
        path: str,
    ) -> tuple[bytes, str]:
        with self._lock:
            self.cat_calls.append((endpoint.url, path))
        return self._raw(endpoint, path), "svn_cat"


def _records() -> list[dict]:
    return [
        {
            "id": "SOURCE",
            "region": "KR",
            "track": "FIX",
            "label": "Source",
            "url": "mock://source",
            "logical_scopes": ["TABLE"],
            "physical_path_filters": {"TABLE": "Source/Table"},
            "enabled": True,
        },
        {
            "id": "TARGET",
            "region": "KR",
            "track": "FIX",
            "label": "Target",
            "url": "mock://target",
            "logical_scopes": ["TABLE"],
            "physical_path_filters": {"TABLE": "Source/Table"},
            "enabled": True,
        },
    ]


def _service(
    provider: BulkExportProvider,
    cache_root: Path,
    *,
    min_files: int = 3,
    sink=None,
) -> SnapshotService:
    return SnapshotService(
        provider,
        allowed_schemes=("mock",),
        max_workers=2,
        content_read_workers=4,
        bulk_export_enabled=True,
        bulk_export_min_files=min_files,
        reuse_ttl_seconds=0,
        persistent_content_cache=PersistentSnapshotContentCache(cache_root),
        phase_timing_enabled=True,
        phase_timing_sink=sink,
    )


def _run(service: SnapshotService):
    return service.create_snapshot_at_revisions(
        _records(),
        source_id="SOURCE",
        source_revision=101,
        target_id="TARGET",
        target_revision=202,
        reuse=False,
    )


def test_cold_snapshot_uses_one_export_per_side_and_attributes_metrics(tmp_path):
    provider = BulkExportProvider()
    metrics = []

    snapshot = _run(_service(provider, tmp_path / "cache", sink=metrics.append))

    assert len(provider.export_calls) == 2
    assert provider.cat_calls == []
    assert snapshot.source.stats.file_count == 4
    assert snapshot.target.stats.file_count == 4
    assert snapshot.source.stats.failed_count == 0
    assert snapshot.target.stats.failed_count == 0
    assert metrics[-1]["summary"]["provider_export"]["calls"] == 2
    assert metrics[-1]["summary"]["provider_read"]["calls"] == 8
    assert metrics[-1]["summary"]["provider_read"]["sources"] == {"svn_export": 8}
    assert metrics[-1]["counters"]["bulk_export.calls"] == 2


def test_restart_with_complete_persistent_cache_skips_export_and_cat(tmp_path):
    cache_root = tmp_path / "cache"
    first_provider = BulkExportProvider()
    first = _run(_service(first_provider, cache_root))
    second_provider = BulkExportProvider()

    second = _run(_service(second_provider, cache_root))

    assert second_provider.export_calls == []
    assert second_provider.cat_calls == []
    assert [item.content_hash for item in second.source.files] == [
        item.content_hash for item in first.source.files
    ]
    assert [item.content_hash for item in second.target.files] == [
        item.content_hash for item in first.target.files
    ]


def test_export_failure_falls_back_to_cat(tmp_path):
    provider = BulkExportProvider(export_failure=True)
    metrics = []

    snapshot = _run(_service(provider, tmp_path / "cache", sink=metrics.append))

    assert len(provider.export_calls) == 2
    assert len(provider.cat_calls) == 8
    assert snapshot.source.stats.failed_count == 0
    assert snapshot.target.stats.failed_count == 0
    assert metrics[-1]["summary"]["provider_export"]["failures"] == 2
    assert metrics[-1]["counters"]["bulk_export.fallbacks"] == 2
    assert metrics[-1]["summary"]["provider_read"]["sources"] == {"svn_cat": 8}


def test_missing_export_file_only_falls_back_for_that_file(tmp_path):
    provider = BulkExportProvider(omit_one=True)
    metrics = []

    snapshot = _run(_service(provider, tmp_path / "cache", sink=metrics.append))

    assert len(provider.export_calls) == 2
    assert len(provider.cat_calls) == 2
    assert snapshot.source.stats.failed_count == 0
    assert snapshot.target.stats.failed_count == 0
    assert metrics[-1]["counters"]["bulk_export.missing_files"] == 2
    assert metrics[-1]["summary"]["provider_read"]["sources"] == {
        "svn_cat": 2,
        "svn_export": 6,
    }


def test_below_threshold_uses_cat_without_export(tmp_path):
    provider = BulkExportProvider(file_count=2)

    snapshot = _run(_service(provider, tmp_path / "cache", min_files=3))

    assert provider.export_calls == []
    assert len(provider.cat_calls) == 4
    assert snapshot.source.stats.failed_count == 0
    assert snapshot.target.stats.failed_count == 0


def test_cli_export_is_frozen_selective_and_cleans_temporary_directory(
    monkeypatch,
):
    provider = CLISVNProvider(cache_dir=None)
    monkeypatch.setattr(provider.client, "available", lambda: True)
    captured: dict[str, object] = {}

    def run(*args, timeout):
        destination = Path(args[-1])
        captured["args"] = args
        captured["destination"] = destination
        destination.mkdir(parents=True)
        (destination / "Book.xlsx").write_bytes(b"book")
        (destination / "Ignored.txt").write_bytes(b"ignored")
        return 0, "", ""

    monkeypatch.setattr("core.svn_provider.svn_client._run", run)
    result = provider.export_files(
        EndpointSpec(url="https://svn.example/branch", revision=123),
        "Source/Table",
        ["Source/Table/Book.xlsx"],
    )

    assert result.files == {"Source/Table/Book.xlsx": b"book"}
    assert result.exported_file_count == 2
    assert result.exported_bytes == 11
    assert captured["args"][:-1] == (
        "export",
        "--non-interactive",
        "--force",
        "--ignore-externals",
        "--quiet",
        "-r",
        "123",
        "https://svn.example/branch/Source/Table@123",
    )
    assert not Path(captured["destination"]).exists()


@pytest.mark.parametrize(
    ("revision", "prefix", "paths", "code"),
    [
        ("HEAD", "Source/Table", ["Source/Table/Book.xlsx"], "SVN_INVALID_REVISION"),
        (123, "", ["Source/Table/Book.xlsx"], "SVN_INVALID_PATH"),
        (123, "Source/Table", ["Outside/Book.xlsx"], "SVN_INVALID_PATH"),
    ],
)
def test_cli_export_rejects_unfrozen_or_unsafe_requests(
    monkeypatch,
    revision,
    prefix,
    paths,
    code,
):
    provider = CLISVNProvider(cache_dir=None)
    monkeypatch.setattr(provider.client, "available", lambda: True)

    with pytest.raises(SVNProviderError) as caught:
        provider.export_files(
            EndpointSpec(url="https://svn.example/branch", revision=revision),
            prefix,
            paths,
        )

    assert caught.value.code == code
