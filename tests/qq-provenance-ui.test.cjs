const { test } = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const vm = require("node:vm");
const path = require("node:path");
const source = fs.readFileSync(path.join(__dirname, "../chatui/qq-provenance-ui.js"), "utf8");
const settle = () => new Promise(resolve => setImmediate(resolve));
class Element {
  constructor(tag) { this.tag = tag; this.children = []; this.events = {}; this.open = false; this.attributes = {}; }
  appendChild(node) { this.children.push(node); return node; }
  replaceChildren(...nodes) { this.children = nodes; }
  addEventListener(name, handler) { this.events[name] = handler; }
  setAttribute(name, value) { this.attributes[name] = value; }
  set innerHTML(_) { throw new Error("must render source data as text"); }
  get text() { return [this.textContent, ...this.children.map(node => node.text)].filter(Boolean).join(" "); }
}
function harness(api, options = {}) {
  let scope = { account: "a:synthetic", user: "u:synthetic", generation: 1 };
  const document = { createElement: tag => new Element(tag) };
  const timers = [], cleared = [];
  const window = { QQSyncUI: { message: () => "安全错误提示" }, AbortController,
    setTimeout: (callback, ms) => { timers.push({ callback, ms }); return timers.length; }, clearTimeout: id => cleared.push(id) };
  vm.runInNewContext(source, { window, Date, Number, Array, URLSearchParams });
  const details = window.QQProvenanceUI.create({ document, api, getScope: () => ({ ...scope }), ...options });
  const byClass = name => details.children.find(node => node.className === name);
  return { details, byClass, timers, cleared, setScope: value => { scope = value; },
    open: async () => { details.open = true; details.events.toggle(); await settle(); } };
}
const entry = extra => ({ runId: 1, kind: "forward", format: "qce-api", state: "complete",
  completedAtMs: 1700000000000, interfaceWindow: [0, 1000], recordRange: [0, 1000], acceptedRows: 2,
  rejectedRows: 0, counts: { inserted: 1, unchanged: 1 }, sourceVersion: "6.3.0", ...extra });
const response = (entries = [], extra = {}) => ({ account: "a:synthetic", user: "u:synthetic", entries, nextBefore: null, ...extra });

