(() => {
  const PAGE_STATES = new Set(["idle", "snapshot_loading", "snapshot_error", "candidates_ready"]);
  const DIFF_STATES = new Set(["idle", "diff_loading", "diff_empty", "diff_error", "diff_ready"]);
  const FILE_STATUS = {
    modified: { label: "内容变化", mark: "M" },
    left_only: { label: "仅左侧", mark: "−" },
    right_only: { label: "仅右侧", mark: "+" },
    read_error: { label: "读取失败", mark: "!" },
  };
  const TASK_CONTEXT_KEY = "excelDiffTaskContext";
  const MOCK_WORKBOOKS = [
    {
      path: "Table/Config_Item.xlsx",
      status: "modified",
      sourceFile: { path: "Table/Config_Item.xlsx", size: 184320, revision: 1042, author: "designer.a" },
      targetFile: { path: "Table/Config_Item.xlsx", size: 190464, revision: 1087, author: "designer.b" },
      resultState: "diff_ready",
      sheets: [
      {
        id: "Items",
        label: "Items",
        rows: [
          { key: "10001", label: "生命药水", change: "modified", fields: [
            { name: "Name", oldValue: "初级生命药水", newValue: "生命药水·小", location: "Items!B12" },
            { name: "SellPrice", oldValue: "50", newValue: "60", location: "Items!F12" },
          ] },
          { key: "10002", label: "魔力药水", change: "modified", fields: [
            { name: "Cooldown", oldValue: "3.0", newValue: "2.5", location: "Items!H13" },
            { name: "Weight", oldValue: "1.0", newValue: "0.8", location: "Items!J13" },
          ] },
          { key: "10008", label: "耐力合剂", change: "added", fields: [
            { name: "整行", oldValue: "—", newValue: "新增道具配置", location: "Items!A19:J19" },
          ] },
          { key: "10010", label: "旧版恢复剂", change: "deleted", fields: [
            { name: "整行", oldValue: "旧版道具配置", newValue: "—", location: "Items!A21:J21" },
          ] },
        ],
      },
      {
        id: "CharacterLevel",
        label: "CharacterLevel",
        rows: [
          { key: "Lv.20", label: "20级成长", change: "modified", fields: [
            { name: "MaxHP", oldValue: "1280", newValue: "1320", location: "CharacterLevel!D22" },
            { name: "Attack", oldValue: "186", newValue: "192", location: "CharacterLevel!E22" },
          ] },
          { key: "Lv.21", label: "21级成长", change: "added", fields: [
            { name: "整行", oldValue: "—", newValue: "新增等级配置", location: "CharacterLevel!A23:F23" },
          ] },
        ],
      },
        { id: "DropTable", label: "DropTable", rows: [] },
      ],
    },
    {
      path: "Table/Config_Npc.xlsx",
      status: "modified",
      sourceFile: { path: "Table/Config_Npc.xlsx", size: 96256, revision: 1042, author: "designer.c" },
      targetFile: { path: "Table/Config_Npc.xlsx", size: 97792, revision: 1087, author: "designer.c" },
      resultState: "diff_ready",
      sheets: [{
        id: "Npc",
        label: "Npc",
        rows: [
          { key: "NPC_204", label: "港口商人", change: "modified", fields: [
            { name: "ShopId", oldValue: "SHOP_07", newValue: "SHOP_12", location: "Npc!G35" },
            { name: "TalkGroup", oldValue: "TALK_204_A", newValue: "TALK_204_B", location: "Npc!K35" },
          ] },
        ],
      }],
    },
    {
      path: "Table/Localization.xlsx",
      status: "modified",
      sourceFile: { path: "Table/Localization.xlsx", size: 420864, revision: 1042, author: "localization" },
      targetFile: { path: "Table/Localization.xlsx", size: 421120, revision: 1087, author: "localization" },
      resultState: "diff_empty",
      sheets: [],
    },
    {
      path: "Table/Legacy_Config.xls",
      status: "read_error",
      sourceFile: { path: "Table/Legacy_Config.xls", size: 58368, revision: 1042, author: "system", error: { code: "UNSUPPORTED_SAMPLE" } },
      targetFile: { path: "Table/Legacy_Config.xls", size: 58368, revision: 1087, author: "system" },
      resultState: "diff_error",
      error: "UI 示例：旧版样本读取失败，未生成差异结果。",
      sheets: [],
    },
  ];

  const state = {
    endpoints: new Map(),
    registryRecords: [],
    sourceId: "",
    targetId: "",
    busy: false,
    snapshot: null,
    candidates: [],
    selectedPath: "",
    selectedResultPath: "",
    comparisonBusy: false,
    mockMode: false,
    results: new Map(),
    pageState: "idle",
  };

  const $ = (id) => document.getElementById(id);
  const sourceInput = $("source-endpoint");
  const targetInput = $("target-endpoint");
  const swapButton = $("swap-endpoints");
  const snapshotButton = $("create-snapshot");
  const candidateSearch = $("candidate-search");
  const candidateStatusFilter = $("candidate-status-filter");
  let progressTimer = null;
  let progressStartedAt = 0;

  async function request(path, options = {}) {
    const response = await fetch(path, {
      headers: { "Content-Type": "application/json", ...(options.headers || {}) },
      ...options,
    });
    const body = await response.json().catch(() => ({}));
    if (!response.ok) throw body;
    return body;
  }

  function requestId() {
    if (globalThis.crypto?.randomUUID) return globalThis.crypto.randomUUID();
    return "00000000-0000-4000-8000-" + String(Date.now()).padStart(12, "0").slice(-12);
  }

  function setExecuteDiffLabel(label) {
    const labelNode = $("execute-diff").firstChild;
    if (labelNode?.nodeType === Node.TEXT_NODE) labelNode.nodeValue = `${label} `;
  }

  function errorMessage(error) {
    const payload = error && error.error ? error.error : error;
    return `${payload?.code || "REQUEST_FAILED"}：${payload?.message || "请求失败"}`;
  }

  function setFormError(message = "") {
    const box = $("endpoint-form-error");
    box.textContent = message;
    box.classList.toggle("hidden", !message);
  }

  function setDot(id, text, kind = "unknown") {
    const dot = $(id);
    if (!dot) return;
    dot.textContent = text;
    dot.className = `status-dot status-${kind}`;
  }

  function setProcess() {}

  function setPageState(nextState, detail = "") {
    if (!PAGE_STATES.has(nextState)) return;
    state.pageState = nextState;
    document.body.dataset.pageState = nextState;
    const copy = {
      idle: ["等待选择端点", detail || "未创建快照", "snapshot"],
      snapshot_loading: ["正在生成快照", detail || "冻结 HEAD 并读取文件", "snapshot"],
      snapshot_error: ["快照读取失败", detail || "保留端点，可直接重试", "snapshot"],
      candidates_ready: ["文件候选已就绪", detail || "请选择一个工作簿", "candidates"],
    }[nextState];
    $("current-task-state").textContent = copy[0];
    $("current-task-detail").textContent = copy[1];
    setProcess(copy[2]);
  }

  function setDiffState(nextState, detail = "") {
    if (!DIFF_STATES.has(nextState)) return;
    const workbench = $("diff-workbench");
    workbench.dataset.diffState = nextState;
    workbench.querySelectorAll(".diff-state-view").forEach((view) => {
      view.classList.toggle("is-active", view.dataset.state === nextState);
    });
    const badges = {
      idle: "未执行",
      diff_loading: "解析中",
      diff_empty: "已完成 · 无差异",
      diff_error: "执行失败",
      diff_ready: "结果已就绪",
    };
    $("diff-state-badge").textContent = badges[nextState];
    if (nextState === "diff_loading" && detail) $("diff-loading-detail").textContent = detail;
    if (nextState === "diff_error" && detail) $("diff-error-detail").textContent = detail;
  }

  function updateProgressElapsed() {
    const elapsed = Math.max(0, Math.floor((Date.now() - progressStartedAt) / 1000));
    $("snapshot-progress-elapsed").textContent = `已耗时 ${elapsed} 秒`;
  }

  function setSnapshotProgress(text) {
    $("snapshot-progress-text").textContent = text;
    updateProgressElapsed();
  }

  function startSnapshotProgress() {
    const box = $("snapshot-progress");
    const bar = $("snapshot-progress-bar");
    box.classList.remove("hidden", "is-error", "is-complete");
    bar.removeAttribute("style");
    progressStartedAt = Date.now();
    setSnapshotProgress("正在准备两个端点");
    if (progressTimer) clearInterval(progressTimer);
    progressTimer = setInterval(updateProgressElapsed, 1000);
  }

  function finishSnapshotProgress(success) {
    if (progressTimer) clearInterval(progressTimer);
    progressTimer = null;
    updateProgressElapsed();
    const box = $("snapshot-progress");
    box.classList.toggle("is-complete", success);
    box.classList.toggle("is-error", !success);
    setSnapshotProgress(success ? "完成：已生成文件级差异候选" : "读取失败：请检查错误信息");
  }

  function formatUrl(url) {
    if (!url) return "请选择一个已启用端点";
    try {
      const parsed = new URL(url);
      return `${parsed.origin}${parsed.pathname}`;
    } catch {
      return url;
    }
  }

  function endpointIdForMatch(match) {
    return `${match.region}_${match.track}_${match.branch}`.replace(/[^A-Za-z0-9._-]/g, "_").slice(0, 128);
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
    state.endpoints = new Map();
    state.registryRecords.forEach((record) => state.endpoints.set(record.id, { ...record, pendingRegistration: false }));
    (matches || []).forEach((match) => {
      const candidate = recordFromMatch(match);
      if (!state.endpoints.has(candidate.id)) state.endpoints.set(candidate.id, candidate);
    });
  }

  function scopePath(endpoint) {
    const path = endpoint?.physical_path_filters?.TABLE;
    return path ? `物理绑定：${path}` : "确认时发现 Table 物理路径";
  }

  function renderSide(side, endpointId) {
    const endpoint = state.endpoints.get(endpointId);
    const prefix = side === "source" ? "source" : "target";
    if (!endpoint) {
      $(`${prefix}-label`).textContent = "未选择";
      $(`${prefix}-url`).textContent = "请选择一个已启用端点或匹配分支";
      $(`${prefix}-scope-detail`).textContent = "全量 Excel";
      setDot(`${prefix}-status`, "待选择", "unknown");
      return;
    }
    $(`${prefix}-label`).textContent = endpoint.label;
    $(`${prefix}-url`).textContent = formatUrl(endpoint.url);
    $(`${prefix}-scope-detail`).textContent = scopePath(endpoint);
    setDot(`${prefix}-status`, endpoint.pendingRegistration ? "SVN 候选" : "已登记", endpoint.pendingRegistration ? "unknown" : "ok");
  }

  function endpointDirectoryName(endpoint) {
    if (endpoint?.branch) return endpoint.branch;
    const labelParts = String(endpoint?.label || "").split("·");
    if (labelParts.length > 1) return labelParts[labelParts.length - 1].trim();
    try {
      const path = new URL(endpoint?.url || "").pathname.replace(/\/$/, "");
      return decodeURIComponent(path.split("/").pop() || "");
    } catch {
      return String(endpoint?.label || "");
    }
  }

  function matchingEndpoints(query) {
    const normalized = String(query || "").trim().toLocaleLowerCase();
    return [...state.endpoints.values()]
      .filter((endpoint) => !normalized || [endpointDirectoryName(endpoint), endpoint.label, endpoint.url]
        .some((value) => String(value || "").toLocaleLowerCase().includes(normalized)))
      .sort((a, b) => endpointDirectoryName(a).localeCompare(endpointDirectoryName(b), "zh-CN"));
  }

  function renderEndpointMatches(side, query = "") {
    const prefix = side === "source" ? "source" : "target";
    const list = $(`${prefix}-endpoint-matches`);
    const status = $(`${prefix}-match-state`);
    list.textContent = "";
    const matches = matchingEndpoints(query);
    if (!state.endpoints.size) {
      status.textContent = "暂无可用分支，请检查 SVN 连接和系统配置";
      return;
    }
    status.textContent = query.trim()
      ? `匹配到 ${matches.length} 个分支${matches.length > 12 ? "，仅展示前 12 个" : ""}`
      : `共 ${matches.length} 个可选分支，输入目录名开始匹配`;
    matches.slice(0, 12).forEach((endpoint) => {
      const button = document.createElement("button");
      button.type = "button";
      button.className = `endpoint-match-item${endpoint.id === (side === "source" ? state.sourceId : state.targetId) ? " is-selected" : ""}`;
      const name = document.createElement("span");
      name.textContent = endpointDirectoryName(endpoint);
      const source = document.createElement("small");
      source.textContent = endpoint.pendingRegistration ? "SVN 候选" : "已登记";
      button.append(name, source);
      button.addEventListener("click", () => {
        $(`${prefix}-endpoint`).value = endpointDirectoryName(endpoint);
        setSelection(side, endpoint.id);
        renderEndpointMatches(side, $(`${prefix}-endpoint`).value);
      });
      list.appendChild(button);
    });
  }

  function populateEndpointInput(input, side, selectedId) {
    const endpoint = state.endpoints.get(selectedId);
    input.value = endpoint ? endpointDirectoryName(endpoint) : "";
    input.disabled = state.endpoints.size === 0 || state.busy;
    renderEndpointMatches(side, input.value);
  }

  function updateControls() {
    const validPair = Boolean(
      state.sourceId && state.targetId && state.sourceId !== state.targetId
        && state.endpoints.has(state.sourceId) && state.endpoints.has(state.targetId),
    );
    sourceInput.disabled = state.busy || state.endpoints.size === 0;
    targetInput.disabled = state.busy || state.endpoints.size === 0;
    swapButton.disabled = !validPair || state.busy;
    snapshotButton.disabled = !validPair || state.busy;
    $("snapshot-button-state").textContent = state.busy ? "读取中" : (validPair ? "可执行" : "先选择两个端点");
    const connection = $("endpoint-connection-state");
    const dot = connection.querySelector(".status-dot");
    const strong = connection.querySelector("strong");
    const small = connection.querySelector("small");
    if (validPair) {
      dot.className = "status-dot status-ok";
      strong.textContent = "两个端点已准备";
      small.textContent = "确认后登记候选并分别冻结 HEAD";
    } else {
      dot.className = "status-dot status-unknown";
      strong.textContent = state.endpoints.size ? "请选择两个不同端点" : "暂无可选端点";
      small.textContent = state.endpoints.size ? "左右端点不能相同" : "请先登记或匹配到分支";
    }
    updateComparisonControls();
  }

  function setSelection(side, endpointId) {
    if (side === "source") state.sourceId = endpointId;
    else state.targetId = endpointId;
    renderSide(side, endpointId);
    updateControls();
  }

  function formatBytes(value) {
    const bytes = Number(value || 0);
    if (!bytes) return "0 B";
    if (bytes < 1024) return `${bytes} B`;
    if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KB`;
    return `${(bytes / 1024 / 1024).toFixed(1)} MB`;
  }

  function relativeTablePath(file, endpoint) {
    const path = String(file?.path || "").replace(/\\/g, "/");
    const physical = String(endpoint?.physical_path_filters?.TABLE || "").replace(/\\/g, "/").replace(/^\/+|\/+$/g, "");
    if (!physical) return path;
    const foldedPath = path.toLocaleLowerCase();
    const foldedPhysical = physical.toLocaleLowerCase();
    if (foldedPath === foldedPhysical) return "";
    if (foldedPath.startsWith(`${foldedPhysical}/`)) return path.slice(physical.length + 1);
    return path;
  }

  function buildDifferenceFiles(sourceFiles, targetFiles, sourceEndpoint, targetEndpoint) {
    const sourceMap = new Map((sourceFiles || []).map((file) => [relativeTablePath(file, sourceEndpoint), file]));
    const targetMap = new Map((targetFiles || []).map((file) => [relativeTablePath(file, targetEndpoint), file]));
    const paths = new Set([...sourceMap.keys(), ...targetMap.keys()]);
    return [...paths].sort((a, b) => a.localeCompare(b, "zh-CN"))
      .filter((path) => {
        const source = sourceMap.get(path);
        const target = targetMap.get(path);
        if (!source || !target) return true;
        if (source.error || target.error) return true;
        return source.content_hash !== target.content_hash;
      })
      .map((path) => {
        const sourceFile = sourceMap.get(path) || null;
        const targetFile = targetMap.get(path) || null;
        let status = "modified";
        if (!sourceFile) status = "right_only";
        else if (!targetFile) status = "left_only";
        else if (sourceFile.error || targetFile.error) status = "read_error";
        return { path, status, sourceFile, targetFile };
      });
  }

  function fileMeta(file) {
    if (!file) return "文件不存在";
    if (file.error) return `读取失败 · ${file.error.code || "UNKNOWN"}`;
    const revision = file.revision ? `r${file.revision}` : "无版本";
    return `${revision} · ${formatBytes(file.size)}${file.author ? ` · ${file.author}` : ""}`;
  }

  function fileName(path) {
    return String(path || "").replace(/\\/g, "/").split("/").pop() || "—";
  }

  function selectedCandidate() {
    return state.candidates.find((candidate) => candidate.path === state.selectedPath) || null;
  }

  function updateComparisonControls() {
    const hasCandidates = state.candidates.length > 0;
    $("execute-diff-count").textContent = String(state.candidates.length);
    $("execute-diff").disabled = state.busy || !hasCandidates;
    const resultsLink = $("results-page-link");
    try {
      const stored = JSON.parse(sessionStorage.getItem(TASK_CONTEXT_KEY) || "null");
      if (stored?.candidates?.length) {
        resultsLink.href = stored.mode === "demo" ? "/compare/demo/results" : "/compare/results";
        resultsLink.classList.remove("is-disabled");
        resultsLink.removeAttribute("aria-disabled");
      }
    } catch {
      sessionStorage.removeItem(TASK_CONTEXT_KEY);
    }
  }

  function visibleCandidates() {
    const query = candidateSearch.value.trim().toLocaleLowerCase();
    const status = candidateStatusFilter.value;
    return state.candidates.filter((candidate) => {
      const matchesQuery = !query || candidate.path.toLocaleLowerCase().includes(query);
      const matchesStatus = status === "all" || candidate.status === status;
      return matchesQuery && matchesStatus;
    });
  }

  function createStatusTag(status) {
    const meta = FILE_STATUS[status];
    const tag = document.createElement("span");
    tag.className = `file-status file-status-${status}`;
    const mark = document.createElement("b");
    mark.textContent = meta.mark;
    tag.append(mark, meta.label);
    return tag;
  }

  function renderManifest() {
    const body = $("manifest-body");
    const files = visibleCandidates();
    body.textContent = "";
    if (!files.length) {
      const row = document.createElement("tr");
      row.className = "manifest-empty";
      const cell = document.createElement("td");
      cell.colSpan = 5;
      const symbol = document.createElement("div");
      symbol.className = "empty-symbol";
      symbol.textContent = state.candidates.length ? "⌕" : "□";
      const title = document.createElement("strong");
      title.textContent = state.candidates.length ? "没有匹配的候选文件" : (state.snapshot ? "快照已完成，没有文件级差异" : "尚未生成文件候选");
      const detail = document.createElement("span");
      detail.textContent = state.candidates.length ? "调整搜索词或文件状态筛选。" : (state.snapshot ? "这是文件级结果，不代表语义 Diff 已执行。" : "锁定快照后在此显示新增、删除和内容变化文件。");
      cell.append(symbol, title, detail);
      row.appendChild(cell);
      body.appendChild(row);
    } else {
      files.forEach((candidate) => {
        const row = document.createElement("tr");
        row.className = "candidate-row";
        const statusCell = document.createElement("td");
        statusCell.appendChild(createStatusTag(candidate.status));
        const pathCell = document.createElement("td");
        const code = document.createElement("code");
        code.textContent = candidate.path;
        pathCell.appendChild(code);
        const sourceCell = document.createElement("td");
        sourceCell.textContent = fileMeta(candidate.sourceFile);
        const targetCell = document.createElement("td");
        targetCell.textContent = fileMeta(candidate.targetFile);
        const parseCell = document.createElement("td");
        const parseTag = document.createElement("span");
        parseTag.className = candidate.status === "read_error" ? "parse-status is-error" : "parse-status";
        parseTag.textContent = candidate.status === "read_error" ? "文件读取失败" : "待语义引擎";
        parseCell.appendChild(parseTag);
        row.append(statusCell, pathCell, sourceCell, targetCell, parseCell);
        body.appendChild(row);
      });
    }
    $("candidate-count").textContent = `${state.candidates.length} 个候选`;
    $("manifest-caption").textContent = state.snapshot
      ? `当前显示 ${files.length} / ${state.candidates.length} 个文件级候选`
      : "快照未执行";
    updateComparisonControls();
  }

  function contextSource(endpoint, file, side) {
    if (!file) return `${side === "old" ? "Old" : "New"} 侧不存在该文件`;
    if (!endpoint) return `本地 ${side === "old" ? "Old" : "New"} 样本 · ${formatBytes(file.size)}`;
    return `${endpoint.label} · 冻结 r${endpoint.resolved_revision}`;
  }

  function resetDetail() {
    $("detail-state").textContent = "未选择";
    $("detail-field").textContent = "—";
    $("detail-old-value").textContent = "—";
    $("detail-new-value").textContent = "—";
    $("detail-location").textContent = "—";
    $("detail-caption").textContent = "选择真实单元格差异后显示详情。";
  }

  function renderEmptySheetNavigation() {
    const navigation = $("sheet-navigation");
    navigation.className = "sheet-empty";
    navigation.textContent = "";
    const icon = document.createElement("span");
    icon.setAttribute("aria-hidden", "true");
    icon.textContent = "▦";
    const title = document.createElement("strong");
    title.textContent = "暂无 Sheet 数据";
    const detail = document.createElement("small");
    detail.textContent = "后续语义引擎接入后显示状态和差异数。";
    navigation.append(icon, title, detail);
  }

  function resetResultArea() {
    state.results = new Map();
    state.selectedResultPath = "";
    $("diff-workbench").classList.remove("is-mock-preview");
    $("mock-result-notice").classList.add("hidden");
    $("results-gate").classList.remove("hidden");
    $("results-content").classList.add("hidden");
    $("toggle-detail").disabled = true;
    $("workbook-count").textContent = "0";
    const workbookNavigation = $("workbook-navigation");
    workbookNavigation.className = "workbook-empty";
    workbookNavigation.textContent = "";
    const workbookIcon = document.createElement("span");
    workbookIcon.setAttribute("aria-hidden", "true");
    workbookIcon.textContent = "▤";
    const workbookTitle = document.createElement("strong");
    workbookTitle.textContent = "暂无比对结果";
    const workbookDetail = document.createElement("small");
    workbookDetail.textContent = "完成阶段 2 后显示工作簿状态和差异数。";
    workbookNavigation.append(workbookIcon, workbookTitle, workbookDetail);
    $("sheet-count").textContent = "0";
    renderEmptySheetNavigation();
    const tableBody = $("semantic-table-body");
    tableBody.textContent = "";
    const empty = document.createElement("div");
    empty.className = "semantic-table-empty";
    empty.textContent = "结果容器已就绪，等待语义引擎注入真实数据。";
    tableBody.appendChild(empty);
    resetDetail();
    setDiffState("idle");
  }

  function sheetDiffCount(sheet) {
    return sheet.rows.reduce((count, row) => count + row.fields.length, 0);
  }

  function renderSheetNavigation(result, activeSheetId) {
    const navigation = $("sheet-navigation");
    navigation.className = "sheet-list";
    navigation.textContent = "";
    result.sheets.forEach((sheet) => {
      const count = sheetDiffCount(sheet);
      const button = document.createElement("button");
      button.type = "button";
      button.className = `sheet-nav-item${sheet.id === activeSheetId ? " is-selected" : ""}`;
      button.setAttribute("aria-pressed", String(sheet.id === activeSheetId));
      const name = document.createElement("strong");
      name.textContent = sheet.label;
      const meta = document.createElement("span");
      meta.className = `sheet-status${count ? " is-changed" : ""}`;
      meta.textContent = count ? `修改 · ${count}` : "无差异 · 0";
      button.append(name, meta);
      button.addEventListener("click", () => renderResultSheet(result, sheet.id));
      navigation.appendChild(button);
    });
  }

  function updateResultDetail(field, button) {
    document.querySelectorAll(".field-diff-button.is-selected").forEach((current) => current.classList.remove("is-selected"));
    button?.classList.add("is-selected");
    $("detail-state").textContent = "UI 示例";
    $("detail-field").textContent = field.name;
    $("detail-old-value").textContent = field.oldValue;
    $("detail-new-value").textContent = field.newValue;
    $("detail-location").textContent = field.location;
    $("detail-caption").textContent = "当前内容来自开发模式 UI 假数据，不代表实际工作簿结果。";
  }

  function renderResultSheet(result, sheetId) {
    const sheet = result.sheets.find((item) => item.id === sheetId) || result.sheets[0];
    if (!sheet) return;
    renderSheetNavigation(result, sheet.id);
    $("sheet-count").textContent = String(result.sheets.length);
    const tableBody = $("semantic-table-body");
    tableBody.textContent = "";
    if (!sheet.rows.length) {
      const empty = document.createElement("div");
      empty.className = "semantic-table-empty";
      empty.textContent = "UI 示例：该 Sheet 已完成且没有差异。";
      tableBody.appendChild(empty);
      resetDetail();
      $("detail-caption").textContent = "当前 Sheet 没有可选择的示例差异。";
      return;
    }

    let firstField = null;
    let firstButton = null;
    const changeLabels = { modified: "修改", added: "新增", deleted: "删除" };
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
      status.className = `row-change-status is-${row.change}`;
      status.textContent = changeLabels[row.change];
      const fields = document.createElement("div");
      fields.className = "field-diff-list";
      row.fields.forEach((field) => {
        const button = document.createElement("button");
        button.type = "button";
        button.className = "field-diff-button";
        button.setAttribute("aria-label", `${row.key} ${field.name}：${field.oldValue} 变为 ${field.newValue}`);
        const name = document.createElement("strong");
        name.textContent = field.name;
        const values = document.createElement("span");
        values.textContent = `${field.oldValue} → ${field.newValue}`;
        button.append(name, values);
        button.addEventListener("click", () => updateResultDetail(field, button));
        fields.appendChild(button);
        if (!firstField) {
          firstField = field;
          firstButton = button;
        }
      });
      rowElement.append(key, status, fields);
      tableBody.appendChild(rowElement);
    });
    updateResultDetail(firstField, firstButton);
  }

  function resultFieldCount(result) {
    return result.sheets.reduce((total, sheet) => total + sheetDiffCount(sheet), 0);
  }

  function renderResultContext(result) {
    const candidate = result.candidate;
    const source = state.snapshot?.source || null;
    const target = state.snapshot?.target || null;
    $("context-old-file").textContent = candidate.sourceFile ? fileName(candidate.sourceFile.path || candidate.path) : "文件不存在";
    $("context-new-file").textContent = candidate.targetFile ? fileName(candidate.targetFile.path || candidate.path) : "文件不存在";
    $("context-old-source").textContent = contextSource(source, candidate.sourceFile, "old");
    $("context-new-source").textContent = contextSource(target, candidate.targetFile, "new");
    $("context-path").textContent = candidate.path;
    $("context-file-status").textContent = FILE_STATUS[candidate.status]?.label || "文件候选";
  }

  function renderWorkbookNavigation() {
    const navigation = $("workbook-navigation");
    const results = [...state.results.values()];
    navigation.className = "workbook-list";
    navigation.textContent = "";
    $("workbook-count").textContent = String(results.length);
    const labels = {
      diff_ready: (result) => `${resultFieldCount(result)} 个字段差异`,
      diff_empty: () => "无语义差异",
      diff_error: () => "执行失败",
    };
    results.forEach((result) => {
      const button = document.createElement("button");
      button.type = "button";
      button.className = `workbook-nav-item is-${result.state}${result.candidate.path === state.selectedResultPath ? " is-selected" : ""}`;
      button.setAttribute("aria-pressed", String(result.candidate.path === state.selectedResultPath));
      const name = document.createElement("strong");
      name.textContent = fileName(result.candidate.path);
      const path = document.createElement("small");
      path.textContent = result.candidate.path;
      const meta = document.createElement("span");
      meta.textContent = (labels[result.state] || (() => "未知状态"))(result);
      button.append(name, path, meta);
      button.addEventListener("click", () => selectComparisonResult(result.candidate.path));
      navigation.appendChild(button);
    });
  }

  function selectComparisonResult(path) {
    const result = state.results.get(path);
    if (!result) return;
    state.selectedResultPath = path;
    $("results-gate").classList.add("hidden");
    $("results-content").classList.remove("hidden");
    $("toggle-detail").disabled = false;
    $("diff-workbench").classList.toggle("is-mock-preview", state.mockMode);
    $("mock-result-notice").classList.toggle("hidden", !state.mockMode);
    renderResultContext(result);
    renderWorkbookNavigation();
    resetDetail();

    if (result.state === "diff_error") {
      $("sheet-count").textContent = "0";
      renderEmptySheetNavigation();
      setDiffState("diff_error", result.error || "差异比对失败。请查看任务日志。");
      $("workbench-caption").textContent = `工作簿 ${fileName(path)} 比对失败，结果未降级为空差异。`;
    } else if (result.state === "diff_empty") {
      $("sheet-count").textContent = "0";
      renderEmptySheetNavigation();
      setDiffState("diff_empty");
      $("workbench-caption").textContent = `工作簿 ${fileName(path)} 已完成，未发现语义差异。`;
    } else {
      setDiffState("diff_ready");
      renderResultSheet(result, result.sheets[0]?.id);
      $("diff-state-badge").textContent = state.mockMode
        ? `UI 示例 · ${resultFieldCount(result)} 个字段`
        : `已完成 · ${resultFieldCount(result)} 个字段`;
      $("workbench-caption").textContent = `正在查看 ${fileName(path)}，可继续按 Sheet、业务主键和字段下钻。`;
    }
    setProcess("workbench");
    $("current-task-state").textContent = "差异结果已就绪";
    $("current-task-detail").textContent = `${fileName(path)} · ${state.mockMode ? "UI 示例数据" : "语义 Diff 结果"}`;
  }

  function mockResultFor(candidate) {
    const workbook = MOCK_WORKBOOKS.find((item) => item.path === candidate.path);
    return {
      candidate,
      state: workbook?.resultState || "diff_error",
      error: workbook?.error || "UI 示例中没有该工作簿的结果数据。",
      sheets: workbook?.sheets || [],
    };
  }

  function wait(milliseconds) {
    return new Promise((resolve) => window.setTimeout(resolve, milliseconds));
  }

  async function executeComparison(scope) {
    const targets = scope === "current" ? [selectedCandidate()].filter(Boolean) : [...state.candidates];
    if (!targets.length || state.comparisonBusy) return;
    if (!state.mockMode) {
      $("comparison-section").dataset.comparisonState = "unavailable";
      $("comparison-state-badge").textContent = "后端未接入";
      $("comparison-message").textContent = "语义 Diff API 尚未接入；本次没有执行，也没有生成或伪造差异结果。";
      $("current-task-state").textContent = "差异比对不可用";
      $("current-task-detail").textContent = "等待后续语义 Diff API";
      return;
    }

    state.comparisonBusy = true;
    resetResultArea();
    updateComparisonControls();
    $("comparison-section").dataset.comparisonState = "running";
    $("comparison-progress").classList.remove("hidden", "is-complete");
    $("comparison-progress-bar").style.width = "0%";
    $("comparison-progress-count").textContent = `0 / ${targets.length}`;
    $("comparison-progress-title").textContent = scope === "current" ? "正在比对当前工作簿" : "正在批量比对工作簿";
    $("comparison-message").textContent = "开发模式正在运行 UI 示例流程，不会读取或解析真实 Excel 内容。";
    setProcess("candidates");
    let completed = 0;
    const results = new Map();
    try {
      for (const candidate of targets) {
        $("comparison-progress-detail").textContent = `正在处理 ${fileName(candidate.path)}`;
        await wait(180);
        results.set(candidate.path, mockResultFor(candidate));
        completed += 1;
        $("comparison-progress-count").textContent = `${completed} / ${targets.length}`;
        $("comparison-progress-bar").style.width = `${Math.round((completed / targets.length) * 100)}%`;
      }
      state.results = results;
      $("comparison-progress").classList.add("is-complete");
      $("comparison-progress-title").textContent = "UI 示例比对已完成";
      $("comparison-progress-detail").textContent = `已生成 ${completed} 个明确标注的工作簿示例状态`;
      $("comparison-message").textContent = "以下内容均为开发模式 UI 假数据，仅用于测试页面流程与状态。";
      const preferredPath = results.has(state.selectedPath) ? state.selectedPath : results.keys().next().value;
      selectComparisonResult(preferredPath);
      $("diff-workbench").scrollIntoView({ behavior: "smooth", block: "start" });
    } finally {
      state.comparisonBusy = false;
      $("comparison-section").dataset.comparisonState = "complete";
      updateComparisonControls();
      if (completed) $("comparison-state-badge").textContent = `UI 示例完成 · ${completed} 个工作簿`;
    }
  }

  function openMockPreview() {
    state.mockMode = true;
    state.snapshot = {
      source: { label: "UI 示例 Old", resolved_revision: 1042 },
      target: { label: "UI 示例 New", resolved_revision: 1087 },
    };
    state.candidates = MOCK_WORKBOOKS.map(({ path, status, sourceFile, targetFile }) => ({ path, status, sourceFile, targetFile }));
    state.selectedPath = "";
    candidateSearch.value = "";
    candidateStatusFilter.value = "all";
    candidateSearch.disabled = false;
    candidateStatusFilter.disabled = false;
    clearTaskContext();
    $("source-file-count").textContent = "4";
    $("target-file-count").textContent = "4";
    $("source-revision").textContent = "UI 示例 Old · 冻结 r1042";
    $("target-revision").textContent = "UI 示例 New · 冻结 r1087";
    $("snapshot-total-size").textContent = "UI 示例";
    $("snapshot-state").textContent = "示例已载入";
    $("snapshot-summary-badge").textContent = "DEV · 非真实快照";
    $("snapshot-captured-at").textContent = "仅用于前端流程测试";
    $("registry-badge").textContent = "DEV · 4 个工作簿示例";
    renderManifest();
    setPageState("candidates_ready", `${state.candidates.length} 个 UI 示例候选`);
    $("current-task-state").textContent = "UI 示例候选已就绪";
    $("current-task-detail").textContent = "可执行全部候选并进入结果页";
  }

  function compactTaskFile(file) {
    if (!file) return null;
    return {
      path: file.path || "",
      size: Number(file.size || 0),
      revision: file.revision || null,
      author: file.author || "",
      error: file.error ? { code: file.error.code || "UNKNOWN" } : null,
    };
  }

  function clearTaskContext() {
    sessionStorage.removeItem(TASK_CONTEXT_KEY);
    const resultsLink = $("results-page-link");
    resultsLink.removeAttribute("href");
    resultsLink.classList.add("is-disabled");
    resultsLink.setAttribute("aria-disabled", "true");
  }

  async function openResultsPage() {
    if (!state.candidates.length) return;
    const sourceEndpoint = state.endpoints.get(state.sourceId);
    const targetEndpoint = state.endpoints.get(state.targetId);
    const context = {
      version: 2,
      mode: state.mockMode ? "demo" : "formal",
      capturedAt: state.snapshot?.captured_at || new Date().toISOString(),
      source: {
        endpointId: state.mockMode ? "" : state.sourceId,
        label: state.snapshot?.source?.label || "左侧",
        branch: state.mockMode ? "UI 示例 Old" : endpointDirectoryName(sourceEndpoint) || state.snapshot?.source?.label || "左侧",
        resolvedRevision: state.snapshot?.source?.resolved_revision || null,
      },
      target: {
        endpointId: state.mockMode ? "" : state.targetId,
        label: state.snapshot?.target?.label || "右侧",
        branch: state.mockMode ? "UI 示例 New" : endpointDirectoryName(targetEndpoint) || state.snapshot?.target?.label || "右侧",
        resolvedRevision: state.snapshot?.target?.resolved_revision || null,
      },
      candidates: state.candidates.map((candidate) => ({
        path: candidate.path,
        status: candidate.status,
        sourceFile: compactTaskFile(candidate.sourceFile),
        targetFile: compactTaskFile(candidate.targetFile),
      })),
      results: state.mockMode ? MOCK_WORKBOOKS : [],
    };
    if (!state.mockMode) {
      state.busy = true;
      const button = $("execute-diff");
      button.disabled = true;
      setExecuteDiffLabel("正在创建批量任务");
      $("current-task-state").textContent = "正在创建批量任务";
      $("current-task-detail").textContent = "服务端将按冻结 Revision 重建全部候选";
      try {
        const task = await request("/api/diff/batches", {
          method: "POST",
          body: JSON.stringify({
            schema_version: "m2.batch-create.request.v1",
            request_id: requestId(),
            source: {
              endpoint_id: state.sourceId,
              revision: Number(state.snapshot.source.resolved_revision),
            },
            target: {
              endpoint_id: state.targetId,
              revision: Number(state.snapshot.target.resolved_revision),
            },
          }),
        });
        context.batchTaskId = task.task_id;
        sessionStorage.setItem(TASK_CONTEXT_KEY, JSON.stringify(context));
        window.location.assign("/compare/results");
      } catch (error) {
        const message = errorMessage(error);
        setFormError(message);
        $("current-task-state").textContent = "批量任务创建失败";
        $("current-task-detail").textContent = message;
      } finally {
        state.busy = false;
        setExecuteDiffLabel("比对全部");
        updateControls();
      }
      return;
    }
    sessionStorage.setItem(TASK_CONTEXT_KEY, JSON.stringify(context));
    window.location.assign(state.mockMode ? "/compare/demo/results" : "/compare/results");
  }

  function renderSnapshot(snapshot) {
    state.mockMode = false;
    state.snapshot = snapshot;
    const source = snapshot.source;
    const target = snapshot.target;
    $("source-file-count").textContent = source.stats.file_count;
    $("target-file-count").textContent = target.stats.file_count;
    $("source-revision").textContent = `${source.label} · 冻结 r${source.resolved_revision}`;
    $("target-revision").textContent = `${target.label} · 冻结 r${target.resolved_revision}`;
    $("snapshot-total-size").textContent = formatBytes(source.stats.total_size + target.stats.total_size);
    $("snapshot-state").textContent = "已锁定";
    $("snapshot-summary-badge").textContent = "已生成 · Table Excel";
    $("snapshot-captured-at").textContent = `确认时间 ${new Date(snapshot.captured_at).toLocaleString("zh-CN")}`;
    state.candidates = buildDifferenceFiles(source.files, target.files, source, target);
    state.selectedPath = "";
    clearTaskContext();
    candidateSearch.disabled = false;
    candidateStatusFilter.disabled = false;
    renderManifest();
    setPageState("candidates_ready", `${state.candidates.length} 个文件级候选`);
  }

  async function loadEndpoints() {
    try {
      const config = await request("/api/svn/config");
      const registryBody = await request("/api/svn/endpoints");
      let matches = [];
      if (config.server_url) {
        try {
          const params = new URLSearchParams({ url: config.server_url, revision: "HEAD" });
          const candidateBody = await request(`/api/svn/branch-candidates?${params.toString()}`);
          matches = candidateBody.matches || [];
        } catch {
          matches = [];
        }
      }
      mergeEndpointSources(registryBody.endpoints || [], matches);
      populateEndpointInput(sourceInput, "source", state.sourceId);
      populateEndpointInput(targetInput, "target", state.targetId);
      renderSide("source", state.sourceId);
      renderSide("target", state.targetId);
      $("registry-badge").textContent = `${state.registryRecords.length} 个已登记 · ${matches.length} 个配置匹配候选`;
      setPageState("idle");
      updateControls();
    } catch (error) {
      $("registry-badge").textContent = "端点候选读取失败";
      setFormError(errorMessage(error));
      sourceInput.disabled = true;
      targetInput.disabled = true;
      setPageState("idle", "端点注册表读取失败");
    }
  }

  async function persistPendingCandidates() {
    const pending = [state.sourceId, state.targetId]
      .map((id) => state.endpoints.get(id))
      .filter((endpoint) => endpoint?.pendingRegistration);
    if (!pending.length) return;
    const records = new Map(state.registryRecords.map((record) => [record.id, record]));
    pending.forEach((endpoint) => {
      const { pendingRegistration, branch, match_type, ...record } = endpoint;
      records.set(record.id, record);
    });
    const body = await request("/api/svn/endpoints", {
      method: "POST",
      body: JSON.stringify({ endpoints: [...records.values()] }),
    });
    state.registryRecords = body.endpoints || [];
    state.registryRecords.forEach((record) => {
      const current = state.endpoints.get(record.id);
      state.endpoints.set(record.id, { ...record, pendingRegistration: false, branch: current?.branch, match_type: current?.match_type });
    });
    populateEndpointInput(sourceInput, "source", state.sourceId);
    populateEndpointInput(targetInput, "target", state.targetId);
    renderSide("source", state.sourceId);
    renderSide("target", state.targetId);
  }

  function handleEndpointInput(side, input) {
    const matches = matchingEndpoints(input.value);
    const query = input.value.trim().toLocaleLowerCase();
    const exact = matches.filter((endpoint) => endpointDirectoryName(endpoint).toLocaleLowerCase() === query);
    setSelection(side, exact.length === 1 ? exact[0].id : "");
    renderEndpointMatches(side, input.value);
  }

  sourceInput.addEventListener("input", () => handleEndpointInput("source", sourceInput));
  targetInput.addEventListener("input", () => handleEndpointInput("target", targetInput));
  swapButton.addEventListener("click", () => {
    [state.sourceId, state.targetId] = [state.targetId, state.sourceId];
    populateEndpointInput(sourceInput, "source", state.sourceId);
    populateEndpointInput(targetInput, "target", state.targetId);
    renderSide("source", state.sourceId);
    renderSide("target", state.targetId);
    updateControls();
  });

  snapshotButton.addEventListener("click", async () => {
    if (snapshotButton.disabled) return;
    state.busy = true;
    state.mockMode = false;
    state.candidates = [];
    state.selectedPath = "";
    clearTaskContext();
    renderManifest();
    startSnapshotProgress();
    setFormError("");
    $("snapshot-state").textContent = "读取中";
    $("snapshot-summary-badge").textContent = "正在登记并读取";
    setPageState("snapshot_loading");
    updateControls();
    try {
      setSnapshotProgress("正在登记所选候选分支");
      await persistPendingCandidates();
      setSnapshotProgress("正在冻结 HEAD 并读取两个 Table 快照");
      const snapshot = await request("/api/svn/snapshots", {
        method: "POST",
        body: JSON.stringify({ source: { endpoint_id: state.sourceId }, target: { endpoint_id: state.targetId } }),
      });
      renderSnapshot(snapshot);
      finishSnapshotProgress(true);
    } catch (error) {
      const message = errorMessage(error);
      $("snapshot-state").textContent = "失败";
      $("snapshot-summary-badge").textContent = "读取失败";
      finishSnapshotProgress(false);
      setFormError(message);
      setPageState("snapshot_error", message);
    } finally {
      state.busy = false;
      updateControls();
    }
  });

  candidateSearch.addEventListener("input", renderManifest);
  candidateStatusFilter.addEventListener("change", renderManifest);
  $("execute-diff").addEventListener("click", openResultsPage);

  window.ExcelDiffWorkbench = Object.freeze({ setState: setDiffState });
  renderManifest();
  const demoPage = document.body.dataset.demoMode === "true";
  if (demoPage) openMockPreview();
  else loadEndpoints();
})();
