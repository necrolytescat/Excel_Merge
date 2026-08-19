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
    assert '<span>版本比对</span><span class="nav-current">M2</span>' in response.text
    assert "确认两个端点" in response.text
    assert "端点注册表" in response.text
    assert 'id="source-endpoint"' in response.text
    assert 'id="target-endpoint"' in response.text
    assert 'id="swap-endpoints"' in response.text
    assert 'id="create-snapshot"' in response.text
    assert 'src="http://testserver/static/compare.js?v=1.4.1"' in response.text
    assert 'id="source-revision-trigger"' in response.text
    assert 'id="target-revision-trigger"' in response.text
    assert 'role="listbox"' in response.text
    assert "提交 LOG" in response.text
    assert "冻结所选 Revision" in response.text
    assert "分别冻结当前 HEAD" not in response.text
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
    assert "/api/svn/branch-logs" in response.text
    assert "loadRevisionPage" in response.text
    assert "maybeLoadMoreRevisions" in response.text
    assert 'addEventListener("scroll"' in response.text
    assert "formatRevisionDate" in response.text
    assert "invalidateSnapshotContext" in response.text
    assert "invalidateRevisionRequest" in response.text
    assert "[state.revisions.source, state.revisions.target]" in response.text
    assert "requestToken !== selection.requestToken" in response.text
    assert "options.contains(document.activeElement)" in response.text
    assert "focusedOption.focus({ preventScroll: true })" in response.text
    assert 'event.key === "Enter" || event.key === " "' in response.text
    assert 'value === "HEAD" ? "HEAD" : Number(value)' in response.text
    assert "revision: state.revisions.source.selected" in response.text
    assert "revision: state.revisions.target.selected" in response.text
    assert 'sessionStorage.removeItem(TASK_CONTEXT_KEY)' in response.text
    assert "BATCH_ENDPOINT_REVISIONS_MUST_DIFFER" not in response.text
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
    export_script = api.get("/static/compare_results_export.js")
    mapper = api.get("/static/m2_diff_mapper.js")
    batch_script = api.get("/static/compare_results_batch.js")
    readability = api.get("/static/compare_results_readability.css")
    batch_styles = api.get("/static/compare_results_batch.css")
    offline_script = api.get("/static/offline_replay.js")

    assert page.status_code == 200
    assert '<span>版本比对</span><span class="nav-current">M2</span>' in page.text
    assert "m2_diff_mapper.js?v=1.1.0" in page.text
    assert "app.css?v=0.3.3" in page.text
    assert "compare_results_readability.css?v=2.2.2" in page.text
    assert "compare_results_batch.css?v=1.1.2" in page.text
    assert "compare_results.js?v=2.3.2" in page.text
    assert "compare_results_export.js?v=1.2.0" in page.text
    assert "compare_results_batch.js?v=1.4.0" in page.text
    assert export_script.status_code == 200
    assert 'class="result-overview-grid"' in page.text
    assert 'id="batch-task-panel"' in page.text
    assert 'id="result-heading-panel"' in page.text
    assert page.text.index('id="batch-task-panel"') < page.text.index('id="result-heading-panel"')
    assert page.text.index('id="result-heading-panel"') < page.text.index('id="diff-workbench"')
    assert "repeat(auto-fit, minmax(min(100%, 360px), 1fr))" in readability.text
    assert ".diff-workbench > .sheet-strip" in readability.text
    assert ".result-overview-grid .batch-task-heading" in batch_styles.text
    assert "padding: 12px 14px;" in batch_styles.text
    assert "grid-template-columns: minmax(0, 1fr) auto;" in batch_styles.text
    assert ".result-heading-panel .section-heading > div:first-child" in readability.text
    assert "flex-wrap: nowrap;" in readability.text
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
    assert "/api/svn/endpoints" in batch_script.text
    assert "endpointDirectoryName" in batch_script.text
    assert "endpointNames.get(task.source.endpoint_id)" in batch_script.text
    assert "m2.batch-cancel.request.v1" in batch_script.text
    assert "m2.batch-retry.request.v1" in batch_script.text
    assert '" · r" + side.resolvedRevision' in script.text
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
    assert 'id="paired-diff-shell"' in page.text
    assert 'id="diff-source-scroll"' in page.text
    assert 'id="diff-status-scroll"' in page.text
    assert 'id="diff-status-scroll" aria-hidden="true"' not in page.text
    assert 'id="diff-target-scroll"' in page.text
    assert 'id="diff-selection-detail"' in page.text
    assert 'id="field-view-switch"' in page.text
    assert 'aria-label="字段显示范围"' in page.text
    assert 'id="show-diff-fields"' in page.text
    assert 'id="show-original-fields"' in page.text
    assert page.text.index('id="workbench-heading"') < page.text.index('id="field-view-switch"')
    assert page.text.index('id="field-view-switch"') < page.text.index('id="compare-current-workbook"')
    assert '$("result-heading-panel").classList.toggle("hidden", !visible);' in script.text
    assert 'id="semantic-table-body"' not in page.text
    assert 'class="semantic-table-header"' not in page.text
    assert "function sheetColumnModel(sheet, rows, fieldViewMode)" in script.text
    assert 'fieldViewMode: "diff"' in script.text
    assert "function syncFieldViewControls(enabled)" in script.text
    assert "function setFieldViewMode(mode)" in script.text
    assert "sheetColumnModel(sheet, rows, state.fieldViewMode)" in script.text
    assert '"show-diff-fields").addEventListener' in script.text
    assert '"show-original-fields").addEventListener' in script.text
    assert "function fieldDisplayName(definition, side)" in script.text
    assert "diff-grid-header-display-name" in script.text
    assert "diff-grid-header-field-name" in script.text
    assert "source_display_name" in script.text
    assert "target_display_name" in script.text
    assert 'row.status === "source_only" || row.status === "target_only"' in script.text
    assert "function createSideRow(view, row, rowIndex, side)" in script.text
    assert "function modifiedFieldTargets(view, row)" in script.text
    assert "function centerDiffField(view, rowIndex, fieldIndex)" in script.text
    assert "function navigateModifiedField(view, row, rowIndex)" in script.text
    assert "function targetDiffSegments(sourceValue, targetValue)" in script.text
    assert "function renderTargetDiff(button, sourceValue, targetValue)" in script.text
    assert 'side === "target" && changed' in script.text
    assert 'document.createTextNode(segment.text)' in script.text
    assert 'highlight.className = "diff-target-change"' in script.text
    assert "TARGET_DIFF_MAX_MATRIX_CELLS" in script.text
    assert "innerHTML" not in script.text
    assert 'row.status === "modified" && row.changedFields.size > 0' in script.text
    assert 'document.createElement(navigable ? "button" : "div")' in script.text
    assert 'status.addEventListener("click", () => navigateModifiedField(view, row, rowIndex));' in script.text
    assert "targets[(currentIndex + 1) % targets.length]" in script.text
    assert "function renderDiffWindow(view, force = false)" in script.text
    assert "function syncDiffScroll(view, origin)" in script.text
    assert "function bindDragPan(view, scroller)" in script.text
    assert 'event.pointerType !== "mouse"' in script.text
    drag_pan = script.text.split("function bindDragPan(view, scroller)", 1)[1].split(
        "function setPairedDiffEmpty", 1
    )[0]
    assert drag_pan.index("drag.moved = true;") < drag_pan.index(
        "scroller.setPointerCapture?.(event.pointerId);"
    )
    assert 'window.addEventListener("pointermove", onPointerMove);' in drag_pan
    assert 'scroller.addEventListener("pointermove", onPointerMove);' not in drag_pan
    assert 'target_only: "右侧新增"' in script.text
    assert 'source_only: "右侧删除"' in script.text
    assert ".paired-diff-grid" in readability.text
    assert ".field-view-switch" in readability.text
    assert ".field-view-button" in readability.text
    assert "grid-template-columns: minmax(0, 1fr) 88px minmax(0, 1fr)" in readability.text
    assert ".diff-grid-cell.is-primary-key" in readability.text
    assert ".diff-grid-cell.is-selected" in readability.text
    assert "button.diff-status-row.is-navigable" in readability.text
    assert ".diff-grid-cell.is-changed.has-target-diff" in readability.text
    assert ".diff-target-change" in readability.text
    target_change_rule = readability.text.split(".diff-target-change", 1)[1].split("}", 1)[0]
    assert "color: #b42318" in target_change_rule
    focus_rule = readability.text.split(".diff-grid-cell:focus-visible", 1)[1].split("}", 1)[0]
    selected_rule = readability.text.split(".diff-grid-cell.is-selected", 1)[1].split("}", 1)[0]
    assert "z-index" not in focus_rule
    assert "z-index" not in selected_rule
    assert ".diff-selection-values pre" in readability.text
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
    assert 'result.itemStatus === "queued"' in script.text
    assert 'return "待处理"' in script.text
    assert 'result.itemStatus === "running"' in script.text
    assert 'result.itemStatus === "succeeded" && !result.summary' in script.text
    assert "function loadAvailableSummaries()" in batch_script.text
    assert "void loadAvailableSummaries();" in batch_script.text
    assert "let navigationChanged = false;" in batch_script.text
    assert "if (navigationChanged) bridge.renderWorkbookNavigation();" in batch_script.text
    assert "loadCompletedSummaries" not in batch_script.text
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
    assert "compare_readability.css?v=1.2.0" in formal_response.text
    assert "本地样本入口" not in formal_response.text
    assert demo_response.status_code == 200
    assert 'data-demo-mode="true"' in demo_response.text
    assert 'class="compare-readable-page"' in demo_response.text
    assert "compare_readability.css?v=1.2.0" in demo_response.text
    assert "Excel Diff 流程示例" in demo_response.text
    assert results_response.status_code == 200
    assert 'class="results-readable-page"' in results_response.text
    assert "compare_results_readability.css?v=2.2.2" in results_response.text
    assert "差异结果" in results_response.text
    assert demo_results_response.status_code == 200
    assert 'data-demo-mode="true"' in demo_results_response.text
    assert 'class="results-readable-page"' in demo_results_response.text
    assert "compare_results_readability.css?v=2.2.2" in demo_results_response.text
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

