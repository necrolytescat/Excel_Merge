(() => {
  const bridge = globalThis.ExcelDiffResultsBridge;
  if (!bridge) return;

  const TASK_CONTEXT_KEY = "excelDiffTaskContext";
  const TERMINAL_TASKS = new Set(["completed", "completed_with_failures", "cancelled", "failed"]);
  const COMPLETED_TASKS = new Set(["completed", "completed_with_failures"]);
  const TASK_LABELS = {
    queued: "等待准备",
    preparing: "正在重建候选",
    running: "正在比对全部工作簿",
    cancelling: "正在取消",
    completed: "批量比对已完成",
    completed_with_failures: "批量比对部分失败",
    cancelled: "批量任务已取消",
    failed: "批量任务失败",
  };
  const state = bridge.state;
  const loadingRefs = new Set();
  const summaryLoadingRefs = new Set();
  let pollTimer = 0;
  let commandBusy = false;

  const $ = (id) => document.getElementById(id);

  function requestId() {
    if (globalThis.crypto?.randomUUID) return globalThis.crypto.randomUUID();
    return "00000000-0000-4000-8000-" + String(Date.now()).padStart(12, "0").slice(-12);
  }

  function sideFile(side, candidatePath, revision) {
    if (!side?.exists) return null;
    return {
      path: candidatePath,
      size: Number(side.size_bytes || 0),
      revision,
      author: "",
      error: side.read_error ? { ...side.read_error } : null,
    };
  }

  function candidateFromItem(item) {
    const candidate = item.candidate;
    return {
      path: candidate.path,
      status: candidate.status,
      fingerprintSha256: candidate.fingerprint_sha256,
      sourceFile: sideFile(
        candidate.source,
        candidate.path,
        state.context.source?.resolvedRevision,
      ),
      targetFile: sideFile(
        candidate.target,
        candidate.path,
        state.context.target?.resolvedRevision,
      ),
    };
  }

  function orchestrationMessage(item) {
    const error = item.orchestration_error;
    return error ? error.code + "：" + error.message : "单工作簿批量编排失败。";
  }

  function activeResultMode() {
    return state.context?.mode === "replay"
      ? (state.context.replayResultMode || "golden") : "";
  }
  function resultFromItem(item, previous) {
    const candidate = candidateFromItem(item);
    if (
      item.result_ref
      && (previous?.resultLoaded || previous?.summaryLoaded)
      && previous.resultRef === item.result_ref
      && previous.resultMode === activeResultMode()
    ) {
      return {
        ...previous,
        candidate,
        itemStatus: item.status,
        diffStatus: item.diff_status || "",
        diffErrorCount: Number(item.diff_error_count || 0),
      };
    }

    const base = {
      candidate,
      itemId: item.item_id,
      itemStatus: item.status,
      diffStatus: item.diff_status || "",
      diffErrorCount: Number(item.diff_error_count || 0),
      resultRef: item.result_ref || "",
      resultLoaded: false,
      summaryLoaded: false,
      summaryError: false,
      resultMode: activeResultMode(),
      error: "",
      errors: [],
      sheets: [],
      summary: null,
      partial: false,
    };
    if (item.status === "succeeded") {
      return {
        ...base,
        state: "diff_pending",
        error: "批量处理已完成，正在按需读取结果。",
      };
    }
    if (item.status === "business_failed") {
      const count = Number(item.diff_error_count || 0);
      const partial = item.diff_status === "partial";
      const label = partial ? "部分完成" : "执行失败";
      const detail = count ? `包含 ${count} 个业务错误` : "包含业务错误";
      return {
        ...base,
        state: "diff_error",
        partial,
        error: `${label}：${detail}。点击工作簿读取详细原因。`,
        errors: [{
          code: partial ? "M2_DIFF_PARTIAL" : "M2_DIFF_FAILED",
          message: `${detail}，正在按需读取详细原因。`,
        }],
      };
    }
    if (item.status === "orchestration_failed") {
      const error = item.orchestration_error || {};
      return {
        ...base,
        state: "diff_error",
        error: orchestrationMessage(item),
        errors: [{
          code: error.code || "BATCH_ITEM_FAILED",
          message: error.message || "单工作簿批量编排失败。",
        }],
      };
    }
    if (item.status === "queued" || item.status === "running") {
      return {
        ...base,
        state: "diff_loading",
        error: item.status === "running" ? "正在执行语义 Diff。" : "等待批量调度。",
      };
    }
    return {
      ...base,
      state: "diff_unavailable",
      error: item.status === "cancelled" ? "该工作簿在开始前已取消。" : "该候选不执行语义 Diff。",
    };
  }

  function defaultRetryable(task) {
    return (task.items || []).some((item) => (
      item.status === "orchestration_failed"
      || item.status === "cancelled"
      || (item.status === "skipped" && item.candidate.status === "read_error")
    ));
  }

  function completedDiffDetail(task) {
    if (!COMPLETED_TASKS.has(task.status)) return "";
    let unchanged = 0;
    let modified = 0;
    (task.items || []).forEach((item) => {
      if (item.status !== "succeeded") return;
      if (item.diff_status === "unchanged") unchanged += 1;
      if (item.diff_status === "modified") modified += 1;
    });
    return "无差异文件 " + unchanged + " · 有差异文件 " + modified + " · ";
  }

  function renderBatchTask(task) {
    const panel = $("batch-task-panel");
    panel.classList.remove("hidden");
    panel.dataset.taskStatus = task.status;
    $("batch-task-status").textContent = TASK_LABELS[task.status] || task.status;
    $("batch-task-id").textContent = task.task_id;

    const progress = task.progress || {};
    const total = progress.total_items;
    const processed = Number(progress.processed_items || 0);
    const ratio = total === null || total === undefined ? 0 : Number(progress.ratio || 0);
    $("batch-task-progress-bar").style.width = Math.round(ratio * 100) + "%";
    $("batch-task-progress").textContent = total === null || total === undefined
      ? "正在准备服务端候选清单"
      : processed + " / " + total + " 已处理";
    $("batch-task-detail").textContent = total === null || total === undefined
      ? "候选由服务端在固定 Revision 上重新构建"
      : completedDiffDetail(task)
        + "成功 " + Number(progress.succeeded_items || 0)
        + " · 业务失败 " + Number(progress.business_failed_items || 0)
        + (Number(progress.business_failed_items || 0) ? "（点击左侧失败工作簿查看原因）" : "")
        + " · 编排失败 " + Number(progress.orchestration_failed_items || 0)
        + " · 跳过 " + Number(progress.skipped_items || 0)
        + " · 取消 " + Number(progress.cancelled_items || 0);

    const errors = (task.errors || []).map((error) => error.code + "：" + error.message);
    $("batch-task-error").textContent = errors.join("；");
    $("batch-task-error").classList.toggle("hidden", !errors.length);
    $("cancel-batch-task").disabled = commandBusy || TERMINAL_TASKS.has(task.status);
    $("retry-batch-task").disabled = commandBusy || !TERMINAL_TASKS.has(task.status) || !defaultRetryable(task);
  }

  function persistContext() {
    if (state.context?.mode !== "replay") sessionStorage.setItem(TASK_CONTEXT_KEY, JSON.stringify(state.context));
  }

  function syncTaskUrl(taskId) {
    if (state.context?.mode !== "formal" || !taskId) return;
    const canonical = "/compare/results?task_id=" + encodeURIComponent(taskId);
    if (location.pathname + location.search !== canonical) history.replaceState(null, "", canonical);
  }
  function showNoItems(task) {
    bridge.showMissingContext();
    $("results-missing").classList.remove("hidden");
    const heading = $("missing-heading");
    const detail = $("missing-detail");
    if (task.status === "failed") {
      heading.textContent = "批量任务未能准备候选";
      detail.textContent = "任务错误已保留在上方，可返回版本页重新创建。";
    } else if (TERMINAL_TASKS.has(task.status)) {
      heading.textContent = "冻结版本之间没有差异候选";
      detail.textContent = "服务端已完成候选重建，本次没有需要执行的工作簿。";
    } else {
      heading.textContent = "正在准备批量候选";
      detail.textContent = "服务端正在固定 Revision 上重建完整候选清单。";
    }
  }

  function syncTask(task) {
    state.context.source = {
      endpointId: task.source.endpoint_id,
      label: task.source.endpoint_id,
      branch: task.source.endpoint_id,
      resolvedRevision: task.source.revision,
    };
    state.context.target = {
      endpointId: task.target.endpoint_id,
      label: task.target.endpoint_id,
      branch: task.target.endpoint_id,
      resolvedRevision: task.target.revision,
    };
    state.context.capturedAt = task.created_at;
    const previous = state.results;
    const next = new Map();
    (task.items || []).forEach((item) => {
      const path = item.candidate.path;
      next.set(path, resultFromItem(item, previous.get(path)));
    });
    state.results = next;
    state.context.batchTaskId = task.task_id;
    syncTaskUrl(task.task_id);
    state.context.candidates = [...next.values()].map((result) => result.candidate);
    state.context.retryOfBatchTaskId = task.retry_of_task_id || null;
    persistContext();
    renderBatchTask(task);
    bridge.renderTaskContext();
    $("result-workbook-total").textContent = String(next.size);

    if (!next.size) {
      showNoItems(task);
      return;
    }

    $("results-missing").classList.add("hidden");
    $("diff-workbench").classList.remove("hidden");
    const selected = next.has(state.selectedPath) ? state.selectedPath : next.keys().next().value;
    bridge.selectWorkbook(selected);
    void loadAvailableSummaries();
  }

  function schedulePoll(delay = 700) {
    window.clearTimeout(pollTimer);
    pollTimer = window.setTimeout(refreshTask, delay);
  }

  async function refreshTask() {
    const taskId = state.context?.batchTaskId;
    if (!taskId) return;
    try {
      const task = await bridge.request("/api/diff/batches/" + encodeURIComponent(taskId));
      syncTask(task);
      if (!TERMINAL_TASKS.has(task.status)) schedulePoll();
    } catch (error) {
      const code = error?.error?.code || "";
      const terminalLookupError = new Set([
        "BATCH_TASK_EXPIRED",
        "BATCH_TASK_NOT_FOUND",
        "BATCH_TASK_FORBIDDEN",
      ]).has(code);
      $("batch-task-error").textContent = bridge.errorMessage(error);
      $("batch-task-error").classList.remove("hidden");
      if (terminalLookupError) {
        bridge.showMissingContext();
        $("missing-heading").textContent = code === "BATCH_TASK_EXPIRED" ? "任务已过期" : "任务不可用";
        $("missing-detail").textContent = code === "BATCH_TASK_EXPIRED"
          ? "任务和正式结果已超过保留期。"
          : "请从历史任务重新选择任务。";
        return;
      }
      schedulePoll(1600);
    }
  }
  function resultPath(result, requestedMode) {
    return state.context?.mode === "replay"
      ? "/api/replay/results/" + encodeURIComponent(result.itemId)
        + "?mode=" + encodeURIComponent(requestedMode)
      : "/api/diff/batch-results/" + encodeURIComponent(result.resultRef);
  }

  async function loadResultSummary(result) {
    const requestedMode = activeResultMode();
    const loadingKey = result?.resultRef + ":" + requestedMode;
    let navigationChanged = false;
    if (
      !result?.resultRef
      || result.resultLoaded
      || result.summaryLoaded
      || loadingRefs.has(loadingKey)
      || summaryLoadingRefs.has(loadingKey)
    ) return;
    summaryLoadingRefs.add(loadingKey);
    try {
      const payload = await bridge.request(resultPath(result, requestedMode));
      if (payload?.schema_version !== "m2.diff.v1" || !payload.summary) {
        throw new Error("工作簿结果缺少 m2.diff.v1 summary");
      }
      const current = state.results.get(result.candidate.path);
      if (current?.resultRef === result.resultRef && current.resultMode === requestedMode) {
        state.results.set(result.candidate.path, {
          ...current,
          summary: { ...payload.summary },
          summaryLoaded: true,
          summaryError: false,
        });
        navigationChanged = true;
      }
    } catch {
      const current = state.results.get(result.candidate.path);
      if (current?.resultRef === result.resultRef && current.resultMode === requestedMode) {
        state.results.set(result.candidate.path, {
          ...current,
          summaryError: true,
        });
        navigationChanged = true;
      }
    } finally {
      summaryLoadingRefs.delete(loadingKey);
      if (navigationChanged) bridge.renderWorkbookNavigation();
    }
  }

  async function loadAvailableSummaries() {
    const queue = [...state.results.values()].filter((result) => (
      result.resultRef && !result.resultLoaded && !result.summaryLoaded
    ));
    let cursor = 0;
    async function worker() {
      while (cursor < queue.length) {
        const result = queue[cursor];
        cursor += 1;
        await loadResultSummary(result);
      }
    }
    const workerCount = Math.min(4, queue.length);
    await Promise.all(Array.from({ length: workerCount }, worker));
  }

  async function loadResult(result) {
    const requestedMode = activeResultMode();
    const loadingKey = result?.resultRef + ":" + requestedMode;
    if (!result?.resultRef || result.resultLoaded || loadingRefs.has(loadingKey)) return;
    loadingRefs.add(loadingKey);
    try {
      const payload = await bridge.request(resultPath(result, requestedMode));
      const mapped = globalThis.M2DiffMapper.mapDiffPayload(payload, result.candidate);
      state.results.set(result.candidate.path, {
        ...mapped,
        itemId: result.itemId,
        itemStatus: result.itemStatus,
        resultRef: result.resultRef,
        resultLoaded: true,
        summaryLoaded: true,
        summaryError: false,
        resultMode: requestedMode,
      });
    } catch (error) {
      state.results.set(result.candidate.path, {
        ...result,
        state: "diff_error",
        error: bridge.errorMessage(error),
        errors: [],
      });
    } finally {
      loadingRefs.delete(loadingKey);
      if (state.selectedPath === result.candidate.path) bridge.selectWorkbook(result.candidate.path);
      else bridge.renderWorkbookNavigation();
    }
  }

  async function cancelTask() {
    if (commandBusy || !state.context?.batchTaskId) return;
    commandBusy = true;
    $("cancel-batch-task").disabled = true;
    try {
      const task = await bridge.request(
        "/api/diff/batches/" + encodeURIComponent(state.context.batchTaskId) + "/cancel",
        {
          method: "POST",
          body: JSON.stringify({
            schema_version: "m2.batch-cancel.request.v1",
            request_id: requestId(),
            reason: "用户从差异结果页取消",
          }),
        },
      );
      syncTask(task);
      schedulePoll(150);
    } catch (error) {
      $("batch-task-error").textContent = bridge.errorMessage(error);
      $("batch-task-error").classList.remove("hidden");
    } finally {
      commandBusy = false;
    }
  }

  async function retryTask() {
    if (commandBusy || !state.context?.batchTaskId) return;
    commandBusy = true;
    $("retry-batch-task").disabled = true;
    try {
      const task = await bridge.request(
        "/api/diff/batches/" + encodeURIComponent(state.context.batchTaskId) + "/retry",
        {
          method: "POST",
          body: JSON.stringify({
            schema_version: "m2.batch-retry.request.v1",
            request_id: requestId(),
          }),
        },
      );
      state.selectedPath = "";
      state.results = new Map();
      syncTask(task);
      schedulePoll(150);
    } catch (error) {
      $("batch-task-error").textContent = bridge.errorMessage(error);
      $("batch-task-error").classList.remove("hidden");
    } finally {
      commandBusy = false;
    }
  }

  globalThis.ExcelDiffBatchRuntime = Object.freeze({ loadResult, refreshTask, syncTask });
  $("cancel-batch-task")?.addEventListener("click", cancelTask);
  $("retry-batch-task")?.addEventListener("click", retryTask);
  window.addEventListener("beforeunload", () => window.clearTimeout(pollTimer));

  const demoPage = document.body.dataset.demoMode === "true";
  if (!demoPage && state.context?.mode === "formal" && state.context.batchTaskId) {
    $("batch-task-panel").classList.remove("hidden");
    void refreshTask();
  }
})();
