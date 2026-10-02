const { test } = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const vm = require("node:vm");
const path = require("node:path");
const source = fs.readFileSync(path.join(__dirname, "../chatui/qq-support-ui.js"), "utf8");
const settle = () => new Promise(resolve => setImmediate(resolve));
class Element {
  constructor(tag) { this.tag = tag; this.children = []; this.events = {}; this.open = false; this.hidden = true; }
  replaceChildren(...nodes) { this.children = nodes; }
  appendChild(node) { this.children.push(node); return node; }
  addEventListener(name, handler) { this.events[name] = handler; }
  set innerHTML(_) { throw new Error("scope data must never become HTML"); }
  click() { this.clicked = true; this.events.click?.(); }
  remove() { this.removed = true; }
  get text() { return [this.textContent, ...this.children.map(node => node.text)].filter(Boolean).join(" "); }
}
function harness(api, options = {}) {
  const nodes = new Map(), created = [], blobs = [], revoked = [], timers = [];
  const get = id => { if (!nodes.has(id)) nodes.set(id, new Element("div")); return nodes.get(id); };
  const document = { getElementById: get, body: new Element("body"), createElement(tag) {
    const item = new Element(tag); created.push(item); return item;
  } };
  let scope = { account: "a:synthetic", user: "u:synthetic-peer", generation: 1 };
  const window = { QQSyncUI: { message: () => "安全错误提示" },
    Blob: class { constructor(parts, options) { this.parts = parts; this.options = options; blobs.push(this); } },
    URL: { createObjectURL: () => "blob:synthetic-only", revokeObjectURL: value => revoked.push(value) },
    setTimeout: (callback, ms) => { timers.push({ callback, ms }); } };
  vm.runInNewContext(source, { window, Date, Number, Array, URLSearchParams });
  const ui = window.QQSupportUI.mount({ document, api, getScope: () => ({ ...scope }), ...options });
  return { ui, get, created, blobs, revoked, timers, setScope: value => { scope = value; } };
}
const data = extra => ({ account: "a:synthetic", user: "u:synthetic-peer", dataRevision: 3,
  counts: { messages: 100, validTexts: 80, peerValidTexts: 40 }, range: { startMs: 1700000000000, endMs: 1700000001000 },
  normalizeVersions: [{ version: "qq-v3", messages: 100 }], forward: null, ...extra });

test("scope loads on demand and shows own counts, version and unknown completeness", async () => {
  const calls = [], f = harness(async url => { calls.push(url); return data(); });
  assert.equal(calls.length, 0);
  await f.ui.load();
  const url = new URL(calls[0], "http://synthetic.invalid");
  assert.equal(url.pathname, "/api/qq/data-scope");
  assert.equal(url.searchParams.get("account"), "a:synthetic");
  assert.equal(url.searchParams.get("user"), "u:synthetic-peer");
  assert.match(f.get("qqDataScopeRows").text, /100 条.*80 条.*40 条/);
  assert.match(f.get("qqDataScopeRows").text, /qq-v3/);
  assert.match(f.get("qqDataScopeRows").text, /不能证明 QQ 全历史完整/);
  assert.match(f.get("qqDataScopeRows").text, /前向接口扫描：暂无保存记录/);
});

test("pending history ranges and persisted scan time remain distinct from local message range", async () => {
  const f = harness(async () => data({ forward: { state: "complete", windowStartMs: 1000, windowEndMs: 2000, scannedThroughMs: 2000 },
    history: { state: "partial", anchorEndMs: 2000, lastCompletedAtMs: 1700000000000,
               stack: [[0, 1000]], window: [1000, 2000], reason: "page-repeated" } }));
  await f.ui.load();
  assert.match(f.get("qqDataScopeRows").text, /本轮窗口已扫描/);
  assert.match(f.get("qqDataScopeRows").text, /尚有未读完区间/);
  assert.equal(f.get("qqDataScopeRows").children.filter(node => node.text.startsWith("旧历史补读待扫描")).length, 2);
  assert.match(f.get("qqDataScopeRows").text, /接口起点（0）/);
  assert.match(f.get("qqDataScopeRows").text, /最近成功扫描时间/);
});

test("version and failure payloads are rendered as text without HTML interpretation", async () => {
  const f = harness(async () => data({ normalizeVersions: [{ version: "<img src=x onerror=alert(1)>", messages: 100 }] }));
  await f.ui.load();
  assert.match(f.get("qqDataScopeRows").text, /<img src=x/);
});

test("switching scope invalidates a late response and clears the old summary", async () => {
  let finish;
  const f = harness(() => new Promise(resolve => { finish = resolve; }));
  const pending = f.ui.load();
  f.setScope({ account: "a:other", user: "u:other", generation: 2 });
  f.ui.scopeChanged(); finish(data()); await pending;
  assert.doesNotMatch(f.get("qqDataScopeRows").text, /100 条|qq-v3/);
  assert.match(f.get("qqDataScopeRows").text, /单聊已变化/);
});

test("a mismatching account response cannot be displayed", async () => {
  const f = harness(async () => data({ account: "a:wrong" }));
  await f.ui.load();
  assert.match(f.get("qqDataScopeRows").text, /暂不可用/);
  assert.doesNotMatch(f.get("qqDataScopeRows").text, /100 条/);
});

test("no selected local conversation makes no scope request", async () => {
  const f = harness(() => { throw new Error("must not fetch"); });
  f.setScope({ account: null, user: null, generation: 2 });
  await f.ui.load();
  assert.match(f.get("qqDataScopeRows").text, /请先选择/);
});

test("diagnostics use a JSON Blob and fixed filename, then revoke its temporary URL", async () => {
  const calls = [], report = { schema: "qq-diagnostics-v1", software: { product: "QQVibe" } };
  const f = harness(async url => { calls.push(url); return report; });
  await f.ui.exportDiagnostic();
  assert.deepEqual(calls, ["/api/qq/diagnostics"]);
  assert.equal(f.blobs.length, 1);
  assert.deepEqual(JSON.parse(f.blobs[0].parts[0]), report);
  assert.equal(f.blobs[0].options.type, "application/json;charset=utf-8");
  const link = f.created.find(item => item.tag === "a");
  assert.equal(link.download, "QQVibe-diagnostics.json");
  assert.equal(link.href, "blob:synthetic-only"); assert.equal(link.clicked, true); assert.equal(link.removed, true);
  assert.match(f.get("qqDiagnosticsStatus").text, /已发起.*下载/);
  f.timers[0].callback(); assert.deepEqual(f.revoked, ["blob:synthetic-only"]);
});

test("duplicate exports are suppressed and controls recover after failure", async () => {
  let finish, calls = 0;
  const f = harness(() => { calls++; return new Promise(resolve => { finish = resolve; }); });
  const pending = f.ui.exportDiagnostic(); await f.ui.exportDiagnostic();
  assert.equal(calls, 1); assert.equal(f.get("btnExportQQDiagnostics").disabled, true);
  finish({ schema: "wrong", error: "SYNTHETIC_PRIVATE_ERROR" }); await pending;
  assert.equal(f.get("btnExportQQDiagnostics").disabled, false);
  assert.match(f.get("qqDiagnosticsStatus").text, /未完成/);
  assert.doesNotMatch(f.get("qqDiagnosticsStatus").text, /SYNTHETIC_PRIVATE_ERROR/);
  assert.equal(f.blobs.length, 0);
});
