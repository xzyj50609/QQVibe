const assert = require("node:assert/strict");
const { readFileSync } = require("node:fs");
const path = require("node:path");
const { it } = require("node:test");
const vm = require("node:vm");
const { seed } = require("./helpers/view-state-harness.cjs");

const root = path.join(__dirname, "..");
const script = readFileSync(path.join(root, "chatui/app.js"), "utf8");
const html = readFileSync(path.join(root, "chatui/index.html"), "utf8");
function section(start, end) {
  const first = script.indexOf(start);
  const last = script.indexOf(end, first);
  assert.ok(first >= 0 && last > first, `Missing section: ${start}`);
  return script.slice(first, last);
}
const settingsCode = section("async function api(", "function status(") +
  section("const MODEL_SOURCE_PROTOCOLS", "let managedAccounts = [];") +
  "globalThis.ui = { showModelSource, loadModelSource, fetchApiModels, testApiModel, " +
  "activateModelSource, clearStoredApiKey, invalidateModelDiscovery, invalidateModelTest, " +
  "syncRuntimeControl, syncSavedApiKeyHint, getSnapshot: () => settingsState.modelSourceSnapshot, " +
  "markDirty: () => { settingsState.modelSourceDraftDirty = true; } };";

function makeNode() {
  return {
    value: "", textContent: "", hidden: false, disabled: false, options: [], dataset: {},
    replaceChildren(...children) { this.options = children; },
    appendChild(child) { this.options.push(child); },
  };
}
function harness(fetchImpl) {
  const nodes = new Map();
  const cardClasses = new Set();
  const byId = id => {
    if (!nodes.has(id)) nodes.set(id, makeNode());
    return nodes.get(id);
  };
  const card = { classList: { toggle(name, enabled) {
    if (enabled) cardClasses.add(name);
    else cardClasses.delete(name);
  } } };
  byId("settingsModal").querySelector = () => card;
  const context = vm.createContext({
    URL,
    AbortController,
    setTimeout,
    clearTimeout,
    document: { createElement: () => makeNode() },
    byId,
    text: (id, value) => { byId(id).textContent = value == null ? "" : String(value); },
    settings: { intent: true },
    syncPortraitMode() {},
    cancelApiPortraitPoll() {},
    clearApiPortraitView() {},
    loadProfile() {},
    clearInlineIntentPending() {},
    setIntentActionState() {},
    refreshLabels() {},
    submitManualRecent() {},
    fetch: fetchImpl,
    localStorage: { setItem() { throw new Error("provider settings must not use browser storage"); } },
  });
  seed(context, {
    runtimeSnapshot: { requestedProvider: "gpu" }, runtimeBusy: false,
    currentAccount: null, currentUser: null, view: "chat", controller: null, generation: 0,
    apiPortraitSnapshot: null, incrementalFailed: false, recentFailed: false,
    localModelResolved: false, localModelReady: false,
  });
  vm.runInContext(settingsCode, context);
  return { ui: context.ui, byId, cardClasses, context };
}
const response = body => ({ ok: true, json: async () => body });
const localState = { mode: "local", api: null, sourceId: "local", status: "ready" };
const apiState = { mode: "api", api: { protocol: "responses", baseUrl: "https://example.test/v1", model: "model-b", contextTokens: 128000, hasKey: true }, sourceId: "api", status: "ready" };

it("shows both deployment paths and keeps the key in a password field", () => {
  for (const id of ["selectModelSource", "localModelSettings", "apiModelSettings", "btnFetchApiModels", "apiModelCount",
    "selectApiModel", "inputApiModelId", "inputApiContextTokens", "btnTestApiModel", "btnActivateApi", "btnClearApiKey"])
    assert.match(html, new RegExp(`id="${id}"`));
  assert.match(html, /本地模型<\/span>[\s\S]*?>Laya<\/span>/);
  assert.match(html, /id="inputApiKey" type="password"/);
  for (const protocol of ["anthropic", "responses", "chat_completions", "gemini", "ollama"])
    assert.match(html, new RegExp(`value="${protocol}"`));
});

it("loads the active source and disables local device changes while API is active", async () => {
  const { ui, byId, cardClasses } = harness(async () => response(apiState));
  await ui.loadModelSource();
  assert.equal(byId("selectModelSource").value, "api");
  assert.equal(byId("modelSourceActive").textContent, "当前 API");
  assert.equal(byId("inputApiKey").value, "");
  assert.equal(byId("inputApiContextTokens").value, 128000);
  assert.equal(byId("apiKeySaved").hidden, false);
  assert.equal(byId("selectRuntimeProvider").disabled, true);
  assert.equal(byId("localModelSettings").hidden, true);
  assert.equal(cardClasses.has("api-source-open"), true);
});

