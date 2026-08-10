(function () {
  "use strict";

  const $ = (id) => document.getElementById(id);
  const STATUS_LABELS = {
    syncing: "同步中", active: "运行中", paused: "已暂停",
    scheduler_error: "调度异常", ended: "已结束", archived: "已归档",
    queued: "等待运行", running: "运行中", succeeded: "成功",
    partial: "部分成功", failed: "失败", pending: "待同步",
    synced: "已同步", drifted: "存在漂移", error: "同步失败",
    not_present: "未创建", enabled: "启用", disabled: "停用", removed: "移除",
    scheduled: "定时触发", automatic_retry: "自动重试", manual_retry: "人工重试",
    pause: "暂停边界", end: "结束边界",
  };
  const EDITABLE = new Set(["active", "scheduler_error", "paused"]);
  const commandLedger = new globalThis.MonitorRequestLedger();
  const state = {
    query: "",
    status: "",
    tasks: [],
    nextCursor: null,
    hasMore: false,
    listEtag: "",
    listEtagUrl: "",
    loading: false,
    selectedTask: null,
    selectedRuns: [],
    pendingAction: null,
    pollTimer: 0,
    lastSuccessAt: null,
    requestGeneration: 0,
    requestController: null,
  };

  function node(tag, options = {}, children = []) {
    const element = document.createElement(tag);
    Object.entries(options).forEach(([key, value]) => {
      if (key === "className") element.className = value;
      else if (key === "text") element.textContent = value;
      else if (key === "dataset") Object.assign(element.dataset, value);
      else if (key === "disabled") element.disabled = Boolean(value);
      else element.setAttribute(key, value);
    });
    children.forEach((child) => element.append(child));
    return element;
  }

  function clear(element) { element.replaceChildren(); }

  function statusBadge(status) {
    return node("span", {
      className: "monitor-status",
      text: STATUS_LABELS[status] || status || "未知",
      dataset: { status: status || "unknown" },
    });
  }

  function errorMessage(error) {
    if (error && error.error && error.error.message) return error.error.message;
    if (error instanceof Error && error.message) return error.message;
    return "请求失败，请稍后重试";
  }

  async function requestJson(url, options = {}) {
    const response = await fetch(url, options);
    if (response.status === 304) return { notModified: true, etag: response.headers.get("ETag") || "" };
    const body = await response.json().catch(() => ({}));
    if (!response.ok) throw body;
    return { body, etag: response.headers.get("ETag") || "" };
  }

  function formatInstant(value, seconds = false) {
    if (!value) return "—";
    const date = new Date(value);
    if (Number.isNaN(date.getTime())) return "—";
    return new Intl.DateTimeFormat("zh-CN", {
      timeZone: "Asia/Shanghai",
      year: "numeric", month: "2-digit", day: "2-digit",
      hour: "2-digit", minute: "2-digit",
      second: seconds ? "2-digit" : undefined,
      hourCycle: "h23",
    }).format(date);
  }

  function utcToShanghaiInput(value) {
    if (!value) return "";
    const date = new Date(value);
    if (Number.isNaN(date.getTime())) return "";
    return new Date(date.getTime() + 8 * 60 * 60 * 1000).toISOString().slice(0, 19);
  }

  function shanghaiInputToUtc(value) {
    const match = /^(\d{4})-(\d{2})-(\d{2})T(\d{2}):(\d{2})(?::(\d{2}))?$/.exec(value);
    if (!match) return null;
    const date = new Date(Date.UTC(
      Number(match[1]), Number(match[2]) - 1, Number(match[3]),
      Number(match[4]) - 8, Number(match[5]), Number(match[6] || 0),
    ));
    return Number.isNaN(date.getTime()) ? null : date.toISOString();
  }

  function setAlert(id, message, success = false) {
    const alert = $(id);
    alert.textContent = message || "";
    alert.classList.toggle("is-hidden", !message);
    alert.classList.toggle("is-success", Boolean(message && success));
  }

  function persistUrl(taskId = state.selectedTask && state.selectedTask.task_id) {
    const params = new URLSearchParams();
    if (state.query) params.set("q", state.query);
    if (state.status) params.set("status", state.status);
    if (taskId) params.set("task", taskId);
    const query = params.toString();
    history.replaceState(null, "", location.pathname + (query ? "?" + query : ""));
  }

  function loadUrlState() {
    const params = new URLSearchParams(location.search);
    state.query = (params.get("q") || "").slice(0, 128);
    const status = params.get("status") || "";
    state.status = Array.from($("monitor-task-status").options).some((option) => option.value === status) ? status : "";
    $("monitor-task-query").value = state.query;
    $("monitor-task-status").value = state.status;
    return params.get("task") || "";
  }

  function listUrl(cursor = null, limit = 30) {
    const params = new URLSearchParams({ limit: String(limit) });
    if (state.query) params.set("q", state.query);
    if (state.status) params.append("status", state.status);
    if (cursor) params.set("cursor", cursor);
    return "/api/monitor/tasks?" + params.toString();
  }

  function taskCell(task) {
    const cell = node("td");
    const link = node("button", { className: "monitor-action is-accent", type: "button", text: task.name, title: task.name });
    link.addEventListener("click", () => void openDetail(task.task_id));
    cell.append(link);
    cell.append(node("small", { text: task.branch.label + " · " + task.branch.repository_relative_path }));
    cell.append(node("code", { text: "r" + task.branch.bound_revision + " · " + task.task_id }));
    return cell;
  }

  function appendTaskRow(task) {
    const row = node("tr");
    row.append(taskCell(task));

    const statusCell = node("td");
    statusCell.append(statusBadge(task.status));
    if (task.pending_run_count) statusCell.append(node("small", { className: "monitor-backlog", text: task.pending_run_count + " 个待处理运行" }));
    row.append(statusCell);

    const scheduleCell = node("td");
    scheduleCell.append(node("strong", { text: task.schedule.daily_trigger_time + " · 上海" }));
    scheduleCell.append(node("small", { text: "下次 " + formatInstant(task.schedule.next_logical_cutoff_at) }));
    if (task.schedule.end_at) scheduleCell.append(node("small", { text: "结束 " + formatInstant(task.schedule.end_at) }));
    row.append(scheduleCell);

    const runCell = node("td");
    if (task.latest_run) {
      runCell.append(statusBadge(task.latest_run.status));
      runCell.append(node("small", { text: "截止 " + formatInstant(task.latest_run.interval.logical_cutoff_at) }));
      if (task.latest_run.summary) {
        runCell.append(node("small", { text: task.latest_run.summary.change_count + " 项变化 · " + task.latest_run.summary.error_count + " 项错误" }));
      }
    } else {
      runCell.append(node("span", { text: "尚未运行" }));
    }
    row.append(runCell);

    const schedulerCell = node("td");
    schedulerCell.append(statusBadge(task.scheduler.sync_status));
    schedulerCell.append(node("small", { text: "心跳 " + formatInstant(task.last_runner_heartbeat_at, true) }));
    if (task.scheduler.last_error) schedulerCell.append(node("small", { className: "monitor-error-text", text: task.scheduler.last_error.message }));
    row.append(schedulerCell);

    const actionCell = node("td");
    const actions = node("div", { className: "monitor-actions" });
    const detailButton = node("button", { className: "monitor-action", type: "button", text: "查看" });
    detailButton.addEventListener("click", () => void openDetail(task.task_id));
    actions.append(detailButton);
    if (task.latest_report) {
      actions.append(node("a", {
        className: "monitor-action is-accent",
        href: "/api/monitor/tasks/" + encodeURIComponent(task.task_id) + "/latest-report",
        target: "_blank", rel: "noopener", text: "报告",
      }));
    }
    actionCell.append(actions);
    row.append(actionCell);
    $("monitor-task-rows").append(row);
  }

  function renderTasks() {
    clear($("monitor-task-rows"));
    state.tasks.forEach(appendTaskRow);
    const empty = state.tasks.length === 0;
    document.querySelector(".monitor-task-index .monitor-table-scroll").classList.toggle("is-hidden", empty);
    $("monitor-task-empty").classList.toggle("is-hidden", !empty);
    $("monitor-load-more").classList.toggle("is-hidden", !state.hasMore || empty);
    const updated = state.lastSuccessAt
      ? " · 更新 " + state.lastSuccessAt.toLocaleTimeString("zh-CN", { hourCycle: "h23" })
      : "";
    $("monitor-task-meta").textContent = state.tasks.length + " 条已载入" + (state.hasMore ? " · 仍有更多" : "") + updated;
  }

  function setTaskEmpty(title, message) {
    const empty = $("monitor-task-empty");
    empty.querySelector("strong").textContent = title;
    empty.querySelector("span").textContent = message;
  }

  function schedulePoll() {
    window.clearTimeout(state.pollTimer);
    if (!document.hidden) state.pollTimer = window.setTimeout(() => void loadTasks({ automatic: true }), 30000);
  }

  async function loadTasks({ append = false, automatic = false } = {}) {
    if (state.loading) return;
    state.loading = true;
    const generation = ++state.requestGeneration;
    const controller = new AbortController();
    state.requestController = controller;
    const refresh = $("monitor-task-refresh");
    refresh.disabled = true;
    refresh.classList.add("is-loading");
    const cursor = append ? state.nextCursor : null;
    const url = listUrl(cursor, append ? 50 : 30);
    const headers = { Accept: "application/json" };
    if (!cursor && state.listEtag && state.listEtagUrl === url) headers["If-None-Match"] = state.listEtag;
    try {
      const response = await requestJson(url, { headers, signal: controller.signal });
      if (generation !== state.requestGeneration) return;
      if (!response.notModified) {
        if (response.body.schema_version !== "m3.monitor-task-list.v1") throw new Error("监控任务列表契约无效");
        state.tasks = append ? state.tasks.concat(response.body.items) : response.body.items;
        state.nextCursor = response.body.next_cursor;
        state.hasMore = Boolean(response.body.has_more);
        if (!cursor) {
          state.listEtag = response.etag;
          state.listEtagUrl = url;
        }
        renderTasks();
      }
      state.lastSuccessAt = new Date();
      setTaskEmpty("当前筛选无任务", "调整筛选条件或新建任务");
      renderTasks();
      setAlert("monitor-task-alert", "");
    } catch (error) {
      if (error && error.name === "AbortError") return;
      if (generation !== state.requestGeneration) return;
      const suffix = state.lastSuccessAt
        ? "；最后成功刷新 " + state.lastSuccessAt.toLocaleTimeString("zh-CN", { hourCycle: "h23" })
        : "";
      setAlert("monitor-task-alert", "刷新失败，当前数据可能已过期" + suffix + "：" + errorMessage(error));
      if (!state.tasks.length) {
        renderTasks();
        setTaskEmpty("任务读取失败", "请检查服务状态后重试");
        $("monitor-task-meta").textContent = "读取失败";
      }
    } finally {
      if (generation !== state.requestGeneration) return;
      state.loading = false;
      state.requestController = null;
      refresh.disabled = false;
      refresh.classList.remove("is-loading");
      schedulePoll();
    }
  }

  function resetAndLoad() {
    state.requestGeneration += 1;
    if (state.requestController) state.requestController.abort();
    state.requestController = null;
    state.loading = false;
    state.tasks = [];
    state.nextCursor = null;
    state.hasMore = false;
    state.listEtag = "";
    state.listEtagUrl = "";
    persistUrl();
    clear($("monitor-task-rows"));
    $("monitor-task-rows").append(node("tr", {}, [node("td", { className: "monitor-loading", colspan: "6", text: "正在读取任务" })]));
    document.querySelector(".monitor-task-index .monitor-table-scroll").classList.remove("is-hidden");
    $("monitor-task-empty").classList.add("is-hidden");
    void loadTasks();
  }

  function actionButton(label, callback, danger = false) {
    const button = node("button", {
      className: "monitor-action" + (danger ? " is-danger" : ""),
      type: "button", text: label,
    });
    button.addEventListener("click", callback);
    return button;
  }

  function commandConfig(command, task) {
    const configs = {
      pause: ["暂停任务", "暂停后不再产生新的定时运行；恢复后使用已保存的调度。", "暂停"],
      resume: ["恢复任务", "恢复后会重新同步 Windows 计划任务，并按当前调度继续运行。", "恢复"],
      end: ["结束任务", "结束会固定任务终态；之后只能查看、归档或修复调度移除状态。", "结束"],
      archive: ["归档任务", "归档后任务业务只读，历史报告仍按保留规则访问。", "归档"],
      "scheduler-sync": ["同步计划任务", "系统将检查并修复当前 Windows 计划任务定义。", "同步"],
    };
    const config = configs[command];
    return { title: config[0], message: task.name + "：" + config[1], submit: config[2] };
  }

  function openConfirm(action) {
    state.pendingAction = action;
    $("monitor-confirm-title").textContent = action.title;
    $("monitor-confirm-message").textContent = action.message;
    $("monitor-confirm-submit").textContent = action.submit;
    $("monitor-confirm-submit").classList.toggle("monitor-danger", action.danger !== false);
    $("monitor-confirm-submit").classList.toggle("monitor-primary", action.danger === false);
    $("monitor-confirm-dialog").showModal();
    $("monitor-confirm-cancel").focus();
  }

  function requestTaskCommand(command) {
    const task = state.selectedTask;
    const config = commandConfig(command, task);
    openConfirm({
      ...config,
      danger: command !== "scheduler-sync" && command !== "resume",
      execute: () => executeTaskCommand(command),
    });
  }

  async function executeTaskCommand(command) {
    const taskId = state.selectedTask.task_id;
    const target = "/api/monitor/tasks/" + encodeURIComponent(taskId) + "/" + command;
    const response = await commandLedger.send(target, {
      method: "POST",
      schemaVersion: "m3.monitor-command.request.v1",
      payload: {},
    });
    if (response.body.schema_version !== "m3.monitor-task.v1") throw new Error("任务命令响应契约无效");
    state.selectedTask = response.body;
    renderDetail(response.body);
    state.listEtag = "";
    await loadTasks();
    return "操作已提交，当前状态：" + (STATUS_LABELS[response.body.status] || response.body.status);
  }

  function renderDetail(task) {
    $("monitor-detail-title").textContent = task.name;
    $("monitor-detail-id").textContent = task.task_id;
    $("monitor-detail-branch").textContent = task.branch.label + " · " + task.branch.repository_relative_path + " · r" + task.branch.bound_revision;
    $("monitor-detail-schedule").textContent = "每日 " + task.schedule.daily_trigger_time + " · 生效 " + formatInstant(task.schedule.effective_at) + (task.schedule.end_at ? " · 结束 " + formatInstant(task.schedule.end_at) : "");
    $("monitor-detail-scheduler").textContent = (STATUS_LABELS[task.scheduler.sync_status] || task.scheduler.sync_status) + " · 期望" + (STATUS_LABELS[task.scheduler.desired_state] || task.scheduler.desired_state) + " · G" + task.scheduler.generation;
    $("monitor-detail-heartbeat").textContent = formatInstant(task.last_runner_heartbeat_at, true) + " · 积压 " + task.pending_run_count;
    if (task.scheduler.last_error) setAlert("monitor-detail-alert", task.scheduler.last_error.message);
    else setAlert("monitor-detail-alert", "");

    const actions = $("monitor-detail-actions");
    clear(actions);
    actions.append(statusBadge(task.status));
    if (["active", "scheduler_error"].includes(task.status)) actions.append(actionButton("暂停", () => requestTaskCommand("pause")));
    if (task.status === "paused") actions.append(actionButton("恢复", () => requestTaskCommand("resume")));
    if (EDITABLE.has(task.status)) actions.append(actionButton("修改调度", openEdit));
    if (EDITABLE.has(task.status)) actions.append(actionButton("结束", () => requestTaskCommand("end"), true));
    if (task.status === "ended" && task.pending_run_count === 0) actions.append(actionButton("归档", () => requestTaskCommand("archive"), true));
    if (["active", "scheduler_error", "paused", "ended", "archived"].includes(task.status)) {
      actions.append(actionButton("同步计划任务", () => requestTaskCommand("scheduler-sync")));
    }
    if (task.latest_report) {
      actions.append(node("a", {
        className: "monitor-action is-accent",
        href: "/api/monitor/tasks/" + encodeURIComponent(task.task_id) + "/latest-report",
        target: "_blank", rel: "noopener", text: "最近报告",
      }));
    }
    renderRuns(state.selectedRuns, task.status === "archived");
  }

  function renderRuns(runs, archived) {
    const rows = $("monitor-run-rows");
    clear(rows);
    if (!runs.length) {
      rows.append(node("tr", {}, [node("td", { className: "monitor-loading", colspan: "5", text: "暂无运行记录" })]));
      $("monitor-run-meta").textContent = "0 条";
      return;
    }
    runs.forEach((run) => {
      const row = node("tr");
      const interval = node("td");
      interval.append(node("strong", { text: formatInstant(run.interval.start_at) + " → " + formatInstant(run.interval.end_at) }));
      interval.append(node("small", { text: STATUS_LABELS[run.interval.boundary_kind] || run.interval.boundary_kind }));
      row.append(interval);

      const status = node("td");
      status.append(statusBadge(run.status));
      row.append(status);

      const summary = node("td");
      if (run.summary) {
        summary.append(node("strong", { text: run.summary.change_count + " 项净值变化" }));
        summary.append(node("small", { text: run.summary.changed_workbook_count + "/" + run.summary.workbook_count + " 工作簿 · " + run.summary.error_count + " 错误" }));
      } else if (run.errors.length) {
        summary.append(node("span", { className: "monitor-error-text", text: run.errors[0].message }));
      } else summary.append(node("span", { text: "尚无结果" }));
      row.append(summary);

      const attempt = node("td");
      attempt.append(node("strong", { text: run.attempt_count + " 次尝试" }));
      if (run.attempts.length) {
        const latest = run.attempts[run.attempts.length - 1];
        attempt.append(node("small", { text: (STATUS_LABELS[latest.trigger] || latest.trigger) + " · " + formatInstant(latest.started_at, true) }));
      }
      row.append(attempt);

      const report = node("td");
      const reportExpired = run.report_expires_at
        && new Date(run.report_expires_at).getTime() <= Date.now();
      if (["succeeded", "partial"].includes(run.status) && !reportExpired) {
        report.append(node("a", {
          className: "monitor-table-link",
          href: "/monitor/reports/" + encodeURIComponent(run.run_id),
          target: "_blank", rel: "noopener", text: "打开报告",
        }));
        report.append(node("small", { text: "保留至 " + formatInstant(run.report_expires_at) }));
      } else if (["succeeded", "partial"].includes(run.status) && reportExpired) {
        report.append(statusBadge("archived"));
        report.append(node("small", { text: "历史报告已过期" }));
      } else if (run.status === "failed" && !archived) {
        const retry = actionButton("人工重试", () => {
          openConfirm({
            title: "重试运行",
            message: "将按原逻辑区间重新执行。请求会先进入持久队列，页面无需保持打开。",
            submit: "提交重试",
            danger: false,
            execute: () => retryRun(run.run_id),
          });
        });
        report.append(retry);
      } else report.append(node("span", { text: "—" }));
      row.append(report);
      rows.append(row);
    });
    $("monitor-run-meta").textContent = runs.length + " 条最近运行";
  }

  async function retryRun(runId) {
    const target = "/api/monitor/runs/" + encodeURIComponent(runId) + "/retry";
    const response = await commandLedger.send(target, {
      method: "POST",
      schemaVersion: "m3.monitor-run-retry.request.v1",
      payload: {},
    });
    if (response.body.schema_version !== "m3.monitor-run-retry.accepted.v1") throw new Error("重试受理响应契约无效");
    await refreshDetail();
    return "重试请求已持久化，后台将按原区间执行";
  }

  async function fetchDetail(taskId) {
    const [taskResponse, runsResponse] = await Promise.all([
      requestJson("/api/monitor/tasks/" + encodeURIComponent(taskId), { headers: { Accept: "application/json" } }),
      requestJson("/api/monitor/tasks/" + encodeURIComponent(taskId) + "/runs?limit=50", { headers: { Accept: "application/json" } }),
    ]);
    if (taskResponse.body.schema_version !== "m3.monitor-task.v1" || runsResponse.body.schema_version !== "m3.monitor-run-list.v1") {
      throw new Error("监控详情契约无效");
    }
    return { task: taskResponse.body, runs: runsResponse.body.items };
  }

  async function openDetail(taskId, options = {}) {
    const dialog = $("monitor-detail-dialog");
    if (!dialog.open) dialog.showModal();
    $("monitor-detail-title").textContent = "正在读取任务";
    $("monitor-detail-id").textContent = taskId;
    setAlert("monitor-detail-alert", "");
    try {
      const result = await fetchDetail(taskId);
      state.selectedTask = result.task;
      state.selectedRuns = result.runs;
      renderDetail(result.task);
      if (options.updateUrl !== false) persistUrl(taskId);
    } catch (error) {
      setAlert("monitor-detail-alert", errorMessage(error));
    }
  }

  async function refreshDetail() {
    if (!state.selectedTask) return;
    const result = await fetchDetail(state.selectedTask.task_id);
    state.selectedTask = result.task;
    state.selectedRuns = result.runs;
    renderDetail(result.task);
  }

  function closeDetail() {
    $("monitor-detail-dialog").close();
    state.selectedTask = null;
    state.selectedRuns = [];
    persistUrl(null);
  }

  function openEdit() {
    const task = state.selectedTask;
    $("monitor-edit-trigger").value = task.schedule.daily_trigger_time;
    $("monitor-edit-end").value = utcToShanghaiInput(task.schedule.end_at);
    setAlert("monitor-edit-alert", "");
    $("monitor-edit-dialog").showModal();
  }

  async function saveEdit(event) {
    event.preventDefault();
    const endValue = $("monitor-edit-end").value;
    const endAt = endValue ? shanghaiInputToUtc(endValue) : null;
    if (endValue && !endAt) {
      setAlert("monitor-edit-alert", "结束时间格式无效");
      return;
    }
    const submit = $("monitor-edit-form").querySelector('button[type="submit"]');
    submit.disabled = true;
    try {
      const trigger = $("monitor-edit-trigger").value;
      const target = "/api/monitor/tasks/" + encodeURIComponent(state.selectedTask.task_id);
      const response = await commandLedger.send(target, {
        method: "PATCH",
        schemaVersion: "m3.monitor-task-patch.request.v1",
        payload: {
          daily_trigger_time: trigger.length === 5 ? trigger + ":00" : trigger,
          end_at: endAt,
        },
      });
      if (response.body.schema_version !== "m3.monitor-task.v1") throw new Error("修改任务响应契约无效");
      state.selectedTask = response.body;
      $("monitor-edit-dialog").close();
      renderDetail(response.body);
      state.listEtag = "";
      await loadTasks();
      setAlert("monitor-detail-alert", "调度已保存并同步", true);
    } catch (error) {
      setAlert("monitor-edit-alert", errorMessage(error));
    } finally {
      submit.disabled = false;
    }
  }

  async function executePendingAction() {
    if (!state.pendingAction) return;
    const action = state.pendingAction;
    const submit = $("monitor-confirm-submit");
    submit.disabled = true;
    try {
      const message = await action.execute();
      $("monitor-confirm-dialog").close();
      state.pendingAction = null;
      setAlert("monitor-detail-alert", message, true);
    } catch (error) {
      $("monitor-confirm-dialog").close();
      state.pendingAction = null;
      setAlert("monitor-detail-alert", errorMessage(error));
    } finally {
      submit.disabled = false;
    }
  }

  $("monitor-task-filter").addEventListener("submit", (event) => {
    event.preventDefault();
    state.query = $("monitor-task-query").value.trim();
    state.status = $("monitor-task-status").value;
    resetAndLoad();
  });
  $("monitor-filter-reset").addEventListener("click", () => {
    state.query = "";
    state.status = "";
    $("monitor-task-query").value = "";
    $("monitor-task-status").value = "";
    resetAndLoad();
  });
  $("monitor-task-refresh").addEventListener("click", () => {
    state.listEtag = "";
    void loadTasks();
  });
  $("monitor-load-more").addEventListener("click", () => void loadTasks({ append: true }));
  $("monitor-detail-close").addEventListener("click", closeDetail);
  $("monitor-detail-dialog").addEventListener("cancel", (event) => { event.preventDefault(); closeDetail(); });
  $("monitor-detail-dialog").addEventListener("click", (event) => { if (event.target === $("monitor-detail-dialog")) closeDetail(); });
  $("monitor-edit-close").addEventListener("click", () => $("monitor-edit-dialog").close());
  $("monitor-edit-cancel").addEventListener("click", () => $("monitor-edit-dialog").close());
  $("monitor-edit-dialog").addEventListener("cancel", (event) => { event.preventDefault(); $("monitor-edit-dialog").close(); });
  $("monitor-edit-form").addEventListener("submit", (event) => void saveEdit(event));
  $("monitor-confirm-cancel").addEventListener("click", () => { state.pendingAction = null; $("monitor-confirm-dialog").close(); });
  $("monitor-confirm-dialog").addEventListener("cancel", (event) => { event.preventDefault(); state.pendingAction = null; $("monitor-confirm-dialog").close(); });
  $("monitor-confirm-submit").addEventListener("click", () => void executePendingAction());
  document.addEventListener("visibilitychange", () => {
    if (document.hidden) window.clearTimeout(state.pollTimer);
    else void loadTasks({ automatic: true });
  });
  window.addEventListener("beforeunload", () => window.clearTimeout(state.pollTimer));

  const initialTaskId = loadUrlState();
  void loadTasks();
  if (initialTaskId) void openDetail(initialTaskId, { updateUrl: false });
})();
