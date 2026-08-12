(() => {
  const body = document.body;
  const planId = body.dataset.planId || "";
  const source = document.getElementById("source-endpoint");
  const sourceQuery = document.getElementById("source-endpoint-query");
  const sourceOptions = document.getElementById("source-endpoint-options");
  const sourceToggle = document.getElementById("source-endpoint-toggle");
  const sourceState = document.getElementById("source-endpoint-state");
  const targetQuery = document.getElementById("target-endpoint-query");
  const targetToggle = document.getElementById("target-endpoint-toggle");
  const targetList = document.getElementById("target-list");
  const targetState = document.getElementById("target-endpoint-state");
  const nameInput = document.getElementById("plan-name");
  const workbookQuery = document.getElementById("workbook-query");
  const workbookList = document.getElementById("workbook-list");
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
    registryRecords: [],
    workbooks: [],
    selectedWorkbooks: new Set(),
    selectedTargets: new Set(),
    revisions: new Map(),
    version: null,
    initialSource: "",
    pendingSource: "",
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

  function endpointIdForMatch(match) {
    return (match.region + "_" + match.track + "_" + match.branch)
      .replace(/[^A-Za-z0-9._-]/g, "_").slice(0, 128);
  }

  function endpointBranch(endpoint) {
    if (endpoint?.branch) return endpoint.branch;
    const parts = String(endpoint?.label || "").split("·");
    if (parts.length > 1) return parts[parts.length - 1].trim();
    try {
      const path = new URL(endpoint?.url || "").pathname.replace(/\/$/, "");
      return decodeURIComponent(path.split("/").pop() || endpoint?.id || "");
    } catch {
      return endpoint?.id || "";
    }
  }

  function recordFromMatch(match) {
    return {
      id: endpointIdForMatch(match),
      region: match.region,
      track: match.track,
      label: match.label || match.branch,
      url: match.url,
      logical_scopes: ["TABLE"],
      physical_path_filters: {},
      enabled: true,
      branch: match.branch,
      match_type: match.match_type,
      pendingRegistration: true,
    };
  }

  function mergeEndpointSources(records, matches) {
    state.registryRecords = records || [];
    const endpoints = new Map();
    state.registryRecords.forEach((record) => endpoints.set(record.id, { ...record, pendingRegistration: false }));
    (matches || []).forEach((match) => {
      const candidate = recordFromMatch(match);
      if (!endpoints.has(candidate.id)) endpoints.set(candidate.id, candidate);
    });
    state.endpoints = [...endpoints.values()]
      .filter((endpoint) => endpoint.enabled)
      .sort((left, right) => endpointBranch(left).localeCompare(endpointBranch(right), "zh-CN", { numeric: true }));
  }

  function endpointById(id) {
    return state.endpoints.find((item) => item.id === id);
  }

  function endpointLabel(id) {
    return endpointById(id)?.label || id;
  }

  function matchingEndpoints(query, { excludeSource = false } = {}) {
    const normalized = String(query || "").trim().toLocaleLowerCase();
    return state.endpoints.filter((endpoint) => {
      if (excludeSource && endpoint.id === source.value) return false;
      return !normalized || [endpointBranch(endpoint), endpoint.label, endpoint.region, endpoint.track]
        .some((value) => String(value || "").toLocaleLowerCase().includes(normalized));
    });
  }

  async function ensureEndpointsRegistered(endpointIds) {
    const pending = [...new Set(endpointIds)]
      .map(endpointById)
      .filter((endpoint) => endpoint?.pendingRegistration);
    if (!pending.length) return;
    const records = new Map(state.registryRecords.map((record) => [record.id, record]));
    pending.forEach((endpoint) => {
      const { pendingRegistration, branch, match_type, ...record } = endpoint;
      records.set(record.id, record);
    });
    const payload = await api("/api/svn/endpoints", {
      method: "POST",
      body: JSON.stringify({ endpoints: [...records.values()] }),
    });
    state.registryRecords = payload.endpoints || [];
    const registered = new Map(state.registryRecords.map((record) => [record.id, record]));
    state.endpoints = state.endpoints.map((endpoint) => {
      const record = registered.get(endpoint.id);
      return record ? { ...record, branch: endpointBranch(endpoint), match_type: endpoint.match_type, pendingRegistration: false } : endpoint;
    });
  }

  function setComboboxOpen(kind, open) {
    const input = kind === "source" ? sourceQuery : targetQuery;
    const button = kind === "source" ? sourceToggle : targetToggle;
    const panel = kind === "source" ? sourceOptions : targetList;
    panel.classList.toggle("hidden", !open);
    input.setAttribute("aria-expanded", String(open));
    button.setAttribute("aria-expanded", String(open));
    if (open) kind === "source" ? renderSourceOptions() : renderTargets();
  }

  function bindOptionKeyboard(panel, kind) {
    panel.addEventListener("keydown", (event) => {
      if (!["ArrowDown", "ArrowUp", "Escape"].includes(event.key)) return;
      event.preventDefault();
      if (event.key === "Escape") {
        setComboboxOpen(kind, false);
        (kind === "source" ? sourceQuery : targetQuery).focus();
        return;
      }
      const options = [...panel.querySelectorAll("button:not(:disabled), input:not(:disabled)")];
      const index = options.indexOf(document.activeElement);
      const next = event.key === "ArrowDown"
        ? Math.min(options.length - 1, index + 1)
        : Math.max(0, index - 1);
      options[next]?.focus();
    });
  }

  function bindComboboxInput(input, toggle, kind) {
    input.addEventListener("focus", () => setComboboxOpen(kind, true));
    input.addEventListener("input", () => setComboboxOpen(kind, true));
    input.addEventListener("keydown", (event) => {
      if (event.key === "Escape") {
        setComboboxOpen(kind, false);
      } else if (event.key === "ArrowDown") {
        event.preventDefault();
        setComboboxOpen(kind, true);
        const panel = kind === "source" ? sourceOptions : targetList;
        panel.querySelector("button:not(:disabled), input:not(:disabled)")?.focus();
      } else if (event.key === "Enter" && kind === "source") {
        const option = sourceOptions.querySelector("button[data-endpoint-id]");
        if (option) { event.preventDefault(); option.click(); }
      }
    });
    toggle.addEventListener("click", () => {
      const panel = kind === "source" ? sourceOptions : targetList;
      setComboboxOpen(kind, panel.classList.contains("hidden"));
      input.focus();
    });
  }

  function renderSourceOptions() {
    const matches = matchingEndpoints(sourceQuery.value);
    sourceOptions.replaceChildren();
    sourceState.textContent = "匹配到 " + matches.length + " 个分支";
    if (!matches.length) {
      const empty = document.createElement("div");
      empty.className = "diff-plan-pane-empty";
      empty.textContent = "没有匹配的 SVN 分支";
      sourceOptions.append(empty);
      return;
    }
    matches.forEach((endpoint) => {
      const button = document.createElement("button");
      button.type = "button";
      button.role = "option";
      button.dataset.endpointId = endpoint.id;
      button.className = "diff-plan-endpoint-option" + (endpoint.id === source.value ? " is-selected" : "");
      button.setAttribute("aria-selected", String(endpoint.id === source.value));
      const name = document.createElement("span");
      name.textContent = endpointBranch(endpoint);
      const meta = document.createElement("small");
      meta.textContent = endpoint.region + " · " + endpoint.track + (endpoint.pendingRegistration ? " · SVN 候选" : "");
      button.append(name, meta);
      button.addEventListener("click", () => requestSource(endpoint.id));
      sourceOptions.append(button);
    });
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
    targetState.textContent = "已选择 " + state.selectedTargets.size + " 个 · 共 " + matchingEndpoints("", { excludeSource: true }).length + " 个可选";
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
    const choices = matchingEndpoints(targetQuery.value, { excludeSource: true });
    targetList.replaceChildren();
    if (!choices.length) {
      const empty = document.createElement("div");
      empty.className = "diff-plan-pane-empty";
      empty.textContent = state.endpoints.length ? "没有匹配的目标分支" : "没有其他可用分支";
      targetList.append(empty);
      syncCounts();
      return;
    }
    choices.forEach((item) => {
      const row = document.createElement("label");
      row.className = "diff-plan-choice";
      row.dataset.targetChoice = item.id;
      row.role = "option";
      row.setAttribute("aria-selected", String(state.selectedTargets.has(item.id)));
      const checkbox = document.createElement("input");
      checkbox.type = "checkbox";
      checkbox.checked = state.selectedTargets.has(item.id);
      checkbox.addEventListener("change", () => {
        if (checkbox.checked) state.selectedTargets.add(item.id);
        else {
          state.selectedTargets.delete(item.id);
          state.revisions.delete(item.id);
        }
        row.setAttribute("aria-selected", String(checkbox.checked));
        syncCounts();
      });
      const label = document.createElement("span");
      label.textContent = endpointBranch(item);
      const meta = document.createElement("small");
      meta.textContent = item.region + " · " + item.track + (item.pendingRegistration ? " · SVN 候选" : "");
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
      await ensureEndpointsRegistered([endpointId]);
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
      renderSourceOptions();
    } catch (error) {
      state.workbooks = [];
      workbookList.innerHTML = '<div class="diff-plan-pane-empty">TABLE 清单加载失败</div>';
      catalogContext.textContent = "未能读取基准分支";
      showError(error.message);
    }
  }

  function applySource(endpointId, { reset = false } = {}) {
    const endpoint = endpointById(endpointId);
    source.value = endpointId;
    sourceQuery.value = endpoint ? endpointBranch(endpoint) : endpointId;
    setComboboxOpen("source", false);
    if (reset) {
      state.selectedWorkbooks.clear();
      state.revisions.clear();
    }
    state.selectedTargets.delete(endpointId);
    targetQuery.disabled = false;
    targetToggle.disabled = false;
    targetQuery.value = "";
    renderTargets();
    void loadCatalog(endpointId);
  }

  function requestSource(endpointId) {
    if (!endpointId || endpointId === source.value) {
      setComboboxOpen("source", false);
      return;
    }
    const previous = state.initialSource || source.value;
    if (previous && state.selectedWorkbooks.size) {
      state.pendingSource = endpointId;
      sourceDialog.showModal();
      return;
    }
    state.initialSource = endpointId;
    applySource(endpointId);
  }

  async function loadEndpoints() {
    const [config, registry] = await Promise.all([api("/api/svn/config"), api("/api/svn/endpoints")]);
    let matches = [];
    if (config.server_url) {
      try {
        const params = new URLSearchParams({ url: config.server_url, revision: "HEAD" });
        const candidates = await api("/api/svn/branch-candidates?" + params.toString());
        matches = candidates.matches || [];
      } catch {
        matches = [];
      }
    }
    mergeEndpointSources(registry.endpoints || [], matches);
    source.replaceChildren();
    const placeholder = document.createElement("option");
    placeholder.value = "";
    placeholder.textContent = "选择基准分支";
    source.append(placeholder);
    state.endpoints.forEach((item) => {
      const option = document.createElement("option");
      option.value = item.id;
      option.textContent = item.label;
      source.append(option);
    });
    source.disabled = false;
    sourceQuery.disabled = false;
    sourceToggle.disabled = false;
    sourceState.textContent = "共 " + state.endpoints.length + " 个 SVN 分支";
    renderSourceOptions();
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
      await ensureEndpointsRegistered([source.value, ...state.selectedTargets]);
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

  source.addEventListener("change", () => requestSource(source.value));
  sourceDialog.addEventListener("close", () => {
    if (sourceDialog.returnValue === "confirm" && state.pendingSource) {
      state.initialSource = state.pendingSource;
      applySource(state.pendingSource, { reset: true });
    }
    state.pendingSource = "";
  });
  bindComboboxInput(sourceQuery, sourceToggle, "source");
  bindComboboxInput(targetQuery, targetToggle, "target");
  bindOptionKeyboard(sourceOptions, "source");
  bindOptionKeyboard(targetList, "target");
  document.addEventListener("click", (event) => {
    if (!document.getElementById("source-combobox").contains(event.target)) setComboboxOpen("source", false);
    if (!document.getElementById("target-combobox").contains(event.target)) setComboboxOpen("target", false);
  });
  workbookQuery.addEventListener("input", renderWorkbooks);
  saveOnly.addEventListener("click", () => void save({ run: false }));
  form.addEventListener("submit", (event) => { event.preventDefault(); void save({ run: true }); });

  (async () => {
    try {
      await loadEndpoints();
      if (planId) await loadPlan();
      else {
        targetList.replaceChildren();
        const empty = document.createElement("div");
        empty.className = "diff-plan-pane-empty";
        empty.textContent = "请先选择基准分支";
        targetList.append(empty);
      }
    } catch (error) {
      showError(error.message);
      source.disabled = true;
      sourceQuery.disabled = true;
      sourceToggle.disabled = true;
    }
  })();
})();