it("only offers reuse of a saved key for its original protocol and Base URL", () => {
  const { ui, byId } = harness(async () => response(localState));
  ui.showModelSource(apiState);
  assert.equal(byId("apiKeySaved").hidden, false);
  byId("inputApiBaseUrl").value = "https://different.test/v1";
  ui.syncSavedApiKeyHint();
  assert.equal(byId("apiKeySaved").hidden, true);
  assert.equal(byId("btnClearApiKey").hidden, false);
  assert.equal(byId("inputApiKey").placeholder, "按服务要求填写 API Key");
});

it("refreshes active status without overwriting a draft after service recovery", async () => {
  const { ui, byId } = harness(async () => response(localState));
  ui.showModelSource(apiState);
  byId("inputApiModelId").value = "unsaved-model";
  ui.markDirty();
  await ui.loadModelSource(true);
  assert.equal(ui.getSnapshot().mode, "local");
  assert.equal(byId("modelSourceActive").textContent, "当前本地");
  assert.equal(byId("selectModelSource").value, "api");
  assert.equal(byId("inputApiModelId").value, "unsaved-model");
});

it("fetches a model list, allows manual IDs, and resets stale discovery on configuration edit", async () => {
  const requests = [];
  const { ui, byId } = harness(async (url, options) => {
    requests.push({ url, options });
    return response({ supported: true, models: [{ id: "model-a", name: "Model A" },
      { id: "model-b", contextTokens: 65536 }] });
  });
  ui.showModelSource(localState);
  byId("selectApiProtocol").value = "responses";
  byId("inputApiBaseUrl").value = "https://example.test/v1";
  byId("inputApiKey").value = "SYNTHETIC_KEY";
  byId("inputApiModelId").value = "model-b";
  await ui.fetchApiModels();
  assert.deepEqual(JSON.parse(requests[0].options.body), {
    protocol: "responses", baseUrl: "https://example.test/v1", apiKey: "SYNTHETIC_KEY",
  });
  assert.equal(byId("apiModelCount").textContent, "2 个模型可用");
  assert.equal(byId("selectApiModel").options.length, 3);
  assert.equal(byId("selectApiModel").disabled, false);
  assert.equal(byId("inputApiContextTokens").value, "65536");
  byId("apiModelTestStatus").textContent = "连接成功";
  ui.invalidateModelDiscovery();
  assert.equal(byId("apiModelCount").textContent, "");
  assert.equal(byId("apiModelTestStatus").textContent, "");
  assert.equal(byId("selectApiModel").options.length, 1);
});

it("ignores a late model list after the user edits its connection settings", async () => {
  let resolveFetch;
  const { ui, byId } = harness(() => new Promise(resolve => { resolveFetch = resolve; }));
  ui.showModelSource(localState);
  byId("inputApiBaseUrl").value = "https://example.test/v1";
  const pending = ui.fetchApiModels();
  ui.invalidateModelDiscovery();
  resolveFetch(response({ supported: true, models: [{ id: "obsolete" }] }));
  await pending;
  assert.equal(byId("apiModelCount").textContent, "");
  assert.equal(byId("selectApiModel").options.length, 1);
});

it("cancels discovery when connection settings change", async () => {
  let cancelled = false;
  const { ui, byId } = harness((_url, options) => new Promise((_resolve, reject) => {
    options.signal.addEventListener("abort", () => {
      cancelled = true;
      reject(Object.assign(new Error("aborted"), { name: "AbortError" }));
    }, { once: true });
  }));
  ui.showModelSource(localState);
  byId("inputApiBaseUrl").value = "https://example.test/v1";
  const pending = ui.fetchApiModels();
  ui.invalidateModelDiscovery();
  await pending;
  assert.equal(cancelled, true);
  assert.equal(byId("apiModelCount").textContent, "");
});

