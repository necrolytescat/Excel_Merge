from __future__ import annotations

from pathlib import Path
import subprocess
import textwrap

from fastapi.testclient import TestClient

from app.main import create_app
from core.svn_provider import MockSVNProvider


ROOT = Path(__file__).resolve().parents[2]


def test_monitor_pages_and_static_assets_are_served():
    app = create_app(
        config={"svn": {"provider": "mock"}},
        provider=MockSVNProvider(),
    )
    with TestClient(app) as client:
        overview = client.get("/monitor")
        tasks = client.get("/monitor/tasks")
        styles = client.get("/static/monitor.css")
        request_script = client.get("/static/monitor_request.js")
        overview_script = client.get("/static/monitor.js")
        task_script = client.get("/static/monitor_tasks.js")

    assert overview.status_code == 200
    assert tasks.status_code == 200
    assert styles.status_code == 200
    assert request_script.status_code == 200
    assert overview_script.status_code == 200
    assert task_script.status_code == 200
    assert "monitor.css?v=1.0.0" in overview.text
    assert "monitor.js?v=1.0.1" in overview.text
    assert "monitor_request.js?v=1.0.0" in overview.text
    assert "monitor_tasks.js?v=1.0.0" in tasks.text
    assert 'id="monitor-create-form"' in overview.text
    assert 'id="monitor-create-alert"' in overview.text
    assert 'id="monitor-recent-alert"' in overview.text
    assert 'id="monitor-alert"' not in overview.text
    assert 'id="monitor-task-filter"' in tasks.text
    assert 'id="monitor-detail-dialog"' in tasks.text


def test_monitor_navigation_keeps_version_comparison_before_monitoring():
    for template_name in (
        "index.html",
        "compare.html",
        "compare_results.html",
        "history_tasks.html",
        "monitor.html",
        "monitor_tasks.html",
    ):
        text = (ROOT / "app" / "templates" / template_name).read_text(
            encoding="utf-8"
        )
        assert 'href="/compare"' in text
        assert 'href="/monitor"' in text
        assert text.index('href="/compare"') < text.index('href="/monitor"')
        assert "版本监控" in text


def test_monitor_scripts_keep_url_etag_and_safe_dom_contracts():
    overview_script = (ROOT / "app" / "static" / "monitor.js").read_text(
        encoding="utf-8"
    )
    task_script = (ROOT / "app" / "static" / "monitor_tasks.js").read_text(
        encoding="utf-8"
    )
    styles = (ROOT / "app" / "static" / "monitor.css").read_text(
        encoding="utf-8"
    )

    for script in (overview_script, task_script):
        assert "innerHTML" not in script
        assert "insertAdjacentHTML" not in script
        assert "textContent" in script
        assert "If-None-Match" in script
        assert "document.hidden" in script

    assert "URLSearchParams" in task_script
    assert "AbortController" in task_script
    assert "requestGeneration" in task_script
    assert "刷新失败，当前数据可能已过期" in task_script
    assert "listUrl(cursor, append ? 50 : 30)" in task_script
    assert "Math.max(30" not in task_script
    assert "reportExpired" in task_script
    assert "MonitorTaskRefreshPolicy.shouldPauseAutomaticRefresh" in task_script
    assert "已加载多页，为保留当前列表已暂停自动刷新" in task_script
    assert 'statusBadge("expired")' in task_script
    assert 'expired: "已过期"' in task_script
    assert "crypto.randomUUID" not in overview_script
    assert "crypto.randomUUID" not in task_script
    assert 'text: "查看报告"' in overview_script
    assert "text: STATUS_LABELS[task.latest_report.status]" not in overview_script
    assert 'params.set("q"' in task_script
    assert 'params.set("status"' in task_script
    assert 'params.set("task"' in task_script
    assert '"/pause"' not in task_script
    for route in (
        "scheduler-sync",
        "/retry",
        "/latest-report",
        "/monitor/reports/",
    ):
        assert route in task_script
    for status in (
        "active",
        "scheduler_error",
        "paused",
        "ended",
        "archived",
        "failed",
        "partial",
    ):
        assert status in task_script

    assert "@media (max-width: 760px)" in styles
    assert "overflow-x: auto" in styles
    assert ":focus-visible" in styles
    assert "overflow-wrap: anywhere" in styles
    assert '.monitor-status[data-status="expired"]' in styles

    recent_loader = overview_script.split(
        "async function loadRecent", 1
    )[1].split("async function createTask", 1)[0]
    create_handler = overview_script.split("async function createTask", 1)[1]
    assert 'setAlert("monitor-recent-alert", "")' in recent_loader
    assert "monitor-create-alert" not in recent_loader
    assert "monitor-create-alert" in create_handler
    assert "monitor-recent-alert" not in create_handler


