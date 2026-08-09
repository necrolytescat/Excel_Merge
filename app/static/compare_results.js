(() => {
  const TASK_CONTEXT_KEY = "excelDiffTaskContext";
  const REVIEW_STATE_KEY_PREFIX = "excelDiffConfirmedWorkbooks:";
  const FILE_STATUS = {
    modified: "内容变化",
    left_only: "仅左侧",
    right_only: "仅右侧",
    read_error: "读取失败",
  };

  const state = {
    context: null,
    results: new Map(),
    selectedPath: "",
    selectedSheet: null,
    showUnchanged: false,
    showConfirmed: false,
    showAllSheets: false,
    confirmedPaths: new Set(),
    reviewScope: "",
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

  function workbookDisplayName(path) {
    return fileName(path).replace(/\.(?:xlsm|xlsx)$/i, "");
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

  function resetDetail() {
    const result = state.results.get(state.selectedPath);
    if (result) $("workbench-caption").textContent = workbookCaption(result);
  }

  function renderEmptySheetNavigation(detail = "当前工作簿没有可用的 Sheet 结果。", result = null) {
    syncSheetFilterControls(result);
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

  function selectedFieldCaption(field) {
    const result = state.results.get(state.selectedPath);
    if (!result) return "";
    const parts = [workbookCaption(result)];
    if (!field) return parts[0];
    if (state.selectedSheet?.label) parts.push(state.selectedSheet.label);
    const sides = [];
    if (field.sourceRowNumber) sides.push("左侧第 " + field.sourceRowNumber + " 行");
    if (field.targetRowNumber) sides.push("右侧第 " + field.targetRowNumber + " 行");
    if (sides.length) parts.push(sides.join(" / "));
    return parts.join(" · ");
  }
  function updateDetail(field, button) {
    document.querySelectorAll(".field-diff-button.is-selected").forEach((current) => current.classList.remove("is-selected"));
    if (!field) {
      resetDetail();
      return;
    }
    button?.classList.add("is-selected");
    $("workbench-caption").textContent = selectedFieldCaption(field);
  }

  function sheetMetrics(sheet) {
    if (sheet.status === "failed" || !sheet.summary) return null;
    const addedFields = (sheet.rows || []).reduce(
      (count, row) => count + (row.status === "target_only" ? (row.fields || []).length : 0),
      0,
    );
    return {
      modified: Number(sheet.summary.modified_fields || 0) + addedFields,
      deleted: Number(sheet.summary.source_only_rows || 0),
    };
  }

  function visibleSheetResults(result) {
    return (result?.sheets || []).filter((sheet) => state.showAllSheets || sheet.status !== "unchanged");
  }

  function syncSheetFilterControls(result) {
    const sheets = result?.sheets || [];
    const visible = visibleSheetResults(result);
    const enabled = sheets.length > 0;
    const modifiedButton = $("show-modified-sheets");
    const allButton = $("show-all-sheets");
    modifiedButton.disabled = !enabled;
    allButton.disabled = !enabled;
    modifiedButton.setAttribute("aria-pressed", String(!state.showAllSheets));
    allButton.setAttribute("aria-pressed", String(state.showAllSheets));
    modifiedButton.classList.toggle("is-selected", !state.showAllSheets);
    allButton.classList.toggle("is-selected", state.showAllSheets);
    $("sheet-count").textContent = visible.length + " / " + sheets.length;
  }

  function preferredVisibleSheet(result) {
    const sheets = visibleSheetResults(result);
    return sheets.find((sheet) => sheet.status !== "failed")
      || sheets[0]
      || null;
  }
  function renderSheetNavigation(result, activeSheetId) {
    syncSheetFilterControls(result);
    const navigation = $("sheet-navigation");
    navigation.className = "sheet-list";
    navigation.textContent = "";
    visibleSheetResults(result).forEach((sheet) => {
      const button = document.createElement("button");
      button.type = "button";
      button.className = "sheet-nav-item" + (sheet.id === activeSheetId ? " is-selected" : "");
      button.setAttribute("aria-pressed", String(sheet.id === activeSheetId));
      const name = document.createElement("strong");
      name.textContent = sheet.label;
      const meta = document.createElement("span");
      meta.className = "sheet-status";
      const metrics = sheetMetrics(sheet);
      if (!metrics) {
        meta.classList.add("is-failed");
        meta.textContent = "失败";
      } else {
        const modified = document.createElement("span");
        modified.className = "is-modified";
        modified.textContent = "+" + metrics.modified;
        meta.append(modified);
        if (metrics.deleted > 0) {
          const separator = document.createElement("span");
          separator.className = "is-separator";
          separator.textContent = "/";
          const deleted = document.createElement("span");
          deleted.className = "is-deleted";
          deleted.textContent = "-" + metrics.deleted;
          meta.append(separator, deleted);
        }
      }
      button.append(name, meta);
      button.addEventListener("click", () => renderSheet(result, sheet.id));
      navigation.appendChild(button);
    });
  }

  function renderSheet(result, sheetId) {
    const visible = visibleSheetResults(result);
    const sheet = visible.find((item) => item.id === sheetId) || preferredVisibleSheet(result);
    const tableBody = $("semantic-table-body");
    tableBody.textContent = "";
    if (!sheet) {
      state.selectedSheet = null;
      renderEmptySheetNavigation("当前筛选没有可显示的 Sheet。", result);
      const empty = document.createElement("div");
      empty.className = "semantic-table-empty";
      empty.textContent = "切换到“显示全部”查看无变化 Sheet。";
      tableBody.appendChild(empty);
      resetDetail();
      return;
    }
    state.selectedSheet = sheet;
    renderSheetNavigation(result, sheet.id);
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
      resetDetail();
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
  function setSheetFilterMode(showAll) {
    if (state.showAllSheets === showAll) return;
    state.showAllSheets = showAll;
    const result = state.results.get(state.selectedPath);
    if (!result?.sheets?.length) {
      syncSheetFilterControls(null);
      return;
    }
    renderSheet(result, state.selectedSheet?.id);
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

  function workbookRowMetrics(result) {
    if (!result.summary) return null;
    const modified = Number(result.summary.modified_rows || 0);
    const added = Number(result.summary.target_only_rows || 0);
    const deleted = Number(result.summary.source_only_rows || 0);
    return {
      changed: modified + added + deleted,
      deleted,
    };
  }

  function metricValue(value, sign) {
    return sign + value;
  }

  function workbookCardStatus(result) {
    if (result.itemStatus === "business_failed") {
      return result.diffStatus === "partial" ? "部分完成" : "执行失败";
    }
    if (result.itemStatus === "orchestration_failed") return "编排失败";
    if (result.itemStatus === "cancelled") return "已取消";
    if (result.itemStatus === "skipped") return "已跳过";
    if (result.summaryError) return "统计不可用";
    if (result.state === "diff_loading") return "处理中";
    if (result.state === "diff_pending" && !result.summary) return "统计读取中";
    if (result.state === "diff_unavailable") return "未执行";
    if (result.state === "diff_error") return result.partial ? "部分完成" : "执行失败";
    return "";
  }

  function isUnchangedResult(result) {
    if (!result) return false;
    if (result.itemStatus) {
      return result.itemStatus === "succeeded"
        && (result.diffStatus === "unchanged" || result.state === "diff_empty");
    }
    return result.state === "diff_empty";
  }

  function currentReviewScope() {
    const context = state.context;
    if (!context) return "";
    if (context.mode === "formal" && context.batchTaskId) {
      return "formal:" + context.batchTaskId;
    }
    if (context.mode === "replay" && context.fixtureId) {
      return "replay:" + context.fixtureId + ":" + (context.replayResultMode || "golden");
    }
    if (context.mode === "demo") {
      return "demo:" + (context.capturedAt || "current");
    }
    return "";
  }

  function reviewStateStorageKey() {
    const scope = currentReviewScope();
    return scope ? REVIEW_STATE_KEY_PREFIX + scope : "";
  }

  function syncConfirmedPaths() {
    const scope = currentReviewScope();
    if (scope === state.reviewScope) return;
    state.reviewScope = scope;
    state.showConfirmed = false;
    state.confirmedPaths = new Set();
    const storageKey = reviewStateStorageKey();
    if (!storageKey) return;
    try {
      const stored = JSON.parse(sessionStorage.getItem(storageKey) || "[]");
      if (Array.isArray(stored)) {
        state.confirmedPaths = new Set(stored.filter((path) => typeof path === "string"));
      }
    } catch {
      sessionStorage.removeItem(storageKey);
    }
  }

  function persistConfirmedPaths() {
    const storageKey = reviewStateStorageKey();
    if (!storageKey) return;
    sessionStorage.setItem(storageKey, JSON.stringify([...state.confirmedPaths]));
  }

  function isConfirmableResult(result) {
    if (!result || result.partial || !result.summary) return false;
    if (result.itemStatus) return result.itemStatus === "succeeded";
    return result.state === "diff_ready" || result.state === "diff_empty";
  }

  function isConfirmedResult(result) {
    return Boolean(result?.candidate?.path && state.confirmedPaths.has(result.candidate.path));
  }

  function visibleWorkbookResults(allResults = [...state.results.values()]) {
    return allResults.filter((result) => (
      (state.showUnchanged || !isUnchangedResult(result))
      && (state.showConfirmed || !isConfirmedResult(result))
    ));
  }

  function setWorkbookConfirmed(path, confirmed, { render = true } = {}) {
    syncConfirmedPaths();
    const result = state.results.get(path);
    if (!result || (confirmed && !isConfirmableResult(result))) return;
    if (confirmed) state.confirmedPaths.add(path);
    else state.confirmedPaths.delete(path);
    persistConfirmedPaths();

    if (!render) return;
    if (confirmed && !state.showConfirmed && state.selectedPath === path) {
      const allResults = [...state.results.values()];
      const currentIndex = allResults.findIndex((item) => item.candidate.path === path);
      const visibleResults = visibleWorkbookResults(allResults);
      const replacement = visibleResults.find((item) => (
        allResults.indexOf(item) > currentIndex
      )) || visibleResults[visibleResults.length - 1];
      if (replacement) {
        selectWorkbook(replacement.candidate.path);
        return;
      }
    }
    renderWorkbookNavigation();
  }

  function clearWorkbookConfirmations(path = "") {
    syncConfirmedPaths();
    if (path) state.confirmedPaths.delete(path);
    else state.confirmedPaths.clear();
    persistConfirmedPaths();
    renderWorkbookNavigation();
  }

  function renderHiddenWorkbooksEmpty(navigation, unchangedCount, confirmedCount) {
    navigation.className = "workbook-empty";
    const icon = document.createElement("span");
    icon.setAttribute("aria-hidden", "true");
    icon.textContent = "▤";
    const title = document.createElement("strong");
    title.textContent = "当前筛选已隐藏所有工作簿";
    const reasons = [];
    if (!state.showUnchanged && unchangedCount) reasons.push(unchangedCount + " 个无变化");
    if (!state.showConfirmed && confirmedCount) reasons.push(confirmedCount + " 个已确认");
    const detail = document.createElement("small");
    detail.textContent = "使用标题栏按钮显示" + reasons.join("、") + "工作簿。";
    navigation.append(icon, title, detail);
  }

  function renderWorkbookNavigation() {
    syncConfirmedPaths();
    const navigation = $("workbook-navigation");
    const allResults = [...state.results.values()];
    const unchangedCount = allResults.filter(isUnchangedResult).length;
    const confirmedCount = allResults.filter(isConfirmedResult).length;
    if (confirmedCount === 0) state.showConfirmed = false;
    const results = visibleWorkbookResults(allResults);
    const unchangedToggle = $("toggle-unchanged-workbooks");
    unchangedToggle.disabled = unchangedCount === 0;
    unchangedToggle.setAttribute("aria-pressed", String(state.showUnchanged));
    unchangedToggle.textContent = (state.showUnchanged ? "隐藏无变化 " : "显示无变化 ") + unchangedCount;
    const confirmedToggle = $("toggle-confirmed-workbooks");
    confirmedToggle.disabled = confirmedCount === 0;
    confirmedToggle.setAttribute("aria-pressed", String(state.showConfirmed));
    confirmedToggle.textContent = (state.showConfirmed ? "隐藏已确认 " : "显示已确认 ") + confirmedCount;
    navigation.className = "workbook-list";
    navigation.textContent = "";
    $("workbook-count").textContent = results.length + " / " + allResults.length;
    if (!results.length && allResults.length) {
      renderHiddenWorkbooksEmpty(navigation, unchangedCount, confirmedCount);
      return;
    }
    results.forEach((result) => {
      const path = result.candidate.path;
      const confirmed = isConfirmedResult(result);
      const confirmable = isConfirmableResult(result);
      const item = document.createElement("div");
      item.className = "workbook-nav-item is-" + result.state
        + (path === state.selectedPath ? " is-selected" : "")
        + (confirmed ? " is-confirmed" : "");
      const selectButton = document.createElement("button");
      selectButton.type = "button";
      selectButton.className = "workbook-nav-select";
      selectButton.setAttribute("aria-pressed", String(path === state.selectedPath));
      const name = document.createElement("strong");
      name.textContent = workbookDisplayName(path);
      const metrics = workbookRowMetrics(result);
      const rowSummary = document.createElement("div");
      rowSummary.className = "workbook-row-summary";
      const modified = document.createElement("span");
      modified.className = "is-modified";
      modified.textContent = metrics ? metricValue(metrics.changed, "+") : "—";
      const deleted = document.createElement("span");
      deleted.className = "is-deleted";
      deleted.textContent = metrics ? metricValue(metrics.deleted, "-") : "—";
      rowSummary.append(modified, deleted);
      const statusText = workbookCardStatus(result);
      selectButton.setAttribute(
        "aria-label",
        name.textContent + "，变化行 " + modified.textContent + "，删除行 " + deleted.textContent
          + (statusText ? "，" + statusText : ""),
      );
      selectButton.append(name, rowSummary);
      if (statusText) {
        const status = document.createElement("span");
        status.className = "workbook-result-status";
        status.textContent = statusText;
        selectButton.appendChild(status);
      }
      selectButton.addEventListener("click", () => selectWorkbook(path));

      const confirmControl = document.createElement("label");
      confirmControl.className = "workbook-confirm-control" + (confirmable ? "" : " is-disabled");
      confirmControl.title = confirmable ? "标记为已确认" : "当前结果不可确认";
      const confirmation = document.createElement("input");
      confirmation.type = "checkbox";
      confirmation.checked = confirmed;
      confirmation.disabled = !confirmable;
      confirmation.setAttribute("aria-label", (confirmed ? "取消确认 " : "确认 ") + name.textContent + " 差异");
      confirmation.addEventListener("change", () => setWorkbookConfirmed(path, confirmation.checked));
      confirmControl.appendChild(confirmation);
      item.append(selectButton, confirmControl);
      navigation.appendChild(item);
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

  function workbookCaption(result) {
    return result.workbook?.name || fileName(result.candidate.path);
  }

  function selectWorkbook(path) {
    syncConfirmedPaths();
    let result = state.results.get(path);
    if (!result) return;
    const visibleResults = visibleWorkbookResults();
    if (!visibleResults.some((item) => item.candidate.path === path)) {
      result = visibleResults[0] || result;
      path = result.candidate.path;
    }
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
    } else if (result.state === "diff_error") {
      $("sheet-count").textContent = "0";
      renderEmptySheetNavigation("该工作簿执行失败，没有可用 Sheet 结果。");
      setDiffState("diff_error", result.error || "工作簿差异比对失败。");
      $("workbench-caption").textContent = fileName(path) + " 执行失败，结果未降级为空差异。";
    } else if (result.state === "diff_empty") {
      $("sheet-count").textContent = "0";
      renderEmptySheetNavigation("该工作簿已完成且没有语义差异。");
      setDiffState("diff_empty");
      $("workbench-caption").textContent = workbookCaption(result) + " · 无语义差异";
    } else {
      setDiffState("diff_ready");
      renderSheet(result, result.sheets[0]?.id);
      $("diff-state-badge").textContent = resultFieldCount(result) + " 个修改字段";
    }
    $("result-action-message").textContent = "";
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
    setWorkbookConfirmed(current.candidate.path, false, { render: false });
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

  function syncWorkbookSidebarVisibility() {
    const visible = !$("diff-workbench").classList.contains("hidden");
    $("workbook-sidebar").classList.toggle("hidden", !visible);
    $("result-page-body").classList.toggle("has-workbook-sidebar", visible);
  }

  function applyWorkbookVisibilityFilter() {
    const selected = state.results.get(state.selectedPath);
    const visibleResults = visibleWorkbookResults();
    if (selected && !visibleResults.some((result) => result.candidate.path === state.selectedPath)) {
      const replacement = visibleResults[0];
      if (replacement) {
        selectWorkbook(replacement.candidate.path);
        return;
      }
    }
    renderWorkbookNavigation();
  }

  function toggleUnchangedWorkbooks() {
    state.showUnchanged = !state.showUnchanged;
    applyWorkbookVisibilityFilter();
  }

  function toggleConfirmedWorkbooks() {
    state.showConfirmed = !state.showConfirmed;
    applyWorkbookVisibilityFilter();
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
  $("show-modified-sheets").addEventListener("click", () => setSheetFilterMode(false));
  $("show-all-sheets").addEventListener("click", () => setSheetFilterMode(true));
  $("toggle-unchanged-workbooks").addEventListener("click", toggleUnchangedWorkbooks);
  $("toggle-confirmed-workbooks").addEventListener("click", toggleConfirmedWorkbooks);
  new MutationObserver(syncWorkbookSidebarVisibility).observe($("diff-workbench"), {
    attributes: true,
    attributeFilter: ["class"],
  });
  syncWorkbookSidebarVisibility();
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
    clearWorkbookConfirmations,
    renderTaskContext,
    showMissingContext,
    setDiffState,
  });
  loadContext();
})();