def test_history_tasks_page_and_task_url_recovery_contract():
    api = TestClient(
        create_app(
            config={"svn": {"provider": "mock"}},
            provider=MockSVNProvider(),
        )
    )

    history_page = api.get("/compare/history")
    history_script = api.get("/static/history_tasks.js")
    history_styles = api.get("/static/history_tasks.css")
    compare_script = api.get("/static/compare.js")
    results_script = api.get("/static/compare_results.js")
    batch_script = api.get("/static/compare_results_batch.js")

    assert history_page.status_code == 200
    assert "Config Atlas · 历史任务" in history_page.text
    assert '<span>版本比对</span><span class="nav-current">M2</span>' in history_page.text
    assert 'href="/compare/history" aria-current="page"' in history_page.text
    assert 'id="history-task-rows"' in history_page.text
    assert 'id="history-status-switch"' in history_page.text
    assert 'id="history-load-more"' in history_page.text
    assert "history_tasks.css?v=3.0.0" in history_page.text
    assert "history_tasks.js?v=3.0.0" in history_page.text
    assert 'id="history-detail-dialog"' in history_page.text
    assert 'id="history-detail-events"' in history_page.text
    assert 'id="history-delete-confirm"' in history_page.text
    assert 'data-history-view="tasks"' in history_page.text
    assert 'data-history-view="logs"' in history_page.text
    assert 'data-history-view="cache"' in history_page.text
    assert 'id="history-log-filters"' in history_page.text
    assert 'id="history-cache-metrics"' in history_page.text
    assert 'id="history-cache-dialog"' in history_page.text
    assert "/api/diff/batches?" in history_script.text
    assert "m2.batch-list.v1" in history_script.text
    assert "m2.batch-management.v1" in history_script.text
    assert "m2.batch-delete.request.v1" in history_script.text
    assert 'method: "DELETE"' in history_script.text
    assert "/api/operations/logs" in history_script.text
    assert "m2.operations-log-list.v1" in history_script.text
    assert "/api/operations/svn-cache" in history_script.text
    assert "m2.svn-cache-clear.request.v1" in history_script.text
    assert 'confirmation !== "清空全局 SVN 缓存"' in history_script.text
    assert "If-None-Match" in history_script.text
    assert "data-status-group" in history_page.text
    assert "@media (max-width: 720px)" in history_styles.text
    assert ".history-detail-dialog" in history_styles.text
    assert ".history-delete-confirm" in history_styles.text
    assert ".history-log-table" in history_styles.text
    assert ".history-cache-metrics" in history_styles.text
    assert "formalResultsUrl(task.task_id)" in compare_script.text
    assert 'get("task_id")' in results_script.text
    assert "history.replaceState" in results_script.text
    assert "syncTaskUrl(task.task_id)" in batch_script.text
    assert "BATCH_TASK_EXPIRED" in batch_script.text

    for path in ("/", "/compare", "/compare/results", "/compare/history"):
        page = api.get(path)
        assert "历史任务" in page.text
        assert "任务与报告" not in page.text
        assert 'href="/compare/history"' in page.text