def test_monitor_request_id_is_reused_after_unknown_network_result():
    script = textwrap.dedent(
        """
        const assert = require("assert");
        const {
          MonitorRequestLedger,
          shouldPauseAutomaticRefresh,
        } = require("./app/static/monitor_request.js");
        assert.strictEqual(shouldPauseAutomaticRefresh(true, 30), false);
        assert.strictEqual(shouldPauseAutomaticRefresh(true, 31), true);
        assert.strictEqual(shouldPauseAutomaticRefresh(true, 130), true);
        assert.strictEqual(shouldPauseAutomaticRefresh(false, 130), false);
        const requestBodies = [];
        let callCount = 0;
        let uuidCount = 0;
        const ledger = new MonitorRequestLedger({
          uuidFactory: () => "00000000-0000-4000-8000-" + String(++uuidCount).padStart(12, "0"),
          fetchImpl: async (url, options) => {
            callCount += 1;
            requestBodies.push(JSON.parse(options.body));
            if (callCount === 1) throw new Error("network disconnected");
            return {
              ok: true,
              status: 200,
              headers: { get: () => '\"etag\"' },
              json: async () => ({ schema_version: "m3.monitor-task.v1" }),
            };
          },
        });
        const options = {
          method: "PATCH",
          schemaVersion: "m3.monitor-task-patch.request.v1",
          payload: { daily_trigger_time: "18:00:00", end_at: null },
        };
        (async () => {
          await assert.rejects(() => ledger.send("/api/monitor/tasks/one", options));
          await ledger.send("/api/monitor/tasks/one", options);
          assert.strictEqual(requestBodies[0].request_id, requestBodies[1].request_id);
          await ledger.send("/api/monitor/tasks/one", options);
          assert.notStrictEqual(requestBodies[1].request_id, requestBodies[2].request_id);

          const parseBodies = [];
          let parseCalls = 0;
          let parseUuidCount = 0;
          const parseLedger = new MonitorRequestLedger({
            uuidFactory: () => "10000000-0000-4000-8000-" + String(++parseUuidCount).padStart(12, "0"),
            fetchImpl: async (url, requestOptions) => {
              parseCalls += 1;
              parseBodies.push(JSON.parse(requestOptions.body));
              return {
                ok: true,
                status: 201,
                headers: { get: () => '\"etag\"' },
                json: async () => {
                  if (parseCalls === 1) throw new Error("truncated response body");
                  return { schema_version: "m3.monitor-task.v1" };
                },
              };
            },
          });
          await assert.rejects(
            () => parseLedger.send("/api/monitor/tasks", {
              method: "POST",
              schemaVersion: "m3.monitor-task-create.request.v1",
              payload: { name: "QA", endpoint_id: "KR", effective_at: "2026-08-10T10:00:00Z", end_at: null, daily_trigger_time: "18:00:00" },
            }),
            /响应体无法确认/,
          );
          await parseLedger.send("/api/monitor/tasks", {
            method: "POST",
            schemaVersion: "m3.monitor-task-create.request.v1",
            payload: { name: "QA", endpoint_id: "KR", effective_at: "2026-08-10T10:00:00Z", end_at: null, daily_trigger_time: "18:00:00" },
          });
          assert.strictEqual(parseBodies[0].request_id, parseBodies[1].request_id);
        })().catch((error) => { console.error(error); process.exit(1); });
        """
    )
    completed = subprocess.run(
        ["node", "-e", script],
        cwd=ROOT,
        capture_output=True,
        text=True,
        timeout=20,
        check=False,
    )
    assert completed.returncode == 0, completed.stderr
