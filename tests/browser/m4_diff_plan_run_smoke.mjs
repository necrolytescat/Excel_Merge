import { spawn } from "node:child_process";
import { mkdtemp } from "node:fs/promises";
import { tmpdir } from "node:os";
import path from "node:path";

const baseUrl = process.argv[2] || "http://127.0.0.1:5566";
const runId = process.argv[3];
if (!runId) throw new Error("用法: node m4_diff_plan_run_smoke.mjs <baseUrl> <runId>");

const edge = process.env.EDGE_PATH || "C:\\Program Files (x86)\\Microsoft\\Edge\\Application\\msedge.exe";
const port = 9335;
const profile = await mkdtemp(path.join(tmpdir(), "m4-run-edge-"));
const child = spawn(edge, [
  "--headless=new",
  "--disable-gpu",
  "--no-first-run",
  `--remote-debugging-port=${port}`,
  `--user-data-dir=${profile}`,
  "about:blank",
], { stdio: "ignore" });

const sleep = (milliseconds) => new Promise((resolve) => setTimeout(resolve, milliseconds));
const assert = (condition, message) => { if (!condition) throw new Error(message); };

async function retry(action, attempts = 80) {
  let error;
  for (let index = 0; index < attempts; index += 1) {
    try { return await action(); } catch (caught) { error = caught; await sleep(250); }
  }
  throw error;
}

let socket;
let sequence = 0;
const pending = new Map();
const browserErrors = [];

function command(method, params = {}) {
  const id = ++sequence;
  socket.send(JSON.stringify({ id, method, params }));
  return new Promise((resolve, reject) => pending.set(id, { resolve, reject }));
}

async function evaluate(expression, { awaitPromise = false } = {}) {
  const response = await command("Runtime.evaluate", { expression, awaitPromise, returnByValue: true });
  if (response.exceptionDetails) throw new Error(response.exceptionDetails.text || "页面脚本执行失败");
  return response.result.value;
}

async function navigate(url, width, height) {
  await command("Emulation.setDeviceMetricsOverride", {
    width, height, deviceScaleFactor: 1, mobile: width <= 650,
  });
  await command("Page.navigate", { url });
  await retry(async () => {
    if (!await evaluate("document.readyState === 'complete'")) throw new Error("页面尚未加载完成");
    return true;
  });
}

async function waitForRunPage() {
  return retry(async () => {
    const snapshot = await evaluate(`(() => ({
      status: document.querySelector('#batch-task-status')?.textContent.trim() || '',
      progress: document.querySelector('#batch-task-progress')?.textContent.trim() || '',
      cells: document.querySelectorAll('.m4-matrix-status').length,
      tabs: document.querySelectorAll('.m4-run-tab').length,
    }))()`);
    if (snapshot.status !== "计划比对完成" || snapshot.progress !== "10 / 10 已处理" || snapshot.cells !== 10) {
      throw new Error("真实运行页尚未渲染完成");
    }
    return snapshot;
  });
}

async function layoutSnapshot() {
  return evaluate(`(() => ({
    viewport: document.documentElement.clientWidth,
    bodyWidth: document.body.scrollWidth,
    documentWidth: document.documentElement.scrollWidth,
    matrixScrollsInternally: document.querySelector('.m4-matrix-scroll')?.scrollWidth >= document.querySelector('.m4-matrix-scroll')?.clientWidth,
    overflowers: [...document.querySelectorAll('body *')]
      .filter((node) => node.scrollWidth > node.clientWidth + 1 || node.getBoundingClientRect().right > innerWidth + 1)
      .slice(0, 12)
      .map((node) => ({ tag: node.tagName, id: node.id, className: String(node.className), client: node.clientWidth, scroll: node.scrollWidth, right: Math.round(node.getBoundingClientRect().right) })),
  }))()`);
}

async function selectChangedWorkbook() {
  await evaluate(`(() => {
    const row = [...document.querySelectorAll('#m4-matrix-body tr')]
      .find((item) => item.querySelector('th')?.textContent.trim() === 'Activity.xlsm');
    const button = row?.querySelector('.m4-matrix-status.is-changed');
    if (!button) throw new Error('Activity.xlsm 差异矩阵单元格不存在');
    button.click();
  })()`);
  return retry(async () => {
    const detail = await evaluate(`(() => ({
      search: location.search,
      activeTab: document.querySelector('.m4-run-tab.is-active')?.textContent.trim() || '',
      selectedWorkbook: document.querySelector('.workbook-nav-item.is-selected strong')?.textContent.trim() || '',
      sheetCount: document.querySelector('#sheet-count')?.textContent.trim() || '',
      sheets: document.querySelectorAll('.sheet-nav-item').length,
      sourceRows: document.querySelectorAll('#diff-source-rows .diff-grid-row').length,
      targetRows: document.querySelectorAll('#diff-target-rows .diff-grid-row').length,
      fieldCells: document.querySelectorAll('.diff-grid-cell').length,
      matrixHidden: document.querySelector('#m4-matrix-panel')?.classList.contains('hidden'),
    }))()`);
    if (!detail.search.includes("workbook=Activity.xlsm") || detail.selectedWorkbook !== "Activity"
        || detail.sheets < 1 || detail.fieldCells < 1 || !detail.matrixHidden) {
      throw new Error("矩阵跳转后的 M2 明细尚未加载: " + JSON.stringify(detail));
    }
    return detail;
  }, 160);
}

