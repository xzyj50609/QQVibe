const { test } = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");
const source = fs.readFileSync(path.join(__dirname, "../chatui/qq-import-ui.js"), "utf8");
class Element {
  constructor(tag) { this.tag = tag; this.children = []; this.events = {}; this.value = ""; this.checked = false; }
  appendChild(node) { this.children.push(node); return node; }
  replaceChildren(...nodes) { this.children = nodes; }
  addEventListener(name, handler) { this.events[name] = handler; }
  get firstChild() { return this.children[0]; }
  get text() { return [this.textContent, ...this.children.map(child => child.text)].filter(Boolean).join(" "); }
}
const settle = () => new Promise(done => setImmediate(done));
function harness(api, options = {}) {
  const nodes = new Map(), scheduled = new Map();
  const document = { createElement: tag => new Element(tag), getElementById(id) {
    if (!nodes.has(id)) nodes.set(id, new Element("div"));
    return nodes.get(id);
  } };
  const window = {};
  vm.runInNewContext(source, { window, Date, Set });
  let serial = 0;
  const timers = { setTimeout(fn) { const id = ++serial; scheduled.set(id, fn); return id; },
    clearTimeout(id) { scheduled.delete(id); } };
  const view = window.QQImportUI.mount({ document, api, timers, ...options });
  const node = id => document.getElementById(id);
  return { view, node, ui: window.QQImportUI,
    click: async id => { await node(id).events.click(); await settle(); },
    poll: async () => { const entry = scheduled.entries().next().value; if (entry) { scheduled.delete(entry[0]); await entry[1](); await settle(); } },
    timers: scheduled };
}
function ready(extra = {}) {
  return { jobId: "synth", state: "ready", previewToken: "token", preview: {
    ownerUin: "10001", peerUin: "10002", peerUid: "u_peer", name: "<script>不执行</script>",
    counts: { rowsTotal: 2, rowsOk: 2, rowsRejected: 0, rowsConflict: 0, unknownTypes: 0 },
    directions: { self: 1, peer: 1 }, duplicatesInFile: 0, uniqueMessages: 2,
    samples: [{ side: "self", text: "合成文本" }], range: [1700000000000, 1700000001000], ...extra } };
}
test("preview is explicit and binds confirmation to the returned token", async () => {
  const calls = [], view = harness(async (url, options) => { calls.push([url, JSON.parse(options.body)]); return ready(); });
  assert.equal(view.node("btnChooseQQExport").hidden, true);
  await view.click("btnPreviewQQExport");
  assert.equal(calls.length, 0);
  view.node("qqImportPath").value = "D:/synthetic.json";
  await view.click("btnPreviewQQExport");
  assert.equal(calls.length, 1);
  assert.match(view.node("qqImportPreview").text, /<script>不执行<\/script>/);
  assert.equal(view.node("btnCommitQQImport").disabled, false);
  await view.click("btnCommitQQImport");
  assert.deepEqual(calls[1], ["/api/qq/import/commit", { jobId: "synth", previewToken: "token", acceptPartial: false }]);
});
test("partial import stays disabled until the rejection choice is checked", async () => {
  const data = ready({ counts: { rowsTotal: 3, rowsOk: 2, rowsRejected: 1, rowsConflict: 0 },
    rejected: [{ row: 3, reason: "invalid-body" }] });
  const calls = [], view = harness(async (url, options) => { calls.push([url, JSON.parse(options.body)]); return data; });
  view.node("qqImportPath").value = "D:/synthetic.json";
  await view.click("btnPreviewQQExport");
  assert.equal(view.node("btnCommitQQImport").disabled, true);
  assert.match(view.node("qqImportPreview").text, /被拒绝的行：3/);
  view.node("qqImportAcceptPartial").checked = true;
  view.node("qqImportAcceptPartial").events.change();
  assert.equal(view.node("btnCommitQQImport").disabled, false);
  await view.click("btnCommitQQImport");
  assert.equal(calls[1][1].acceptPartial, true);
});
test("busy preview cannot submit twice", async () => {
  let done;
  const pending = new Promise(resolve => { done = resolve; });
  let calls = 0;
  const view = harness(async () => { calls++; return pending; });
  view.node("qqImportPath").value = "D:/synthetic.json";
  await view.click("btnPreviewQQExport");
  await view.click("btnPreviewQQExport");
  assert.equal(calls, 1);
  done(ready());
  await settle();
  assert.equal(view.node("btnPreviewQQExport").disabled, false);
});
test("cancelled preview rejects a late in-flight status response", async () => {
  let finishPoll;
  const pending = new Promise(resolve => { finishPoll = resolve; });
  const view = harness(async url => url.endsWith("/preview") ? { jobId: "synth", state: "reading" } :
    url.endsWith("/cancel") ? { jobId: "synth", state: "cancelled" } : pending);
  view.node("qqImportPath").value = "D:/synthetic.json";
  await view.click("btnPreviewQQExport");
  const polling = view.poll();
  await settle();
  await view.click("btnCancelQQImport");
  finishPoll(ready());
  await polling;
  assert.equal(view.view.getCurrent().state, "cancelled");
  assert.equal(view.node("qqImportPreview").hidden, true);
});
test("completion refreshes data and opens only the returned account", async () => {
  const result = { accountId: "chosen", inserted: 2, unchanged: 0, conflicts: 0, backfilled: 0 };
  let completed, opened;
  const view = harness(async url => url.endsWith("/preview") ? ready() : { jobId: "synth", state: "complete", result },
    { onComplete: item => { completed = item; }, openAccount: item => { opened = item; } });
  view.node("qqImportPath").value = "D:/synthetic.json";
  await view.click("btnPreviewQQExport");
  await view.click("btnCommitQQImport");
  assert.equal(completed.accountId, "chosen");
  await view.click("btnOpenQQImport");
  assert.equal(opened.accountId, "chosen");
  assert.equal(view.node("btnCommitQQImport").hidden, true);
});
test("identity choice is explicit and sends no message content", async () => {
  const calls = [], view = harness(async (url, options) => {
    calls.push([url, JSON.parse(options.body)]);
    return { jobId: "synth", state: "identity", reason: "owner-required", participants: [{ uin: "10001", name: "本人" }] };
  });
  view.node("qqImportPath").value = "D:/synthetic.json";
  await view.click("btnPreviewQQExport");
  assert.equal(view.node("btnMapQQImport").disabled, true);
  view.node("qqImportOwner").value = "10001";
  view.node("qqImportOwner").events.change();
  await view.click("btnMapQQImport");
  assert.deepEqual(calls[1], ["/api/qq/import/identity", { jobId: "synth", ownerUin: "10001" }]);
});
test("errors hide technical paths and read failures allow another preview", async () => {
  const view = harness(async () => { throw { code: "private C:/path and raw data" }; });
  view.node("qqImportPath").value = "D:/synthetic.json";
  await view.click("btnPreviewQQExport");
  assert.equal(view.node("btnPreviewQQExport").disabled, false);
  assert.doesNotMatch(view.node("qqImportStatus").text, /C:\/|private/);
  assert.match(view.ui.errorMessage("missing-export-file"), /未找到/);
});
test("native picker is called by a click and fills only the selected path", async () => {
  let picks = 0;
  const view = harness(async () => ready(), { chooseFile: async () => { picks++; return "D:/synthetic.json"; } });
  assert.equal(picks, 0);
  await view.click("btnChooseQQExport");
  assert.equal(view.node("qqImportPath").value, "D:/synthetic.json");
  assert.equal(picks, 1);
});
test("lost commit response polls status instead of submitting again", async () => {
  let commits = 0;
  const view = harness(async url => {
    if (url.endsWith("/preview")) return ready();
    if (url.endsWith("/commit")) { commits++; throw new Error("connection lost"); }
    return { jobId: "synth", state: "complete", result: { accountId: "chosen", inserted: 2, unchanged: 0, conflicts: 0, backfilled: 0 } };
  });
  view.node("qqImportPath").value = "D:/synthetic.json";
  await view.click("btnPreviewQQExport");
  await view.click("btnCommitQQImport");
  assert.equal(view.node("btnCommitQQImport").hidden, true);
  await view.poll();
  assert.equal(view.view.getCurrent().state, "complete");
  assert.equal(commits, 1);
});
test("five status failures stop polling and allow a fresh preview", async () => {
  const view = harness(async url => url.endsWith("/preview") ? { jobId: "synth", state: "reading" } :
    Promise.reject(new Error("network unavailable")));
  view.node("qqImportPath").value = "D:/synthetic.json";
  await view.click("btnPreviewQQExport");
  for (let index = 0; index < 5; index++) await view.poll();
  assert.equal(view.timers.size, 0);
  assert.equal(view.node("btnPreviewQQExport").disabled, false);
  assert.match(view.node("qqImportStatus").text, /暂时无法确认/);
});
test("owner absent from sender list can be entered explicitly", async () => {
  const calls = [], view = harness(async (url, options) => {
    calls.push([url, JSON.parse(options.body)]);
    return { jobId: "synth", state: "identity", reason: "owner-required", participants: [{ uin: "10002", name: "对方" }] };
  });
  view.node("qqImportPath").value = "D:/synthetic.json";
  await view.click("btnPreviewQQExport");
  view.node("qqImportOwner").value = "manual";
  view.node("qqImportOwner").events.change();
  view.node("qqImportOwnerManual").value = "10001";
  view.node("qqImportOwnerManual").events.input();
  await view.click("btnMapQQImport");
  assert.equal(calls[1][1].ownerUin, "10001");
});
test("Electron export picker enforces trusted QQ frames and handles selection/cancel", async () => {
  const shell = fs.readFileSync(path.join(__dirname, "../scripts/real-client-shell.cjs"), "utf8");
  const start = shell.indexOf('ipcMain.handle("real-client:qq-choose-export"');
  const end = shell.indexOf('ipcMain.handle("real-client:check-updates"', start);
  assert.ok(start >= 0 && end > start);
  let handler, calls = 0, result = { canceled: false, filePaths: ["D:/合成 导出.json"] };
  const context = { ipcMain: { handle(_name, value) { handler = value; } },
    PRODUCT: { key: "qq" }, selfTest: false, updateValidation: false, window: {},
    trustedFrame: event => event.trusted,
    dialog: { async showOpenDialog(_window, options) { calls++; assert.deepEqual(Array.from(options.properties), ["openFile"]); return result; } } };
  vm.runInNewContext(shell.slice(start, end), context);
  assert.equal(await handler({ trusted: false }), null);
  assert.equal(calls, 0);
  assert.equal(await handler({ trusted: true }), "D:/合成 导出.json");
  result = { canceled: true, filePaths: [] };
  assert.equal(await handler({ trusted: true }), null);
  context.PRODUCT.key = "wechat";
  assert.equal(await handler({ trusted: true }), null);
  assert.equal(calls, 2);
});
