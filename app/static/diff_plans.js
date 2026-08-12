(() => {
  const list = document.getElementById("plan-list");
  const empty = document.getElementById("plan-empty");
  const alert = document.getElementById("plan-alert");
  const query = document.getElementById("plan-query");
  const meta = document.getElementById("plan-list-meta");
  const state = { archived: false, plans: [] };

  function showError(message) {
    alert.textContent = message;
    alert.classList.remove("hidden");
  }

  function requestId() {
    return globalThis.crypto?.randomUUID?.() || "00000000-0000-4000-8000-" + String(Date.now()).padStart(12, "0").slice(-12);
  }

  async function api(path, options = {}) {
    const response = await fetch(path, { headers: { "Content-Type": "application/json", ...(options.headers || {}) }, ...options });
    const payload = response.status === 204 ? null : await response.json().catch(() => null);
    if (!response.ok) throw new Error(payload?.error?.message || "计划服务请求失败");
    return payload;
  }

  function endpointText(plan) {
    return plan.source_endpoint_id + " → " + plan.target_endpoint_ids.join("、");
  }

  function visiblePlans() {
    const value = query.value.trim().toLocaleLowerCase();
    if (!value) return state.plans;
    return state.plans.filter((plan) => [plan.name, endpointText(plan), ...plan.workbook_paths].join(" ").toLocaleLowerCase().includes(value));
  }

  function render() {
    const plans = visiblePlans();
    list.replaceChildren();
    empty.classList.toggle("hidden", plans.length > 0);
    list.classList.toggle("hidden", plans.length === 0);
    meta.textContent = "当前显示 " + plans.length + " / " + state.plans.length + " 个计划";
    empty.querySelector("strong").textContent = state.archived ? "暂无归档计划" : "暂无有效计划";
    empty.querySelector("p").textContent = state.archived ? "归档计划会保留定义与历史运行摘要。" : "创建计划后，可以重复选择最新 SVN Revision 执行表格比对。";
    empty.querySelector("a").classList.toggle("hidden", state.archived);
    plans.forEach((plan) => {
      const row = document.createElement("article");
      row.className = "diff-plan-row";
      const name = document.createElement("div");
      name.className = "diff-plan-row-name";
      const title = document.createElement("strong");
      title.textContent = plan.name;
      const id = document.createElement("code");
      id.textContent = plan.plan_id;
      name.append(title, id);
      const scope = document.createElement("div");
      scope.className = "diff-plan-row-scope";
      const branch = document.createElement("span");
      branch.textContent = endpointText(plan);
      const updated = document.createElement("span");
      updated.textContent = "更新于 " + new Date(plan.updated_at).toLocaleString("zh-CN", { hour12: false });
      scope.append(branch, updated);
      const counts = document.createElement("div");
      counts.className = "diff-plan-row-counts";
      counts.innerHTML = "<span>表格 <strong>" + plan.workbook_paths.length + "</strong></span><span>目标 <strong>" + plan.target_endpoint_ids.length + "</strong></span>";
      const actions = document.createElement("div");
      actions.className = "diff-plan-row-actions";
      const open = document.createElement("a");
      open.href = "/diff-plans/" + plan.plan_id;
      open.textContent = "查看";
      const command = document.createElement("button");
      command.type = "button";
      command.textContent = state.archived ? "恢复" : "归档";
      command.addEventListener("click", async () => {
        command.disabled = true;
        try {
          await api("/api/diff-plans/" + plan.plan_id + "/" + (state.archived ? "restore" : "archive"), {
            method: "POST",
            body: JSON.stringify({ schema_version: "m4.diff-plan-command.request.v1", request_id: requestId(), expected_version: plan.version }),
          });
          await load();
        } catch (error) {
          showError(error.message);
          command.disabled = false;
        }
      });
      actions.append(open, command);
      row.append(name, scope, counts, actions);
      list.append(row);
    });
  }

  async function load() {
    alert.classList.add("hidden");
    list.innerHTML = '<div class="diff-plan-loading"><span class="diff-plan-spinner" aria-hidden="true"></span>正在读取计划</div>';
    try {
      const payload = await api("/api/diff-plans?archived=" + state.archived);
      state.plans = payload.plans;
      render();
    } catch (error) {
      list.replaceChildren();
      showError(error.message);
    }
  }

  document.querySelectorAll("[data-plan-view]").forEach((button) => button.addEventListener("click", () => {
    state.archived = button.dataset.planView === "archived";
    document.querySelectorAll("[data-plan-view]").forEach((item) => {
      const selected = item === button;
      item.classList.toggle("is-selected", selected);
      item.setAttribute("aria-pressed", String(selected));
    });
    void load();
  }));
  query.addEventListener("input", render);
  void load();
})();
