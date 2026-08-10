(function () {
  "use strict";

  const $ = (id) => document.getElementById(id);
  const STATUS_LABELS = {
    syncing: "同步中",
    active: "运行中",
    paused: "已暂停",
    scheduler_error: "调度异常",
    ended: "已结束",
    archived: "已归档",
    queued: "等待运行",
    running: "运行中",
    succeeded: "成功",
    partial: "部分成功",
    failed: "失败",
    pending: "待同步",
    synced: "已同步",
    drifted: "存在漂移",
    error: "同步失败",
    not_present: "未创建",
  };
  const state = { etag: "", loading: false, pollTimer: 0, lastSuccessAt: null };
  const commandLedger = new globalThis.MonitorRequestLedger();

  function node(tag, options = {}, children = []) {
    const element = document.createElement(tag);
    Object.entries(options).forEach(([key, value]) => {
      if (key === "className") element.className = value;
      else if (key === "text") element.textContent = value;
      else if (key === "dataset") Object.assign(element.dataset, value);
      else element.setAttribute(key, value);
    });
    children.forEach((child) => element.append(child));
    return element;
  }

  function clear(element) {
    element.replaceChildren();
  }

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

  function shanghaiInputToUtc(value) {
    const match = /^(\d{4})-(\d{2})-(\d{2})T(\d{2}):(\d{2})(?::(\d{2}))?$/.exec(value);
    if (!match) return null;
    const utcMs = Date.UTC(
      Number(match[1]), Number(match[2]) - 1, Number(match[3]),
      Number(match[4]) - 8, Number(match[5]), Number(match[6] || 0),
    );
    const date = new Date(utcMs);
    return Number.isNaN(date.getTime()) ? null : date.toISOString();
  }

  function utcToShanghai(value, includeSeconds = false) {
    if (!value) return "—";
    const date = new Date(value);
    if (Number.isNaN(date.getTime())) return "—";
    return new Intl.DateTimeFormat("zh-CN", {
      timeZone: "Asia/Shanghai",
      year: "numeric",
      month: "2-digit",
      day: "2-digit",
      hour: "2-digit",
      minute: "2-digit",
      second: includeSeconds ? "2-digit" : undefined,
      hourCycle: "h23",
    }).format(date);
  }

  function setDefaultEffectiveAt() {
    const shifted = new Date(Date.now() + 8 * 60 * 60 * 1000);
    shifted.setUTCSeconds(0, 0);
    $("monitor-effective-at").value = shifted.toISOString().slice(0, 16);
  }

  function setAlert(message, success = false) {
    const alert = $("monitor-alert");
    alert.textContent = message || "";
    alert.classList.toggle("is-hidden", !message);
    alert.classList.toggle("is-success", Boolean(message && success));
  }

  async function loadEndpoints() {
    const select = $("monitor-endpoint");
    try {
      const response = await requestJson("/api/monitor/endpoint-options", {
        headers: { Accept: "application/json" },
      });
      if (response.body.schema_version !== "m3.monitor-endpoint-options.v1") {
        throw new Error("固定分支列表契约无效");
      }
      clear(select);
      select.append(node("option", { value: "", text: response.body.items.length ? "请选择固定分支" : "暂无可用固定分支" }));
      response.body.items.forEach((item) => {
        select.append(node("option", { value: item.endpoint_id, text: item.label }));
      });
      select.disabled = response.body.items.length === 0;
    } catch (error) {
      clear(select);
      select.append(node("option", { value: "", text: "固定分支读取失败" }));
      select.disabled = true;
      setAlert(errorMessage(error));
    }
  }

  function appendTextCell(row, primary, secondary = "", className = "") {
    const cell = node("td", { className });
    cell.append(node("strong", { text: primary }));
    if (secondary) cell.append(node("small", { text: secondary }));
    row.append(cell);
  }

  function renderRecent(items) {
    const rows = $("monitor-recent-rows");
    clear(rows);
    document.querySelector(".monitor-recent .monitor-table-scroll").classList.toggle("is-hidden", items.length === 0);
    $("monitor-recent-empty").classList.toggle("is-hidden", items.length !== 0);
    items.forEach((task) => {
      const row = node("tr");
      const taskCell = node("td");
      taskCell.append(node("a", {
        className: "monitor-table-link",
        href: "/monitor/tasks?task=" + encodeURIComponent(task.task_id),
        text: task.name,
      }));
      taskCell.append(node("small", { text: task.branch.label + " · r" + task.branch.bound_revision }));
      row.append(taskCell);

      const statusCell = node("td");
      statusCell.append(statusBadge(task.status));
      row.append(statusCell);

      const schedulerCell = node("td");
      schedulerCell.append(statusBadge(task.scheduler.sync_status));
      if (task.scheduler.last_error) {
        schedulerCell.append(node("small", { className: "monitor-error-text", text: task.scheduler.last_error.message }));
      }
      row.append(schedulerCell);

      appendTextCell(row, utcToShanghai(task.schedule.next_logical_cutoff_at), task.schedule.daily_trigger_time + " · 上海");

      const reportCell = node("td");
      if (task.latest_report) {
        const summary = task.latest_report.summary;
        reportCell.append(node("a", {
          className: "monitor-table-link",
          href: "/api/monitor/tasks/" + encodeURIComponent(task.task_id) + "/latest-report",
          target: "_blank",
          rel: "noopener",
          text: STATUS_LABELS[task.latest_report.status],
        }));
        reportCell.append(node("small", { text: summary.change_count + " 项变化 · " + summary.error_count + " 项错误" }));
      } else if (task.latest_run) {
        reportCell.append(statusBadge(task.latest_run.status));
        reportCell.append(node("small", { text: "报告尚未发布" }));
      } else {
        reportCell.append(node("span", { text: "尚未运行" }));
      }
      row.append(reportCell);

      const backlogCell = node("td");
      backlogCell.append(node("strong", {
        className: task.pending_run_count ? "monitor-backlog" : "",
        text: String(task.pending_run_count),
      }));
      backlogCell.append(node("small", { text: task.pending_run_count ? "待处理运行" : "无积压" }));
      row.append(backlogCell);
      rows.append(row);
    });
  }

  function setRecentEmpty(title, message) {
    const empty = $("monitor-recent-empty");
    empty.querySelector("strong").textContent = title;
    empty.querySelector("span").textContent = message;
  }

  function updateSuccessTime() {
    state.lastSuccessAt = new Date();
    $("monitor-recent-updated").textContent = "更新 " + state.lastSuccessAt.toLocaleTimeString(
      "zh-CN", { hour: "2-digit", minute: "2-digit", second: "2-digit", hourCycle: "h23" },
    );
  }

  function schedulePoll() {
    window.clearTimeout(state.pollTimer);
    if (!document.hidden) state.pollTimer = window.setTimeout(() => void loadRecent(true), 30000);
  }

  async function loadRecent(automatic = false) {
    if (state.loading) return;
    state.loading = true;
    const refresh = $("monitor-refresh");
    refresh.disabled = true;
    refresh.classList.add("is-loading");
    const headers = { Accept: "application/json" };
    if (state.etag) headers["If-None-Match"] = state.etag;
    try {
      const response = await requestJson("/api/monitor/tasks?limit=8", { headers });
      if (!response.notModified) {
        if (response.body.schema_version !== "m3.monitor-task-list.v1") throw new Error("监控任务列表契约无效");
        state.etag = response.etag;
        renderRecent(response.body.items);
      }
      setRecentEmpty("暂无监控任务", "创建后会在此显示运行与报告状态");
      updateSuccessTime();
      setAlert("");
    } catch (error) {
      const suffix = state.lastSuccessAt
        ? "；最后成功刷新 " + state.lastSuccessAt.toLocaleTimeString("zh-CN", { hourCycle: "h23" })
        : "";
      setAlert("刷新失败，当前数据可能已过期" + suffix + "：" + errorMessage(error));
      if (!state.etag) {
        renderRecent([]);
        setRecentEmpty("任务读取失败", "请检查服务状态后重试");
      }
    } finally {
      state.loading = false;
      refresh.disabled = false;
      refresh.classList.remove("is-loading");
      schedulePoll();
    }
  }

  async function createTask(event) {
    event.preventDefault();
    const effectiveAt = shanghaiInputToUtc($("monitor-effective-at").value);
    const endValue = $("monitor-end-at").value;
    const endAt = endValue ? shanghaiInputToUtc(endValue) : null;
    if (!effectiveAt || (endValue && !endAt)) {
      setAlert("时间格式无效");
      return;
    }
    if (endAt && endAt <= effectiveAt) {
      setAlert("结束时间必须晚于生效时间");
      return;
    }
    const submit = $("monitor-create-submit");
    const status = $("monitor-create-status");
    submit.disabled = true;
    status.textContent = "正在验证分支并同步计划任务";
    setAlert("");
    try {
      const response = await commandLedger.send("/api/monitor/tasks", {
        method: "POST",
        schemaVersion: "m3.monitor-task-create.request.v1",
        payload: {
          name: $("monitor-name").value.trim(),
          endpoint_id: $("monitor-endpoint").value,
          effective_at: effectiveAt,
          end_at: endAt,
          daily_trigger_time: $("monitor-trigger").value.length === 5
            ? $("monitor-trigger").value + ":00"
            : $("monitor-trigger").value,
        },
      });
      if (response.body.schema_version !== "m3.monitor-task.v1") throw new Error("新建任务响应契约无效");
      $("monitor-create-form").reset();
      $("monitor-trigger").value = "18:00:00";
      setDefaultEffectiveAt();
      status.textContent = "";
      setAlert("任务已创建，调度状态：" + (STATUS_LABELS[response.body.status] || response.body.status), true);
      state.etag = "";
      await loadRecent();
    } catch (error) {
      status.textContent = "";
      setAlert(errorMessage(error));
    } finally {
      submit.disabled = false;
    }
  }

  $("monitor-create-form").addEventListener("submit", (event) => void createTask(event));
  $("monitor-refresh").addEventListener("click", () => {
    state.etag = "";
    void loadRecent();
  });
  document.addEventListener("visibilitychange", () => {
    if (document.hidden) window.clearTimeout(state.pollTimer);
    else void loadRecent(true);
  });
  window.addEventListener("beforeunload", () => window.clearTimeout(state.pollTimer));

  setDefaultEffectiveAt();
  void loadEndpoints();
  void loadRecent();
})();
