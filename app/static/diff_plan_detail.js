(() => {
  const planId = document.body.dataset.planId;
  const alert = document.getElementById("detail-alert");
  const archive = document.getElementById("archive-plan");
  const runButton = document.getElementById("run-plan");
  const runHistory = document.getElementById("detail-run-history");
  let plan = null;
  let commandBusy = false;

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

  function render() {
    document.getElementById("detail-plan-name").textContent = plan.name;
    document.getElementById("detail-plan-caption").textContent = plan.archived ? "计划已归档，定义与运行摘要继续保留" : "长期复用的 TABLE 表格对比范围";
    document.getElementById("edit-plan-link").classList.toggle("hidden", plan.archived);
    archive.disabled = false;
    archive.textContent = plan.archived ? "恢复计划" : "归档计划";
    runButton.disabled = plan.archived || commandBusy;
    const summary = document.getElementById("detail-summary");
    summary.replaceChildren();
    [
      ["基准分支", plan.source_endpoint_id],
      ["目标分支", plan.target_endpoint_ids.length + " 个"],
      ["TABLE 表格", plan.workbook_paths.length + " 张"],
      ["计划版本", "v" + plan.version],
    ].forEach(([label, value]) => {
      const item = document.createElement("div");
      item.className = "diff-plan-detail-stat";
      const span = document.createElement("span");
      span.textContent = label;
      const strong = document.createElement("strong");
      strong.textContent = value;
      item.append(span, strong);
      summary.append(item);
    });
    const targets = document.getElementById("detail-targets");
    const workbooks = document.getElementById("detail-workbooks");
    targets.replaceChildren(...plan.target_endpoint_ids.map((value) => Object.assign(document.createElement("li"), { textContent: value })));
    workbooks.replaceChildren(...plan.workbook_paths.map((value) => Object.assign(document.createElement("li"), { textContent: value })));
  }

  function runStatus(status) {
    return ({ queued: "等待准备", preparing: "冻结快照", running: "运行中", cancelling: "取消中", completed: "已完成", completed_with_failures: "部分失败", cancelled: "已取消", failed: "编排失败" })[status] || status;
  }

  function renderRuns(payload) {
    runHistory.replaceChildren();
    if (!payload.runs.length) {
      const empty = document.createElement("div");
      empty.className = "diff-plan-run-empty";
      empty.innerHTML = "<strong>尚无运行记录</strong><p>开始运行后会冻结计划定义与各分支 Revision。</p>";
      runHistory.append(empty);
      return;
    }
    payload.runs.forEach((run) => {
      const row = document.createElement("a");
      row.className = "diff-plan-run-row is-" + run.status;
      row.href = "/diff-plan-runs/" + run.run_id;
      const title = document.createElement("strong");
      title.textContent = runStatus(run.status);
      const revisions = document.createElement("span");
      revisions.textContent = "基准 r" + run.source_revision + " · " + Object.entries(run.target_revisions).map(([id, revision]) => id + " r" + revision).join(" · ");
      const progress = document.createElement("span");
      progress.textContent = run.progress.processed_items + " / " + run.progress.total_items + " 已处理" + (run.retry_of_run_id ? " · 失败项重试" : "");
      const time = document.createElement("time");
      time.dateTime = run.created_at;
      time.textContent = new Date(run.created_at).toLocaleString("zh-CN", { hour12: false });
      row.append(title, revisions, progress, time);
      runHistory.append(row);
    });
  }

  async function loadRuns() {
    try {
      renderRuns(await api("/api/diff-plans/" + planId + "/runs"));
    } catch (error) {
      runHistory.innerHTML = '<div class="diff-plan-run-empty"><strong>运行历史读取失败</strong><p></p></div>';
      runHistory.querySelector("p").textContent = error.message;
    }
  }

  async function startRun(revisions = {}) {
    if (commandBusy || plan?.archived) return;
    commandBusy = true;
    runButton.disabled = true;
    runButton.textContent = "正在冻结 Revision";
    try {
      const run = await api("/api/diff-plans/" + planId + "/runs", {
        method: "POST",
        body: JSON.stringify({ schema_version: "m4.diff-plan-run-start.request.v1", request_id: requestId(), revisions }),
      });
      location.href = "/diff-plan-runs/" + run.run_id;
    } catch (error) {
      showError(error.message);
      commandBusy = false;
      runButton.disabled = plan.archived;
      runButton.textContent = "开始新运行";
    }
  }

  archive.addEventListener("click", async () => {
    archive.disabled = true;
    try {
      plan = await api("/api/diff-plans/" + planId + "/" + (plan.archived ? "restore" : "archive"), {
        method: "POST",
        body: JSON.stringify({ schema_version: "m4.diff-plan-command.request.v1", request_id: requestId(), expected_version: plan.version }),
      });
      render();
    } catch (error) {
      showError(error.message);
      archive.disabled = false;
    }
  });
  runButton.addEventListener("click", () => void startRun());

  api("/api/diff-plans/" + planId).then(async (payload) => {
    plan = payload;
    render();
    await loadRuns();
    const pending = new URLSearchParams(location.search).get("run") === "pending";
    if (pending) {
      let revisions = {};
      try {
        const saved = JSON.parse(sessionStorage.getItem("m4PendingRun") || "null");
        if (saved?.planId === planId) revisions = saved.revisions || {};
      } catch { revisions = {}; }
      sessionStorage.removeItem("m4PendingRun");
      history.replaceState(null, "", "/diff-plans/" + planId);
      await startRun(revisions);
    }
  }).catch((error) => showError(error.message));
})();
