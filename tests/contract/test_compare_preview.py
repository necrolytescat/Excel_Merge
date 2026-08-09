from fastapi.testclient import TestClient

from app.main import create_app
from core.svn_provider import MockSVNProvider


def test_compare_input_is_formalized_and_table_excel_only():
    api = TestClient(
        create_app(
            config={"svn": {"provider": "mock"}},
            provider=MockSVNProvider(),
        )
    )

    response = api.get("/compare")

    assert response.status_code == 200
    assert "Config Atlas · 版本对比" in response.text
    assert "确认两个端点" in response.text
    assert "端点注册表" in response.text
    assert 'id="source-endpoint"' in response.text
    assert 'id="target-endpoint"' in response.text
    assert 'id="swap-endpoints"' in response.text
    assert 'id="create-snapshot"' in response.text
    assert 'src="http://testserver/static/compare.js?v=1.1.2"' in response.text
    assert "锁定并读取快照" in response.text
    assert "Table" in response.text
    assert "全量 Excel" in response.text
    assert "CONFIG" not in response.text
    assert "TABLECSV" not in response.text
    assert ".csv" not in response.text.lower()
    assert "Revision 下拉" not in response.text
    assert "日期" not in response.text
    assert "左侧快照" not in response.text
    assert "右侧快照" not in response.text
    assert "DIFF CANDIDATES" in response.text
    assert "snapshot-progress" in response.text


def test_settings_navigation_renames_compare_entry():
    api = TestClient(
        create_app(
            config={"svn": {"provider": "mock"}},
            provider=MockSVNProvider(),
        )
    )

    response = api.get("/")

    assert response.status_code == 200
    assert "版本对比" in response.text
    assert "双端点对比" not in response.text

def test_compare_script_uses_registered_and_branch_candidates():
    api = TestClient(
        create_app(
            config={"svn": {"provider": "mock"}},
            provider=MockSVNProvider(),
        )
    )
    response = api.get("/static/compare.js")
    assert response.status_code == 200
    assert "/api/svn/endpoints" in response.text
    assert "/api/svn/branch-candidates" in response.text
    assert "/api/svn/snapshots" in response.text
    assert "pendingRegistration" in response.text
    assert "buildDifferenceFiles" in response.text
    assert "content_hash" in response.text
    assert 'mode: state.mockMode ? "demo" : "formal"' in response.text
    assert "endpointId: state.mockMode" in response.text
    assert "/api/diff/batches" in response.text
    assert "m2.batch-create.request.v1" in response.text