try {
  const target = await retry(async () => {
    const response = await fetch(`http://127.0.0.1:${port}/json/list`);
    const targets = await response.json();
    if (!targets[0]?.webSocketDebuggerUrl) throw new Error("Edge 调试目标尚未就绪");
    return targets[0];
  });
  socket = new WebSocket(target.webSocketDebuggerUrl);
  await new Promise((resolve, reject) => {
    socket.addEventListener("open", resolve, { once: true });
    socket.addEventListener("error", reject, { once: true });
  });
  socket.addEventListener("message", (event) => {
    const message = JSON.parse(event.data);
    if (message.id) {
      const handler = pending.get(message.id);
      if (!handler) return;
      pending.delete(message.id);
      if (message.error) handler.reject(new Error(message.error.message));
      else handler.resolve(message.result);
      return;
    }
    if (message.method === "Runtime.exceptionThrown") {
      browserErrors.push(message.params.exceptionDetails.text || "uncaught exception");
    }
    if (message.method === "Log.entryAdded" && message.params.entry.level === "error") {
      browserErrors.push(message.params.entry.text);
    }
  });
  await command("Page.enable");
  await command("Runtime.enable");
  await command("Log.enable");

  const results = [];
  for (const viewport of [{ name: "desktop", width: 1440, height: 1000 }, { name: "mobile", width: 390, height: 844 }]) {
    await navigate(`${baseUrl}/diff-plan-runs/${runId}`, viewport.width, viewport.height);
    const summary = await waitForRunPage();
    const matrix = await evaluate(`(() => ({
      caption: document.querySelector('#m4-matrix-caption')?.textContent.trim(),
      changed: document.querySelectorAll('.m4-matrix-status.is-changed').length,
      semanticEqual: document.querySelectorAll('.m4-matrix-status.is-semantic_equal').length,
      identical: document.querySelectorAll('.m4-matrix-status.is-identical').length,
      cancelDisabled: document.querySelector('#cancel-batch-task')?.disabled,
      retryDisabled: document.querySelector('#retry-batch-task')?.disabled,
    }))()`);
    assert(summary.tabs === 2, `${viewport.name}: 概览和目标分支页签数量异常`);
    assert(matrix.caption === "10 张表格 × 1 个目标分支", `${viewport.name}: 矩阵摘要异常`);
    assert(matrix.changed === 6 && matrix.semanticEqual === 3 && matrix.identical === 1, `${viewport.name}: 矩阵状态分布异常`);
    assert(matrix.cancelDisabled && matrix.retryDisabled, `${viewport.name}: 终态运行操作状态异常`);
    const overviewLayout = await layoutSnapshot();
    assert(overviewLayout.documentWidth <= overviewLayout.viewport + 1, `${viewport.name}: 概览页存在页面级横向溢出 ${JSON.stringify(overviewLayout)}`);

    const detail = await selectChangedWorkbook();
    assert(detail.activeTab.includes("KR-Fix-1.0.1.0"), `${viewport.name}: 目标分支页签未激活`);
    assert(detail.sheetCount === "1 / 6", `${viewport.name}: M2 Sheet 筛选计数异常`);
    assert(detail.sourceRows > 0 && detail.targetRows > 0, `${viewport.name}: M2 双栏字段差异未渲染`);
    const branchUrl = await evaluate("location.href");
    const branchLayout = await layoutSnapshot();
    assert(branchLayout.documentWidth <= branchLayout.viewport + 1, `${viewport.name}: 分支明细存在页面级横向溢出 ${JSON.stringify(branchLayout)}`);

    await navigate(branchUrl, viewport.width, viewport.height);
    const restored = await retry(async () => {
      const state = await evaluate(`(() => ({
        selectedWorkbook: document.querySelector('.workbook-nav-item.is-selected strong')?.textContent.trim() || '',
        activeTab: document.querySelector('.m4-run-tab.is-active')?.textContent.trim() || '',
        fieldCells: document.querySelectorAll('.diff-grid-cell').length,
      }))()`);
      if (state.selectedWorkbook !== "Activity" || state.fieldCells < 1) throw new Error("URL 定位尚未恢复");
      return state;
    }, 160);
    assert(restored.activeTab.includes("KR-Fix-1.0.1.0"), `${viewport.name}: 刷新后目标分支未恢复`);
    results.push({ viewport: viewport.name, matrix, detail, restored, overviewLayout, branchLayout });
  }

  assert(browserErrors.length === 0, `浏览器控制台存在异常: ${browserErrors.join(" | ")}`);
  console.log(JSON.stringify({ ok: true, runId, results }, null, 2));
} finally {
  if (socket?.readyState === WebSocket.OPEN) socket.close();
  child.kill();
}
