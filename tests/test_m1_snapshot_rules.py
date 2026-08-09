from __future__ import annotations

from pathlib import Path

from app.services.snapshot_service import SnapshotService
from app.schemas.svn import (
    EndpointRecordPayload,
    SnapshotEndpointPayload,
    SnapshotResponsePayload,
    SnapshotStatsPayload,
)
from core.models import EndpointSpec, TreeEntry


class URLContentProvider:
    def read_bytes(self, endpoint: EndpointSpec, path: str) -> bytes:
        return endpoint.url.encode("utf-8")


def make_record(endpoint_id: str) -> dict:
    return EndpointRecordPayload(
        id=endpoint_id,
        region="KR",
        track="FIX",
        label=endpoint_id,
        url=f"https://svn.example/{endpoint_id}",
        logical_scopes=["TABLE"],
        physical_path_filters={},
        enabled=True,
    ).model_dump()


def make_endpoint(endpoint_id: str, physical_path: str) -> SnapshotEndpointPayload:
    return SnapshotEndpointPayload(
        endpoint_id=endpoint_id,
        label=endpoint_id,
        url=f"https://svn.example/{endpoint_id}",
        resolved_revision=105,
        physical_path_filters={"TABLE": physical_path},
        files=[],
        stats=SnapshotStatsPayload(file_count=0, total_size=0, failed_count=0),
    )


def test_snapshot_cache_isolated_by_endpoint_url():
    service = SnapshotService(URLContentProvider(), allowed_schemes=("https",))
    entry = TreeEntry(path="Table/Data.xlsx")
    left = service._read_binary(EndpointSpec(url="https://svn.example/branches/KR-Fix-1.0.0.0", revision=105), entry, "same-repository")
    right = service._read_binary(EndpointSpec(url="https://svn.example/branches/KR-Fix-1.0.1.0", revision=105), entry, "same-repository")
    assert left != right
    assert len(service._content_cache) == 2


def test_snapshot_scopes_are_bound_back_to_registry():
    service = SnapshotService(URLContentProvider(), allowed_schemes=("https",))
    records = [make_record("LEFT"), make_record("RIGHT")]
    snapshot = SnapshotResponsePayload(
        captured_at="2026-08-05T00:00:00Z",
        logical_scopes=["TABLE"],
        source=make_endpoint("LEFT", "Resource/Table"),
        target=make_endpoint("RIGHT", "resource/table"),
    )
    bound = service.bind_snapshot_scopes(records, snapshot)
    by_id = {record["id"]: record for record in bound}
    assert by_id["LEFT"]["physical_path_filters"] == {"TABLE": "Resource/Table"}
    assert by_id["RIGHT"]["physical_path_filters"] == {"TABLE": "resource/table"}


def test_compare_page_has_file_candidate_and_progress_contract():
    root = Path(__file__).resolve().parents[1]
    compare_js = (root / "app" / "static" / "compare.js").read_text(encoding="utf-8")
    compare_html = (root / "app" / "templates" / "compare.html").read_text(encoding="utf-8")
    assert "buildDifferenceFiles" in compare_js
    assert "content_hash" in compare_js
    assert "DIFF CANDIDATES" in compare_html
    assert "snapshot-progress" in compare_html