def test_formal_results_page_calls_m2_diff_api_without_demo_fixture_dependency():
    api = TestClient(
        create_app(
            config={"svn": {"provider": "mock"}},
            provider=MockSVNProvider(),
        )
    )

    page = api.get("/compare/results")
    script = api.get("/static/compare_results.js")
    mapper = api.get("/static/m2_diff_mapper.js")
    batch_script = api.get("/static/compare_results_batch.js")
    readability = api.get("/static/compare_results_readability.css")
    offline_script = api.get("/static/offline_replay.js")

    assert page.status_code == 200
    assert "m2_diff_mapper.js?v=1.0.0" in page.text
    assert "app.css?v=0.3.3" in page.text
    assert "compare_results_readability.css?v=1.9.3" in page.text
    assert "compare_results.js?v=1.9.3" in page.text
    assert "compare_results_batch.js?v=1.2.0" in page.text
    assert 'id="batch-task-panel"' in page.text
    assert "语义 Diff 服务尚未接入" not in page.text
    assert "左侧 · SOURCE" in page.text
    assert "右侧 · TARGET" in page.text
    assert "/api/diff/workbooks/compare" in script.text
    assert "m2.workbook-compare.request.v1" in script.text
    assert "M2DiffMapper.mapDiffPayload" in script.text
    assert 'replace(/\\.(?:xlsm|xlsx)$/i, "")' in script.text
    assert "name.textContent = workbookDisplayName(path);" in script.text
    assert ".workbook-nav-item strong" in readability.text
    assert "grid-template-columns: 280px minmax(0, 1fr);" in readability.text
    assert "width: 280px;" in readability.text
    assert "font-size: 11px;" in readability.text
    assert 'const demo = context.mode === "demo"' in script.text
    assert "m2.diff.v1" in mapper.text
    assert "/api/diff/batches/" in batch_script.text
    assert "/api/diff/batch-results/" in batch_script.text
    assert "m2.batch-cancel.request.v1" in batch_script.text
    assert "m2.batch-retry.request.v1" in batch_script.text
    assert "batchTaskId" in batch_script.text
    assert "result_ref" in batch_script.text
    assert 'state: "diff_pending"' in batch_script.text
    assert "点击左侧失败工作簿查看原因" in batch_script.text
    assert 'result.itemStatus === "business_failed"' in script.text
    assert "source_only" in mapper.text
    assert '"整行"' not in mapper.text
    assert 'const COMPLETED_TASKS = new Set(["completed", "completed_with_failures"])' in batch_script.text
    assert 'item.status !== "succeeded"' in batch_script.text
    assert 'item.diff_status === "unchanged"' in batch_script.text
    assert 'item.diff_status === "modified"' in batch_script.text
    assert '"无差异文件 " + unchanged + " · 有差异文件 " + modified' in batch_script.text
    assert '<p class="eyebrow">比对结果</p>' in page.text
    assert "WORKBOOK DIFF RESULTS" not in page.text
    assert "function summaryCaption(result)" not in script.text
    assert "function workbookCaption(result)" in script.text
    assert "function selectedFieldCaption(field)" in script.text
    assert 'sides.push("左侧第 " + field.sourceRowNumber + " 行")' in script.text
    assert 'sides.push("右侧第 " + field.targetRowNumber + " 行")' in script.text
    assert 'parts.push(sides.join(" / "))' in script.text
    assert 'return parts.join(" · ")' in script.text
    assert '$("workbench-caption").textContent = selectedFieldCaption(field);' in script.text
    assert 'aria-label="当前差异详情"' not in page.text
    assert 'id="toggle-detail"' not in page.text
    assert '$("detail-location")' not in script.text
    assert '$("toggle-detail")' not in script.text
    assert "grid-template-columns: minmax(0, 1fr);" in readability.text
    assert 'class="sheet-strip"' in page.text
    assert page.text.index('id="workbench-caption"') < page.text.index('id="sheet-navigation"')
    assert 'class="sheet-pane"' not in page.text
    assert 'id="show-modified-sheets" type="button" aria-pressed="true"' in page.text
    assert 'id="show-all-sheets" type="button" aria-pressed="false"' in page.text
    assert '>0 / 0</span>' in page.text
    assert "showAllSheets: false" in script.text
    assert "function visibleSheetResults(result)" in script.text
    assert 'state.showAllSheets || sheet.status !== "unchanged"' in script.text
    assert "function preferredVisibleSheet(result)" in script.text
    assert 'visible.length + " / " + sheets.length' in script.text
    assert "visibleSheetResults(result).forEach" in script.text
    assert "function setSheetFilterMode(showAll)" in script.text
    assert '$("show-modified-sheets").addEventListener("click", () => setSheetFilterMode(false))' in script.text
    assert '$("show-all-sheets").addEventListener("click", () => setSheetFilterMode(true))' in script.text
    assert "function sheetMetrics(sheet)" in script.text
    assert "sheet.summary.modified_fields" in script.text
    assert "sheet.summary.source_only_rows" in script.text
    assert 'row.status === "target_only" ? (row.fields || []).length : 0' in script.text
    assert "Number(sheet.summary.modified_fields || 0) + addedFields" in script.text
    assert 'modified.textContent = "+" + metrics.modified' in script.text
    assert 'deleted.textContent = "-" + metrics.deleted' in script.text
    assert "if (metrics.deleted > 0)" in script.text
    assert "meta.append(modified)" in script.text
    assert "meta.append(separator, deleted)" in script.text
    assert 'separator.textContent = "/"' in script.text
    assert 'meta.classList.add("is-failed")' in script.text
    assert 'meta.textContent = "失败"' in script.text
    assert "grid-template-columns: minmax(0, 1fr) auto" in readability.text
    assert (
        "grid-template-columns: minmax(0, 1fr);\n"
        "  grid-template-rows: auto auto;\n"
        "  align-items: start;"
    ) not in readability.text
    assert ".sheet-status .is-modified" in readability.text
    assert ".sheet-status .is-separator" in readability.text
    assert ".sheet-status .is-deleted" in readability.text
    assert ".sheet-status.is-failed" in readability.text
    assert "color: #087c6d" in readability.text
    assert "color: #b42318" in readability.text
    assert '.sheet-filter-button[aria-pressed="true"]' in readability.text
    assert "display: flex;" in readability.text
    assert "overflow-x: auto;" in readability.text
    assert "min-width: 140px;" in readability.text
    assert "当前工作簿：" not in script.text
    assert 'id="result-action-message" role="status" aria-live="polite"></p>' in page.text
    assert "result.summary.modified_rows" in script.text
    assert "result.summary.target_only_rows" in script.text
    assert "result.summary.source_only_rows" in script.text
    assert 'modified.textContent = metrics ? metricValue(metrics.changed, "+") : "—"' in script.text
    assert 'deleted.textContent = metrics ? metricValue(metrics.deleted, "-") : "—"' in script.text
    assert "return sign + value" in script.text
    assert '"，变化行 " + modified.textContent + "，删除行 "' in script.text
    assert "修改 " not in script.text
    assert "删除 " not in script.text
    assert "path.textContent = result.candidate.path" not in script.text
    assert "summaryLoadingRefs" in batch_script.text
    assert "Math.min(4, queue.length)" in batch_script.text
    assert ".workbook-row-summary .is-modified" in readability.text
    assert "color: #087c6d" in readability.text
    assert ".workbook-row-summary .is-deleted" in readability.text
    assert "color: #b42318" in readability.text
    assert 'class="workbook-pane workbook-sidebar hidden"' in page.text
    assert page.text.index('aria-label="主导航"') < page.text.index('id="workbook-sidebar"')
    assert page.text.index('class="app-content compare-content"') < page.text.index('id="workbook-sidebar"')
    assert page.text.index('class="topbar compare-topbar result-topbar"') < page.text.index('id="workbook-sidebar"')
    assert page.text.index('class="task-page-nav"') < page.text.index('id="workbook-sidebar"')
    assert 'class="result-page-body"' in page.text
    assert 'class="result-page-main"' in page.text
    assert "grid-template-rows: auto minmax(0, 1fr)" in readability.text
    assert "grid-template-columns: 1fr" in readability.text
    assert "height: 100vh" in readability.text
    assert "overflow-y: auto" in readability.text
    assert "border-radius: 6px" in readability.text
    assert 'id="toggle-unchanged-workbooks"' in page.text
    assert 'class="workbook-sidebar-header"' in page.text
    assert page.text.index("<strong>工作簿</strong>") < page.text.index('id="toggle-unchanged-workbooks"')
    assert "显示无变化 0" in page.text
    assert "showUnchanged: false" in script.text
    assert 'result.itemStatus === "succeeded"' in script.text
    assert 'result.diffStatus === "unchanged" || result.state === "diff_empty"' in script.text
    assert "function visibleWorkbookResults(" in script.text
    assert 'results.length + " / " + allResults.length' in script.text
    assert '$("toggle-unchanged-workbooks").addEventListener("click", toggleUnchangedWorkbooks)' in script.text
    assert "function syncWorkbookSidebarVisibility()" in script.text
    assert "new MutationObserver(syncWorkbookSidebarVisibility)" in script.text
    assert ".workbook-visibility-toggle" in readability.text
    assert "border-left: 3px solid transparent" in readability.text
    assert "border-radius: 0" in readability.text
    assert ".workbook-nav-item.is-selected" in readability.text
    assert 'id="result-page-body"' in page.text
    assert 'class="workbook-sidebar-actions"' in page.text
    assert 'id="toggle-confirmed-workbooks"' in page.text
    assert "显示已确认 0" in page.text
    assert "showConfirmed: false" in script.text
    assert "confirmedPaths: new Set()" in script.text
    assert "excelDiffConfirmedWorkbooks:" in script.text
    assert 'confirmation.type = "checkbox"' in script.text
    assert 'confirmation.addEventListener("change"' in script.text
    assert "function isConfirmableResult(result)" in script.text
    assert "function setWorkbookConfirmed(" in script.text
    assert "function clearWorkbookConfirmations(" in script.text
    assert '$("toggle-confirmed-workbooks").addEventListener("click", toggleConfirmedWorkbooks)' in script.text
    assert 'grid-area: workbooks' in readability.text
    assert '"workbooks tabs"' in readability.text
    assert '"tabs"' in readability.text
    assert ".workbook-nav-select" in readability.text
    assert ".workbook-confirm-control input" in readability.text
    assert "accent-color: #3157a4" in readability.text
    assert "bridge.clearWorkbookConfirmations();" in offline_script.text
    assert "bridge.clearWorkbookConfirmations(result.candidate.path);" in offline_script.text
    assert "m2.diff.v1" not in offline_script.text
    assert "m2.batch.v1" not in offline_script.text


