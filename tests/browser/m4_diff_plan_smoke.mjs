import { spawn } from "node:child_process";
import { mkdtemp } from "node:fs/promises";
import { tmpdir } from "node:os";
import path from "node:path";

const baseUrl = process.argv[2] || "http://127.0.0.1:5566";
const edge = process.env.EDGE_PATH || "C:\\Program Files (x86)\\Microsoft\\Edge\\Application\\msedge.exe";
const port = 9334;
const profile = await mkdtemp(path.join(tmpdir(), "m4-edge-"));
const child = spawn(edge, [
  "--headless=new",
  "--disable-gpu",
  "--no-first-run",
  `--remote-debugging-port=${port}`,
  `--user-data-dir=${profile}`,
  "about:blank",
], { stdio: "ignore" });

const sleep = (milliseconds) => new Promise((resolve) => setTimeout(resolve, milliseconds));

async function retry(action, attempts = 40) {
  let error;
  for (let index = 0; index < attempts; index += 1) {
    try {
      return await action();
    } catch (caught) {
      error = caught;
      await sleep(250);
    }
  }
  throw error;
}

function assert(condition, message) {
  if (!condition) throw new Error(message);
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
  const response = await command("Runtime.evaluate", {
    expression,
    awaitPromise,
    returnByValue: true,
  });
  if (response.exceptionDetails) {
    throw new Error(response.exceptionDetails.text || "页面脚本执行失败");
  }
  return response.result.value;
}

async function navigate(url, width, height) {
  await command("Emulation.setDeviceMetricsOverride", {
    width,
    height,
    deviceScaleFactor: 1,
    mobile: width <= 650,
  });
  await command("Page.navigate", { url });
  await retry(async () => {
    const ready = await evaluate("document.readyState === 'complete'");
    if (!ready) throw new Error("页面尚未加载完成");
    return ready;
  });
  await sleep(500);
}