def test_m2_workbench_exposes_layout_and_all_page_states_with_real_diff_api():
    root = Path(__file__).resolve().parents[1]
    compare_js = (root / "app" / "static" / "compare.js").read_text(encoding="utf-8")
    compare_html = (root / "app" / "templates" / "compare.html").read_text(encoding="utf-8")
    results_js = (root / "app" / "static" / "compare_results.js").read_text(encoding="utf-8")
    results_html = (root / "app" / "templates" / "compare_results.html").read_text(encoding="utf-8")

    for element_id in (
        "candidate-search",
        "candidate-status-filter",
        "execute-diff",
        "results-page-link",
    ):
        assert f'id="{element_id}"' in compare_html

    for element_id in (
        "compare-current-workbook",
        "result-source-label",
        "result-target-label",
        "result-workbook-total",
        "results-missing",
        "workbook-navigation",
        "diff-workbench",
        "sheet-navigation",
    ):
        assert f'id="{element_id}"' in results_html
    assert 'id="detail-pane"' not in results_html
    assert 'id="toggle-detail"' not in results_html

    assert "COMPARISON OUTPUT" not in results_html
    assert "result-execution-state" not in results_html
    assert "endpointDirectoryName(sourceEndpoint)" in compare_js
    assert "context.source?.branch" in results_js

    for state in (
        "idle",
        "snapshot_loading",
        "snapshot_error",
        "candidates_ready",
        "diff_loading",
        "diff_empty",
        "diff_error",
        "diff_ready",
    ):
        assert state in compare_js or state in results_js or f'data-state="{state}"' in results_html

    assert "待语义引擎" in compare_js
    assert "FileReader" not in compare_js
    assert "readAsArrayBuffer" not in compare_js
    assert "/api/diff/batches" in compare_js
    assert "/api/diff/workbooks/compare" not in compare_js
    assert "语义 Diff API 尚未接入" not in results_js
    assert "/api/diff/workbooks/compare" in results_js


def test_m2_compare_flow_enters_results_and_runs_single_workbook_actions():
    root = Path(__file__).resolve().parents[1]
    compare_js = (root / "app" / "static" / "compare.js").read_text(encoding="utf-8")
    compare_html = (root / "app" / "templates" / "compare.html").read_text(encoding="utf-8")
    results_html = (root / "app" / "templates" / "compare_results.html").read_text(encoding="utf-8")

    assert "比对全部" in compare_html
    assert "进入差异结果" not in compare_html
    assert "比对当前工作簿" in results_html
    assert 'class="task-page-tab is-active"' in compare_html
    assert 'class="task-page-tab is-active"' in results_html
    assert 'window.location.assign(state.mockMode ? "/compare/demo/results" : "/compare/results")' in compare_js


def test_local_sample_entry_is_removed_from_compare_workbench():
    root = Path(__file__).resolve().parents[1]
    compare_html = (root / "app" / "templates" / "compare.html").read_text(encoding="utf-8")
    compare_js = (root / "app" / "static" / "compare.js").read_text(encoding="utf-8")
    assert "本地样本入口" not in compare_html
    assert 'id="local-old-file"' not in compare_html
    assert 'id="local-new-file"' not in compare_html
    assert 'id="preview-mock-diff"' not in compare_html
    assert "open-local-pair" not in compare_js


def test_m2_mock_diff_is_a_labeled_development_only_ui_fixture():
    root = Path(__file__).resolve().parents[1]
    compare_js = (root / "app" / "static" / "compare.js").read_text(encoding="utf-8")
    results_js = (root / "app" / "static" / "compare_results.js").read_text(encoding="utf-8")
    results_html = (root / "app" / "templates" / "compare_results.html").read_text(encoding="utf-8")

    assert "UI 示例假数据" in results_html
    assert "MOCK_WORKBOOKS" in compare_js
    assert "openMockPreview" in compare_js
    assert "renderSheet" in results_js
    assert 'id="mock-result-notice"' in results_html
    assert "FileReader" not in compare_js
    assert "/api/diff/batches" in compare_js
    assert "/api/diff/workbooks/compare" not in compare_js


def test_compare_controls_reenable_batch_action_after_snapshot_finishes() -> None:
    root = Path(__file__).resolve().parents[1]
    compare_js = (root / "app" / "static" / "compare.js").read_text(encoding="utf-8")

    update_controls = compare_js.split("function updateControls()", 1)[1].split(
        "function setSelection", 1
    )[0]
    assert "updateComparisonControls();" in update_controls
    assert "function requestId()" in compare_js
    assert 'button.textContent = "正在创建批量任务"' not in compare_js
    assert 'button.textContent = "比对全部"' not in compare_js
