(() => {
  const TASK_CONTEXT_KEY = "excelDiffTaskContext";
  const REVIEW_STATE_KEY_PREFIX = "excelDiffConfirmedWorkbooks:";
  const FILE_STATUS = {
    modified: "内容变化",
    left_only: "仅左侧",
    right_only: "仅右侧",
    read_error: "读取失败",
  };
  const DIFF_ROW_HEIGHT = 48;
  const DIFF_HEADER_HEIGHT = 52;
  const DIFF_OVERSCAN = 8;
  const TARGET_DIFF_MAX_MATRIX_CELLS = 250000;
  const ROW_STATUS_LABELS = {
    modified: "修改",
    target_only: "右侧新增",
    source_only: "右侧删除",
  };

  const state = {
    context: null,
    results: new Map(),
    selectedPath: "",
    selectedSheet: null,
    showUnchanged: false,
    showConfirmed: false,
    showAllSheets: false,
    fieldViewMode: "diff",
    confirmedPaths: new Set(),
    reviewScope: "",
    selectedDiffCell: null,
    busy: false,
  };
  const $ = (id) => document.getElementById(id);
  let activeSheetView = null;

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
    state.selectedDiffCell = null;
    const detail = $("diff-selection-detail");
    if (!detail) return;
    detail.classList.add("is-empty");
    $("diff-selection-heading").textContent = "未选择";
    $("diff-selection-meta").textContent = "选择任一侧单元格查看完整值";
    $("diff-selection-source").textContent = "—";
    $("diff-selection-target").textContent = "—";
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

  function detailValue(field, side) {
    if (field[side + "MissingRow"]) return "该侧无此行";
    if (field[side + "MissingField"]) return "该侧无此字段";
    const value = field[side + "Value"];
    return value === "" ? "（空字符串）" : String(value ?? "");
  }

  function updateDetail(field, buttons = []) {
    document.querySelectorAll(".diff-grid-cell.is-selected").forEach((current) => current.classList.remove("is-selected"));
    if (!field) {
      resetDetail();
      return;
    }
    buttons.forEach((button) => button.classList.add("is-selected"));
    state.selectedDiffCell = {
      sheetId: state.selectedSheet?.id || "",
      selectionKey: field.selectionKey,
      rowIndex: field.rowIndex,
      fieldIndex: field.fieldIndex,
      fieldName: field.name,
    };
    $("workbench-caption").textContent = selectedFieldCaption(field);
    $("diff-selection-detail").classList.remove("is-empty");
    $("diff-selection-heading").textContent = field.name;
    $("diff-selection-meta").textContent = "主键 " + field.key
      + (field.sourceRowNumber ? " · 左侧第 " + field.sourceRowNumber + " 行" : "")
      + (field.targetRowNumber ? " · 右侧第 " + field.targetRowNumber + " 行" : "");
    $("diff-selection-source").textContent = detailValue(field, "source");
    $("diff-selection-target").textContent = detailValue(field, "target");
  }

  function normalizedRowStatus(row) {
    if (row.status) return row.status;
    return {
      modified: "modified",
      added: "target_only",
      deleted: "source_only",
    }[row.change] || row.change;
  }

  function legacySideValues(row, side, primaryKey, status) {
    if ((status === "target_only" && side === "source") || (status === "source_only" && side === "target")) {
      return null;
    }
    const values = { [primaryKey]: row.key };
    (row.fields || []).forEach((field) => {
      const value = sideValue(field, side);
      if (value !== "—") values[field.name] = value;
    });
    return values;
  }

  function normalizeSheetRow(row, primaryKey) {
    const status = normalizedRowStatus(row);
    const sourceValues = Object.prototype.hasOwnProperty.call(row, "sourceValues")
      ? row.sourceValues
      : legacySideValues(row, "source", primaryKey, status);
    const targetValues = Object.prototype.hasOwnProperty.call(row, "targetValues")
      ? row.targetValues
      : legacySideValues(row, "target", primaryKey, status);
    const firstField = (row.fields || [])[0] || null;
    return {
      ...row,
      status,
      sourceRowNumber: row.sourceRowNumber ?? firstField?.sourceRowNumber ?? null,
      targetRowNumber: row.targetRowNumber ?? firstField?.targetRowNumber ?? null,
      sourceValues,
      targetValues,
      changedFields: new Map((row.fields || []).map((field) => [field.name, field])),
    };
  }

  function sheetColumnModel(sheet, rows, fieldViewMode) {
    const primaryKey = sheet.primaryKey || "Id";
    const definitions = [...(sheet.fieldDefinitions || [])];
    const known = new Set(definitions.map((field) => field.name));
    rows.forEach((row) => {
      [row.sourceValues, row.targetValues].forEach((values) => {
        Object.keys(values || {}).forEach((name) => {
          if (known.has(name)) return;
          known.add(name);
          definitions.push({ name, status: "common" });
        });
      });
      (row.fields || []).forEach((field) => {
        if (known.has(field.name)) return;
        known.add(field.name);
        definitions.push({ name: field.name, status: field.status || "modified" });
      });
    });
    const showAllFields = fieldViewMode === "original"
      || rows.some((row) => row.status === "source_only" || row.status === "target_only");
    const requested = new Set();
    if (showAllFields) {
      definitions.forEach((field) => requested.add(field.name));
    } else {
      rows.forEach((row) => row.changedFields.forEach((_field, name) => requested.add(name)));
    }
    requested.delete(primaryKey);
    const fields = definitions.filter((field) => requested.has(field.name));
    const ordered = new Set(fields.map((field) => field.name));
    requested.forEach((name) => {
      if (ordered.has(name)) return;
      fields.push({ name, status: "modified" });
    });
    return {
      primaryKey,
      fields,
      definitions: new Map(definitions.map((field) => [field.name, field])),
      showAllFields,
    };
  }

  function sideCellValue(row, side, fieldName) {
    const values = row[side + "Values"];
    if (!values) return { value: "", missingRow: true, missingField: false };
    if (!Object.prototype.hasOwnProperty.call(values, fieldName)) {
      return { value: "", missingRow: false, missingField: true };
    }
    return { value: String(values[fieldName] ?? ""), missingRow: false, missingField: false };
  }

  function selectedCellData(view, row, rowIndex, fieldName, fieldIndex) {
    const source = sideCellValue(row, "source", fieldName);
    const target = sideCellValue(row, "target", fieldName);
    return {
      name: fieldName,
      key: row.key,
      rowIndex,
      fieldIndex,
      selectionKey: rowIndex + ":" + fieldIndex,
      sourceValue: source.value,
      targetValue: target.value,
      sourceMissingRow: source.missingRow,
      targetMissingRow: target.missingRow,
      sourceMissingField: source.missingField,
      targetMissingField: target.missingField,
      sourceRowNumber: row.sourceRowNumber,
      targetRowNumber: row.targetRowNumber,
      definition: view.columns.definitions.get(fieldName) || null,
    };
  }

  function selectDiffCell(view, row, rowIndex, fieldName, fieldIndex) {
    const field = selectedCellData(view, row, rowIndex, fieldName, fieldIndex);
    const peers = Array.from(document.querySelectorAll(
      '.diff-grid-cell[data-selection-key="' + field.selectionKey + '"]',
    ));
    updateDetail(field, peers);
  }

  function modifiedFieldTargets(view, row) {
    const fieldNames = [view.columns.primaryKey, ...view.columns.fields.map((field) => field.name)];
    return fieldNames.flatMap((fieldName, fieldIndex) => (
      row.changedFields.has(fieldName) ? [{ fieldName, fieldIndex }] : []
    ));
  }

  function centerDiffField(view, rowIndex, fieldIndex) {
    if (fieldIndex === 0) return;
    const selectionKey = rowIndex + ":" + fieldIndex;
    const cell = view.sourceRows.querySelector(
      '.diff-grid-cell[data-selection-key="' + selectionKey + '"]',
    );
    if (!cell) return;
    const frozenWidth = 52 + 144;
    const visibleWidth = Math.max(0, view.sourceScroll.clientWidth - frozenWidth);
    const centeredOffset = Math.max(0, (visibleWidth - cell.offsetWidth) / 2);
    const maxScrollLeft = Math.max(0, view.sourceScroll.scrollWidth - view.sourceScroll.clientWidth);
    const nextScrollLeft = Math.min(
      maxScrollLeft,
      Math.max(0, cell.offsetLeft - frozenWidth - centeredOffset),
    );
    view.sourceScroll.scrollLeft = nextScrollLeft;
    view.targetScroll.scrollLeft = nextScrollLeft;
  }

  function navigateModifiedField(view, row, rowIndex) {
    const targets = modifiedFieldTargets(view, row);
    if (!targets.length) return;
    const selected = state.selectedDiffCell;
    const currentIndex = selected?.sheetId === view.sheet.id && selected.rowIndex === rowIndex
      ? targets.findIndex((target) => target.fieldIndex === selected.fieldIndex)
      : -1;
    const next = targets[(currentIndex + 1) % targets.length];
    selectDiffCell(view, row, rowIndex, next.fieldName, next.fieldIndex);
    centerDiffField(view, rowIndex, next.fieldIndex);
  }

  function diffGridTemplate(fieldCount) {
    return "52px 144px" + (fieldCount ? " repeat(" + fieldCount + ", minmax(160px, 1fr))" : "");
  }

  function fieldMissingOnSide(definition, side) {
    if (!definition) return false;
    return (definition.status === "source_only" && side === "target")
      || (definition.status === "target_only" && side === "source");
  }

  function fieldDisplayName(definition, side) {
    const preferredKey = side === "source" ? "source_display_name" : "target_display_name";
    const fallbackKey = side === "source" ? "target_display_name" : "source_display_name";
    const preferred = String(definition?.[preferredKey] ?? "").trim();
    return preferred || String(definition?.[fallbackKey] ?? "").trim();
  }

  function createHeaderCell(definition, side, className = "", singleLine = false) {
    const fieldName = definition?.name || "";
    const displayName = singleLine ? "" : fieldDisplayName(definition, side);
    const cell = document.createElement("div");
    cell.className = "diff-grid-header-cell" + (className ? " " + className : "");
    cell.setAttribute("role", "columnheader");
    if (singleLine) {
      cell.classList.add("is-single-line");
      const label = document.createElement("span");
      label.className = "diff-grid-header-single-label";
      label.textContent = fieldName;
      cell.appendChild(label);
    } else {
      const display = document.createElement("span");
      display.className = "diff-grid-header-display-name";
      display.textContent = displayName;
      const name = document.createElement("span");
      name.className = "diff-grid-header-field-name";
      name.textContent = fieldName;
      cell.append(display, name);
    }
    cell.title = displayName ? displayName + "\n" + fieldName : fieldName;
    cell.setAttribute("aria-label", displayName ? displayName + "，字段 " + fieldName : fieldName);
    return cell;
  }

  function renderDiffHeader(view, side) {
    const header = side === "source" ? view.sourceHeader : view.targetHeader;
    const primaryKeyDefinition = view.columns.definitions.get(view.columns.primaryKey)
      || { name: view.columns.primaryKey };
    header.textContent = "";
    header.style.gridTemplateColumns = view.gridTemplate;
    header.appendChild(createHeaderCell({ name: "行号" }, side, "is-row-number", true));
    header.appendChild(createHeaderCell(primaryKeyDefinition, side, "is-primary-key"));
    view.columns.fields.forEach((field) => {
      const cell = createHeaderCell(field, side);
      if (fieldMissingOnSide(field, side)) {
        cell.classList.add("is-missing-field");
        cell.title += "\n该字段仅" + (side === "source" ? "右侧" : "左侧") + "存在";
      }
      header.appendChild(cell);
    });
  }

  function sideLabel(side) {
    return side === "source" ? "左侧" : "右侧";
  }

  function appendTargetDiffSegment(segments, text, changed) {
    if (!text) return;
    const previous = segments[segments.length - 1];
    if (previous && previous.changed === changed) {
      previous.text += text;
      return;
    }
    segments.push({ text, changed });
  }

  function targetDiffSegments(sourceValue, targetValue) {
    const sourceText = String(sourceValue ?? "");
    const targetText = String(targetValue ?? "");
    if (sourceText === targetText) return targetText ? [{ text: targetText, changed: false }] : [];

    const source = Array.from(sourceText);
    const target = Array.from(targetText);
    let prefixLength = 0;
    const maxPrefix = Math.min(source.length, target.length);
    while (prefixLength < maxPrefix && source[prefixLength] === target[prefixLength]) {
      prefixLength += 1;
    }

    let suffixLength = 0;
    const maxSuffix = Math.min(source.length - prefixLength, target.length - prefixLength);
    while (
      suffixLength < maxSuffix
      && source[source.length - 1 - suffixLength] === target[target.length - 1 - suffixLength]
    ) {
      suffixLength += 1;
    }

    const segments = [];
    appendTargetDiffSegment(segments, target.slice(0, prefixLength).join(""), false);
    const sourceMiddle = source.slice(prefixLength, source.length - suffixLength);
    const targetMiddle = target.slice(prefixLength, target.length - suffixLength);
    if (!sourceMiddle.length || sourceMiddle.length * targetMiddle.length > TARGET_DIFF_MAX_MATRIX_CELLS) {
      appendTargetDiffSegment(segments, targetMiddle.join(""), true);
    } else if (targetMiddle.length) {
      const sourceLength = sourceMiddle.length;
      const targetLength = targetMiddle.length;
      const matrix = Array.from(
        { length: sourceLength + 1 },
        () => new Uint32Array(targetLength + 1),
      );
      for (let sourceIndex = sourceLength - 1; sourceIndex >= 0; sourceIndex -= 1) {
        for (let targetIndex = targetLength - 1; targetIndex >= 0; targetIndex -= 1) {
          matrix[sourceIndex][targetIndex] = sourceMiddle[sourceIndex] === targetMiddle[targetIndex]
            ? matrix[sourceIndex + 1][targetIndex + 1] + 1
            : Math.max(matrix[sourceIndex + 1][targetIndex], matrix[sourceIndex][targetIndex + 1]);
        }
      }

      let sourceIndex = 0;
      let targetIndex = 0;
      while (sourceIndex < sourceLength && targetIndex < targetLength) {
        if (sourceMiddle[sourceIndex] === targetMiddle[targetIndex]) {
          appendTargetDiffSegment(segments, targetMiddle[targetIndex], false);
          sourceIndex += 1;
          targetIndex += 1;
        } else if (matrix[sourceIndex + 1][targetIndex] >= matrix[sourceIndex][targetIndex + 1]) {
          sourceIndex += 1;
        } else {
          appendTargetDiffSegment(segments, targetMiddle[targetIndex], true);
          targetIndex += 1;
        }
      }
      appendTargetDiffSegment(segments, targetMiddle.slice(targetIndex).join(""), true);
    }
    appendTargetDiffSegment(
      segments,
      suffixLength ? target.slice(target.length - suffixLength).join("") : "",
      false,
    );
    return segments;
  }

  function renderTargetDiff(button, sourceValue, targetValue) {
    targetDiffSegments(sourceValue, targetValue).forEach((segment) => {
      if (!segment.changed) {
        button.appendChild(document.createTextNode(segment.text));
        return;
      }
      const highlight = document.createElement("span");
      highlight.className = "diff-target-change";
      highlight.textContent = segment.text;
      button.appendChild(highlight);
    });
  }

  function createGridCell(view, row, rowIndex, side, fieldName, fieldIndex) {
    const info = sideCellValue(row, side, fieldName);
    const button = document.createElement("button");
    const selectionKey = rowIndex + ":" + fieldIndex;
    const definition = view.columns.definitions.get(fieldName);
    button.type = "button";
    button.className = "diff-grid-cell" + (fieldIndex === 0 ? " is-primary-key" : "");
    button.dataset.selectionKey = selectionKey;
    const changed = row.status === "modified" && row.changedFields.has(fieldName);
    if (side === "target" && changed) {
      button.classList.add("has-target-diff");
      renderTargetDiff(button, sideCellValue(row, "source", fieldName).value, info.value);
    } else {
      button.textContent = info.value;
    }
    if (info.missingRow) button.classList.add("is-missing-row");
    if (info.missingField || fieldMissingOnSide(definition, side)) button.classList.add("is-missing-field");
    if (changed) button.classList.add("is-changed");
    if (row.status === "target_only" && side === "target") button.classList.add("is-added");
    if (row.status === "source_only" && side === "source") button.classList.add("is-deleted");
    if (
      state.selectedDiffCell?.sheetId === view.sheet.id
      && state.selectedDiffCell.selectionKey === selectionKey
    ) {
      button.classList.add("is-selected");
    }
    const valueLabel = info.missingRow
      ? "该侧无此行"
      : (info.missingField ? "该侧无此字段" : (info.value === "" ? "空字符串" : info.value));
    button.title = fieldName + "：" + valueLabel;
    button.setAttribute(
      "aria-label",
      sideLabel(side) + "，主键 " + row.key + "，字段 " + fieldName + "，" + valueLabel,
    );
    button.addEventListener("click", () => {
      if (view.suppressClick) return;
      selectDiffCell(view, row, rowIndex, fieldName, fieldIndex);
    });
    return button;
  }

  function createSideRow(view, row, rowIndex, side) {
    const rowElement = document.createElement("div");
    rowElement.className = "diff-grid-row is-" + row.status;
    if (!row[side + "Values"]) rowElement.classList.add("is-placeholder");
    rowElement.setAttribute("role", "row");
    rowElement.setAttribute("aria-rowindex", String(rowIndex + 2));
    rowElement.setAttribute("aria-label", "主键 " + row.key + "，" + (ROW_STATUS_LABELS[row.status] || row.status));
    rowElement.style.top = (rowIndex * DIFF_ROW_HEIGHT) + "px";
    rowElement.style.gridTemplateColumns = view.gridTemplate;
    const rowNumber = document.createElement("div");
    rowNumber.className = "diff-row-number";
    rowNumber.setAttribute("role", "rowheader");
    rowNumber.textContent = row[side + "RowNumber"] || "";
    rowNumber.title = row[side + "RowNumber"]
      ? sideLabel(side) + "第 " + row[side + "RowNumber"] + " 行"
      : sideLabel(side) + "无对应行";
    rowElement.appendChild(rowNumber);
    rowElement.appendChild(createGridCell(view, row, rowIndex, side, view.columns.primaryKey, 0));
    view.columns.fields.forEach((field, index) => {
      rowElement.appendChild(createGridCell(view, row, rowIndex, side, field.name, index + 1));
    });
    return rowElement;
  }

  function createStatusRow(view, row, rowIndex) {
    const navigable = row.status === "modified" && row.changedFields.size > 0;
    const status = document.createElement(navigable ? "button" : "div");
    if (navigable) {
      status.type = "button";
      status.title = "定位下一个修改字段";
      status.setAttribute("aria-label", "主键 " + row.key + "，循环定位修改字段");
      status.addEventListener("click", () => navigateModifiedField(view, row, rowIndex));
    }
    status.className = "diff-status-row is-" + row.status;
    if (navigable) status.classList.add("is-navigable");
    status.style.top = (rowIndex * DIFF_ROW_HEIGHT) + "px";
    status.textContent = ROW_STATUS_LABELS[row.status] || row.status;
    return status;
  }

  function renderDiffWindow(view, force = false) {
    const viewportHeight = Math.max(
      view.sourceScroll.clientHeight - DIFF_HEADER_HEIGHT,
      view.targetScroll.clientHeight - DIFF_HEADER_HEIGHT,
      320,
    );
    const offset = Math.max(0, view.sourceScroll.scrollTop - DIFF_HEADER_HEIGHT);
    const start = Math.max(0, Math.floor(offset / DIFF_ROW_HEIGHT) - DIFF_OVERSCAN);
    const end = Math.min(
      view.rows.length,
      Math.ceil((offset + viewportHeight) / DIFF_ROW_HEIGHT) + DIFF_OVERSCAN,
    );
    const rangeKey = start + ":" + end;
    if (!force && view.rangeKey === rangeKey) return;
    view.rangeKey = rangeKey;
    const sourceFragment = document.createDocumentFragment();
    const statusFragment = document.createDocumentFragment();
    const targetFragment = document.createDocumentFragment();
    for (let index = start; index < end; index += 1) {
      const row = view.rows[index];
      sourceFragment.appendChild(createSideRow(view, row, index, "source"));
      statusFragment.appendChild(createStatusRow(view, row, index));
      targetFragment.appendChild(createSideRow(view, row, index, "target"));
    }
    view.sourceRows.textContent = "";
    view.statusRows.textContent = "";
    view.targetRows.textContent = "";
    view.sourceRows.appendChild(sourceFragment);
    view.statusRows.appendChild(statusFragment);
    view.targetRows.appendChild(targetFragment);
  }

  function scheduleDiffWindow(view, force = false) {
    if (force) view.rangeKey = "";
    if (view.renderFrame) return;
    view.renderFrame = window.requestAnimationFrame(() => {
      view.renderFrame = 0;
      renderDiffWindow(view, force);
    });
  }

  function syncDiffScroll(view, origin) {
    if (view.syncing) return;
    view.syncing = true;
    const peer = origin === view.sourceScroll ? view.targetScroll : view.sourceScroll;
    peer.scrollTop = origin.scrollTop;
    peer.scrollLeft = origin.scrollLeft;
    view.statusScroll.scrollTop = origin.scrollTop;
    scheduleDiffWindow(view);
    if (view.syncFrame) window.cancelAnimationFrame(view.syncFrame);
    view.syncFrame = window.requestAnimationFrame(() => {
      view.syncing = false;
      view.syncFrame = 0;
    });
  }

  function bindDragPan(view, scroller) {
    let drag = null;
    const onPointerDown = (event) => {
      if (event.pointerType !== "mouse" || event.button !== 0) return;
      drag = {
        pointerId: event.pointerId,
        x: event.clientX,
        y: event.clientY,
        left: scroller.scrollLeft,
        top: scroller.scrollTop,
        moved: false,
      };
    };
    const finish = (event) => {
      if (!drag || event.pointerId !== drag.pointerId) return;
      if (drag.moved) {
        view.suppressClick = true;
        window.clearTimeout(view.clickTimer);
        view.clickTimer = window.setTimeout(() => { view.suppressClick = false; }, 0);
      }
      scroller.classList.remove("is-dragging");
      if (scroller.hasPointerCapture?.(event.pointerId)) scroller.releasePointerCapture(event.pointerId);
      drag = null;
    };
    const onPointerMove = (event) => {
      if (!drag || event.pointerId !== drag.pointerId) return;
      const deltaX = event.clientX - drag.x;
      const deltaY = event.clientY - drag.y;
      if (!drag.moved) {
        if (Math.hypot(deltaX, deltaY) < 5) return;
        drag.moved = true;
        scroller.setPointerCapture?.(event.pointerId);
        scroller.classList.add("is-dragging");
      }
      scroller.scrollLeft = drag.left - deltaX;
      scroller.scrollTop = drag.top - deltaY;
      event.preventDefault();
    };
    scroller.addEventListener("pointerdown", onPointerDown);
    window.addEventListener("pointermove", onPointerMove);
    window.addEventListener("pointerup", finish);
    window.addEventListener("pointercancel", finish);
    return () => {
      scroller.removeEventListener("pointerdown", onPointerDown);
      window.removeEventListener("pointermove", onPointerMove);
      window.removeEventListener("pointerup", finish);
      window.removeEventListener("pointercancel", finish);
    };
  }

  function setPairedDiffEmpty(message = "") {
    const empty = $("paired-diff-empty");
    const hasMessage = Boolean(message);
    empty.textContent = message;
    empty.classList.toggle("hidden", !hasMessage);
    $("paired-diff-shell").classList.toggle("hidden", hasMessage);
    $("diff-selection-detail").classList.toggle("hidden", hasMessage);
  }

  function destroySheetView() {
    activeSheetView?.destroy();
    activeSheetView = null;
    syncFieldViewControls(false);
  }

  function createSheetView(sheet, restoredView = null) {
    const primaryKey = sheet.primaryKey || "Id";
    const rows = (sheet.rows || []).map((row) => normalizeSheetRow(row, primaryKey));
    const columns = sheetColumnModel(sheet, rows, state.fieldViewMode);
    const gridTemplate = diffGridTemplate(columns.fields.length);
    const canvasWidth = 52 + 144 + (columns.fields.length * 160);
    const bodyHeight = rows.length * DIFF_ROW_HEIGHT;
    const view = {
      sheet,
      rows,
      columns,
      gridTemplate,
      sourceScroll: $("diff-source-scroll"),
      targetScroll: $("diff-target-scroll"),
      statusScroll: $("diff-status-scroll"),
      sourceHeader: $("diff-source-header"),
      targetHeader: $("diff-target-header"),
      sourceRows: $("diff-source-rows"),
      targetRows: $("diff-target-rows"),
      statusRows: $("diff-status-rows"),
      rangeKey: "",
      syncing: false,
      suppressClick: false,
      renderFrame: 0,
      syncFrame: 0,
      clickTimer: 0,
    };
    $("diff-source-label").textContent = state.context?.source?.branch || state.context?.source?.label || "左侧";
    $("diff-target-label").textContent = state.context?.target?.branch || state.context?.target?.label || "右侧";
    [view.sourceScroll, view.targetScroll].forEach((scroller) => {
      scroller.scrollTop = 0;
      scroller.scrollLeft = 0;
      scroller.setAttribute("aria-rowcount", String(rows.length + 1));
      scroller.setAttribute("aria-colcount", String(columns.fields.length + 2));
    });
    view.statusScroll.scrollTop = 0;
    [$("diff-source-canvas"), $("diff-target-canvas")].forEach((canvas) => {
      canvas.style.minWidth = canvasWidth + "px";
    });
    [view.sourceRows, view.targetRows, view.statusRows].forEach((container) => {
      container.style.height = bodyHeight + "px";
    });
    renderDiffHeader(view, "source");
    renderDiffHeader(view, "target");
    const restoredScrollTop = Math.max(0, Number(restoredView?.scrollTop || 0));
    const restoredScrollLeft = Math.max(0, Number(restoredView?.scrollLeft || 0));
    view.sourceScroll.scrollTop = restoredScrollTop;
    view.targetScroll.scrollTop = restoredScrollTop;
    view.statusScroll.scrollTop = restoredScrollTop;
    view.sourceScroll.scrollLeft = restoredScrollLeft;
    view.targetScroll.scrollLeft = restoredScrollLeft;
    renderDiffWindow(view, true);
    const onSourceScroll = () => syncDiffScroll(view, view.sourceScroll);
    const onTargetScroll = () => syncDiffScroll(view, view.targetScroll);
    const onResize = () => scheduleDiffWindow(view, true);
    view.sourceScroll.addEventListener("scroll", onSourceScroll, { passive: true });
    view.targetScroll.addEventListener("scroll", onTargetScroll, { passive: true });
    window.addEventListener("resize", onResize);
    const dragCleanups = [bindDragPan(view, view.sourceScroll), bindDragPan(view, view.targetScroll)];
    view.destroy = () => {
      view.sourceScroll.removeEventListener("scroll", onSourceScroll);
      view.targetScroll.removeEventListener("scroll", onTargetScroll);
      window.removeEventListener("resize", onResize);
      dragCleanups.forEach((cleanup) => cleanup());
      if (view.renderFrame) window.cancelAnimationFrame(view.renderFrame);
      if (view.syncFrame) window.cancelAnimationFrame(view.syncFrame);
      window.clearTimeout(view.clickTimer);
    };
    activeSheetView = view;
    syncFieldViewControls(true);
    resetDetail();
    const restoredSelection = restoredView?.selection;
    const restoredRowIndex = Number(restoredSelection?.rowIndex);
    const selectedRowIndex = Number.isInteger(restoredRowIndex)
      && restoredRowIndex >= 0
      && restoredRowIndex < rows.length
      ? restoredRowIndex
      : 0;
    const selectedRow = rows[selectedRowIndex];
    if (selectedRow) {
      const visibleFieldNames = new Set([
        columns.primaryKey,
        ...columns.fields.map((field) => field.name),
      ]);
      const preferredField = visibleFieldNames.has(restoredSelection?.fieldName)
        ? restoredSelection.fieldName
        : (selectedRow.changedFields.keys().next().value || columns.primaryKey);
      const fieldIndex = preferredField === columns.primaryKey
        ? 0
        : Math.max(0, columns.fields.findIndex((field) => field.name === preferredField) + 1);
      const fieldName = fieldIndex === 0 ? columns.primaryKey : columns.fields[fieldIndex - 1].name;
      const field = selectedCellData(view, selectedRow, selectedRowIndex, fieldName, fieldIndex);
      const peers = Array.from(document.querySelectorAll(
        '.diff-grid-cell[data-selection-key="' + field.selectionKey + '"]',
      ));
      updateDetail(field, peers);
    }
  }

  function syncFieldViewControls(enabled) {
    const diffButton = $("show-diff-fields");
    const originalButton = $("show-original-fields");
    const showingOriginal = state.fieldViewMode === "original";
    diffButton.disabled = !enabled;
    originalButton.disabled = !enabled;
    diffButton.setAttribute("aria-pressed", String(!showingOriginal));
    originalButton.setAttribute("aria-pressed", String(showingOriginal));
    diffButton.classList.toggle("is-selected", !showingOriginal);
    originalButton.classList.toggle("is-selected", showingOriginal);
  }

  function setFieldViewMode(mode) {
    if (mode !== "diff" && mode !== "original") return;
    if (state.fieldViewMode === mode) {
      syncFieldViewControls(Boolean(activeSheetView));
      return;
    }
    const restoredView = activeSheetView
      ? {
        scrollTop: activeSheetView.sourceScroll.scrollTop,
        scrollLeft: activeSheetView.sourceScroll.scrollLeft,
        selection: state.selectedDiffCell ? { ...state.selectedDiffCell } : null,
      }
      : null;
    const sheet = state.selectedSheet;
    state.fieldViewMode = mode;
    if (!activeSheetView || !sheet) {
      syncFieldViewControls(false);
      return;
    }
    destroySheetView();
    createSheetView(sheet, restoredView);
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
    destroySheetView();
    const visible = visibleSheetResults(result);
    const sheet = visible.find((item) => item.id === sheetId) || preferredVisibleSheet(result);
    if (!sheet) {
      state.selectedSheet = null;
      renderEmptySheetNavigation("当前筛选没有可显示的 Sheet。", result);
      setPairedDiffEmpty("切换到“显示全部”查看无变化 Sheet。");
      resetDetail();
      return;
    }
    state.selectedSheet = sheet;
    renderSheetNavigation(result, sheet.id);
    if (!sheet.rows.length) {
      if (sheet.status === "failed") {
        setPairedDiffEmpty("该 Sheet 执行失败；结构化错误保留在当前工作簿状态中。");
      } else {
        setPairedDiffEmpty(state.context?.mode === "demo"
          ? "UI 示例：该 Sheet 已完成且没有差异。"
          : "该 Sheet 没有行级差异。");
      }
      resetDetail();
      return;
    }
    setPairedDiffEmpty();
    createSheetView(sheet);
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
    if (result.itemStatus === "queued") return "待处理";
    if (result.itemStatus === "running") return "处理中";
    if (result.summaryError) return "统计不可用";
    if (result.itemStatus === "succeeded" && !result.summary) return "统计读取中";
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
    globalThis.M4DiffPlanRuntime?.onWorkbookSelected?.(path);
    destroySheetView();
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
    function sideLabel(side, fallback) {
      const name = side?.branch || side?.label || fallback;
      return side?.resolvedRevision ? name + " · r" + side.resolvedRevision : name;
    }
    $("result-source-label").textContent = sideLabel(context.source, "左侧");
    $("result-target-label").textContent = sideLabel(context.target, "右侧");
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
    $("result-heading-panel").classList.toggle("hidden", !visible);
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
    if (document.body.dataset.m4RunId) {
      showMissingContext();
      return;
    }
    if (document.body.dataset.replayMode === "true") {
      showMissingContext();
      return;
    }
    const demoPage = document.body.dataset.demoMode === "true";
    const taskIdParam = demoPage ? "" : (new URLSearchParams(location.search).get("task_id") || "").trim();
    const validTaskId = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i.test(taskIdParam);
    let context = null;
    if (taskIdParam) {
      if (!validTaskId) {
        showMissingContext();
        $("missing-heading").textContent = "Task ID 格式无效";
        $("missing-detail").textContent = "请从历史任务重新选择任务。";
        return;
      }
      context = {
        version: 3,
        mode: "formal",
        batchTaskId: taskIdParam.toLowerCase(),
        capturedAt: "",
        source: { endpointId: "", label: "左侧", branch: "", resolvedRevision: null },
        target: { endpointId: "", label: "右侧", branch: "", resolvedRevision: null },
        candidates: [],
        results: [],
      };
      sessionStorage.setItem(TASK_CONTEXT_KEY, JSON.stringify(context));
    } else {
      try {
        context = JSON.parse(sessionStorage.getItem(TASK_CONTEXT_KEY) || "null");
      } catch {
        sessionStorage.removeItem(TASK_CONTEXT_KEY);
      }
    }
    if ((!context?.candidates?.length && !context?.batchTaskId) || (demoPage && context.mode !== "demo") || (!demoPage && context.mode === "demo")) {
      showMissingContext();
      return;
    }
    if (!demoPage && context.mode === "formal" && context.batchTaskId) {
      const canonical = "/compare/results?task_id=" + encodeURIComponent(context.batchTaskId);
      if (location.pathname + location.search !== canonical) history.replaceState(null, "", canonical);
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
  $("show-diff-fields").addEventListener("click", () => setFieldViewMode("diff"));
  $("show-original-fields").addEventListener("click", () => setFieldViewMode("original"));
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
