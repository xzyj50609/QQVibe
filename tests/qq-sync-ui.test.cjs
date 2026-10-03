const { test } = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");
const source = fs.readFileSync(path.join(__dirname, "../chatui/qq-sync-ui.js"), "utf8");
class Element {
  constructor(tag) { this.tag = tag; this.value = ""; this.children = []; this.events = {}; this.open = false; }
  appendChild(node) { this.children.push(node); return node; }
  replaceChildren(...nodes) { this.children = nodes; }
  addEventListener(name, handler) { this.events[name] = handler; }
  get text() { return [this.textContent, ...this.children.map(child => child.text)].filter(Boolean).join(" "); }
}
const settle = () => new Promise(done => setImmediate(done));
const state = extra => ({ state: "disabled", liveValidated: true, baseUrl: "http://127.0.0.1:12345",
  ownerUin: "10001", tokenConfigured: true, enabled: false, conversations: {}, account: "a:chosen", ...extra });
function harness(api, options = {}) {
  const nodes = new Map();
  const document = { createElement: tag => new Element(tag), getElementById(id) {
    if (!nodes.has(id)) nodes.set(id, new Element("div"));
    return nodes.get(id);
  } };
  const window = {};
  vm.runInNewContext(source, { window, Date, Object, Number });
  const ui = window.QQSyncUI.mount({ document, api, ...options });
  const node = id => document.getElementById(id);
  return { ui, node, click: async id => { await node(id).events.click(); await settle(); } };
}

test("quick-add delegates contact and group to the existing connection operations", async () => {
  const posts = []; let refreshes = 0;
  const view = harness(async (url, options) => {
    if (options) posts.push([url, JSON.parse(options.body)]);
    return state({ enabled: true, state: "online" });
  }, { onChange: async () => { refreshes++; } });
  await settle();
  assert.equal((await view.ui.addConversation("contact", "12345678")).ok, true);
  assert.equal((await view.ui.addConversation("group", "87654321")).ok, true);
  assert.deepEqual(posts, [
    ["/api/qq/connection/add-contact", { peerUin: "12345678", name: "" }],
    ["/api/qq/connection/add-group", { groupCode: "87654321" }],
  ]);
  assert.equal(refreshes, 2);
});

test("quick-add rejects disconnected and malformed requests without posting", async () => {
  let posts = 0;
  const view = harness(async (_url, options) => { if (options) posts++; return state(); });
  await settle();
  assert.equal((await view.ui.addConversation("contact", "12345678")).ok, false);
  for (const [kind, number] of [["group", "x"], ["contact", "123"], ["unknown", "12345678"]])
    assert.equal((await view.ui.addConversation(kind, number)).ok, false);
  assert.equal(posts, 0);
});

