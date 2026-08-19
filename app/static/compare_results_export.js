(() => {
  "use strict";

  const host = globalThis.ExcelDiffResultsBridge;
  if (!host) return;

  const $ = (id) => document.getElementById(id);
  const drafts = new Map();
  let issueCache = { result: null, draft: null, version: -1, issues: [] };
  let exportMode = false;
  let busy = false;

  function currentResult() {
    return host.state.results.get(host.state.selectedPath);
  }

  function isReady(result = currentResult()) {
    const mode = host.state.context?.mode;
    return Boolean(
      result
      && result.resultRef
      && result.resultLoaded
      && result.sheets?.length
      && ["formal", "m4"].includes(mode),
    );
  }

  function exportEndpoint(result) {
    const prefix = host.state.context?.mode === "m4"
      ? "/api/diff-plans/run-results/"
      : "/api/diff/batch-results/";
    return prefix + encodeURIComponent(result.resultRef) + "/export";
  }

  function draftFor(path = host.state.selectedPath) {
    if (!path) return null;
    let draft = drafts.get(path);
    if (!draft) {
      draft = {
        targetLayout: "target",
        decisions: new Map(),
        serverIssues: [],
        version: 0,
        defaultsInitialized: false,
        batchNotice: "",
      };
      drafts.set(path, draft);
    }
    if (!draft.targetLayout) draft.targetLayout = "target";
    return draft;
  }

  function normalizedRows(sheet) {
    const primaryKey = sheet.primaryKey || "Id";
    return (sheet.rows || []).map((row) => host.normalizeSheetRow(row, primaryKey));
  }

  function decisionMap(draft, sheetId, create = false) {
    let decisions = draft?.decisions.get(sheetId);
    if (!decisions && create) {
      decisions = new Map();
      draft.decisions.set(sheetId, decisions);
    }
    return decisions;
  }

  function selectionCounts(draft = draftFor(), sheetId = "") {
    const maps = sheetId
      ? [draft?.decisions.get(sheetId)].filter(Boolean)
      : [...(draft?.decisions.values() || [])];
    const decisions = maps.flatMap((items) => [...items.values()]);
    const target = decisions.filter((item) => item.action === "write" && item.value_side === "target").length;
    const source = decisions.filter((item) => item.action === "write" && item.value_side === "source").length;
    const deleted = decisions.filter((item) => item.action === "delete").length;
    return { target, source, delete: deleted, write: target + source, total: target + source + deleted };
  }

  function defaultDecisionForRow(row) {
    if (row?.targetValues) return { action: "write", value_side: "target" };
    if (row?.sourceValues) return { action: "write", value_side: "source" };
    return null;
  }

  function initializeDefaultDecisions(result, draft) {
    if (!result || !draft) return;
    let changed = false;
    (result.sheets || []).forEach((sheet) => {
      const decisions = decisionMap(draft, sheet.id, true);
      normalizedRows(sheet).forEach((row) => {
        const decision = defaultDecisionForRow(row);
        if (decision && !decisions.has(String(row.key))) {
          decisions.set(String(row.key), decision);
          changed = true;
        }
      });
    });
    draft.defaultsInitialized = true;
    if (changed) draft.version += 1;
  }

  function localIssues(result = currentResult(), draft = draftFor()) {
    const issues = [];
    if (!result || !draft) return issues;
    const sheetMap = new Map((result.sheets || []).map((sheet) => [sheet.id, sheet]));
    draft.decisions.forEach((decisions, sheetId) => {
      const sheet = sheetMap.get(sheetId);
      if (!sheet) {
        issues.push({ sheet_name: sheetId, message: "Sheet 已不在当前 Diff 结果中。" });
        return;
      }
      const rows = new Map(normalizedRows(sheet).map((row) => [String(row.key), row]));
      decisions.forEach((decision, key) => {
        const row = rows.get(String(key));
        if (!row) {
          issues.push({ sheet_name: sheetId, key, message: "主键已不在当前 Diff 结果中。" });
        } else if (decision.action === "delete") {
          if (row.status !== "target_only" || !row.targetValues) {
            issues.push({ sheet_name: sheetId, key, message: "只有目标侧独有行可以删除。" });
          }
        } else if (!decision.value_side || !row[decision.value_side + "Values"]) {
          issues.push({ sheet_name: sheetId, key, message: "所选数据侧不存在该行。" });
        }
      });
    });
    return issues;
  }

  function allIssues(result = currentResult(), draft = draftFor()) {
    if (
      issueCache.result === result
      && issueCache.draft === draft
      && issueCache.version === (draft?.version ?? -1)
    ) {
      return issueCache.issues;
    }
    const issues = [...localIssues(result, draft), ...(draft?.serverIssues || [])];
    issueCache = { result, draft, version: draft?.version ?? -1, issues };
    return issues;
  }

  function sheetIssueCount(sheetId) {
    return allIssues().filter((issue) => issue.sheet_name === sheetId).length;
  }

  function currentSheetRows() {
    return host.getActiveSheetView()?.rows || [];
  }

  function touchDraft(draft) {
    if (!draft) return;
    draft.serverIssues = [];
    draft.batchNotice = "";
    draft.version += 1;
  }

  function refresh({ rebuildSheet = false } = {}) {
    if (rebuildSheet) {
      host.rerenderSelectedSheet();
    } else {
      host.rerenderSheetNavigation();
      host.rerenderActiveDiffWindow();
    }
    syncControls();
  }

  function setDecision(sheet, row, value) {
    const draft = draftFor();
    if (!draft) return;
    const decisions = decisionMap(draft, sheet.id, true);
    const key = String(row.key);
    if (value === "delete") {
      if (row.status !== "target_only" || !row.targetValues) return;
      decisions.set(key, { action: "delete" });
    } else if (["source", "target"].includes(value) && row[value + "Values"]) {
      decisions.set(key, { action: "write", value_side: value });
    }
    if (!decisions.size) draft.decisions.delete(sheet.id);
    touchDraft(draft);
    refresh();
  }

  function createDecisionRow(view, row, rowIndex, rowHeight) {
    if (!exportMode) return null;
    const draft = draftFor();
    const decision = draft?.decisions.get(view.sheet.id)?.get(String(row.key)) || defaultDecisionForRow(row);
    const wrapper = document.createElement("div");
    wrapper.className = "diff-export-decision-row is-" + row.status;
    wrapper.style.top = (rowIndex * rowHeight) + "px";
    const group = document.createElement("div");
    group.className = "diff-export-decision-group";
    group.setAttribute("role", "group");
    group.setAttribute("aria-label", "主键 " + row.key + " 的导出决策");
    const buttons = [
      ["source", "使用左侧", !row.sourceValues, "source"],
      ["target", "使用右侧", !row.targetValues, "target"],
    ];
    if (row.status === "target_only") buttons.push(["delete", "删除", !row.targetValues, "delete"]);
    buttons.forEach(([value, label, disabled, kind]) => {
      const button = document.createElement("button");
      button.type = "button";
      button.className = "diff-export-decision-button is-" + kind;
      button.dataset.exportKey = String(row.key);
      button.dataset.exportAction = value;
      button.textContent = label;
      button.disabled = Boolean(disabled || busy);
      const selected = value === "delete"
        ? decision?.action === "delete"
        : decision?.action === "write" && decision.value_side === value;
      button.setAttribute("aria-pressed", selected ? "true" : "false");
      button.setAttribute("aria-label", "主键 " + row.key + "：" + label);
      button.addEventListener("click", () => setDecision(view.sheet, row, value));
      group.appendChild(button);
    });
    const issues = allIssues().filter(
      (issue) => issue.sheet_name === view.sheet.id && String(issue.key || "") === String(row.key),
    );
    if (issues.length) {
      wrapper.classList.add("has-error");
      group.title = issues.map((issue) => issue.message).join("；");
    }
    wrapper.appendChild(group);
    return wrapper;
  }

  function appendSheetSummary(meta, sheet) {
    if (!exportMode) return;
    const counts = selectionCounts(draftFor(), sheet.id);
    const errors = sheetIssueCount(sheet.id);
    const summary = document.createElement("span");
    summary.className = "sheet-export-summary" + (errors ? " has-error" : "");
    summary.textContent = "左 " + counts.source + " · 右 " + counts.target + " · 删 " + counts.delete + " · 错 " + errors;
    meta.appendChild(summary);
  }

  function applyVisibleSide(side) {
    const sheet = host.state.selectedSheet;
    const draft = draftFor();
    if (!sheet || !draft) return;
    const decisions = decisionMap(draft, sheet.id, true);
    let skipped = 0;
    currentSheetRows().forEach((row) => {
      if (row[side + "Values"]) {
        decisions.set(String(row.key), { action: "write", value_side: side });
      } else {
        skipped += 1;
      }
    });
    touchDraft(draft);
    draft.batchNotice = skipped
      ? "已跳过 " + skipped + " 行（" + (side === "target" ? "右侧" : "左侧") + "不存在数据）。"
      : "当前 Sheet 已全部更新。";
    refresh();
  }

  function clearVisibleDecisions() {
    const sheet = host.state.selectedSheet;
    const draft = draftFor();
    if (!sheet || !draft) return;
    const decisions = decisionMap(draft, sheet.id, true);
    currentSheetRows().forEach((row) => {
      const decision = defaultDecisionForRow(row);
      if (decision) decisions.set(String(row.key), decision);
    });
    touchDraft(draft);
    draft.batchNotice = "当前 Sheet 已恢复默认决策。";
    refresh();
  }

  function requestSheets(result, draft) {
    return (result.sheets || []).map((sheet) => {
      const decisions = draft.decisions.get(sheet.id);
      if (!decisions?.size) return null;
      const ordered = [];
      normalizedRows(sheet).forEach((row) => {
        const decision = decisions.get(String(row.key));
        if (decision) ordered.push({ key: String(row.key), ...decision });
      });
      return ordered.length ? { sheet_name: sheet.id, decisions: ordered } : null;
    }).filter(Boolean);
  }

  function locateFirstIssue(result, draft) {
    const issue = allIssues(result, draft).find((item) => item.sheet_name);
    if (!issue || !(result.sheets || []).some((sheet) => sheet.id === issue.sheet_name)) return;
    host.renderSheet(result, issue.sheet_name);
    if (issue.key) window.requestAnimationFrame(() => host.focusDiffRow(issue.key));
  }

  async function submitExport() {
    const result = currentResult();
    const draft = draftFor();
    const sheets = result && draft ? requestSheets(result, draft) : [];
    const issues = localIssues(result, draft);
    if (!isReady(result) || !sheets.length || issues.length) {
      host.setResultActionMessage(issues[0]?.message || "请至少保留或同步一行有效数据。");
      syncControls();
      return;
    }
    busy = true;
    touchDraft(draft);
    host.setResultActionMessage("正在校验全部 Sheet 并生成差异导出 Excel。");
    syncControls();
    try {
      const response = await fetch(
        exportEndpoint(result),
        {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({
            schema_version: "m2.export.v1",
            target_layout: "target",
            sheets,
          }),
        },
      );
      if (!response.ok) throw await response.json().catch(() => ({}));
      const blob = await response.blob();
      const url = URL.createObjectURL(blob);
      const link = document.createElement("a");
      const workbookName = result.workbook?.name || host.fileName(result.candidate.path);
      link.href = url;
      link.download = workbookName.replace(/\.(?:xlsx|xlsm|xls)$/i, "") + "-差异导出.xlsx";
      document.body.appendChild(link);
      link.click();
      link.remove();
      window.setTimeout(() => URL.revokeObjectURL(url), 1000);
      const counts = selectionCounts(draft);
      host.setResultActionMessage(
        "差异导出已生成：使用左侧 " + counts.source + " 行，使用右侧 " + counts.target
          + " 行，删除 " + counts.delete
          + " 行。复制写入数据时请从 B 列开始。",
      );
    } catch (error) {
      const serverIssues = error?.error?.details?.issues;
      draft.serverIssues = Array.isArray(serverIssues) && serverIssues.length
        ? serverIssues
        : [{ message: error?.error?.message || "差异导出失败。" }];
      draft.version += 1;
      host.setResultActionMessage(
        draft.serverIssues.map((issue) => (
          [issue.sheet_name, issue.key, issue.field, issue.message].filter(Boolean).join(" · ")
        )).join("；"),
      );
      locateFirstIssue(result, draft);
    } finally {
      busy = false;
      refresh();
    }
  }

  function syncControls() {
    const result = currentResult();
    const ready = isReady(result);
    const active = exportMode && ready;
    const draft = draftFor();
    initializeDefaultDecisions(result, draft);
    const rows = currentSheetRows();
    const counts = selectionCounts(draft);
    const errors = allIssues(result, draft).length;
    $("enter-diff-export")?.classList.toggle("hidden", !ready || exportMode);
    if ($("enter-diff-export")) $("enter-diff-export").disabled = !ready;
    $("diff-export-panel")?.classList.toggle("hidden", !active);
    $("diff-export-submit-bar")?.classList.toggle("hidden", !active);
    document.body.classList.toggle("is-diff-export-mode", active);
    const batchReady = Boolean(active && host.state.selectedSheet && rows.length && !busy);
    ["export-select-source", "export-select-target", "export-clear-visible"].forEach((id) => {
      if ($(id)) $(id).disabled = !batchReady;
    });
    if ($("diff-export-visible-summary")) {
      $("diff-export-visible-summary").textContent = "当前 Sheet " + rows.length + " 行";
    }
    if ($("diff-export-total-summary")) {
      $("diff-export-total-summary").textContent =
        "使用左侧 " + counts.source + " · 使用右侧 " + counts.target
          + " · 删除 " + counts.delete + " · 校验错误 " + errors;
    }
    if ($("submit-diff-export")) {
      $("submit-diff-export").disabled = !active || !counts.total || errors > 0 || busy;
      $("submit-diff-export").textContent = busy ? "正在生成…" : "校验并导出";
    }
    if ($("cancel-diff-export")) $("cancel-diff-export").disabled = busy;
    if ($("diff-status-heading")) $("diff-status-heading").textContent = active ? "导出决策" : "状态";
    if ($("diff-export-mode-hint")) {
      $("diff-export-mode-hint").textContent =
        "导出目标固定为右侧 TARGET；批量操作只影响当前 Sheet 的 "
        + rows.length + " 行。"
        + (draft?.batchNotice ? " " + draft.batchNotice : "");
    }
  }

  $("enter-diff-export")?.addEventListener("click", () => {
    if (!isReady()) return;
    exportMode = true;
    const result = currentResult();
    initializeDefaultDecisions(result, draftFor());
    refresh({ rebuildSheet: true });
  });
  $("cancel-diff-export")?.addEventListener("click", () => {
    exportMode = false;
    refresh({ rebuildSheet: true });
  });
  $("export-select-source")?.addEventListener("click", () => applyVisibleSide("source"));
  $("export-select-target")?.addEventListener("click", () => applyVisibleSide("target"));
  $("export-clear-visible")?.addEventListener("click", clearVisibleDecisions);
  $("submit-diff-export")?.addEventListener("click", submitExport);

  globalThis.ExcelDiffExportRuntime = Object.freeze({
    createDecisionRow,
    appendSheetSummary,
    onWorkbookChanged: syncControls,
    onSheetViewChanged: syncControls,
    syncControls,
  });
  syncControls();
})();
