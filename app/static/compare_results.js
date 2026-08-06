(() => {
  const TASK_CONTEXT_KEY = "excelDiffTaskContext";
  const FILE_STATUS = {
    modified: "内容变化",
    left_only: "仅左侧",
    right_only: "仅右侧",
    read_error: "读取失败",
  };
  const RESULT_LABELS = {
    diff_unavailable: "未执行",
    diff_loading: "处理中",
    diff_pending: "已完成",
    diff_ready: "有差异",
    diff_empty: "无差异",
    diff_error: "执行失败",
  };
  const FIELD_STATUS = {
    common: "两侧共有",
    modified: "值已修改",
    source_only: "仅左侧",
    target_only: "仅右侧",
  };

  const state = {
    context: null,
    results: new Map(),
    selectedPath: "",
    selectedSheet: null,
    busy: false,
  };
  const $ = (id) => document.getElementById(id);

  async function request(path, options = {}) {
    const response = await fetch(path, {
      headers: { "Content-Type": "application/json", ...(options.headers || {}) },
      ...options,
    });
    const body = await response.json().catch(() => ({}));
    if (!response.ok) throw body;
    return body;
  }

  function errorMessage(error) {
    const payload = error && error.error ? error.error : error;
    const message = payload?.message || "请求失败";
    return (payload?.code || "REQUEST_FAILED") + "：" + message;
  }

  function fileName(path) {
    return String(path || "").replace(/\\/g, "/").split("/").pop() || "—";
  }

  function revisionText(value) {
    return value ? "冻结 r" + value : "无冻结 Revision";
  }

  function contextSource(side, file) {
    if (!file) return (side === "source" ? "左侧" : "右侧") + "不存在该文件";
    const endpoint = state.context?.[side];
    return (endpoint?.label || (side === "source" ? "左侧" : "右侧")) + " · " + revisionText(endpoint?.resolvedRevision);
  }

  function sheetDiffCount(sheet) {
    if (sheet.summary && Number.isFinite(Number(sheet.summary.modified_fields))) {
      return Number(sheet.summary.modified_fields);
    }
    return (sheet.rows || []).reduce(
      (count, row) => count + (row.fields || []).length,
      0,
    );
  }

  function sheetChangedRows(sheet) {
    if (!sheet.summary) return (sheet.rows || []).length;
    return Number(sheet.summary.modified_rows || 0)
      + Number(sheet.summary.source_only_rows || 0)
      + Number(sheet.summary.target_only_rows || 0);
  }

  function resultFieldCount(result) {
    if (result.summary && Number.isFinite(Number(result.summary.modified_fields))) {
      return Number(result.summary.modified_fields);
    }
    return (result.sheets || []).reduce(
      (count, sheet) => count + sheetDiffCount(sheet),
      0,
    );
  }

  function setDiffState(nextState, detail = "", showData = false) {
    const workbench = $("diff-workbench");
    workbench.dataset.diffState = nextState;
    const visibleState = nextState === "diff_error" && showData ? "diff_ready" : nextState;
    workbench.querySelectorAll(".diff-state-view").forEach((view) => {
      view.classList.toggle("is-active", view.dataset.state === visibleState);
    });
    const badges = {
      diff_unavailable: "未执行",
      diff_loading: "比对中",
      diff_empty: "已完成 · 无差异",
      diff_error: showData ? "部分完成" : "执行失败",
      diff_ready: "结果已就绪",
    };
    $("diff-state-badge").textContent = badges[nextState] || "未知状态";
    if (nextState === "diff_unavailable" && detail) $("diff-unavailable-detail").textContent = detail;
    if (nextState === "diff_loading" && detail) $("diff-loading-detail").textContent = detail;
    if (nextState === "diff_error" && detail) $("diff-error-detail").textContent = detail;
  }

  function resetDetail(caption = "选择字段差异后显示详情。") {
    $("detail-state").textContent = "未选择";
    $("detail-primary-key").textContent = state.selectedSheet?.primaryKey || "—";
    $("detail-field").textContent = "—";
    $("detail-field-status").textContent = "—";
    $("detail-source-value").textContent = "—";
    $("detail-target-value").textContent = "—";
    $("detail-source-definition").textContent = "—";
    $("detail-target-definition").textContent = "—";
    $("detail-location").textContent = "—";
    $("detail-caption").textContent = caption;
  }

  function renderEmptySheetNavigation(detail = "当前工作簿没有可用的 Sheet 结果。") {
    const navigation = $("sheet-navigation");
    navigation.className = "sheet-empty";
    navigation.textContent = "";
    const icon = document.createElement("span");
    icon.setAttribute("aria-hidden", "true");
    icon.textContent = "▦";
    const title = document.createElement("strong");
    title.textContent = "暂无 Sheet 数据";
    const copy = document.createElement("small");
    copy.textContent = detail;
    navigation.append(icon, title, copy);
  }

  function sideValue(field, side) {
    if (side === "source") return field.sourceValue ?? field.oldValue ?? "—";
    return field.targetValue ?? field.newValue ?? "—";
  }

  function definitionText(definition, side) {
    if (!definition) return "未提供字段定义";
    const type = definition[side + "_type"] || "未声明类型";
    const scope = definition[side + "_scope"] || "未声明范围";
    return type + " · " + scope;
  }

  function updateDetail(field, button) {
    document.querySelectorAll(".field-diff-button.is-selected").forEach((current) => current.classList.remove("is-selected"));
    if (!field) {
      resetDetail("当前行没有可选择的字段值。");
      return;
    }
    button?.classList.add("is-selected");
    $("detail-state").textContent = state.context.mode === "demo" ? "UI 示例" : "已选择";
    $("detail-primary-key").textContent = state.selectedSheet?.primaryKey || "—";
    $("detail-field").textContent = field.name;
    $("detail-field-status").textContent = FIELD_STATUS[field.status] || field.status || "—";
    $("detail-source-value").textContent = sideValue(field, "source");
    $("detail-target-value").textContent = sideValue(field, "target");
    $("detail-source-definition").textContent = definitionText(field.definition, "source");
    $("detail-target-definition").textContent = definitionText(field.definition, "target");
    $("detail-location").textContent = field.location || "—";
    $("detail-caption").textContent = state.context.mode === "demo"
      ? "当前内容来自开发模式 UI 假数据，不代表实际工作簿结果。"
      : "定位使用 Sheet、字段和左右 CSV 逻辑行号。";
  }

  function sheetMeta(sheet) {
    if (sheet.status === "failed") return "失败";
    if (sheet.status === "unchanged") return "无差异";
    const rows = sheetChangedRows(sheet);
    const fields = sheetDiffCount(sheet);
    return rows + " 行 · " + fields + " 修改字段";
  }

  function renderSheetNavigation(result, activeSheetId) {
    const navigation = $("sheet-navigation");
    navigation.className = "sheet-list";
    navigation.textContent = "";
    result.sheets.forEach((sheet) => {
      const button = document.createElement("button");
      button.type = "button";
      button.className = "sheet-nav-item" + (sheet.id === activeSheetId ? " is-selected" : "");
      button.setAttribute("aria-pressed", String(sheet.id === activeSheetId));
      const name = document.createElement("strong");
      name.textContent = sheet.label;
      const meta = document.createElement("span");
      meta.className = "sheet-status" + (sheet.status !== "unchanged" ? " is-changed" : "");
      meta.textContent = sheetMeta(sheet);
      button.append(name, meta);
      button.addEventListener("click", () => renderSheet(result, sheet.id));
      navigation.appendChild(button);
    });
  }

  function renderSheet(result, sheetId) {
    const sheet = result.sheets.find((item) => item.id === sheetId) || result.sheets[0];
    if (!sheet) return;
    state.selectedSheet = sheet;
    renderSheetNavigation(result, sheet.id);
    $("sheet-count").textContent = String(result.sheets.length);
    const tableBody = $("semantic-table-body");
    tableBody.textContent = "";
    if (!sheet.rows.length) {
      const empty = document.createElement("div");
      empty.className = "semantic-table-empty";
      if (sheet.status === "failed") {
        empty.textContent = "该 Sheet 执行失败；结构化错误保留在当前工作簿状态中。";
      } else {
        empty.textContent = state.context.mode === "demo"
          ? "UI 示例：该 Sheet 已完成且没有差异。"
          : "该 Sheet 没有行级差异。";
      }
      tableBody.appendChild(empty);
      resetDetail("当前 Sheet 没有可选择的字段差异。");
      return;
    }

    const changeLabels = { modified: "修改", added: "右侧新增", deleted: "左侧删除" };
    let firstField = null;
    let firstButton = null;
    sheet.rows.forEach((row) => {
      const rowElement = document.createElement("div");
      rowElement.className = "semantic-diff-row";
      const key = document.createElement("div");
      key.className = "semantic-key";
      const keyValue = document.createElement("strong");
      keyValue.textContent = row.key;
      const keyLabel = document.createElement("small");
      keyLabel.textContent = row.label;
      key.append(keyValue, keyLabel);
      const status = document.createElement("span");
      status.className = "row-change-status is-" + row.change;
      status.textContent = changeLabels[row.change] || row.change;
      const fields = document.createElement("div");
      fields.className = "field-diff-list";
      (row.fields || []).forEach((field) => {
        const button = document.createElement("button");
        button.type = "button";
        button.className = "field-diff-button";
        button.setAttribute(
          "aria-label",
          row.key + " " + field.name + "：左侧 " + sideValue(field, "source") + "，右侧 " + sideValue(field, "target"),
        );
        const name = document.createElement("strong");
        name.textContent = field.name;
        const values = document.createElement("span");
        values.textContent = sideValue(field, "source") + " → " + sideValue(field, "target");
        button.append(name, values);
        button.addEventListener("click", () => updateDetail(field, button));
        fields.appendChild(button);
        if (!firstField) {
          firstField = field;
          firstButton = button;
        }
      });
      if (!row.fields?.length) {
        const empty = document.createElement("span");
        empty.className = "field-diff-empty";
        empty.textContent = "该行没有可展示的字段值";
        fields.appendChild(empty);
      }
      rowElement.append(key, status, fields);
      tableBody.appendChild(rowElement);
    });
    updateDetail(firstField, firstButton);
  }

  function appendErrors(list, errors) {
    list.textContent = "";
    (errors || []).forEach((error) => {
      const item = document.createElement("li");
      const scope = error.sheet_name ? " · " + error.sheet_name : "";
      const side = error.side ? " · " + error.side : "";
      item.textContent = error.code + scope + side + "：" + error.message;
      list.appendChild(item);
    });
  }

  function renderErrors(result) {
    const partial = Boolean(result.partial && result.sheets.length);
    $("diff-partial-warning").classList.toggle("hidden", !partial);
    appendErrors($("diff-partial-errors"), partial ? result.errors : []);
    appendErrors($("diff-error-list"), partial ? [] : result.errors);
  }

  function baseWorkbookMeta(result) {
    if (!result.resultLoaded && result.itemStatus === "business_failed") {
      const label = result.diffStatus === "partial" ? "部分完成" : "失败";
      return label + " · " + Number(result.diffErrorCount || 0) + " 个错误";
    }
    if (result.state === "diff_pending") {
      return result.diffStatus === "unchanged" ? "已完成 · 无差异" : "已完成 · 结果可读";
    }
    if (result.partial) return "部分完成 · " + resultFieldCount(result) + " 个修改字段";
    if (result.state === "diff_ready") return resultFieldCount(result) + " 个修改字段";
    if (result.state === "diff_loading") return "处理中";
    return RESULT_LABELS[result.state];
  }

  function workbookMeta(result) {
    const base = baseWorkbookMeta(result);
    if (state.context?.mode !== "replay" || state.context.replayResultMode !== "current") {
      return base;
    }
    const comparison = state.context.replayComparisons?.[result.itemId];
    if (!comparison?.available) return base + " · 未重算";
    return base + (comparison.matches_golden
      ? " · 与黄金一致"
      : " · 与黄金不一致");
  }
  function renderWorkbookNavigation() {
    const navigation = $("workbook-navigation");
    const results = [...state.results.values()];
    navigation.className = "workbook-list";
    navigation.textContent = "";
    $("workbook-count").textContent = String(results.length);
    results.forEach((result) => {
      const button = document.createElement("button");
      button.type = "button";
      button.className = "workbook-nav-item is-" + result.state
        + (result.candidate.path === state.selectedPath ? " is-selected" : "");
      button.setAttribute("aria-pressed", String(result.candidate.path === state.selectedPath));
      const name = document.createElement("strong");
      name.textContent = fileName(result.candidate.path);
      const path = document.createElement("small");
      path.textContent = result.candidate.path;
      const meta = document.createElement("span");
      meta.textContent = workbookMeta(result);
      button.append(name, path, meta);
      button.addEventListener("click", () => selectWorkbook(result.candidate.path));
      navigation.appendChild(button);
    });
  }

  function renderFileContext(result) {
    const candidate = result.candidate;
    $("context-old-file").textContent = candidate.sourceFile ? fileName(candidate.sourceFile.path || candidate.path) : "文件不存在";
    $("context-new-file").textContent = candidate.targetFile ? fileName(candidate.targetFile.path || candidate.path) : "文件不存在";
    $("context-old-source").textContent = contextSource("source", candidate.sourceFile);
    $("context-new-source").textContent = contextSource("target", candidate.targetFile);
    $("context-path").textContent = candidate.path;
    $("context-file-status").textContent = FILE_STATUS[candidate.status] || "文件候选";
  }

  function nonComparableDetail(candidate) {
    const detail = {
      left_only: "仅左侧存在，首轮不执行语义 Diff。",
      right_only: "仅右侧存在，首轮不执行语义 Diff。",
      read_error: "快照读取失败，不能执行语义 Diff。",
    };
    return detail[candidate.status] || "当前候选不支持单工作簿语义 Diff。";
  }

  function summaryCaption(result) {
    const summary = result.summary || {};
    return (result.workbook?.name || fileName(result.candidate.path))
      + " · " + Number(summary.total_sheets || result.sheets.length) + " 个 Sheet"
      + " · " + Number(summary.modified_rows || 0) + " 个修改行"
      + " · " + Number(summary.modified_fields || 0) + " 个修改字段";
  }

  function selectWorkbook(path) {
    const result = state.results.get(path);
    if (!result) return;
    state.selectedPath = path;
    state.selectedSheet = null;
    renderWorkbookNavigation();
    renderFileContext(result);
    renderErrors(result);
    resetDetail();
    const comparable = result.candidate.status === "modified";
    const pending = result.state === "diff_loading" || result.state === "diff_pending";
    $("compare-current-workbook").disabled = state.busy || !comparable || pending;
    $("compare-current-workbook").textContent = state.context?.mode === "replay"
      ? "重算当前工作簿"
      : (result.state === "diff_unavailable"
        ? "比对当前工作簿" : "重新比对当前工作簿");

    if (!comparable) {
      $("sheet-count").textContent = "0";
      renderEmptySheetNavigation("当前文件级状态不适用语义 Diff。");
      const detail = nonComparableDetail(result.candidate);
      setDiffState("diff_unavailable", detail);
      $("workbench-caption").textContent = fileName(path) + " · " + detail;
    } else if (result.state === "diff_loading") {
      $("sheet-count").textContent = "0";
      renderEmptySheetNavigation("批量任务正在处理或读取该工作簿结果。");
      setDiffState("diff_loading", result.error || "等待批量工作簿结果。");
      $("workbench-caption").textContent = fileName(path) + " · " + (result.error || "处理中");
    } else if (result.state === "diff_pending") {
      $("sheet-count").textContent = "0";
      renderEmptySheetNavigation("批量处理已完成，正在读取该工作簿结果。");
      setDiffState("diff_loading", result.error || "批量处理已完成，正在读取结果。");
      $("workbench-caption").textContent = fileName(path) + " · 已完成，正在读取结果";
    } else if (result.state === "diff_unavailable") {
      $("sheet-count").textContent = "0";
      renderEmptySheetNavigation("执行当前工作簿后显示 Sheet 结果。");
      setDiffState("diff_unavailable", "已选择 " + fileName(path) + "，尚未执行语义 Diff。");
      $("workbench-caption").textContent = fileName(path) + " 已建立结果上下文，可执行单工作簿比对。";
    } else if (result.state === "diff_error" && result.partial && result.sheets.length) {
      setDiffState("diff_error", result.error, true);
      const firstAvailableSheet = result.sheets.find((sheet) => sheet.status !== "failed");
      renderSheet(result, firstAvailableSheet?.id || result.sheets[0]?.id);
      $("diff-state-badge").textContent = "部分完成 · " + resultFieldCount(result) + " 个修改字段";
      $("workbench-caption").textContent = summaryCaption(result) + " · 部分 Sheet 失败";
    } else if (result.state === "diff_error") {
      $("sheet-count").textContent = "0";
      renderEmptySheetNavigation("该工作簿执行失败，没有可用 Sheet 结果。");
      setDiffState("diff_error", result.error || "工作簿差异比对失败。");
      $("workbench-caption").textContent = fileName(path) + " 执行失败，结果未降级为空差异。";
    } else if (result.state === "diff_empty") {
      $("sheet-count").textContent = "0";
      renderEmptySheetNavigation("该工作簿已完成且没有语义差异。");
      setDiffState("diff_empty");
      $("workbench-caption").textContent = summaryCaption(result) + " · 无语义差异";
    } else {
      setDiffState("diff_ready");
      renderSheet(result, result.sheets[0]?.id);
      $("diff-state-badge").textContent = resultFieldCount(result) + " 个修改字段";
      $("workbench-caption").textContent = summaryCaption(result);
    }
    $("result-action-message").textContent = "当前工作簿：" + fileName(path);
    if (result.resultRef && !result.resultLoaded) {
      void globalThis.ExcelDiffBatchRuntime?.loadResult(result);
    }
  }

  function buildResults(context) {
    const fixtures = new Map((context.results || []).map((item) => [item.path, item]));
    return new Map(context.candidates.map((candidate) => {
      const fixture = fixtures.get(candidate.path);
      const demo = context.mode === "demo";
      return [candidate.path, {
        candidate,
        state: demo ? (fixture?.resultState || "diff_error") : "diff_unavailable",
        error: demo ? (fixture?.error || "") : "",
        sheets: demo ? (fixture?.sheets || []) : [],
        errors: [],
        summary: null,
        partial: false,
      }];
    }));
  }

  function renderTaskContext() {
    const context = state.context;
    $("result-source-label").textContent = context.source?.branch || context.source?.label || "左侧";
    $("result-target-label").textContent = context.target?.branch || context.target?.label || "右侧";
    $("result-workbook-total").textContent = String(context.candidates.length);
    if (context.mode === "demo") $("mock-result-notice").classList.remove("hidden");
  }

  function requestId() {
    if (globalThis.crypto?.randomUUID) return globalThis.crypto.randomUUID();
    return "00000000-0000-4000-8000-" + String(Date.now()).padStart(12, "0").slice(-12);
  }

  function formalRequestPayload(result) {
    const sourceRevision = Number(state.context.source?.resolvedRevision);
    const targetRevision = Number(state.context.target?.resolvedRevision);
    if (
      !state.context.source?.endpointId
      || !state.context.target?.endpointId
      || !Number.isInteger(sourceRevision)
      || !Number.isInteger(targetRevision)
    ) {
      throw new Error("任务上下文缺少端点 ID 或冻结 Revision");
    }
    return {
      schema_version: "m2.workbook-compare.request.v1",
      request_id: requestId(),
      source: {
        endpoint_id: state.context.source.endpointId,
        revision: sourceRevision,
      },
      target: {
        endpoint_id: state.context.target.endpointId,
        revision: targetRevision,
      },
      workbook_path: result.candidate.path,
    };
  }

  async function compareCurrentWorkbook() {
    const current = state.results.get(state.selectedPath);
    if (!current || state.busy || current.candidate.status !== "modified") return;
    if (state.context.mode === "demo") {
      state.busy = true;
      $("compare-current-workbook").disabled = true;
      $("result-action-message").textContent = "UI 示例：正在重新比对 " + fileName(current.candidate.path);
      setDiffState("diff_loading", "UI 示例：正在处理 " + fileName(current.candidate.path));
      await new Promise((resolve) => window.setTimeout(resolve, 220));
      state.busy = false;
      selectWorkbook(current.candidate.path);
      $("result-action-message").textContent = "UI 示例：" + fileName(current.candidate.path) + " 单工作簿比对完成。";
      return;
    }

    if (state.context.mode === "replay") {
      const runtime = globalThis.OfflineFixtureRuntime;
      if (!runtime) return;
      await runtime.recomputeItem(current);
      return;
    }
    state.busy = true;
    $("compare-current-workbook").disabled = true;
    $("result-action-message").textContent = "正在比对 " + fileName(current.candidate.path);
    setDiffState("diff_loading", "正在读取固定方向的左侧与右侧数据集。");
    let completionMessage = "";
    try {
      const payload = formalRequestPayload(current);
      const diffPayload = await request("/api/diff/workbooks/compare", {
        method: "POST",
        body: JSON.stringify(payload),
      });
      const mapped = globalThis.M2DiffMapper.mapDiffPayload(diffPayload, current.candidate);
      state.results.set(current.candidate.path, mapped);
      completionMessage = mapped.partial
        ? fileName(current.candidate.path) + " 部分完成；可用 Sheet 已保留。"
        : fileName(current.candidate.path) + " 单工作簿比对完成。";
    } catch (error) {
      const detail = errorMessage(error);
      state.results.set(current.candidate.path, {
        candidate: current.candidate,
        state: "diff_error",
        error: detail,
        errors: [],
        sheets: [],
        summary: null,
        partial: false,
      });
      completionMessage = detail + "；可重试当前工作簿。";
    } finally {
      state.busy = false;
      selectWorkbook(current.candidate.path);
      $("result-action-message").textContent = completionMessage;
    }
  }

  function showMissingContext() {
    $("diff-workbench").classList.add("hidden");
    $("results-missing").classList.remove("hidden");
    $("result-source-label").textContent = "—";
    $("result-target-label").textContent = "—";
    $("result-workbook-total").textContent = "0";
  }

  function loadContext() {
    let context = null;
    try {
      context = JSON.parse(sessionStorage.getItem(TASK_CONTEXT_KEY) || "null");
    } catch {
      sessionStorage.removeItem(TASK_CONTEXT_KEY);
    }
    const demoPage = document.body.dataset.demoMode === "true";
    if (document.body.dataset.replayMode === "true") {
      showMissingContext();
      return;
    }
    if ((!context?.candidates?.length && !context?.batchTaskId) || (demoPage && context.mode !== "demo") || (!demoPage && context.mode === "demo")) {
      showMissingContext();
      return;
    }
    state.context = context;
    state.results = buildResults(context);
    renderTaskContext();
    if (context.candidates.length) {
      $("diff-workbench").classList.remove("hidden");
      selectWorkbook(context.candidates[0].path);
    } else {
      showMissingContext();
    }
  }

  $("compare-current-workbook").addEventListener("click", compareCurrentWorkbook);
  $("toggle-detail").addEventListener("click", () => {
    const workbench = $("diff-workbench");
    const collapsed = workbench.classList.toggle("is-detail-collapsed");
    $("toggle-detail").setAttribute("aria-expanded", String(!collapsed));
  });
  globalThis.ExcelDiffResults = Object.freeze({
    selectWorkbook,
    compareCurrentWorkbook,
  });
  globalThis.ExcelDiffResultsBridge = Object.freeze({
    state,
    request,
    errorMessage,
    fileName,
    selectWorkbook,
    renderWorkbookNavigation,
    renderTaskContext,
    showMissingContext,
    setDiffState,
  });
  loadContext();
})();
