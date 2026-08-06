(() => {
  if (document.body.dataset.replayMode !== "true") return;

  const bridge = globalThis.ExcelDiffResultsBridge;
  const batchRuntime = globalThis.ExcelDiffBatchRuntime;
  if (!bridge || !batchRuntime) return;

  const state = bridge.state;
  const $ = (id) => document.getElementById(id);
  const fileInput = $("offline-fixture-file");
  const loadButton = $("load-offline-fixture");
  const recomputeButton = $("recompute-offline-fixture");
  const modeSwitch = $("offline-mode-switch");
  let session = null;
  let busy = false;

  function errorMessage(error) {
    const payload = error?.error || error;
    return (payload?.code || "FIXTURE_REQUEST_FAILED") + "："
      + (payload?.message || "离线夹具请求失败");
  }

  async function requestJson(path, options = {}) {
    const response = await fetch(path, options);
    const body = await response.json().catch(() => ({}));
    if (!response.ok) throw body;
    return body;
  }

  function setError(message = "") {
    $("offline-fixture-error").textContent = message;
    $("offline-fixture-error").classList.toggle("hidden", !message);
  }

  function setBusy(next, status) {
    busy = next;
    fileInput.disabled = next;
    loadButton.disabled = next || !fileInput.files?.length;
    recomputeButton.disabled = next || !session;
    modeSwitch.disabled = next || !session;
    if (status) $("offline-fixture-status").textContent = status;
  }

  function contextFor(currentSession, mode) {
    const task = currentSession.task;
    return {
      version: 3,
      mode: "replay",
      replayResultMode: mode,
      replayComparisons: currentSession.current.comparisons || {},
      source: {
        endpointId: task.source.endpoint_id,
        label: task.source.endpoint_id,
        branch: task.source.endpoint_id,
        resolvedRevision: task.source.revision,
      },
      target: {
        endpointId: task.target.endpoint_id,
        label: task.target.endpoint_id,
        branch: task.target.endpoint_id,
        resolvedRevision: task.target.revision,
      },
      candidates: [],
      fixtureId: currentSession.fixture.fixture_id,
    };
  }

  function renderSession(currentSession, { reset = false, mode = null } = {}) {
    const selectedPath = reset ? "" : state.selectedPath;
    const selectedMode = mode || state.context?.replayResultMode || "golden";
    if (reset) state.results = new Map();
    state.context = contextFor(currentSession, selectedMode);
    state.selectedPath = selectedPath;
    modeSwitch.querySelectorAll("input").forEach((input) => {
      input.checked = input.value === selectedMode;
    });
    batchRuntime.syncTask(currentSession.task);
    $("batch-task-heading").textContent = "夹具工作簿结果";
    $("batch-task-status").textContent = selectedMode === "golden"
      ? "黄金结果"
      : "当前代码重算";
    const fixture = currentSession.fixture;
    const current = currentSession.current;
    $("offline-fixture-sha").textContent = fixture.archive_sha256;
    $("offline-fixture-status").textContent = "已加载 · " + fixture.fixture_id;
    $("offline-fixture-counts").textContent =
      fixture.input_file_count + " 个输入 · "
      + fixture.missing_file_count + " 个缺失 · "
      + current.available_count + "/" + fixture.golden_result_count + " 已重算 · "
      + current.matched_count + " 一致 · "
      + current.mismatched_count + " 不一致";
    modeSwitch.disabled = false;
    recomputeButton.disabled = false;
  }

  async function loadSelectedFixture() {
    const file = fileInput.files?.[0];
    if (!file || busy) return;
    setError();
    setBusy(true, "正在校验夹具");
    try {
      session = await requestJson("/api/replay/fixture", {
        method: "POST",
        headers: { "Content-Type": "application/octet-stream" },
        body: file,
      });
      renderSession(session, { reset: true, mode: "golden" });
    } catch (error) {
      setError(errorMessage(error));
      $("offline-fixture-status").textContent = "夹具加载失败";
    } finally {
      setBusy(false);
    }
  }

  async function recomputeAll() {
    if (!session || busy) return;
    setError();
    setBusy(true, "正在使用当前代码重算全部工作簿");
    $("result-action-message").textContent = "离线重算进行中";
    try {
      session = await requestJson("/api/replay/recompute", { method: "POST" });
      renderSession(session, { mode: "current" });
      $("result-action-message").textContent = session.current.mismatched_count
        ? session.current.mismatched_count + " 个工作簿与黄金结果不一致"
        : "当前代码重算结果与黄金结果全部一致";
    } catch (error) {
      setError(errorMessage(error));
      $("offline-fixture-status").textContent = "离线重算失败";
    } finally {
      setBusy(false);
    }
  }

  async function recomputeItem(result) {
    if (!session || busy || !result?.itemId) return;
    setError();
    setBusy(true, "正在重算 " + bridge.fileName(result.candidate.path));
    bridge.setDiffState("diff_loading", "正在从离线夹具读取原始 Excel/CSV。");
    try {
      session = await requestJson(
        "/api/replay/recompute/" + encodeURIComponent(result.itemId),
        { method: "POST" },
      );
      renderSession(session, { mode: "current" });
      const comparison = session.current.comparisons[result.itemId];
      $("result-action-message").textContent = comparison?.matches_golden
        ? bridge.fileName(result.candidate.path) + " 与黄金结果一致"
        : bridge.fileName(result.candidate.path) + " 与黄金结果不一致";
    } catch (error) {
      setError(errorMessage(error));
      $("offline-fixture-status").textContent = "单工作簿重算失败";
    } finally {
      setBusy(false);
    }
  }

  async function restoreLoadedFixture() {
    try {
      session = await requestJson("/api/replay/fixture");
      renderSession(session, { reset: true, mode: "golden" });
    } catch (error) {
      if ((error?.error || error)?.code !== "FIXTURE_NOT_LOADED") {
        setError(errorMessage(error));
      }
    }
  }

  fileInput.addEventListener("change", () => {
    const file = fileInput.files?.[0];
    $("offline-fixture-file-label").textContent = file?.name || "选择 .m2fixture";
    loadButton.disabled = busy || !file;
  });
  loadButton.addEventListener("click", loadSelectedFixture);
  recomputeButton.addEventListener("click", recomputeAll);
  modeSwitch.addEventListener("change", (event) => {
    if (!session || busy || event.target.name !== "offline-result-mode") return;
    renderSession(session, { mode: event.target.value });
  });

  globalThis.OfflineFixtureRuntime = Object.freeze({ recomputeItem });
  void restoreLoadedFixture();
})();
