(() => {
  const $ = (selector) => document.querySelector(selector);
  const urlInput = $("#svn-url");
  const errorBox = $("#error-box");
  const providerToggle = $("#provider-mode-toggle");
  const providerState = $("#provider-config-state");

  function endpoint() {
    return {
      url: urlInput.value.trim(),
      revision: "HEAD",
      path_filter: [],
    };
  }

  function showError(error) {
    const payload = error && error.error ? error.error : error;
    errorBox.textContent = (payload?.code || "REQUEST_FAILED") + "：" + (payload?.message || "请求失败");
    errorBox.classList.remove("hidden");
    $("#health-state").textContent = "连接失败";
    $("#health-state").className = "status-dot status-error";
  }

  function clearError() {
    errorBox.textContent = "";
    errorBox.classList.add("hidden");
  }

  async function request(path, options = {}) {
    const response = await fetch(path, { headers: { "Content-Type": "application/json" }, ...options });
    const body = await response.json().catch(() => ({}));
    if (!response.ok) throw body;
    return body;
  }
  function applyProviderConfig(config) {
    const activeProvider = String(config.provider || window.M0_CONFIG.activeProvider || "mock").toLowerCase();
    const configuredProvider = String(config.configured_provider || window.M0_CONFIG.configuredProvider || activeProvider).toLowerCase();
    const restartRequired = config.restart_required === undefined
      ? activeProvider !== configuredProvider
      : Boolean(config.restart_required);
    const providerLocked = config.provider_locked === undefined
      ? Boolean(window.M0_CONFIG.providerLocked)
      : Boolean(config.provider_locked);

    window.M0_CONFIG.activeProvider = activeProvider;
    window.M0_CONFIG.configuredProvider = configuredProvider;
    window.M0_CONFIG.providerLocked = providerLocked;
    providerToggle.checked = configuredProvider === "cli";
    providerToggle.disabled = providerLocked;
    $("#provider-mode-label").textContent = configuredProvider.toUpperCase() + " 模式";
    $("#provider-mode-description").textContent = configuredProvider === "mock"
      ? "使用内置示例仓库，不访问真实 SVN。"
      : "使用当前 Windows 用户的 SVN CLI 认证缓存。";

    const probeButton = $("#endpoint-form button[type=\"submit\"]");
    if (probeButton) probeButton.disabled = restartRequired;
    if (providerLocked) {
      providerState.textContent = "运行模式由环境变量 EXCEL_MERGE_SVN_PROVIDER 锁定。";
      providerState.className = "provider-config-state warning";
    } else if (restartRequired) {
      providerState.textContent = "已保存为 " + configuredProvider.toUpperCase() + " 模式。当前进程仍在使用 " + activeProvider.toUpperCase() + "，请重启服务。";
      providerState.className = "provider-config-state warning";
    } else {
      providerState.textContent = "";
      providerState.className = "provider-config-state";
    }
    return restartRequired;
  }

  async function saveProviderMode() {
    const previousProvider = window.M0_CONFIG.configuredProvider || window.M0_CONFIG.activeProvider || "mock";
    const targetProvider = providerToggle.checked ? "cli" : "mock";
    providerToggle.disabled = true;
    providerState.textContent = "正在保存运行模式...";
    providerState.className = "provider-config-state";
    try {
      const body = await request("/api/svn/provider", {
        method: "POST",
        body: JSON.stringify({ provider: targetProvider }),
      });
      const restartRequired = applyProviderConfig(body);
      if (!restartRequired) {
        await Promise.all([autoProbe(), loadBranchCandidates()]);
      }
    } catch (error) {
      const payload = error && error.error ? error.error : error;
      providerToggle.checked = previousProvider === "cli";
      providerState.textContent = (payload?.code || "REQUEST_FAILED") + "：" + (payload?.message || "运行模式保存失败");
      providerState.className = "provider-config-state error";
    } finally {
      providerToggle.disabled = Boolean(window.M0_CONFIG.providerLocked);
    }
  }

  function renderHealth(health) {
    $("#provider-badge").textContent = "Provider: " + health.provider + (health.provider === "mock" ? " · Mock 数据" : " · 正式 CLI");
    $("#health-state").textContent = health.provider === "mock" ? "Mock 就绪" : (health.svn_cli_available ? "CLI 就绪" : "CLI 不可用");
    $("#health-state").className = "status-dot " + (health.provider === "mock" || health.svn_cli_available ? "status-ok" : "status-error");
  }


  function setInfo(info) {
    Object.entries(info).forEach(([key, value]) => {
      const target = document.querySelector("[data-field=\"" + key + "\"]");
      if (target) target.textContent = value || "—";
    });
  }

  async function saveConfig() {
    clearError();
    const button = $("#save-config");
    button.disabled = true;
    try {
      const body = await request("/api/svn/config", {
        method: "POST",
        body: JSON.stringify({ server_url: urlInput.value.trim() }),
      });
      window.M0_CONFIG.defaultUrl = body.server_url;
      const restartRequired = applyProviderConfig(body);
      $("#config-state").textContent = "地址已保存到项目配置";
      $("#config-state").className = "config-state success";
      if (!restartRequired) {
        await loadBranchCandidates();
      }
    } catch (error) {
      showError(error);
    } finally {
      if (button) button.disabled = false;
    }
  }

  function populateTrunkOptions(region, candidates) {
    const select = $("#trunk-" + region);
    if (!select) return;
    const saved = (select.dataset.selected || "").trim();
    const savedMatch = candidates.find((branch) => branch.toLowerCase() === saved.toLowerCase());
    const selectedValue = savedMatch || saved;
    select.textContent = "";
    const placeholder = document.createElement("option");
    placeholder.value = "";
    placeholder.textContent = candidates.length ? "请选择主干分支" : "未读取到主干分支";
    select.appendChild(placeholder);
    candidates.forEach((branch) => {
      const option = document.createElement("option");
      option.value = branch;
      option.textContent = branch;
      option.selected = branch === selectedValue;
      select.appendChild(option);
    });
    if (saved && !savedMatch) {
      const option = document.createElement("option");
      option.value = saved;
      option.textContent = saved + "（已保存，SVN 当前未返回）";
      option.selected = true;
      select.appendChild(option);
    }
  }

  function populateFixCandidates(region, candidates) {
    const container = $("#fix-candidates-" + region);
    if (!container) return;
    container.textContent = "";
    (candidates || []).forEach((branch) => {
      const chip = document.createElement("span");
      chip.className = "branch-candidate-chip";
      chip.textContent = branch;
      container.appendChild(chip);
    });
    const state = $("#fix-state-" + region);
    if (state) state.textContent = candidates?.length ? "按当前规则匹配到以下 FIX 分支" : "当前规则未匹配到 FIX 分支";
  }
  async function loadBranchCandidates() {
    const url = urlInput.value.trim();
    if (!url) return;
    const regions = ["TC", "KR"];
    await Promise.all(regions.map(async (region) => {
      const params = new URLSearchParams({ url, revision: "HEAD", region });
      try {
        const body = await request("/api/svn/branch-candidates?" + params.toString());
        populateTrunkOptions(region, body.trunk_branches || []);
        populateFixCandidates(region, body.fix_branches || []);
        const state = $("#trunk-state-" + region);
        if (state) state.textContent = "按系统配置匹配到 " + (body.trunk_branches || []).length + " 个主干、" + (body.fix_branches || []).length + " 个 FIX 候选";
      } catch (error) {
        const select = $("#trunk-" + region);
        const saved = select ? (select.dataset.selected || "").trim() : "";
        populateTrunkOptions(region, saved ? [saved] : []);
        populateFixCandidates(region, []);
        const state = $("#trunk-state-" + region);
        if (state) state.textContent = "SVN 目录读取失败，当前仅保留已保存值";
      }
    }));
  }
  function endpointCatalog() {
    return {
      regions: {
        TC: { display_name: "港台 TC", trunk_branch: $("#trunk-TC").value.trim(), fix_pattern: $("#fix-TC").value.trim() },
        KR: { display_name: "韩国 KR", trunk_branch: $("#trunk-KR").value.trim(), fix_pattern: $("#fix-KR").value.trim() },
        BT: { display_name: "折扣 BT", trunk_branch: "", fix_pattern: "" },
        JP: { display_name: "日本 JP", trunk_branch: "", fix_pattern: "" },
      },
    };
  }

  async function saveEndpointCatalog(button) {
    const region = button ? button.dataset.regionSave : "";
    const state = region ? $("#endpoint-config-state-" + region) : null;
    if (button) button.disabled = true;
    try {
      await request("/api/svn/endpoint-catalog", {
        method: "POST",
        body: JSON.stringify(endpointCatalog()),
      });
      if (state) { state.textContent = "端点目录配置已保存"; state.className = "config-state success"; }
    } catch (error) {
      if (state) state.textContent = "";
      showError(error);
    } finally {
      if (button) button.disabled = false;
    }
  }
  async function probe() {
    clearError();
    const body = await request("/api/svn/probe", { method: "POST", body: JSON.stringify(endpoint()) });
    setInfo(body);
    $("#health-state").textContent = "连接正常";
    $("#health-state").className = "status-dot status-ok";
    return body;
  }

  async function autoProbe() {
    if (!urlInput.value.trim()) return;
    try {
      await probe();
      $("#config-state").textContent = "已使用保存的地址自动读取";
      $("#config-state").className = "config-state success";
    } catch (error) {
      showError(error);
    }
  }

  $("#save-config").addEventListener("click", saveConfig);
  providerToggle.addEventListener("change", saveProviderMode);
  document.querySelectorAll("[data-region-save]").forEach((button) => { button.addEventListener("click", () => saveEndpointCatalog(button)); });

  $("#endpoint-form").addEventListener("submit", async (event) => {
    event.preventDefault();
    const button = event.submitter;
    if (button) button.disabled = true;
    try { await probe(); await loadBranchCandidates(); } catch (error) { showError(error); }
    finally { if (button) button.disabled = false; }
  });

  async function initialize() {
    try {
      const [health, config] = await Promise.all([
        request("/api/health"),
        request("/api/svn/config"),
      ]);
      renderHealth(health);
      const restartRequired = applyProviderConfig(config);
      if (!restartRequired) {
        await Promise.all([autoProbe(), loadBranchCandidates()]);
      }
    } catch (error) {
      showError(error);
    }
  }

  void initialize();
})();