test("message provenance is lazy and uses only its scoped history cursor", async () => {
  const calls = [], f = harness(async url => { calls.push(url); return response([entry()]); }, { cursor: "synthetic-cursor" });
  assert.equal(calls.length, 0); await f.open();
  const url = new URL(calls[0], "http://synthetic.invalid");
  assert.equal(url.pathname, "/api/qq/message-provenance"); assert.equal(url.searchParams.get("cursor"), "synthetic-cursor");
  assert.equal(url.searchParams.get("account"), "a:synthetic"); assert.equal(url.searchParams.get("limit"), "10");
  assert.match(f.details.text, /前向同步.*QCE 接口.*批次 1/); assert.match(f.details.text, /不证明 QQ 全历史完整/);
});
test("legacy message origins stay explicitly unknown", async () => {
  const f = harness(async () => response(), { cursor: "synthetic" }); await f.open();
  assert.match(f.details.text, /没有保存具体来源.*不会倒推补造/);
});
test("pagination appends earlier complete batches without replacing newer records", async () => {
  const calls = [], f = harness(async url => { calls.push(url); return response([entry({ runId: calls.length === 1 ? 12 : 9 })], { nextBefore: calls.length === 1 ? 10 : null }); });
  await f.open(); assert.equal(f.byClass("qq-ingest-more").hidden, false);
  f.byClass("qq-ingest-more").events.click(); await settle();
  assert.match(calls[1], /before=10/); assert.equal(f.byClass("qq-ingest-records").children.length, 2);
  assert.equal(f.byClass("qq-ingest-more").hidden, true);
});
test("filter changes discard late responses from the old filter", async () => {
  let finish; const calls = [];
  const f = harness(url => { calls.push(url); return calls.length === 1 ? new Promise(resolve => { finish = resolve; }) : Promise.resolve(response([entry({ runId: 2 })])); }, { filter: true });
  await f.open(); const select = f.byClass("qq-ingest-kind"); select.value = "forward"; select.events.change(); await settle();
  finish(response([entry({ runId: 999 })])); await settle();
  assert.match(calls[1], /kind=forward/); assert.doesNotMatch(f.details.text, /999/); assert.match(f.details.text, /批次 2/);
});
test("account switch or A to B to A generation change suppresses late provenance", async () => {
  let finish; const f = harness(() => new Promise(resolve => { finish = resolve; }));
  await f.open(); f.setScope({ account: "a:synthetic", user: "u:synthetic", generation: 3 });
  finish(response([entry()])); await settle(); assert.doesNotMatch(f.details.text, /批次 1/);
});
test("mismatching account responses and private exceptions are not displayed", async () => {
  const f = harness(async () => response([entry()], { account: "a:other" })); await f.open();
  assert.match(f.details.text, /暂不可用/); assert.doesNotMatch(f.details.text, /批次/);
  const g = harness(async () => { throw new Error("PRIVATE_PATH_TOKEN_BODY"); }); await g.open();
  assert.match(g.details.text, /暂不可用/); assert.doesNotMatch(g.details.text, /PRIVATE/);
});
test("source fingerprints and version metadata remain text, never HTML", async () => {
  const payload = "<img onerror=alert(1)>";
  const f = harness(async () => response([entry({ kind: "file-import", format: "qce-single-json", sourceSnapshot: payload,
    sourceVersion: payload, observations: [{ normalizeVersion: payload, disposition: "unchanged", fingerprint: payload }], observationCount: 8 })]));
  await f.open(); assert.match(f.details.text, /不是原文件字节校验/); assert.match(f.details.text, /重复观察/);
  assert.match(f.details.text, /仅展示前 5/); assert.match(f.details.text, /<img/);
});
test("partial and error records never use success wording", async () => {
  const f = harness(async () => response([entry({ state: "partial", reason: "page-repeated" }), entry({ state: "error" })]));
  await f.open(); assert.match(f.details.text, /部分读取/); assert.match(f.details.text, /读取失败/);
  assert.doesNotMatch(f.details.text, /窗口读取完成/);
});
test("refresh replaces the previous page and requests no old before cursor", async () => {
  const calls = [], f = harness(async url => { calls.push(url); return response([entry({ runId: calls.length })], { nextBefore: 10 }); });
  await f.open(); f.byClass("qq-ingest-refresh").events.click(); await settle();
  assert.equal(f.byClass("qq-ingest-records").children.length, 1); assert.doesNotMatch(calls[1], /before=/);
});
test("loading prevents double page dispatch and no account makes no request", async () => {
  let finish, calls = 0; const f = harness(() => { calls++; return new Promise(resolve => { finish = resolve; }); });
  await f.open(); f.byClass("qq-ingest-more").events.click(); f.byClass("qq-ingest-refresh").events.click(); assert.equal(calls, 1);
  finish(response()); await settle();
  const g = harness(() => { throw new Error("must not query"); }); g.setScope({ account: null, user: null, generation: 2 }); await g.open();
});

test("a stalled request aborts at 15 seconds and re-enables retry", async () => {
  let signal;
  const f = harness((url, options, provided) => new Promise((resolve, reject) => {
    signal = provided; signal.addEventListener("abort", () => reject(new Error("synthetic abort")));
  }));
  await f.open(); assert.equal(f.timers[0].ms, 15000); f.timers[0].callback(); await settle();
  assert.equal(signal.aborted, true); assert.match(f.details.text, /超时.*重试/);
  assert.equal(f.byClass("qq-ingest-refresh").disabled, false); assert.equal(f.cleared.length, 1);
});
test("closing provenance cancels its pending request and suppresses its late response", async () => {
  let signal, finish;
  const f = harness((url, options, provided) => { signal = provided; return new Promise(resolve => { finish = resolve; }); });
  await f.open(); f.details.open = false; f.details.events.toggle(); assert.equal(signal.aborted, true);
  finish(response([entry()])); await settle(); assert.doesNotMatch(f.details.text, /批次 1/);
});
