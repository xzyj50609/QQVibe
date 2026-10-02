const assert = require("node:assert/strict");
const { it } = require("node:test");
const vm = require("node:vm");
const path = require("node:path");
const { readFileSync } = require("node:fs");
const source = readFileSync(path.join(__dirname, "../chatui/app.js"), "utf8");
const start = source.indexOf("async function copyDraft(");
const end = source.indexOf('byId("searchInput").addEventListener', start);
const keyboard = source.split("\n").find(line => line.startsWith('byId("chatInput").addEventListener("keydown"'));
assert.ok(start >= 0 && end > start && keyboard);
function harness(value) {
  const copies = [], notices = [];
  let handler;
  const input = { value, addEventListener: (_name, callback) => { handler = callback; } };
  const context = vm.createContext({ byId: () => input, window: { desktopHost: {
    copyDraft: async text => { copies.push(text); return true; },
  } }, navigator: {}, document: {}, toast: value => notices.push(value),
    fetch: () => { throw new Error("draft must never send a request"); } });
  vm.runInContext(source.slice(start, end) + keyboard, context);
  return { copies, notices, input, key: event => handler(event) };
}
const tick = () => new Promise(resolve => setImmediate(resolve));
it("Enter copies exactly the multiline draft and retains it without sending", async () => {
  const value = "合成草稿 🐚\n第二行，保留换行和空格 ";
  const f = harness(value); let prevented = false;
  f.key({ key: "Enter", shiftKey: false, isComposing: false, preventDefault() { prevented = true; } });
  await tick();
  assert.equal(prevented, true); assert.deepEqual(f.copies, [value]);
  assert.equal(f.input.value, value); assert.deepEqual(f.notices, ["草稿已复制，未发送"]);
});
it("Shift+Enter and Chinese composition do not trigger copy or prevent typing", async () => {
  const f = harness("合成文字");
  for (const event of [{ shiftKey: true, isComposing: false }, { shiftKey: false, isComposing: true }]) {
    f.key({ key: "Enter", ...event, preventDefault() { throw new Error("typing must remain native"); } });
  }
  await tick(); assert.deepEqual(f.copies, []); assert.deepEqual(f.notices, []);
});
it("a blank draft does not touch the clipboard", async () => {
  const f = harness(" \n\t"); f.key({ key: "Enter", preventDefault() {} });
  await tick(); assert.deepEqual(f.copies, []); assert.equal(f.input.value, " \n\t");
});
