(() => {
  const body = document.body;
  const planId = body.dataset.planId || "";
  const source = document.getElementById("source-endpoint");
  const nameInput = document.getElementById("plan-name");
  const workbookQuery = document.getElementById("workbook-query");
  const workbookList = document.getElementById("workbook-list");
  const targetList = document.getElementById("target-list");
  const workbookCount = document.getElementById("workbook-selection-count");
  const targetCount = document.getElementById("target-selection-count");
  const catalogContext = document.getElementById("catalog-context");
  const revisionGrid = document.getElementById("revision-grid");
  const form = document.getElementById("diff-plan-form");
  const saveOnly = document.getElementById("save-plan");
  const saveAndRun = document.getElementById("save-and-run");
  const formStatus = document.getElementById("form-status");
  const alert = document.getElementById("form-alert");
  const sourceDialog = document.getElementById("source-change-dialog");
  const state = {
    endpoints: [],
    workbooks: [],
    selectedWorkbooks: new Set(),
    selectedTargets: new Set(),
    revisions: new Map(),
    version: null,
    initialSource: "",
    pendingSource: "",
    loading: false,
  };

  function requestId() {
    return globalThis.crypto?.randomUUID?.() || "00000000-0000-4000-8000-" + String(Date.now()).padStart(12, "0").slice(-12);
  }

  async function api(path, options = {}) {
    const response = await fetch(path, { headers: { "Content-Type": "application/json", ...(options.headers || {}) }, ...options });
    const payload = await response.json().catch(() => null);
    if (!response.ok) throw new Error(payload?.error?.message || "请求失败");
    return payload;
  }

  function showError(message) {
    alert.textContent = message;
    alert.classList.remove("hidden");
  }

  function clearError() {
    alert.classList.add("hidden");
    alert.textContent = "";
  }

  function endpointLabel(id) {
    return state.endpoints.find((item) => item.id === id)?.label || id;
  }

  function formatBytes(value) {
    if (!Number.isFinite(value)) return "大小未知";
    if (value < 1024) return value + " B";
    if (value < 1024 * 1024) return (value / 1024).toFixed(1) + " KiB";
    return (value / 1024 / 1024).toFixed(1) + " MiB";
  }

  function syncCounts() {
    workbookCount.textContent = state.selectedWorkbooks.size + " / 10";
    targetCount.textContent = state.selectedTargets.size + " / 4";
    document.querySelectorAll('[data-workbook-choice] input').forEach((input) => {
      input.disabled = !input.checked && state.selectedWorkbooks.size >= 10;
      input.closest("label").classList.toggle("is-disabled", input.disabled);
    });
    document.querySelectorAll('[data-target-choice] input').forEach((input) => {
      input.disabled = !input.checked && state.selectedTargets.size >= 4;
      input.closest("label").classList.toggle("is-disabled", input.disabled);
    });
    renderRevisions();
  }

  function renderWorkbooks() {
    const value = workbookQuery.value.trim().toLocaleLowerCase();
    const workbooks = state.workbooks.filter((item) => item.path.toLocaleLowerCase().includes(value));
    workbookList.replaceChildren();
    if (!workbooks.length) {
      const empty = document.createElement("div");
      empty.className = "diff-plan-pane-empty";
      empty.textContent = state.workbooks.length ? "没有匹配的 TABLE 表格" : "当前分支没有可用 Excel 表格";
      workbookList.append(empty);
      return;
    }
    workbooks.forEach((item) => {
      const row = document.createElement("label");
      row.className = "diff-plan-choice";
      row.dataset.workbookChoice = item.path;
      const checkbox = document.createElement("input");
      checkbox.type = "checkbox";
      checkbox.checked = state.selectedWorkbooks.has(item.path);
      checkbox.addEventListener("change", () => {
        if (checkbox.checked) state.selectedWorkbooks.add(item.path);
        else state.selectedWorkbooks.delete(item.path);
        syncCounts();
      });
      const path = document.createElement("span");
      path.textContent = item.path;
      const size = document.createElement("small");
      size.textContent = formatBytes(item.size_bytes);
      row.append(checkbox, path, size);
      workbookList.append(row);
    });
    syncCounts();
  }

  function renderTargets() {
    targetList.replaceChildren();
    const choices = state.endpoints.filter((item) => item.enabled && item.id !== source.value);
    if (!choices.length) {
      const empty = document.createElement("div");
      empty.className = "diff-plan-pane-empty";
      empty.textContent = "没有其他可用分支";
      targetList.append(empty);
      return;
    }
    choices.forEach((item) => {
      const row = document.createElement("label");
      row.className = "diff-plan-choice";
      row.dataset.targetChoice = item.id;
      const checkbox = document.createElement("input");
      checkbox.type = "checkbox";
      checkbox.checked = state.selectedTargets.has(item.id);
      checkbox.addEventListener("change", () => {
        if (checkbox.checked) state.selectedTargets.add(item.id);
        else {
          state.selectedTargets.delete(item.id);
          state.revisions.delete(item.id);
        }
        syncCounts();
      });
      const label = document.createElement("span");
      label.textContent = item.label;
      const meta = document.createElement("small");
      meta.textContent = item.region + " · " + item.track;
      row.append(checkbox, label, meta);
      targetList.append(row);
    });
    syncCounts();
  }

  function renderRevisions() {
    revisionGrid.replaceChildren();
    const note = document.createElement("p");
    note.textContent = "留空表示 HEAD。历史 Revision 必须为正整数，且只应用于本次运行。";
    revisionGrid.append(note);
    const endpointIds = [source.value, ...state.selectedTargets].filter(Boolean);
    endpointIds.forEach((endpointId) => {
      const label = document.createElement("label");
      const span = document.createElement("span");
      span.textContent = endpointLabel(endpointId);
      const input = document.createElement("input");
      input.type = "number";
      input.min = "1";
      input.step = "1";
      input.placeholder = "HEAD";
      input.value = state.revisions.get(endpointId) || "";
      input.addEventListener("input", () => {
        if (input.value) state.revisions.set(endpointId, input.value);
        else state.revisions.delete(endpointId);
      });
      label.append(span, input);
      revisionGrid.append(label);
    });
  }

  async function loadCatalog(endpointId) {
    workbookList.innerHTML = '<div class="diff-plan-loading"><span class="diff-plan-spinner"></span>正在读取 TABLE 清单</div>';
    workbookQuery.disabled = true;
    catalogContext.textContent = "正在冻结 " + endpointLabel(endpointId) + " 的 HEAD";
    try {
      const payload = await api("/api/diff-plans/workbook-catalog", {
        method: "POST",
        body: JSON.stringify({ schema_version: "m4.workbook-catalog.request.v1", endpoint_id: endpointId, revision: "HEAD" }),
      });
      state.workbooks = payload.workbooks;
      const available = new Set(state.workbooks.map((item) => item.path));
      state.selectedWorkbooks.forEach((path) => { if (!available.has(path)) state.selectedWorkbooks.delete(path); });
      workbookQuery.disabled = false;
      catalogContext.textContent = endpointLabel(endpointId) + " · r" + payload.resolved_revision + " · TABLE 共 " + payload.total + " 张";
      renderWorkbooks();
    } catch (error) {
      state.workbooks = [];
      workbookList.innerHTML = '<div class="diff-plan-pane-empty">TABLE 清单加载失败</div>';
      catalogContext.textContent = "未能读取基准分支";
      showError(error.message);
    }
  }

  function applySource(endpointId, { reset = false } = {}) {
    source.value = endpointId;
    if (reset) {
      state.selectedWorkbooks.clear();
      state.revisions.clear();
    }
    state.selectedTargets.delete(endpointId);
    renderTargets();
    void loadCatalog(endpointId);
  }

  async function loadEndpoints() {
    const payload = await api("/api/svn/endpoints");
    state.endpoints = payload.endpoints;
    source.replaceChildren();
    const placeholder = document.createElement("option");
    placeholder.value = "";
    placeholder.textContent = "选择基准分支";
    source.append(placeholder);
    state.endpoints.filter((item) => item.enabled).forEach((item) => {
      const option = document.createElement("option");
      option.value = item.id;
      option.textContent = item.label;
      source.append(option);
    });
    source.disabled = false;
  }

  async function loadPlan() {
    const plan = await api("/api/diff-plans/" + planId);
    state.version = plan.version;
    state.initialSource = plan.source_endpoint_id;
    nameInput.value = plan.name;
    state.selectedTargets = new Set(plan.target_endpoint_ids);
    state.selectedWorkbooks = new Set(plan.workbook_paths);
    applySource(plan.source_endpoint_id);
    formStatus.textContent = "计划版本 v" + plan.version;
  }

  function validate() {
    if (!nameInput.value.trim()) return "请输入计划名称";
    if (!source.value) return "请选择基准分支";
    if (state.selectedWorkbooks.size < 1 || state.selectedWorkbooks.size > 10) return "请选择 1～10 张 TABLE 表格";
    if (state.selectedTargets.size < 1 || state.selectedTargets.size > 4) return "请选择 1～4 个目标分支";
    for (const value of state.revisions.values()) {
      if (!/^[1-9]\d*$/.test(value)) return "历史 Revision 必须为正整数";
    }
    return "";
  }

  async function save({ run }) {
    clearError();
    const invalid = validate();
    if (invalid) {
      showError(invalid);
      return;
    }
    saveOnly.disabled = true;
    saveAndRun.disabled = true;
    formStatus.textContent = "正在保存计划";
    const definition = {
      request_id: requestId(),
      name: nameInput.value.trim(),
      source_endpoint_id: source.value,
      target_endpoint_ids: [...state.selectedTargets],
      workbook_paths: [...state.selectedWorkbooks],
    };
    try {
      const payload = planId
        ? await api("/api/diff-plans/" + planId, { method: "PUT", body: JSON.stringify({ schema_version: "m4.diff-plan-update.request.v1", expected_version: state.version, ...definition }) })
        : await api("/api/diff-plans", { method: "POST", body: JSON.stringify({ schema_version: "m4.diff-plan-create.request.v1", ...definition }) });
      state.version = payload.version;
      if (run) sessionStorage.setItem("m4PendingRun", JSON.stringify({ planId: payload.plan_id, revisions: Object.fromEntries(state.revisions) }));
      location.href = "/diff-plans/" + payload.plan_id + (run ? "?run=pending" : "");
    } catch (error) {
      showError(error.message);
      formStatus.textContent = "保存失败";
      saveOnly.disabled = false;
      saveAndRun.disabled = false;
    }
  }

  source.addEventListener("change", () => {
    const next = source.value;
    if (!next) return;
    const previous = state.initialSource || state.pendingSource;
    if (previous && previous !== next && state.selectedWorkbooks.size) {
      state.pendingSource = next;
      source.value = previous;
      sourceDialog.showModal();
      return;
    }
    state.initialSource = next;
    applySource(next);
  });
  sourceDialog.addEventListener("close", () => {
    if (sourceDialog.returnValue === "confirm" && state.pendingSource) {
      state.initialSource = state.pendingSource;
      applySource(state.pendingSource, { reset: true });
    }
    state.pendingSource = "";
  });
  workbookQuery.addEventListener("input", renderWorkbooks);
  saveOnly.addEventListener("click", () => void save({ run: false }));
  form.addEventListener("submit", (event) => { event.preventDefault(); void save({ run: true }); });

  (async () => {
    try {
      await loadEndpoints();
      if (planId) await loadPlan();
      else renderTargets();
    } catch (error) {
      showError(error.message);
      source.disabled = true;
    }
  })();
})();
