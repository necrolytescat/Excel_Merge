(() => {
  const ACTIVE_STATUSES = new Set(["queued", "preparing", "running", "cancelling"]);
  const TERMINAL_STATUSES = new Set(["completed", "completed_with_failures", "cancelled", "failed"]);
  const STATUS_GROUPS = {
    all: [],
    active: ["queued", "preparing", "running", "cancelling"],
    completed: ["completed", "completed_with_failures"],
    attention: ["completed_with_failures", "cancelled", "failed"],
  };
  const STATUS_LABELS = {
    queued: "等待准备",
    preparing: "准备候选",
    running: "进行中",
    cancelling: "正在取消",
    completed: "已完成",
    completed_with_failures: "部分失败",
    cancelled: "已取消",
    failed: "任务失败",
  };
  const state = {
    view: "tasks",
    tasks: [],
    group: "all",
    query: "",
    createdFrom: "",
    createdTo: "",
    nextCursor: null,
    hasMore: false,
    loading: false,
    pollTimer: 0,
    etag: "",
    etagKey: "",
    detailTaskId: "",
    detailLoading: false,
    detailEtag: "",
    detailEtagTaskId: "",
    logs: [],
    logLevel: "",
    logTaskId: "",
    logRequestId: "",
    logQuery: "",
    logCreatedFrom: "",
    logCreatedTo: "",
    logNextCursor: null,
    logHasMore: false,
    logLoading: false,
    logEtag: "",
    logEtagKey: "",
    cacheLoading: false,
    cacheStatus: null,
  };
  const $ = (id) => document.getElementById(id);
  const tableScroll = document.querySelector(".history-table-scroll");

  function errorMessage(error) {
    const payload = error?.error || error;
    return (payload?.code ? payload.code + "：" : "") + (payload?.message || "历史任务读取失败");
  }

  function localDateTime(value) {
    if (!value) return "—";
    const date = new Date(value);
    if (Number.isNaN(date.getTime())) return "—";
    return new Intl.DateTimeFormat("zh-CN", {
      month: "2-digit",
      day: "2-digit",
      hour: "2-digit",
      minute: "2-digit",
      second: "2-digit",
      hour12: false,
    }).format(date);
  }

  function formatBytes(value) {
    const bytes = Number(value || 0);
    if (!Number.isFinite(bytes) || bytes <= 0) return "0 B";
    const units = ["B", "KB", "MB", "GB"];
    const index = Math.min(Math.floor(Math.log(bytes) / Math.log(1024)), units.length - 1);
    const amount = bytes / (1024 ** index);
    return (index === 0 ? Math.round(amount) : amount.toFixed(amount >= 10 ? 1 : 2)) + " " + units[index];
  }

  function dateBoundary(value, endOfDay) {
    if (!value) return "";
    const suffix = endOfDay ? "T23:59:59.999" : "T00:00:00.000";
    const date = new Date(value + suffix);
    return Number.isNaN(date.getTime()) ? "" : date.toISOString();
  }

  function buildUrl(cursor = null, limit = 20) {
    const params = new URLSearchParams({ limit: String(limit) });
    if (cursor) params.set("cursor", cursor);
    (STATUS_GROUPS[state.group] || []).forEach((status) => params.append("status", status));
    if (state.query) params.set("q", state.query);
    const createdFrom = dateBoundary(state.createdFrom, false);
    const createdTo = dateBoundary(state.createdTo, true);
    if (createdFrom) params.set("created_from", createdFrom);
    if (createdTo) params.set("created_to", createdTo);
    return "/api/diff/batches?" + params.toString();
  }

  function persistFilters() {
    const params = new URLSearchParams();
    if (state.view !== "tasks") params.set("view", state.view);
    if (state.view === "tasks") {
      if (state.group !== "all") params.set("group", state.group);
      if (state.query) params.set("q", state.query);
      if (state.createdFrom) params.set("from", state.createdFrom);
      if (state.createdTo) params.set("to", state.createdTo);
      if (state.detailTaskId) params.set("detail", state.detailTaskId);
    } else if (state.view === "logs") {
      if (state.logLevel) params.set("log_level", state.logLevel);
      if (state.logTaskId) params.set("task_id", state.logTaskId);
      if (state.logRequestId) params.set("request_id", state.logRequestId);
      if (state.logQuery) params.set("log_q", state.logQuery);
      if (state.logCreatedFrom) params.set("log_from", state.logCreatedFrom);
      if (state.logCreatedTo) params.set("log_to", state.logCreatedTo);
    }
    const query = params.toString();
    history.replaceState(null, "", "/compare/history" + (query ? "?" + query : ""));
  }

  async function requestPage(url) {
    const headers = { Accept: "application/json" };
    if (state.etag && state.etagKey === url) headers["If-None-Match"] = state.etag;
    const response = await fetch(url, { headers });
    if (response.status === 304) return { notModified: true, etag: state.etag };
    const body = await response.json().catch(() => ({}));
    if (!response.ok) throw body;
    if (body?.schema_version !== "m2.batch-list.v1" || !Array.isArray(body.items)) {
      throw new Error("历史任务列表契约无效");
    }
    return { body, etag: response.headers.get("ETag") || "" };
  }

  function element(tag, className, text) {
    const node = document.createElement(tag);
    if (className) node.className = className;
    if (text !== undefined) node.textContent = text;
    return node;
  }

  function progressDetail(task) {
    const progress = task.progress || {};
    if (progress.total_items === null || progress.total_items === undefined) {
      return { ratio: 0, primary: "准备候选", secondary: "等待服务端清单" };
    }
    const total = Number(progress.total_items || 0);
    const processed = Number(progress.processed_items || 0);
    const failures = Number(progress.business_failed_items || 0)
      + Number(progress.orchestration_failed_items || 0);
    return {
      ratio: Number(progress.ratio || 0),
      primary: processed + " / " + total,
      secondary: failures ? "失败 " + failures : "成功 " + Number(progress.succeeded_items || 0),
    };
  }

  function statusCell(task) {
    const wrapper = element("div", "history-status-cell");
    const badge = element("span", "history-task-status", STATUS_LABELS[task.status] || task.status);
    badge.dataset.status = task.status;
    const taskId = element("code", "history-task-id", task.task_id);
    taskId.title = task.task_id;
    wrapper.append(badge, taskId);
    if (task.retry_of_task_id) {
      const retry = element("span", "history-retry-id", "重试自 " + task.retry_of_task_id);
      retry.title = task.retry_of_task_id;
      wrapper.appendChild(retry);
    }
    return wrapper;
  }

  function versionSide(endpoint) {
    const side = element("div", "history-version-side");
    const endpointId = element("strong", "", endpoint?.endpoint_id || "—");
    endpointId.title = endpoint?.endpoint_id || "";
    side.append(endpointId, element("span", "", endpoint?.revision ? "冻结 r" + endpoint.revision : "无 Revision"));
    return side;
  }

  function versionCell(task) {
    const wrapper = element("div", "history-version-cell");
    const pair = element("div", "history-version-pair");
    pair.append(versionSide(task.source), element("span", "history-version-arrow", "→"), versionSide(task.target));
    wrapper.appendChild(pair);
    return wrapper;
  }

  function progressCell(task) {
    const wrapper = element("div", "history-progress-cell");
    const detail = progressDetail(task);
    const track = element("div", "history-progress-track");
    const bar = element("span");
    bar.style.width = Math.round(Math.max(0, Math.min(1, detail.ratio)) * 100) + "%";
    track.appendChild(bar);
    const meta = element("div", "history-progress-meta");
    meta.append(element("strong", "", detail.primary), element("span", "", detail.secondary));
    wrapper.append(track, meta);
    return wrapper;
  }

  function timeCell(task) {
    const wrapper = element("div", "history-time-cell");
    wrapper.append(
      element("strong", "", "创建 " + localDateTime(task.created_at)),
      element("span", "", "更新 " + localDateTime(task.updated_at)),
    );
    return wrapper;
  }

  function expiryCell(task) {
    const wrapper = element("div", "history-expiry-cell");
    if (!task.expires_at) {
      wrapper.append(element("strong", "", "运行中"), element("span", "", "不按年龄清理"));
      return wrapper;
    }
    wrapper.append(element("strong", "", localDateTime(task.expires_at)), element("span", "", "正式结果到期"));
    return wrapper;
  }

  function openLabel(task) {
    if (ACTIVE_STATUSES.has(task.status)) return "查看进度";
    if (task.status === "completed" || task.status === "completed_with_failures") return "查看结果";
    return "打开任务";
  }

  function taskRow(task) {
    const row = document.createElement("tr");
    row.dataset.taskId = task.task_id;
    const cells = [
      ["状态", statusCell(task)],
      ["版本范围", versionCell(task)],
      ["进度", progressCell(task)],
      ["时间", timeCell(task)],
      ["到期", expiryCell(task)],
    ];
    cells.forEach(([label, content]) => {
      const cell = document.createElement("td");
      cell.dataset.label = label;
      cell.appendChild(content);
      row.appendChild(cell);
    });
    const actionCell = document.createElement("td");
    const actions = element("div", "history-row-actions");
    const detailButton = element("button", "history-task-detail", "详情");
    detailButton.type = "button";
    detailButton.setAttribute("aria-label", "查看任务详情 " + task.task_id);
    detailButton.addEventListener("click", () => void openTaskDetail(task.task_id));
    const link = element("a", "history-open-task", openLabel(task));
    link.href = "/compare/results?task_id=" + encodeURIComponent(task.task_id);
    link.setAttribute("aria-label", openLabel(task) + " " + task.task_id);
    actions.append(detailButton, link);
    actionCell.appendChild(actions);
    row.appendChild(actionCell);
    return row;
  }

  function render() {
    const rows = $("history-task-rows");
    rows.textContent = "";
    state.tasks.forEach((task) => rows.appendChild(taskRow(task)));
    const empty = state.tasks.length === 0;
    tableScroll.classList.toggle("hidden", empty);
    $("history-empty").classList.toggle("hidden", !empty);
    $("history-load-more").classList.toggle("hidden", !state.hasMore || empty);
    $("history-loaded-count").textContent = String(state.tasks.length);
    $("history-active-count").textContent = String(
      state.tasks.filter((task) => ACTIVE_STATUSES.has(task.status)).length,
    );
    $("history-list-meta").textContent = state.tasks.length + " 条已载入";
    if (empty) {
      const filtered = state.group !== "all" || state.query || state.createdFrom || state.createdTo;
      $("history-empty-title").textContent = filtered ? "筛选范围内无任务" : "暂无历史任务";
      $("history-empty-detail").textContent = filtered ? "调整筛选条件后重试" : "尚未创建版本对比任务";
    }
  }

  function relationButton(label, taskId) {
    const button = element("button", "history-relation-link");
    button.type = "button";
    button.append(element("span", "", label), element("code", "", taskId));
    button.addEventListener("click", () => void openTaskDetail(taskId));
    return button;
  }

  function renderTaskManagement(detail) {
    $("history-detail-status").textContent = STATUS_LABELS[detail.status] || detail.status;
    $("history-detail-result-count").textContent = String(detail.results?.count || 0) + " 个";
    $("history-detail-result-size").textContent = formatBytes(detail.results?.size_bytes);
    $("history-detail-expires-at").textContent = localDateTime(detail.results?.expires_at);
    $("history-detail-open-result").href = "/compare/results?task_id=" + encodeURIComponent(detail.task_id);

    const relations = $("history-detail-relations");
    relations.textContent = "";
    if (detail.retry_of_task_id) {
      relations.appendChild(relationButton("父任务", detail.retry_of_task_id));
    }
    (detail.retry_child_task_ids || []).forEach((taskId, index) => {
      relations.appendChild(relationButton("重试任务 " + (index + 1), taskId));
    });
    if (!relations.children.length) relations.textContent = "无重试关系";

    const events = $("history-detail-events");
    events.textContent = "";
    (detail.events || []).forEach((event) => {
      const item = element("li", "history-event");
      item.dataset.level = event.level || "info";
      const marker = element("i", "", "");
      marker.setAttribute("aria-hidden", "true");
      const content = element("div");
      const heading = element("div", "history-event-heading");
      heading.append(
        element("strong", "", event.message),
        element("time", "", localDateTime(event.created_at)),
      );
      content.appendChild(heading);
      if (event.details?.code) {
        content.appendChild(element("code", "history-event-code", event.details.code));
      }
      if (event.details?.child_task_id) {
        content.appendChild(relationButton("重试任务", event.details.child_task_id));
      }
      item.append(marker, content);
      events.appendChild(item);
    });
    if (!events.children.length) {
      events.appendChild(element("li", "history-event-empty", "暂无结构化事件"));
    }
    $("history-detail-event-count").textContent = String(detail.events?.length || 0) + " 条事件";
    const deletable = detail.can_delete && TERMINAL_STATUSES.has(detail.status);
    $("history-delete-start").classList.toggle("hidden", !deletable);
    $("history-delete-start").disabled = !deletable;
    $("history-detail-loading").classList.add("hidden");
    $("history-detail-error").classList.add("hidden");
    $("history-detail-content").classList.remove("hidden");
    $("history-detail-actions").classList.remove("hidden");
  }

  function showDetailError(error) {
    $("history-detail-loading").classList.add("hidden");
    $("history-detail-content").classList.add("hidden");
    $("history-detail-actions").classList.add("hidden");
    $("history-detail-error").textContent = errorMessage(error);
    $("history-detail-error").classList.remove("hidden");
  }

  async function loadTaskDetails({ quiet = false } = {}) {
    if (!state.detailTaskId || state.detailLoading) return;
    const requestedTaskId = state.detailTaskId;
    state.detailLoading = true;
    if (!quiet) {
      $("history-detail-loading").classList.remove("hidden");
      $("history-detail-error").classList.add("hidden");
      $("history-detail-content").classList.add("hidden");
      $("history-detail-actions").classList.add("hidden");
    }
    try {
      const headers = { Accept: "application/json" };
      if (state.detailEtag && state.detailEtagTaskId === requestedTaskId) {
        headers["If-None-Match"] = state.detailEtag;
      }
      const response = await fetch(
        "/api/diff/batches/" + encodeURIComponent(requestedTaskId) + "/management",
        { headers },
      );
      if (response.status === 304) return;
      const body = await response.json().catch(() => ({}));
      if (!response.ok) throw body;
      if (requestedTaskId !== state.detailTaskId) return;
      if (body?.schema_version !== "m2.batch-management.v1" || !Array.isArray(body.events)) {
        throw new Error("任务管理详情契约无效");
      }
      state.detailEtag = response.headers.get("ETag") || "";
      state.detailEtagTaskId = requestedTaskId;
      renderTaskManagement(body);
    } catch (error) {
      if (!quiet) showDetailError(error);
    } finally {
      state.detailLoading = false;
      if (state.detailTaskId && requestedTaskId !== state.detailTaskId) {
        void loadTaskDetails();
      }
    }
  }

  async function openTaskDetail(taskId, { updateUrl = true } = {}) {
    state.detailTaskId = taskId;
    state.detailEtag = "";
    state.detailEtagTaskId = "";
    $("history-detail-task-id").textContent = taskId;
    $("history-delete-confirm").classList.add("hidden");
    if (updateUrl) persistFilters();
    const dialog = $("history-detail-dialog");
    if (!dialog.open) dialog.showModal();
    await loadTaskDetails();
  }

  function closeTaskDetail() {
    const dialog = $("history-detail-dialog");
    if (dialog.open) dialog.close();
    state.detailTaskId = "";
    state.detailEtag = "";
    state.detailEtagTaskId = "";
    $("history-delete-confirm").classList.add("hidden");
    persistFilters();
  }

  async function deleteCurrentTask() {
    if (!state.detailTaskId) return;
    const taskId = state.detailTaskId;
    const confirmButton = $("history-delete-confirm-button");
    confirmButton.disabled = true;
    confirmButton.textContent = "正在删除";
    try {
      const response = await fetch("/api/diff/batches/" + encodeURIComponent(taskId), {
        method: "DELETE",
        headers: { "Content-Type": "application/json", Accept: "application/json" },
        body: JSON.stringify({
          schema_version: "m2.batch-delete.request.v1",
          request_id: crypto.randomUUID(),
          reason: "历史任务页面手动删除",
        }),
      });
      const body = await response.json().catch(() => ({}));
      if (!response.ok) throw body;
      if (body?.schema_version !== "m2.batch-delete.result.v1") {
        throw new Error("任务删除响应契约无效");
      }
      closeTaskDetail();
      state.tasks = state.tasks.filter((task) => task.task_id !== taskId);
      render();
      showAlert("");
      await loadTasks({ replace: true });
    } catch (error) {
      $("history-delete-confirm").classList.add("hidden");
      $("history-detail-error").textContent = errorMessage(error);
      $("history-detail-error").classList.remove("hidden");
    } finally {
      confirmButton.disabled = false;
      confirmButton.textContent = "确认删除";
    }
  }

  function showAlert(message) {
    $("history-alert").textContent = message || "";
    $("history-alert").classList.toggle("hidden", !message);
  }

  function updateRefreshTime() {
    $("history-refreshed-at").textContent = new Intl.DateTimeFormat("zh-CN", {
      hour: "2-digit",
      minute: "2-digit",
      second: "2-digit",
      hour12: false,
    }).format(new Date());
  }

  function schedulePoll() {
    window.clearTimeout(state.pollTimer);
    if (document.hidden) {
      $("history-live-indicator").classList.add("is-paused");
      return;
    }
    $("history-live-indicator").classList.remove("is-paused");
    if (state.view === "logs") {
      state.pollTimer = window.setTimeout(
        () => void loadLogs({ replace: true, automatic: true }),
        5000,
      );
      return;
    }
    if (state.view === "cache") {
      state.pollTimer = window.setTimeout(
        () => void loadCacheStatus({ automatic: true }),
        15000,
      );
      return;
    }
    const active = state.tasks.some((task) => ACTIVE_STATUSES.has(task.status));
    state.pollTimer = window.setTimeout(() => void loadTasks({ replace: true, automatic: true }), active ? 2000 : 15000);
  }

  async function loadTasks({ replace = true, append = false, automatic = false } = {}) {
    if (state.loading) return;
    state.loading = true;
    $("history-refresh").classList.add("is-loading");
    $("history-refresh").disabled = true;
    const cursor = append ? state.nextCursor : null;
    const limit = append ? 20 : Math.max(20, Math.min(100, state.tasks.length || 20));
    const url = buildUrl(cursor, limit);
    try {
      const response = await requestPage(url);
      if (!response.notModified) {
        state.tasks = append ? state.tasks.concat(response.body.items) : response.body.items;
        state.nextCursor = response.body.next_cursor;
        state.hasMore = Boolean(response.body.has_more);
        if (!cursor) {
          state.etag = response.etag;
          state.etagKey = url;
        }
        render();
        if (state.detailTaskId && $("history-detail-dialog").open) {
          void loadTaskDetails({ quiet: true });
        }
      }
      showAlert("");
      updateRefreshTime();
    } catch (error) {
      showAlert(errorMessage(error));
      if (!state.tasks.length && !automatic) {
        tableScroll.classList.add("hidden");
        $("history-empty").classList.remove("hidden");
        $("history-empty-title").textContent = "历史任务读取失败";
        $("history-empty-detail").textContent = "服务暂时不可用";
      }
    } finally {
      state.loading = false;
      $("history-refresh").classList.remove("is-loading");
      $("history-refresh").disabled = false;
      schedulePoll();
    }
  }

  const UUID_PATTERN = /^[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/i;

  function updateTopStats(loadedLabel, loadedValue, activeLabel, activeValue) {
    $("history-loaded-label").textContent = loadedLabel;
    $("history-loaded-count").textContent = String(loadedValue);
    $("history-active-label").textContent = activeLabel;
    $("history-active-count").textContent = String(activeValue);
  }

  function logDateBoundary(value) {
    if (!value) return "";
    const date = new Date(value);
    return Number.isNaN(date.getTime()) ? "" : date.toISOString();
  }

  function buildLogUrl(cursor = null, limit = 50) {
    const params = new URLSearchParams({ limit: String(limit) });
    if (cursor) params.set("cursor", cursor);
    if (state.logLevel) params.set("level", state.logLevel);
    if (state.logTaskId) params.set("task_id", state.logTaskId);
    if (state.logRequestId) params.set("request_id", state.logRequestId);
    if (state.logQuery) params.set("q", state.logQuery);
    const createdFrom = logDateBoundary(state.logCreatedFrom);
    const createdTo = logDateBoundary(state.logCreatedTo);
    if (createdFrom) params.set("created_from", createdFrom);
    if (createdTo) params.set("created_to", createdTo);
    return "/api/operations/logs?" + params.toString();
  }

  async function requestLogPage(url) {
    const headers = { Accept: "application/json" };
    if (state.logEtag && state.logEtagKey === url) headers["If-None-Match"] = state.logEtag;
    const response = await fetch(url, { headers });
    if (response.status === 304) return { notModified: true, etag: state.logEtag };
    const body = await response.json().catch(() => ({}));
    if (!response.ok) throw body;
    if (body?.schema_version !== "m2.operations-log-list.v1" || !Array.isArray(body.items)) {
      throw new Error("运行日志列表契约无效");
    }
    return { body, etag: response.headers.get("ETag") || "" };
  }

  function logLevelLabel(level) {
    return { debug: "调试", info: "信息", warning: "警告", error: "错误" }[level] || level;
  }

  function logCorrelation(entry) {
    const wrapper = element("div", "history-log-correlation");
    if (entry.task_id) {
      const task = element("code", "", "Task " + entry.task_id);
      task.title = entry.task_id;
      wrapper.appendChild(task);
    }
    if (entry.request_id) {
      const request = element("code", "", "Request " + entry.request_id);
      request.title = entry.request_id;
      wrapper.appendChild(request);
    }
    if (!wrapper.children.length) wrapper.appendChild(element("span", "", "—"));
    return wrapper;
  }

  function logRow(entry) {
    const row = document.createElement("tr");
    const level = element("span", "history-log-level", logLevelLabel(entry.level));
    level.dataset.level = entry.level;
    const event = element("div", "history-log-event");
    event.append(
      element("strong", "", entry.event),
      element("span", "", entry.logger),
    );
    const message = element("p", "history-log-message", entry.message);
    message.title = entry.message;
    const cells = [
      ["时间", element("time", "", localDateTime(entry.created_at))],
      ["级别", level],
      ["相关性", logCorrelation(entry)],
      ["事件", event],
      ["内容", message],
      ["进程", element("code", "", "PID " + entry.process_id)],
    ];
    cells.forEach(([label, content]) => {
      const cell = document.createElement("td");
      cell.dataset.label = label;
      cell.appendChild(content);
      row.appendChild(cell);
    });
    return row;
  }

  function renderLogs() {
    const rows = $("history-log-rows");
    rows.textContent = "";
    state.logs.forEach((entry) => rows.appendChild(logRow(entry)));
    const empty = state.logs.length === 0;
    document.querySelector(".history-log-table-scroll").classList.toggle("hidden", empty);
    $("history-log-empty").classList.toggle("hidden", !empty);
    $("history-log-load-more").classList.toggle("hidden", !state.logHasMore || empty);
    $("history-log-list-meta").textContent = state.logs.length + " 条已载入";
    const attention = state.logs.filter((entry) => ["warning", "error"].includes(entry.level)).length;
    updateTopStats("当前载入", state.logs.length, "警告 / 错误", attention);
  }

  function showLogAlert(message) {
    $("history-log-alert").textContent = message || "";
    $("history-log-alert").classList.toggle("hidden", !message);
  }

  async function loadLogs({ append = false, automatic = false } = {}) {
    if (state.logLoading) return;
    state.logLoading = true;
    $("history-log-refresh").classList.add("is-loading");
    $("history-log-refresh").disabled = true;
    const cursor = append ? state.logNextCursor : null;
    const limit = append ? 50 : Math.max(50, Math.min(200, state.logs.length || 50));
    const url = buildLogUrl(cursor, limit);
    try {
      const response = await requestLogPage(url);
      if (!response.notModified) {
        state.logs = append ? state.logs.concat(response.body.items) : response.body.items;
        state.logNextCursor = response.body.next_cursor;
        state.logHasMore = Boolean(response.body.has_more);
        if (!cursor) {
          state.logEtag = response.etag;
          state.logEtagKey = url;
        }
        renderLogs();
      }
      showLogAlert("");
      updateRefreshTime();
    } catch (error) {
      showLogAlert(errorMessage(error));
      if (!state.logs.length && !automatic) {
        document.querySelector(".history-log-table-scroll").classList.add("hidden");
        $("history-log-empty").classList.remove("hidden");
      }
    } finally {
      state.logLoading = false;
      $("history-log-refresh").classList.remove("is-loading");
      $("history-log-refresh").disabled = false;
      schedulePoll();
    }
  }

  function resetLogsAndLoad() {
    state.logs = [];
    state.logNextCursor = null;
    state.logHasMore = false;
    state.logEtag = "";
    state.logEtagKey = "";
    persistFilters();
    $("history-log-rows").innerHTML = '<tr class="history-loading-row"><td colspan="6"><span class="history-spinner" aria-hidden="true"></span>正在读取运行日志</td></tr>';
    document.querySelector(".history-log-table-scroll").classList.remove("hidden");
    $("history-log-empty").classList.add("hidden");
    void loadLogs();
  }

  async function loadCacheStatus({ automatic = false } = {}) {
    if (state.cacheLoading) return;
    state.cacheLoading = true;
    $("history-cache-refresh").classList.add("is-loading");
    $("history-cache-refresh").disabled = true;
    try {
      const response = await fetch("/api/operations/svn-cache", {
        headers: { Accept: "application/json" },
      });
      const body = await response.json().catch(() => ({}));
      if (!response.ok) throw body;
      if (body?.schema_version !== "m2.svn-cache-status.v1" || body.scope !== "global_shared") {
        throw new Error("SVN 缓存状态契约无效");
      }
      state.cacheStatus = body;
      $("history-cache-status").textContent = body.enabled
        ? "缓存已启用 · 全局共享 · 可再生"
        : "当前 SVN Provider 未启用落盘缓存";
      $("history-cache-status").dataset.enabled = String(body.enabled);
      $("history-cache-metrics").classList.remove("hidden");
      $("history-cache-boundary").classList.remove("hidden");
      $("history-cache-size").textContent = formatBytes(body.size_bytes);
      $("history-cache-files").textContent = body.file_count + " 个缓存文件"
        + (body.ignored_file_count ? " · " + body.ignored_file_count + " 个未知项未纳管" : "");
      $("history-cache-hit-rate").textContent = body.session_hit_rate === null
        ? "暂无请求"
        : Math.round(body.session_hit_rate * 100) + "%";
      $("history-cache-hit-detail").textContent = "内存 " + body.session_memory_hits
        + " · 磁盘 " + body.session_disk_hits + " · 未命中 " + body.session_misses;
      $("history-cache-memory").textContent = body.memory_entry_count + " 项";
      $("history-cache-clear").disabled = !body.can_clear;
      $("history-cache-clear").title = body.can_clear ? "" : "当前缓存不可清理";
      updateTopStats("缓存文件", body.file_count, "磁盘容量", formatBytes(body.size_bytes));
      $("history-cache-alert").classList.add("hidden");
      updateRefreshTime();
    } catch (error) {
      $("history-cache-alert").textContent = errorMessage(error);
      $("history-cache-alert").classList.remove("hidden");
      if (!automatic) $("history-cache-status").textContent = "缓存状态读取失败";
    } finally {
      state.cacheLoading = false;
      $("history-cache-refresh").classList.remove("is-loading");
      $("history-cache-refresh").disabled = false;
      schedulePoll();
    }
  }

  async function clearGlobalCache() {
    const confirmation = $("history-cache-confirmation").value.trim();
    if (confirmation !== "清空全局 SVN 缓存") return;
    const button = $("history-cache-confirm-button");
    button.disabled = true;
    button.textContent = "正在清理";
    try {
      const response = await fetch("/api/operations/svn-cache/clear", {
        method: "POST",
        headers: { "Content-Type": "application/json", Accept: "application/json" },
        body: JSON.stringify({
          schema_version: "m2.svn-cache-clear.request.v1",
          request_id: crypto.randomUUID(),
          confirmation,
        }),
      });
      const body = await response.json().catch(() => ({}));
      if (!response.ok) throw body;
      if (body?.schema_version !== "m2.svn-cache-clear.result.v1") {
        throw new Error("SVN 缓存清理响应契约无效");
      }
      $("history-cache-dialog").close();
      $("history-cache-confirmation").value = "";
      $("history-cache-confirm-button").disabled = true;
      await loadCacheStatus();
    } catch (error) {
      $("history-cache-clear-error").textContent = errorMessage(error);
      $("history-cache-clear-error").classList.remove("hidden");
      button.disabled = false;
    } finally {
      button.textContent = "确认清空";
    }
  }

  function refreshCurrentView(options = {}) {
    if (state.view === "logs") {
      void loadLogs({ append: false, ...options });
    } else if (state.view === "cache") {
      void loadCacheStatus(options);
    } else {
      void loadTasks({ replace: true, ...options });
    }
  }

  function selectView(view, { load = true, updateUrl = true } = {}) {
    state.view = ["tasks", "logs", "cache"].includes(view) ? view : "tasks";
    document.querySelectorAll("[data-history-view]").forEach((button) => {
      const selected = button.dataset.historyView === state.view;
      button.classList.toggle("is-selected", selected);
      button.setAttribute("aria-pressed", String(selected));
    });
    $("history-tasks-view").classList.toggle("hidden", state.view !== "tasks");
    $("history-logs-view").classList.toggle("hidden", state.view !== "logs");
    $("history-cache-view").classList.toggle("hidden", state.view !== "cache");
    window.clearTimeout(state.pollTimer);
    if (state.view === "tasks") {
      updateTopStats(
        "当前载入",
        state.tasks.length,
        "进行中",
        state.tasks.filter((task) => ACTIVE_STATUSES.has(task.status)).length,
      );
    } else if (state.view === "logs") {
      const attention = state.logs.filter((entry) => ["warning", "error"].includes(entry.level)).length;
      updateTopStats("当前载入", state.logs.length, "警告 / 错误", attention);
    } else if (state.cacheStatus) {
      updateTopStats(
        "缓存文件",
        state.cacheStatus.file_count,
        "磁盘容量",
        formatBytes(state.cacheStatus.size_bytes),
      );
    } else {
      updateTopStats("缓存文件", "—", "磁盘容量", "—");
    }
    if (updateUrl) persistFilters();
    if (load) refreshCurrentView();
  }

  function selectGroup(group) {
    state.group = STATUS_GROUPS[group] ? group : "all";
    document.querySelectorAll("[data-status-group]").forEach((button) => {
      const selected = button.dataset.statusGroup === state.group;
      button.classList.toggle("is-selected", selected);
      button.setAttribute("aria-pressed", String(selected));
    });
  }

  function resetPageAndLoad() {
    state.tasks = [];
    state.nextCursor = null;
    state.hasMore = false;
    state.etag = "";
    state.etagKey = "";
    persistFilters();
    $("history-task-rows").innerHTML = '<tr class="history-loading-row"><td colspan="6"><span class="history-spinner" aria-hidden="true"></span>正在读取历史任务</td></tr>';
    tableScroll.classList.remove("hidden");
    $("history-empty").classList.add("hidden");
    void loadTasks({ replace: true });
  }

  function loadInitialFilters() {
    const params = new URLSearchParams(location.search);
    state.view = ["tasks", "logs", "cache"].includes(params.get("view"))
      ? params.get("view")
      : "tasks";
    selectGroup(params.get("group") || "all");
    state.query = (params.get("q") || "").slice(0, 128);
    state.createdFrom = params.get("from") || "";
    state.createdTo = params.get("to") || "";
    const detailTaskId = params.get("detail") || "";
    state.detailTaskId = /^[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/i.test(detailTaskId)
      ? detailTaskId
      : "";
    $("history-query").value = state.query;
    $("history-created-from").value = state.createdFrom;
    $("history-created-to").value = state.createdTo;
    state.logLevel = params.get("log_level") || "";
    state.logTaskId = params.get("task_id") || "";
    state.logRequestId = params.get("request_id") || "";
    state.logQuery = (params.get("log_q") || "").slice(0, 128);
    state.logCreatedFrom = params.get("log_from") || "";
    state.logCreatedTo = params.get("log_to") || "";
    $("history-log-level").value = state.logLevel;
    $("history-log-task-id").value = state.logTaskId;
    $("history-log-request-id").value = state.logRequestId;
    $("history-log-query").value = state.logQuery;
    $("history-log-created-from").value = state.logCreatedFrom;
    $("history-log-created-to").value = state.logCreatedTo;
  }

  document.querySelectorAll("[data-status-group]").forEach((button) => {
    button.addEventListener("click", () => {
      selectGroup(button.dataset.statusGroup);
      resetPageAndLoad();
    });
  });
  $("history-filters").addEventListener("submit", (event) => {
    event.preventDefault();
    state.query = $("history-query").value.trim();
    state.createdFrom = $("history-created-from").value;
    state.createdTo = $("history-created-to").value;
    resetPageAndLoad();
  });
  $("history-reset").addEventListener("click", () => {
    selectGroup("all");
    state.query = "";
    state.createdFrom = "";
    state.createdTo = "";
    $("history-query").value = "";
    $("history-created-from").value = "";
    $("history-created-to").value = "";
    resetPageAndLoad();
  });
  $("history-refresh").addEventListener("click", () => void loadTasks({ replace: true }));
  $("history-load-more").addEventListener("click", () => void loadTasks({ replace: false, append: true }));
  $("history-detail-close").addEventListener("click", closeTaskDetail);
  $("history-detail-dialog").addEventListener("cancel", (event) => {
    event.preventDefault();
    closeTaskDetail();
  });
  $("history-detail-dialog").addEventListener("click", (event) => {
    if (event.target === $("history-detail-dialog")) closeTaskDetail();
  });
  $("history-delete-start").addEventListener("click", () => {
    $("history-delete-confirm").classList.remove("hidden");
    $("history-delete-confirm-button").focus();
  });
  $("history-delete-cancel").addEventListener("click", () => {
    $("history-delete-confirm").classList.add("hidden");
    $("history-delete-start").focus();
  });
  $("history-delete-confirm-button").addEventListener("click", () => void deleteCurrentTask());
  document.querySelectorAll("[data-history-view]").forEach((button) => {
    button.addEventListener("click", () => {
      if ($("history-detail-dialog").open) closeTaskDetail();
      selectView(button.dataset.historyView);
    });
  });
  $("history-log-filters").addEventListener("submit", (event) => {
    event.preventDefault();
    const taskId = $("history-log-task-id").value.trim();
    const requestId = $("history-log-request-id").value.trim();
    if ((taskId && !UUID_PATTERN.test(taskId)) || (requestId && !UUID_PATTERN.test(requestId))) {
      showLogAlert("Task ID 和 Request ID 必须是完整 UUID");
      return;
    }
    state.logLevel = $("history-log-level").value;
    state.logTaskId = taskId;
    state.logRequestId = requestId;
    state.logQuery = $("history-log-query").value.trim();
    state.logCreatedFrom = $("history-log-created-from").value;
    state.logCreatedTo = $("history-log-created-to").value;
    resetLogsAndLoad();
  });
  $("history-log-reset").addEventListener("click", () => {
    state.logLevel = "";
    state.logTaskId = "";
    state.logRequestId = "";
    state.logQuery = "";
    state.logCreatedFrom = "";
    state.logCreatedTo = "";
    $("history-log-level").value = "";
    $("history-log-task-id").value = "";
    $("history-log-request-id").value = "";
    $("history-log-query").value = "";
    $("history-log-created-from").value = "";
    $("history-log-created-to").value = "";
    resetLogsAndLoad();
  });
  $("history-log-refresh").addEventListener("click", () => void loadLogs());
  $("history-log-load-more").addEventListener("click", () => void loadLogs({ append: true }));
  $("history-cache-refresh").addEventListener("click", () => void loadCacheStatus());
  $("history-cache-clear").addEventListener("click", () => {
    if (!state.cacheStatus?.can_clear) return;
    $("history-cache-confirmation").value = "";
    $("history-cache-confirm-button").disabled = true;
    $("history-cache-clear-error").classList.add("hidden");
    $("history-cache-dialog").showModal();
    $("history-cache-confirmation").focus();
  });
  $("history-cache-confirmation").addEventListener("input", () => {
    $("history-cache-confirm-button").disabled = (
      $("history-cache-confirmation").value.trim() !== "清空全局 SVN 缓存"
    );
  });
  $("history-cache-confirm-button").addEventListener("click", () => void clearGlobalCache());
  document.addEventListener("visibilitychange", () => {
    if (document.hidden) {
      window.clearTimeout(state.pollTimer);
      $("history-live-indicator").classList.add("is-paused");
    } else {
      refreshCurrentView({ automatic: true });
    }
  });
  window.addEventListener("beforeunload", () => window.clearTimeout(state.pollTimer));

  loadInitialFilters();
  selectView(state.view, { load: false, updateUrl: false });
  refreshCurrentView();
  if (state.view === "tasks" && state.detailTaskId) {
    void openTaskDetail(state.detailTaskId, { updateUrl: false });
  }
})();