test("quick-add propagates safe errors and blocks a duplicate in-flight request", async () => {
  let finish, posts = 0;
  const pending = new Promise((_resolve, reject) => { finish = reject; });
  const view = harness(async (_url, options) => options ? (posts++, pending) : state({ enabled: true }));
  await settle();
  const first = view.ui.addConversation("group", "12345678");
  assert.equal((await view.ui.addConversation("group", "12345678")).ok, false);
  finish(Object.assign(new Error("must not expose server details"), { code: "qq-offline" }));
  const result = await first;
  assert.equal(result.ok, false);
  assert.match(result.error, /离线/);
  assert.doesNotMatch(result.error, /server details/);
  assert.equal(posts, 1);
});
test("pending live validation keeps connect and contact reads disabled", async () => {
  const calls = [], view = harness(async url => { calls.push(url); return state({ liveValidated: false, state: "unavailable", reason: "connector-awaiting-validation" }); });
  await settle();
  assert.equal(view.node("btnConnectQQ").disabled, true);
  assert.equal(view.node("btnReadQQContacts").disabled, true);
  assert.match(view.node("qqSyncGate").text, /尚未完成/);
  assert.deepEqual(calls, ["/api/qq/connection"]);
});
test("saving a synthetic token clears the input and never renders it", async () => {
  const calls = [], view = harness(async (url, options) => {
    if (options) calls.push([url, JSON.parse(options.body)]);
    return state();
  });
  await settle();
  view.node("qqSyncToken").value = "FAKE_TOKEN_NOT_A_SECRET";
  await view.click("btnSaveQQConnection");
  assert.equal(calls[0][1].token, "FAKE_TOKEN_NOT_A_SECRET");
  assert.equal(view.node("qqSyncToken").value, "");
  assert.doesNotMatch(view.node("qqSyncStatus").text, /FAKE_TOKEN/);
});
test("standard connection is explicit and sends no token or contact selection", async () => {
  const calls = [], view = harness(async (url, options) => {
    calls.push([url, options?.body]);
    return state(options ? { enabled: true, state: "online", ownerUin: "10002" } : {});
  });
  await settle();
  assert.deepEqual(calls, [["/api/qq/connection", undefined]]);
  await view.click("btnConnectStandardQQ");
  assert.deepEqual(calls[1], ["/api/qq/connection/connect-standard", "{}"]);
  assert.equal(view.node("qqSyncOwner").value, "10002");
  assert.equal(view.node("qqSyncToken").value, "");
  assert.equal(view.node("btnReadQQContacts").disabled, false);
});
test("empty token preserves the saved bearer rather than sending an empty replacement", async () => {
  let body;
  const view = harness(async (_url, options) => { if (options) body = JSON.parse(options.body); return state(); });
  await settle();
  await view.click("btnSaveQQConnection");
  assert.equal(Object.hasOwn(body, "token"), false);
});
test("double submit is suppressed while a connection request is pending", async () => {
  let complete, requests = 0;
  const pending = new Promise(done => { complete = done; });
  const view = harness(async (_url, options) => options ? (requests++, pending) : state());
  await settle();
  await view.click("btnConnectQQ");
  await view.click("btnConnectQQ");
  assert.equal(requests, 1);
  complete(state({ enabled: true, state: "online" }));
  await settle();
  assert.equal(view.node("btnDisconnectQQ").disabled, false);
});
test("window text uses contact names and limits completeness to the interface window", async () => {
  const view = harness(async () => state({ enabled: true, state: "online", conversations: {
    "u:hidden-internal": { name: "合成好友", state: "complete", windowStartMs: 1000, windowEndMs: 2000 },
  } }));
  await settle();
  assert.match(view.node("qqSyncWindows").text, /合成好友.*本次接口窗口已读完/);
  assert.doesNotMatch(view.node("qqSyncWindows").text, /hidden-internal|全部历史/);
});

test("last successful sync is visible and explicitly limited to the current run", async () => {
  let lastSuccess = 1700000000000;
  const view = harness(async () => state({ lastSuccessAtMs: lastSuccess }));
  await settle();
  assert.match(view.node("qqSyncLastSuccess").text, /本次运行最近成功同步/);
  assert.doesNotMatch(view.node("qqSyncLastSuccess").text, /尚无|全部历史/);
  for (const value of [null, 0, "1700000000000", -1, 9000000000000000]) {
    lastSuccess = value;
    await view.ui.load();
    assert.match(view.node("qqSyncLastSuccess").text, /尚无成功同步记录/);
  }
});
test("retry is scoped to the configured account and disabled after an account switch", async () => {
  const data = state({ enabled: true, state: "partial", conversations: {
    "u:chosen": { name: "合成好友", state: "partial", retryAvailable: true, attempts: 3 },
  } });
  const calls = [], view = harness(async (_url, options) => { if (options) calls.push(JSON.parse(options.body)); return data; },
    { getAccount: () => "a:chosen" });
  await settle();
  const button = view.node("qqSyncWindows").children.find(child => child.tag === "button");
  assert.equal(button.disabled, false);
  await button.events.click(); await settle();
  assert.deepEqual(calls[0], { account: "a:chosen", user: "u:chosen" });
  const switched = harness(async () => data, { getAccount: () => "a:other" });
  await settle();
  assert.equal(switched.node("qqSyncWindows").children.find(child => child.tag === "button").disabled, true);
});
test("contact addition sends only the explicitly chosen uin and refreshes status", async () => {
  const calls = [], view = harness(async (url, options) => {
    calls.push([url, options?.body && JSON.parse(options.body)]);
    if (url.startsWith("/api/qq/contacts")) return { contacts: [{ uin: "20001", name: "<script>合成名</script>" }], mayHaveMore: false };
    return state({ enabled: true, state: "online" });
  });
  await settle();
  await view.click("btnReadQQContacts");
  const row = view.node("qqSyncContacts").children[0];
  assert.match(row.text, /<script>合成名<\/script>/);
  await row.children.find(child => child.tag === "button").events.click(); await settle();
  assert.deepEqual(calls.find(([url]) => url.endsWith("/add-contact"))[1], { peerUin: "20001", name: "<script>合成名</script>" });
  assert.equal(view.node("btnConnectQQ").disabled, false);
});
test("backend errors never echo credentials or arbitrary server text into status", async () => {
  const view = harness(async (_url, options) => {
    if (options) throw { code: "FAKE_TOKEN_NOT_A_SECRET and private path" };
    return state();
  });
  await settle();
  await view.click("btnConnectQQ");
  assert.doesNotMatch(view.node("qqSyncStatus").text, /FAKE_TOKEN|private/);
});

