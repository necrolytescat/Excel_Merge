(() => {
  const bridge = globalThis.ExcelDiffResultsBridge;
  const runId = document.body.dataset.m4RunId || "";
  if (!bridge || !runId) return;

  const TERMINAL = new Set(["completed", "completed_with_failures", "cancelled", "failed"]);
  const RETRYABLE = new Set(["read_failed", "business_failed", "orchestration_failed", "cancelled"]);
  const RUN_LABELS = {
    queued: "等待准备", preparing: "正在冻结表格快照", running: "正在执行工作簿比对",
    cancelling: "正在取消", completed: "计划比对完成", completed_with_failures: "计划比对部分失败",
    cancelled: "计划比对已取消", failed: "计划编排失败",
  };
  const ITEM_LABELS = {
    queued: "等待", running: "比对中", identical: "完全相同", semantic_equal: "语义一致",
    changed: "有差异", source_missing: "基准缺失", target_missing: "目标缺失",
    both_missing: "两侧缺失", read_failed: "读取失败", business_failed: "业务失败",
    orchestration_failed: "编排失败", cancelled: "已取消",
  };
  const state = bridge.state;
  let run = null;
  let activeTarget = "overview";
  let pollTimer = 0;
  let commandBusy = false;
  const loadingRefs = new Set();
  const endpointLabels = new Map();

  const $ = (id) => document.getElementById(id);
  function requestId() {
    return globalThis.crypto?.randomUUID?.() || "00000000-0000-4000-8000-" + String(Date.now()).padStart(12, "0").slice(-12);
  }
  function sideFile(exists, path, revision) {
    return exists ? { path, size: 0, revision, author: "", error: null } : null;
  }
  function candidate(item) {
    const status = item.candidate_status === "identical" ? "modified" : (item.candidate_status || "read_error");
    return {
      path: item.workbook_path,
      status,
      fingerprintSha256: item.source_sha256 && item.target_sha256 ? item.source_sha256 + item.target_sha256 : "",
      sourceFile: sideFile(item.source_exists, item.workbook_path, run.source_revision),
      targetFile: sideFile(item.target_exists, item.workbook_path, run.target_revisions[item.target_endpoint_id]),
    };
  }
  function resultFromItem(item, previous) {
    const base = {
      candidate: candidate(item), itemId: item.item_id, itemStatus: item.status,
      resultRef: item.result_ref || "", resultLoaded: false, summaryLoaded: false,
      error: item.error ? item.error.code + "：" + item.error.message : "", errors: [], sheets: [], summary: null, partial: false,
    };
    if (previous?.resultRef === base.resultRef && previous.resultLoaded) return { ...previous, ...base, resultLoaded: true };
    if (item.status === "identical") return { ...base, state: "diff_empty", summary: { modified_fields: 0, modified_rows: 0, source_only_rows: 0, target_only_rows: 0 } };
    if (["semantic_equal", "changed"].includes(item.status)) return { ...base, state: "diff_pending" };
    if (item.status === "business_failed") return { ...base, state: "diff_error", partial: item.diff_status === "partial" };
    if (["queued", "running"].includes(item.status)) return { ...base, state: "diff_loading", error: item.status === "running" ? "正在执行语义 Diff。" : "等待运行调度。" };
    if (["source_missing", "target_missing", "both_missing", "cancelled"].includes(item.status)) return { ...base, state: "diff_unavailable", error: ITEM_LABELS[item.status] };
    return { ...base, state: "diff_error" };
  }
  function targetLabel(id) { return endpointLabels.get(id) || id; }
  function currentItems(target = activeTarget) {
    return (run?.items || []).filter((item) => item.target_endpoint_id === target);
  }
  function syncUrl(workbook = state.selectedPath || "") {
    const url = new URL(location.href);
    if (activeTarget === "overview") url.searchParams.delete("target");
    else url.searchParams.set("target", activeTarget);
    if (activeTarget !== "overview" && workbook) url.searchParams.set("workbook", workbook);
    else url.searchParams.delete("workbook");
    history.replaceState(null, "", url.pathname + url.search);
  }
  function renderTabs() {
    const tabs = $("m4-run-tabs");
    tabs.replaceChildren();
    [{ id: "overview", label: "全部分支概览" }, ...run.target_endpoint_ids.map((id) => ({ id, label: targetLabel(id) }))].forEach((entry) => {
      const button = document.createElement("button");
      button.type = "button";
      button.className = "m4-run-tab" + (activeTarget === entry.id ? " is-active" : "");
      button.setAttribute("aria-pressed", String(activeTarget === entry.id));
      button.textContent = entry.label;
      button.addEventListener("click", () => selectTarget(entry.id));
      tabs.append(button);
    });
  }
  function matrixCell(item) {
    const button = document.createElement("button");
    button.type = "button";
    button.className = "m4-matrix-status is-" + item.status;
    button.textContent = ITEM_LABELS[item.status] || item.status;
    button.title = item.error?.message || "切换到该分支和工作簿";
    button.addEventListener("click", () => selectTarget(item.target_endpoint_id, item.workbook_path));
    return button;
  }
  function renderMatrix() {
    const headRow = document.createElement("tr");
    const workbook = document.createElement("th");
    workbook.scope = "col";
    workbook.textContent = "计划表格";
    headRow.append(workbook);
    run.target_endpoint_ids.forEach((target) => {
      const th = document.createElement("th");
      th.scope = "col";
      th.textContent = targetLabel(target);
      headRow.append(th);
    });
    $("m4-matrix-head").replaceChildren(headRow);
    const byKey = new Map(run.items.map((item) => [item.workbook_path + "\u0000" + item.target_endpoint_id, item]));
    const rows = run.workbook_paths.map((path) => {
      const row = document.createElement("tr");
      const name = document.createElement("th");
      name.scope = "row";
      name.textContent = path;
      name.title = path;
      row.append(name);
      run.target_endpoint_ids.forEach((target) => {
        const cell = document.createElement("td");
        const item = byKey.get(path + "\u0000" + target);
        if (item) cell.append(matrixCell(item));
        else cell.textContent = "—";
        row.append(cell);
      });
      return row;
    });
    $("m4-matrix-body").replaceChildren(...rows);
    $("m4-matrix-caption").textContent = run.workbook_paths.length + " 张表格 × " + run.target_endpoint_ids.length + " 个目标分支";
  }
  function renderRunStatus() {
    const progress = run.progress;
    $("batch-task-panel").classList.remove("hidden");
    $("batch-task-id").textContent = run.run_id;
    $("batch-task-heading").textContent = run.plan_name;
    $("batch-task-status").textContent = RUN_LABELS[run.status] || run.status;
    $("batch-task-progress-bar").style.width = Math.round(progress.ratio * 100) + "%";
    $("batch-task-progress").textContent = progress.processed_items + " / " + progress.total_items + " 已处理";
    $("batch-task-detail").textContent = "完全相同 " + progress.identical_items + " · 语义一致 " + progress.semantic_equal_items + " · 有差异 " + progress.changed_items + " · 缺失 " + progress.missing_items + " · 失败 " + progress.failed_items + " · 取消 " + progress.cancelled_items;
    const messages = (run.errors || []).map((error) => error.code + "：" + error.message);
    if (run.details_expired) messages.push("明细已过期，矩阵摘要与冻结 Revision 仍长期保留。");
    $("batch-task-error").textContent = messages.join("；");
    $("batch-task-error").classList.toggle("hidden", messages.length === 0);
    $("cancel-batch-task").disabled = commandBusy || TERMINAL.has(run.status);
    $("retry-batch-task").disabled = commandBusy || !TERMINAL.has(run.status) || !run.items.some((item) => RETRYABLE.has(item.status));
    $("input-page-link").href = "/diff-plans/" + run.plan_id;
  }
  function renderBranch(target, requestedWorkbook = "") {
    const previous = state.results || new Map();
    const next = new Map();
    currentItems(target).forEach((item) => next.set(item.workbook_path, resultFromItem(item, previous.get(item.workbook_path))));
    state.context = {
      version: 4, mode: "m4", capturedAt: run.created_at,
      source: { endpointId: run.source_endpoint_id, label: run.source_endpoint_id, branch: run.source_endpoint_id, resolvedRevision: run.source_revision },
      target: { endpointId: target, label: targetLabel(target), branch: targetLabel(target), resolvedRevision: run.target_revisions[target] },
      candidates: [...next.values()].map((value) => value.candidate), results: [],
    };
    state.results = next;
    bridge.renderTaskContext();
    $("result-workbook-total").textContent = String(next.size);
    $("results-missing").classList.add("hidden");
    $("diff-workbench").classList.remove("hidden");
    const selected = next.has(requestedWorkbook) ? requestedWorkbook : (next.has(state.selectedPath) ? state.selectedPath : next.keys().next().value);
    if (selected) bridge.selectWorkbook(selected);
  }
  function selectTarget(target, workbook = "") {
    activeTarget = run.target_endpoint_ids.includes(target) ? target : "overview";
    renderTabs();
    const overview = activeTarget === "overview";
    $("results-missing").classList.add("hidden");
    $("m4-matrix-panel").classList.toggle("hidden", !overview);
    $("result-heading-panel").classList.toggle("hidden", overview);
    $("diff-workbench").classList.toggle("hidden", overview);
    $("workbook-sidebar").classList.toggle("hidden", overview);
    $("result-page-body").classList.toggle("has-workbook-sidebar", !overview);
    if (!overview) renderBranch(activeTarget, workbook);
    syncUrl(workbook);
  }
  async function loadResult(result) {
    if (!result?.resultRef || result.resultLoaded || loadingRefs.has(result.resultRef) || run.details_expired) return;
    loadingRefs.add(result.resultRef);
    try {
      const payload = await bridge.request("/api/diff-plans/run-results/" + encodeURIComponent(result.resultRef));
      const mapped = globalThis.M2DiffMapper.mapDiffPayload(payload, result.candidate);
      state.results.set(result.candidate.path, { ...mapped, itemId: result.itemId, itemStatus: result.itemStatus, resultRef: result.resultRef, resultLoaded: true, summaryLoaded: true });
    } catch (error) {
      state.results.set(result.candidate.path, { ...result, state: "diff_error", error: bridge.errorMessage(error), errors: [] });
    } finally {
      loadingRefs.delete(result.resultRef);
      if (state.selectedPath === result.candidate.path) bridge.selectWorkbook(result.candidate.path);
      else bridge.renderWorkbookNavigation();
    }
  }
  function schedulePoll(delay = 800) {
    clearTimeout(pollTimer);
    pollTimer = window.setTimeout(refresh, delay);
  }
  async function refresh() {
    try {
      run = await bridge.request("/api/diff-plans/runs/" + encodeURIComponent(runId));
      renderRunStatus();
      renderMatrix();
      renderTabs();
      const query = new URLSearchParams(location.search);
      const requestedTarget = query.get("target") || activeTarget;
      const requestedWorkbook = query.get("workbook") || state.selectedPath;
      selectTarget(requestedTarget, requestedWorkbook);
      if (!TERMINAL.has(run.status)) schedulePoll();
    } catch (error) {
      $("batch-task-error").textContent = bridge.errorMessage(error);
      $("batch-task-error").classList.remove("hidden");
      schedulePoll(1800);
    }
  }
  async function command(path, body) {
    if (commandBusy) return;
    commandBusy = true;
    renderRunStatus();
    try {
      const payload = await bridge.request(path, { method: "POST", body: JSON.stringify(body) });
      if (payload.run_id !== runId) location.href = "/diff-plan-runs/" + payload.run_id;
      else { run = payload; renderRunStatus(); renderMatrix(); schedulePoll(100); }
    } catch (error) {
      $("batch-task-error").textContent = bridge.errorMessage(error);
      $("batch-task-error").classList.remove("hidden");
    } finally {
      commandBusy = false;
    }
  }
  function onWorkbookSelected(path) {
    if (activeTarget !== "overview") syncUrl(path);
  }

  globalThis.M4DiffPlanRuntime = Object.freeze({ onWorkbookSelected });
  globalThis.ExcelDiffBatchRuntime = Object.freeze({ loadResult, refreshTask: refresh });
  $("cancel-batch-task").addEventListener("click", () => command(
    "/api/diff-plans/runs/" + runId + "/cancel",
    { schema_version: "m4.diff-plan-run-command.request.v1", request_id: requestId() },
  ));
  $("retry-batch-task").addEventListener("click", () => command(
    "/api/diff-plans/runs/" + runId + "/retry",
    { schema_version: "m4.diff-plan-run-retry.request.v1", request_id: requestId() },
  ));
  window.addEventListener("beforeunload", () => clearTimeout(pollTimer));

  void (async () => {
    try {
      const endpoints = await bridge.request("/api/svn/endpoints");
      (endpoints.endpoints || []).forEach((endpoint) => endpointLabels.set(endpoint.id, endpoint.label || endpoint.id));
    } catch { endpointLabels.clear(); }
    await refresh();
  })();
})();