def test_compare_demo_is_separate_and_development_only():
    dev_api = TestClient(
        create_app(
            config={"web": {"dev_mode": True}, "svn": {"provider": "mock"}},
            provider=MockSVNProvider(),
        )
    )

    formal_response = dev_api.get("/compare?demo=1")
    demo_response = dev_api.get("/compare/demo")
    demo_results_response = dev_api.get("/compare/demo/results")
    results_response = dev_api.get("/compare/results")
    replay_response = dev_api.get("/compare/replay")

    assert formal_response.status_code == 200
    assert 'data-demo-mode="false"' in formal_response.text
    assert 'class="compare-readable-page"' in formal_response.text
    assert "compare_readability.css?v=1.0.0" in formal_response.text
    assert "本地样本入口" not in formal_response.text
    assert demo_response.status_code == 200
    assert 'data-demo-mode="true"' in demo_response.text
    assert 'class="compare-readable-page"' in demo_response.text
    assert "compare_readability.css?v=1.0.0" in demo_response.text
    assert "Excel Diff 流程示例" in demo_response.text
    assert results_response.status_code == 200
    assert 'class="results-readable-page"' in results_response.text
    assert "compare_results_readability.css?v=1.9.3" in results_response.text
    assert "差异结果" in results_response.text
    assert demo_results_response.status_code == 200
    assert 'data-demo-mode="true"' in demo_results_response.text
    assert 'class="results-readable-page"' in demo_results_response.text
    assert "compare_results_readability.css?v=1.9.3" in demo_results_response.text
    assert "示例差异结果" in demo_results_response.text

    assert replay_response.status_code == 200
    assert 'data-replay-mode="true"' in replay_response.text
    assert 'id="offline-fixture-file"' in replay_response.text
    assert "offline_replay.js?v=1.1.0" in replay_response.text
    assert "offline_replay.css?v=1.0.0" in replay_response.text

    production_api = TestClient(
        create_app(
            config={"web": {"dev_mode": False}, "svn": {"provider": "mock"}},
            provider=MockSVNProvider(),
        )
    )
    assert production_api.get("/compare/demo").status_code == 404
    assert production_api.get("/compare/demo/results").status_code == 404
    assert production_api.get("/compare/replay").status_code == 404
    assert production_api.get("/__local_verify/atlas").status_code == 404
