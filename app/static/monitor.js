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
  const state = {
    etag: "",
    loading: false,
    pollTimer: 0,
    lastSuccessAt: null,
    endpointOptions: [],
    endpointMatches: [],
    selectedEndpointId: "",
    activeEndpointIndex: -1,
  };
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

  function setAlert(id, message, success = false) {
    const alert = $(id);
    alert.textContent = message || "";
    alert.classList.toggle("is-hidden", !message);
    alert.classList.toggle("is-success", Boolean(message && success));
  }

  function endpointSearchText(value) {
    return String(value || "").trim().toLocaleLowerCase("zh-CN");
  }

  function compactEndpointText(value) {
    return endpointSearchText(value).replace(/[\s._-]+/g, "");
  }

  function filterEndpointOptions(query) {
    const normalized = endpointSearchText(query);
    if (!normalized) return state.endpointOptions.slice();
    const tokens = normalized.split(/\s+/).filter(Boolean);
    return state.endpointOptions
      .filter((item) => {
        const haystack = endpointSearchText(item.label + " " + item.endpoint_id);
        const compact = compactEndpointText(haystack);
        return tokens.every((token) => {
          const compactToken = compactEndpointText(token);
          return haystack.includes(token) || (
            Boolean(compactToken) && compact.includes(compactToken)
          );
        });
      })
      .sort((left, right) => {
        const leftLabel = endpointSearchText(left.label);
        const rightLabel = endpointSearchText(right.label);
        const score = (label, item) => {
          if (label.startsWith(normalized)) return 0;
          if (label.includes(normalized)) return 1;
          if (endpointSearchText(item.endpoint_id).includes(normalized)) return 2;
          return 3;
        };
        return score(leftLabel, left) - score(rightLabel, right)
          || leftLabel.localeCompare(rightLabel, "zh-CN");
      });
  }

  function closeEndpointOptions() {
    $("monitor-endpoint-options").classList.add("is-hidden");
    $("monitor-endpoint-query").setAttribute("aria-expanded", "false");
    $("monitor-endpoint-query").removeAttribute("aria-activedescendant");
    state.activeEndpointIndex = -1;
  }

  function updateActiveEndpoint(index) {
    const options = $("monitor-endpoint-options").querySelectorAll(".monitor-combobox-option");
    state.activeEndpointIndex = index >= 0 && index < options.length ? index : -1;
    options.forEach((option, optionIndex) => {
      option.classList.toggle("is-active", optionIndex === state.activeEndpointIndex);
    });
    if (state.activeEndpointIndex < 0) {
      $("monitor-endpoint-query").removeAttribute("aria-activedescendant");
      return;
    }
    const active = options[state.activeEndpointIndex];
    $("monitor-endpoint-query").setAttribute("aria-activedescendant", active.id);
    active.scrollIntoView({ block: "nearest" });
  }

  function renderEndpointOptions(query = $("monitor-endpoint-query").value) {
    const options = $("monitor-endpoint-options");
    state.endpointMatches = filterEndpointOptions(query);
    clear(options);
    if (!state.endpointMatches.length) {
      options.append(node("span", {
        className: "monitor-combobox-empty",
        text: state.endpointOptions.length ? "没有匹配的固定分支" : "暂无可用固定分支",
      }));
      state.activeEndpointIndex = -1;
      return;
    }
    state.endpointMatches.forEach((item, index) => {
      options.append(node("button", {
        type: "button",
        id: "monitor-endpoint-option-" + index,
        className: "monitor-combobox-option",
        role: "option",
        tabindex: "-1",
        "aria-selected": String(item.endpoint_id === state.selectedEndpointId),
        dataset: { optionIndex: String(index) },
      }, [
        node("strong", { text: item.label }),
      ]));
    });
    const selectedIndex = state.endpointMatches.findIndex(
      (item) => item.endpoint_id === state.selectedEndpointId,
    );
    updateActiveEndpoint(selectedIndex >= 0 ? selectedIndex : 0);
  }

  function openEndpointOptions() {
    const query = $("monitor-endpoint-query");
    if (query.disabled) return;
    $("monitor-endpoint-options").classList.remove("is-hidden");
    query.setAttribute("aria-expanded", "true");
  }

  function selectEndpoint(item) {
    if (!item) return;
    state.selectedEndpointId = item.endpoint_id;
    $("monitor-endpoint").value = item.endpoint_id;
    $("monitor-endpoint-query").value = item.label;
    $("monitor-endpoint-query").setAttribute("aria-invalid", "false");
    closeEndpointOptions();
  }

  function clearEndpointSelection() {
    state.selectedEndpointId = "";
    $("monitor-endpoint").value = "";
  }

  async function loadEndpoints() {
    const query = $("monitor-endpoint-query");
    query.setAttribute("aria-busy", "true");
    try {
      const response = await requestJson("/api/monitor/endpoint-options", {
        headers: { Accept: "application/json" },
      });
      if (response.body.schema_version !== "m3.monitor-endpoint-options.v1") {
        throw new Error("固定分支列表契约无效");
      }
      state.endpointOptions = response.body.items.slice();
      clearEndpointSelection();
      query.value = "";
      query.placeholder = state.endpointOptions.length ? "输入分支名称进行匹配" : "暂无可用固定分支";
      query.disabled = state.endpointOptions.length === 0;
      renderEndpointOptions("");
      closeEndpointOptions();
    } catch (error) {
      state.endpointOptions = [];
      clearEndpointSelection();
      query.value = "";
      query.placeholder = "固定分支读取失败";
      query.disabled = true;
      closeEndpointOptions();
      setAlert("monitor-create-alert", errorMessage(error));
    } finally {
      query.setAttribute("aria-busy", "false");
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
          text: "查看报告",
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
      setAlert("monitor-recent-alert", "");
    } catch (error) {
      const suffix = state.lastSuccessAt
        ? "；最后成功刷新 " + state.lastSuccessAt.toLocaleTimeString("zh-CN", { hourCycle: "h23" })
        : "";
      setAlert("monitor-recent-alert", "刷新失败，当前数据可能已过期" + suffix + "：" + errorMessage(error));
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
    const endpointId = $("monitor-endpoint").value;
    if (!endpointId || endpointId !== state.selectedEndpointId) {
      $("monitor-endpoint-query").setAttribute("aria-invalid", "true");
      setAlert("monitor-create-alert", "请从匹配结果中选择固定分支");
      $("monitor-endpoint-query").focus();
      renderEndpointOptions();
      openEndpointOptions();
      return;
    }
    const effectiveAt = shanghaiInputToUtc($("monitor-effective-at").value);
    const endValue = $("monitor-end-at").value;
    const endAt = endValue ? shanghaiInputToUtc(endValue) : null;
    if (!effectiveAt || (endValue && !endAt)) {
      setAlert("monitor-create-alert", "时间格式无效");
      return;
    }
    if (endAt && endAt <= effectiveAt) {
      setAlert("monitor-create-alert", "结束时间必须晚于生效时间");
      return;
    }
    const submit = $("monitor-create-submit");
    const status = $("monitor-create-status");
    submit.disabled = true;
    status.textContent = "正在验证分支并同步计划任务";
    setAlert("monitor-create-alert", "");
    try {
      const response = await commandLedger.send("/api/monitor/tasks", {
        method: "POST",
        schemaVersion: "m3.monitor-task-create.request.v1",
        payload: {
          name: $("monitor-name").value.trim(),
          endpoint_id: endpointId,
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
      clearEndpointSelection();
      $("monitor-endpoint-query").setAttribute("aria-invalid", "false");
      closeEndpointOptions();
      status.textContent = "";
      setAlert("monitor-create-alert", "任务已创建，调度状态：" + (STATUS_LABELS[response.body.status] || response.body.status), true);
      state.etag = "";
      await loadRecent();
    } catch (error) {
      status.textContent = "";
      setAlert("monitor-create-alert", errorMessage(error));
    } finally {
      submit.disabled = false;
    }
  }

  $("monitor-endpoint-query").addEventListener("focus", () => {
    renderEndpointOptions();
    openEndpointOptions();
  });
  $("monitor-endpoint-query").addEventListener("blur", closeEndpointOptions);
  $("monitor-endpoint-query").addEventListener("input", () => {
    clearEndpointSelection();
    $("monitor-endpoint-query").setAttribute("aria-invalid", "false");
    renderEndpointOptions();
    openEndpointOptions();
  });
  $("monitor-endpoint-query").addEventListener("keydown", (event) => {
    if (event.key === "Escape") {
      closeEndpointOptions();
      return;
    }
    if (event.key === "Enter") {
      if (
        !$("monitor-endpoint-options").classList.contains("is-hidden")
        && state.activeEndpointIndex >= 0
      ) {
        event.preventDefault();
        selectEndpoint(state.endpointMatches[state.activeEndpointIndex]);
      }
      return;
    }
    if (event.key !== "ArrowDown" && event.key !== "ArrowUp") return;
    event.preventDefault();
    if ($("monitor-endpoint-options").classList.contains("is-hidden")) {
      renderEndpointOptions();
      openEndpointOptions();
    }
    if (!state.endpointMatches.length) return;
    const direction = event.key === "ArrowDown" ? 1 : -1;
    const fallback = direction > 0 ? -1 : state.endpointMatches.length;
    const current = state.activeEndpointIndex >= 0 ? state.activeEndpointIndex : fallback;
    updateActiveEndpoint(
      (current + direction + state.endpointMatches.length) % state.endpointMatches.length,
    );
  });
  $("monitor-endpoint-options").addEventListener("mousedown", (event) => {
    if (event.target.closest(".monitor-combobox-option")) event.preventDefault();
  });
  $("monitor-endpoint-options").addEventListener("click", (event) => {
    const option = event.target.closest(".monitor-combobox-option");
    if (!option) return;
    selectEndpoint(state.endpointMatches[Number(option.dataset.optionIndex)]);
    $("monitor-endpoint-query").focus();
  });
  document.addEventListener("click", (event) => {
    if (!event.target.closest(".monitor-combobox")) closeEndpointOptions();
  });
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