async function layoutSnapshot() {
  return evaluate(`(() => {
    const active = document.querySelector('.sidebar-nav a[aria-current="page"]');
    const labels = [...document.querySelectorAll('.sidebar-nav .nav-item')]
      .map((item) => item.textContent.replace(/\\s+/g, ' ').trim());
    const clipped = [...document.querySelectorAll('body *')]
      .filter((node) => {
        const style = getComputedStyle(node);
        if (style.position === 'fixed' || style.position === 'absolute') return false;
        return node.scrollWidth > node.clientWidth + 1 && style.overflowX === 'visible';
      })
      .slice(0, 8)
      .map((node) => ({ tag: node.tagName, className: node.className, client: node.clientWidth, scroll: node.scrollWidth }));
    return {
      title: document.title,
      active: active?.textContent.replace(/\\s+/g, ' ').trim() || '',
      activeClass: active?.className || '',
      labels,
      viewport: document.documentElement.clientWidth,
      bodyWidth: document.body.scrollWidth,
      documentWidth: document.documentElement.scrollWidth,
      clipped,
    };
  })()`);
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
    if (message.method === "Log.entryAdded" && ["error", "warning"].includes(message.params.entry.level)) {
      browserErrors.push(message.params.entry.text);
    }
  });
  await command("Page.enable");
  await command("Runtime.enable");
  await command("Log.enable");

  const results = [];
  for (const viewport of [{ name: "desktop", width: 1440, height: 1000 }, { name: "mobile", width: 390, height: 844 }]) {
    await navigate(`${baseUrl}/diff-plans`, viewport.width, viewport.height);
    const list = await layoutSnapshot();
    assert(list.active.includes("表格计划对比") && list.active.includes("M4") && list.activeClass.includes("active"), `${viewport.name}: M4 导航未高亮`);
    assert(list.labels.findIndex((item) => item.includes("M2")) < list.labels.findIndex((item) => item.includes("M4")), `${viewport.name}: M4 未位于 M2 之后`);
    assert(list.labels.findIndex((item) => item.includes("M4")) < list.labels.findIndex((item) => item.includes("M3")), `${viewport.name}: M4 未位于 M3 之前`);
    assert(list.documentWidth <= list.viewport + 1, `${viewport.name}: 计划列表存在横向溢出`);

    await navigate(`${baseUrl}/diff-plans/new`, viewport.width, viewport.height);
    await retry(async () => {
      const enabled = await evaluate("!document.querySelector('#source-endpoint').disabled");
      if (!enabled) throw new Error("分支列表尚未加载");
      return enabled;
    });
    const endpointCount = await evaluate("document.querySelector('#source-endpoint').options.length - 1");
    assert(endpointCount >= 2, `${viewport.name}: 可用分支不足，无法验证多分支选择`);
    await evaluate(`(() => {
      const source = document.querySelector('#source-endpoint');
      source.value = source.options[1].value;
      source.dispatchEvent(new Event('change', { bubbles: true }));
    })()`);
    await retry(async () => {
      const loaded = await evaluate("!document.querySelector('#workbook-query').disabled && document.querySelectorAll('[data-workbook-choice]').length > 0");
      if (!loaded) throw new Error("TABLE 工作簿清单尚未加载");
      return loaded;
    }, 120);
    const interaction = await evaluate(`(() => {
      const workbook = document.querySelector('[data-workbook-choice] input');
      const target = document.querySelector('[data-target-choice] input');
      workbook.click();
      target.click();
      document.querySelector('.diff-plan-revision-settings').open = true;
      return {
        workbookCount: document.querySelector('#workbook-selection-count').textContent.trim(),
        targetCount: document.querySelector('#target-selection-count').textContent.trim(),
        revisionInputs: document.querySelectorAll('#revision-grid input').length,
        saveText: document.querySelector('#save-plan').textContent.trim(),
        runText: document.querySelector('#save-and-run').textContent.trim(),
      };
    })()`);
    assert(interaction.workbookCount === "1 / 10", `${viewport.name}: 表格勾选计数异常`);
    assert(interaction.targetCount === "1 / 4", `${viewport.name}: 目标分支计数异常`);
    assert(interaction.revisionInputs === 2, `${viewport.name}: Revision 输入未覆盖基准和目标分支`);
    assert(interaction.saveText === "仅保存" && interaction.runText === "保存并开始", `${viewport.name}: 主次操作文案异常`);
    const form = await layoutSnapshot();
    assert(form.documentWidth <= form.viewport + 1, `${viewport.name}: 新建页存在横向溢出`);
    results.push({ viewport: viewport.name, list, form, interaction, endpointCount });
  }

  await navigate(`${baseUrl}/diff-plans/new`, 1440, 1000);
  await retry(async () => {
    const enabled = await evaluate("!document.querySelector('#source-endpoint').disabled");
    if (!enabled) throw new Error("生命周期检查：分支列表尚未加载");
    return enabled;
  });
  await evaluate(`(() => {
    const source = document.querySelector('#source-endpoint');
    source.value = source.options[1].value;
    source.dispatchEvent(new Event('change', { bubbles: true }));
  })()`);
  await retry(async () => {
    const loaded = await evaluate("!document.querySelector('#workbook-query').disabled && document.querySelectorAll('[data-workbook-choice]').length > 0");
    if (!loaded) throw new Error("生命周期检查：TABLE 工作簿清单尚未加载");
    return loaded;
  }, 120);
  await evaluate(`(() => {
    document.querySelector('#plan-name').value = 'M4 浏览器隔离验收计划';
    document.querySelector('[data-workbook-choice] input').click();
    document.querySelector('[data-target-choice] input').click();
    document.querySelector('#save-plan').click();
  })()`);
  await retry(async () => {
    const pathname = await evaluate("location.pathname");
    if (!/^\/diff-plans\/[0-9a-f-]{36}$/.test(pathname)) throw new Error("生命周期检查：计划尚未保存");
    return pathname;
  }, 120);
  const planId = await evaluate("location.pathname.split('/').pop()");
  await retry(async () => {
    const ready = await evaluate("document.querySelector('#detail-plan-name').textContent === 'M4 浏览器隔离验收计划'");
    if (!ready) throw new Error("生命周期检查：计划详情尚未加载");
    return ready;
  });

  await navigate(`${baseUrl}/diff-plans/${planId}/edit`, 1440, 1000);
  await retry(async () => {
    const ready = await evaluate("document.querySelector('#plan-name').value === 'M4 浏览器隔离验收计划' && !document.querySelector('#workbook-query').disabled");
    if (!ready) throw new Error("生命周期检查：编辑页尚未加载");
    return ready;
  }, 120);
  await evaluate(`(() => {
    document.querySelector('#plan-name').value = 'M4 浏览器隔离验收计划（已编辑）';
    document.querySelector('#save-plan').click();
  })()`);
  await retry(async () => {
    const name = await evaluate("document.querySelector('#detail-plan-name')?.textContent");
    if (name !== "M4 浏览器隔离验收计划（已编辑）") throw new Error("生命周期检查：编辑结果尚未保存");
    return name;
  }, 120);
  await evaluate("document.querySelector('#archive-plan').click()");
  await retry(async () => {
    const text = await evaluate("document.querySelector('#archive-plan').textContent");
    if (text !== "恢复计划") throw new Error("生命周期检查：计划尚未归档");
    return text;
  });
  await evaluate("document.querySelector('#archive-plan').click()");
  await retry(async () => {
    const text = await evaluate("document.querySelector('#archive-plan').textContent");
    if (text !== "归档计划") throw new Error("生命周期检查：计划尚未恢复");
    return text;
  });
  results.push({ lifecycle: "create-edit-archive-restore", planId, ok: true });

  assert(browserErrors.length === 0, `浏览器控制台存在异常: ${browserErrors.join(" | ")}`);
  console.log(JSON.stringify({ ok: true, results }, null, 2));
} finally {
  if (socket?.readyState === WebSocket.OPEN) socket.close();
  child.kill();
}