test("group addition requires an explicit chosen number and refreshes selected sessions", async () => {
  const calls = [], changes = [];
  const view = harness(async (url, options) => {
    calls.push([url, options?.body && JSON.parse(options.body)]);
    return state({ enabled: true, state: "online" });
  }, { onChange: data => changes.push(data) });
  await settle();
  assert.deepEqual(calls.map(row => row[0]), ["/api/qq/connection"]);
  await view.click("btnAddQQGroup");
  assert.equal(calls.length, 1);
  view.node("qqSyncGroupCode").value = " 20001 ";
  await view.click("btnAddQQGroup");
  assert.deepEqual(calls[1], ["/api/qq/connection/add-group", { groupCode: "20001" }]);
  assert.equal(calls[2][0], "/api/qq/connection");
  assert.equal(changes.length, 1);
  assert.equal(view.node("btnAddQQGroup").disabled, false);
});

test("history and reconciliation retain separate scopes and never claim all QQ history", async () => {
  const view = harness(async () => state({ enabled: true, state: "online", conversations: {
    "u:chosen": { name: "合成好友", state: "complete", windowStartMs: 9000, windowEndMs: 10000,
      history: { state: "split", window: [1000, 5000], coverageStartMs: 5000, reason: "window-split" },
      reconcile: { state: "complete", window: null, lastWindow: [8000, 10000] },
    },
  } }));
  await settle();
  const text = view.node("qqSyncWindows").text;
  assert.match(text, /本次接口窗口已读完.*旧历史补读.*尚有未读完区间.*已补读至.*缩小窗口.*近期迟到消息核对.*本轮接口区间扫描完成.*本轮范围/);
  assert.doesNotMatch(text, /全部历史|连接操作未完成/);
});

test("parked history retry sends its kind without resetting the forward window", async () => {
  const calls = [], data = state({ enabled: true, state: "online", conversations: {
    "u:chosen": { name: "合成好友", state: "complete", history: {
      state: "partial", window: [0, 1000], attempts: 3, retryAvailable: true,
    } },
  } });
  const view = harness(async (_url, options) => { if (options) calls.push(JSON.parse(options.body)); return data; },
    { getAccount: () => "a:chosen" });
  await settle();
  const button = view.node("qqSyncWindows").children.find(child => child.tag === "button");
  assert.match(button.text, /重试旧历史补读/);
  await button.events.click(); await settle();
  assert.deepEqual(calls[0], { account: "a:chosen", user: "u:chosen", kind: "history" });
  const switched = harness(async () => data, { getAccount: () => "a:other" });
  await settle();
  assert.equal(switched.node("qqSyncWindows").children.find(child => child.tag === "button").disabled, true);
});
