from __future__ import annotations

from datetime import datetime, timezone
import json

from app.tools import monitor_performance_diagnostic as diagnostic
from core.models import TreeEntry
from core.svn_history import BranchCopyBoundary, BranchIdentity
from core.svn_provider import CLISVNProvider, SVNProviderError

from tests.unit.test_monitor_incremental_service import changed, commit, files, layout


class _CacheMetrics:
    def cache_metrics(self):
        return {
            "memory_hits": 0,
            "disk_hits": 0,
            "misses": 0,
            "writes": 0,
            "memory_entries": 0,
        }


class FakeCLIProvider(CLISVNProvider):
    def __init__(self, revisions, start, end):
        self.files = revisions
        self.start = start
        self.end = end
        self.client = _CacheMetrics()
        self.reads = []
        self.identity = BranchIdentity(
            canonical_url="https://private.example/branches/fixed",
            repository_root="https://private.example",
            repository_uuid="20000000-0000-4000-8000-000000000001",
            repository_relative_path="branches/fixed",
            bound_revision=max(revisions),
        )
        self.commits = [
            commit(
                max(revisions),
                "author",
                changed("Source/TableCsv/role.csv"),
            )
        ]

    def resolve_branch_identity(self, endpoint):
        return self.identity

    def resolve_copy_boundary(self, identity):
        return BranchCopyBoundary(revision=50)

    def resolve_revision_at(self, identity, instant):
        return min(self.files) if instant == self.start else max(self.files)

    def list_branch_commits(self, identity, start, end):
        return self.commits

    def list_paths_at_revision(self, identity, revision):
        return [TreeEntry(path=path, kind="file") for path in self.files[revision]]

    def read_path_bytes_at_revision(self, identity, path, revision):
        self.reads.append((path, revision))
        try:
            return self.files[revision][path]
        except KeyError as error:
            raise SVNProviderError("SVN_PATH_NOT_FOUND", "missing") from error


def _config():
    dataset = layout()
    return {
        "dataset_layout": {
            "workbook_source": {"directory_name": "Table"},
            "csv_export": {
                "directory_name": "TableCsv",
                "extension": dataset.csv_extension,
                "filename_template": dataset.filename_template,
                "field_name_row": dataset.field_name_row,
                "field_type_row": dataset.field_type_row,
                "field_scope_row": dataset.field_scope_row,
                "data_start_row": dataset.data_start_row,
                "primary_key_fields": list(dataset.primary_key_fields),
            },
            "manifest": {
                "sheet_name": dataset.manifest_sheet_name,
                "sheet_field": dataset.manifest_sheet_field,
                "csv_name_field": dataset.manifest_csv_name_field,
                "export_flag_field": dataset.manifest_export_flag_field,
            },
        },
        "svn": {
            "provider": "cli",
            "endpoint_registry": [
                {
                    "id": "FIXED",
                    "label": "Fixed branch",
                    "url": "https://private.example/branches/fixed",
                    "enabled": True,
                }
            ],
        },
    }


def _expected():
    return {
        "start_revision": 100,
        "end_revision": 101,
        "workbook_count": 2,
        "reliable_workbook_count": 2,
        "change_count": 1,
        "error_count": 0,
        "unknown_author_count": 0,
        "unresolved_count": 0,
    }


def test_shadow_diagnostic_is_non_publishing_and_redacted(monkeypatch):
    start = datetime(2026, 8, 10, 1, tzinfo=timezone.utc)
    end = datetime(2026, 8, 10, 2, tzinfo=timezone.utc)
    provider = FakeCLIProvider({100: files("100"), 101: files("120")}, start, end)
    monkeypatch.setattr(diagnostic, "provider_from_config", lambda config: provider)

    output = diagnostic.run_diagnostic(
        config=_config(),
        endpoint_id="FIXED",
        start_at=start,
        end_at=end,
        mode="shadow",
        expected=_expected(),
        expected_copy_boundary=50,
        cache_dir="isolated-cache",
    )

    assert output["result"] == _expected()
    assert output["semantic_fingerprint"] == output["legacy_fingerprint"]
    assert output["writes"] == {
        "monitor_store": False,
        "reports": False,
        "latest": False,
        "windows_scheduler": False,
    }
    public = json.dumps(output, ensure_ascii=False)
    assert "private.example" not in public
    assert "Source/Table" not in public
    assert "isolated-cache" not in public


def test_revision_gate_stops_before_content_reads(monkeypatch):
    start = datetime(2026, 8, 10, 1, tzinfo=timezone.utc)
    end = datetime(2026, 8, 10, 2, tzinfo=timezone.utc)
    provider = FakeCLIProvider({100: files("100"), 101: files("120")}, start, end)
    monkeypatch.setattr(diagnostic, "provider_from_config", lambda config: provider)
    expected = _expected()
    expected["end_revision"] = 999

    try:
        diagnostic.run_diagnostic(
            config=_config(),
            endpoint_id="FIXED",
            start_at=start,
            end_at=end,
            mode="shadow",
            expected=expected,
            expected_copy_boundary=50,
        )
    except diagnostic.DiagnosticGateError as error:
        assert str(error) == "end_revision_mismatch"
    else:
        raise AssertionError("revision mismatch must stop the diagnostic")
    assert provider.reads == []