it("uses a manual model when listing is unsupported, tests it, and activates only on success", async () => {
  const requests = [];
  const { ui, byId } = harness(async (url, options) => {
    requests.push({ url, body: options?.body ? JSON.parse(options.body) : null });
    if (url.endsWith("/list")) return response({ supported: false, models: [] });
    if (url.endsWith("/test")) return response({ ok: true, latencyMs: 37.6 });
    if (url.endsWith("/activate")) return response(apiState);
    throw new Error("unexpected fetch");
  });
  ui.showModelSource(localState);
  byId("selectModelSource").value = "api";
  byId("selectApiProtocol").value = "responses";
  byId("inputApiBaseUrl").value = "https://example.test/v1";
  byId("inputApiModelId").value = "model-b";
  byId("inputApiContextTokens").value = "128000";
  byId("inputApiKey").value = "SYNTHETIC_KEY";
  await ui.fetchApiModels();
  assert.equal(byId("apiModelCount").textContent, "此接口不提供模型列表，可手动填写模型 ID");
  await ui.testApiModel();
  assert.equal(byId("apiModelTestStatus").textContent, "连接成功 · 38 ms");
  await ui.activateModelSource("api");
  assert.equal(requests[1].body.model, "model-b");
  assert.equal(requests[2].body.mode, "api");
  assert.equal(requests[2].body.contextTokens, 128000);
  assert.equal(byId("inputApiKey").value, "");
  assert.equal(ui.getSnapshot().mode, "api");
});

it("keeps the previous active mode when activation fails", async () => {
  const { ui, byId } = harness(async () => ({ ok: false, status: 401 }));
  ui.showModelSource(localState);
  byId("selectModelSource").value = "api";
  byId("inputApiBaseUrl").value = "https://example.test/v1";
  byId("inputApiModelId").value = "model-b";
  byId("inputApiContextTokens").value = "4096";
  await ui.activateModelSource("api");
  assert.equal(ui.getSnapshot().mode, "local");
  assert.match(byId("modelSourceStatus").textContent, /启用失败（HTTP 401），当前仍为本地/);
  assert.equal(byId("inputApiModelId").value, "model-b");
});

it("requires a bounded context size before sending API activation", async () => {
  const requests = [];
  const { ui, byId } = harness(async (url, options) => {
    requests.push({ url, body: JSON.parse(options.body) });
    return response(apiState);
  });
  ui.showModelSource(localState);
  byId("selectApiProtocol").value = "responses";
  byId("inputApiBaseUrl").value = "https://example.test/v1";
  byId("inputApiModelId").value = "model-b";
  for (const value of ["", "4095", "1000001", "8192.5"]) {
    byId("inputApiContextTokens").value = value;
    await ui.activateModelSource("api");
  }
  assert.equal(requests.length, 0);
  byId("inputApiContextTokens").value = "4096";
  await ui.activateModelSource("api");
  assert.equal(requests.length, 1);
  assert.equal(requests[0].body.contextTokens, 4096);
});

it("clears the saved key and refreshes the active source from the server", async () => {
  const requests = [];
  const { ui, byId } = harness(async (url, options) => {
    requests.push(url);
    return response(url.endsWith("/clear-key") ? { ok: true } : localState);
  });
  ui.showModelSource(apiState);
  await ui.clearStoredApiKey();
  assert.deepEqual(requests, ["/api/model-source/clear-key", "/api/model-source"]);
  assert.equal(byId("apiKeySaved").hidden, true);
  assert.equal(byId("inputApiKey").value, "");
  assert.equal(ui.getSnapshot().mode, "local");
});

for (const protocol of ["anthropic", "responses", "chat_completions", "gemini", "ollama"]) {
  it(`${protocol}: settings carry the selected endpoint, model, key and context through activation`, async () => {
    const requests = [];
    const active = { ...apiState, api: { ...apiState.api, protocol } };
    const { ui, byId } = harness(async (url, options) => {
      requests.push({ url, body: JSON.parse(options.body) });
      if (url.endsWith("/list")) return response({ supported: true,
        models: [{ id: "model-b", contextTokens: 65536 }] });
      if (url.endsWith("/test")) return response({ ok: true, latencyMs: 20 });
      return response(active);
    });
    ui.showModelSource(localState);
    byId("selectApiProtocol").value = protocol;
    byId("inputApiBaseUrl").value = "https://synthetic.test/v1";
    byId("inputApiModelId").value = "model-b";
    byId("inputApiKey").value = "SYNTHETIC_KEY";
    await ui.fetchApiModels(); await ui.testApiModel(); await ui.activateModelSource("api");
    assert.equal(requests.length, 3);
    for (const request of requests) {
      assert.equal(request.body.protocol, protocol);
      assert.equal(request.body.baseUrl, "https://synthetic.test/v1");
      assert.equal(request.body.apiKey, "SYNTHETIC_KEY");
    }
    assert.equal(requests[2].body.model, "model-b");
    assert.equal(requests[2].body.contextTokens, 65536);
    assert.equal(ui.getSnapshot().api.protocol, protocol);
    assert.equal(byId("inputApiKey").value, "");
  });
}
