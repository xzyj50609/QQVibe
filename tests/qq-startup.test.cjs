const { test } = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");
const code = fs.readFileSync(path.join(__dirname, "../chatui/qq-startup.js"), "utf8");
class Element {
  constructor(tag) { this.tag = tag; this.children = []; this.events = {}; this.hidden = false; }
  appendChild(node) { this.children.push(node); return node; }
  replaceChildren(...nodes) { this.children = nodes; }
  addEventListener(event, callback) { this.events[event] = callback; }
  querySelectorAll(tag) { return this.children.filter(child => child.tag === tag); }
  get text() { return [this.textContent, ...this.children.map(child => child.text)].filter(Boolean).join(" "); }
}
function harness(key = "qq") {
  const nodes = new Map();
  const document = { createElement: tag => new Element(tag), getElementById(id) {
    if (!nodes.has(id)) nodes.set(id, new Element("div"));
    return nodes.get(id);
  } };
  let reloads = 0;
  const window = { ProductConfig: { key }, location: { reload() { reloads++; } } };
  vm.runInNewContext(code, { window, document });
  return { ui: window.QQStartup, nodes, reloads: () => reloads };
}
const account = (suffix, extra = {}) => ({ platform: "qq", accountId: "account-" + suffix,
  displayId: "1000" + suffix, ...extra });
test("QQ empty startup explains offline state and makes no connection request", async () => {
  const view = harness();
  const requests = [];
  await view.ui.show(async url => { requests.push(url); return { platform: "qq", accounts: [] }; });
  assert.deepEqual(requests, ["/api/accounts"]);
  assert.match(view.nodes.get("qqStartupAccounts").text, /还没有已保存/);
  assert.match(view.nodes.get("qqConnectionStatus").text, /自动同步尚未启用/);
});
test("QQ saved-account choice opens exactly the selected library and prevents double submit", async () => {
  const view = harness();
  const calls = [];
  let resolve;
  const pending = new Promise(done => { resolve = done; });
  const api = async (url, options) => {
    calls.push([url, options?.body]);
    return options ? pending : { platform: "qq", accounts: [account("1"), account("2")] };
  };
  await view.ui.show(api);
  const buttons = view.nodes.get("qqStartupAccounts").querySelectorAll("button");
  const click = buttons[1].events.click();
  await buttons[0].events.click();
  resolve({ accountId: "account-2", messagesReady: true });
  await click;
  assert.equal(view.reloads(), 1);
  assert.deepEqual(calls, [["/api/accounts", undefined], ["/api/accounts/activate", '{"accountId":"account-2"}']]);
});
test("pending cleanup is disabled and names are rendered as text", async () => {
  const view = harness();
  await view.ui.show(async () => ({ platform: "qq", accounts: [account("1", {
    deletionPending: true, nickname: "<img src=x onerror=alert(1)>" })] }));
  const button = view.nodes.get("qqStartupAccounts").children.find(child => child.tag === "button");
  assert.equal(button.disabled, true);
  assert.match(button.textContent, /<img/);
  assert.equal(button.children.length, 0);
});
test("activation failure remains retryable without falsely reloading", async () => {
  const view = harness();
  const api = async (_url, options) => {
    if (options) throw new Error("unavailable");
    return { platform: "qq", accounts: [account("1")] };
  };
  await view.ui.show(api);
  const list = view.nodes.get("qqStartupAccounts");
  await list.querySelectorAll("button")[0].events.click();
  assert.match(list.text, /打开失败/);
  assert.equal(view.reloads(), 0);
  await list.querySelectorAll("button").at(-1).events.click();
  await new Promise(resolve => setImmediate(resolve));
  assert.equal(list.querySelectorAll("button")[0].disabled, false);
});
test("late account responses cannot resurrect a picker after readiness", async () => {
  const view = harness();
  let resolve;
  const request = view.ui.show(() => new Promise(done => { resolve = done; }));
  view.ui.ready();
  resolve({ platform: "qq", accounts: [account("1")] });
  await request;
  assert.equal(view.nodes.get("qqStartupAccounts").hidden, true);
  assert.equal(view.nodes.get("qqStartupAccounts").children.length, 0);
  assert.match(view.nodes.get("qqConnectionStatus").text, /本地记录可用/);
});
test("WeChat product does not install or run the QQ account chooser", () => {
  const view = harness("wechat");
  assert.equal(view.ui, undefined);
  assert.equal(view.nodes.size, 0);
});
test("readiness preserves verified online sync status instead of replacing it with offline", () => {
  const view = harness();
  view.ui.setStatus({ localReady: true, connection: "online", sync: { enabled: true, state: "online" } });
  view.ui.ready();
  assert.match(view.nodes.get("qqConnectionStatus").text, /QQ 已连接/);
  assert.doesNotMatch(view.nodes.get("qqConnectionStatus").text, /尚未启用|当前离线/);
  view.ui.setStatus({ localReady: true, sync: { enabled: true, state: "partial" } });
  assert.match(view.nodes.get("qqConnectionStatus").text, /未读完范围/);
  view.ui.setStatus({ localReady: true, sync: { enabled: false, state: "paused" } });
  assert.match(view.nodes.get("qqConnectionStatus").text, /已暂停/);
});
