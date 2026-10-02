const assert = require("node:assert/strict");
const { readFileSync } = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");
const { it } = require("node:test");
const source = readFileSync(path.join(__dirname, "../chatui/view-state.js"), "utf8");
function load() {
  const context = vm.createContext({
    window: {}, fetch() { throw new Error("network during state initialization"); },
    setTimeout() { throw new Error("timer during state initialization"); },
  });
  Object.defineProperty(context, "localStorage", { get() { throw new Error("storage during initialization"); } });
  vm.runInContext(source, context);
  return context.window.ViewState;
}
it("creates all four domains without IO and with independent mutable values", () => {
  const api = load();
  for (const domain of ["chat", "labels", "portrait", "settings"]) {
    const a = api.create(domain), b = api.create(domain);
    assert.notEqual(a, b);
    for (const key of Object.keys(a)) {
      if (a[key] && typeof a[key] === "object") assert.notEqual(a[key], b[key], key);
    }
  }
  const a = api.create("chat"), b = api.create("chat");
  a.sessionCache.set("scope-a", { messages: ["synthetic"] });
  assert.equal(b.sessionCache.size, 0);
});
it("advances request counters without allowing reset or partial reset", () => {
  const api = load();
  for (const [domain, counter] of [["chat", "generation"], ["portrait", "profileGeneration"], ["settings", "modelSourceRevision"]]) {
    const state = api.create(domain);
    state[counter] = 7;
    assert.equal(state.advance(counter), 8);
    assert.throws(() => state.reset(counter), /never reset/);
    assert.equal(state[counter], 8);
  }
  const state = api.create("portrait");
  state.activeAnalysisScope = "keep";
  assert.throws(() => state.reset("activeAnalysisScope", "profileGeneration"), /never reset/);
  assert.equal(state.activeAnalysisScope, "keep");
});
it("resets only requested transient fields and keeps snapshots and active API ownership", () => {
  const state = load().create("portrait");
  state.profileCache.set("account/conversation/source", { summary: "saved" });
  state.storedProfileSnapshots.set("saved-key", { summary: "persisted" });
  state.activeAnalysisScope = "old"; state.currentAnalysisJob = { id: "old" };
  state.apiPortraitBusy = true; state.apiPortraitLoadingKey = "pending";
  state.reset("activeAnalysisScope", "currentAnalysisJob");
  assert.equal(state.activeAnalysisScope, null);
  assert.equal(state.currentAnalysisJob, null);
  assert.equal(state.profileCache.size, 1);
  assert.equal(state.storedProfileSnapshots.size, 1);
  assert.equal(state.apiPortraitBusy, true);
  assert.equal(state.apiPortraitLoadingKey, "pending");
});
it("rejects unknown fields instead of overwriting state methods", () => {
  const state = load().create("settings");
  assert.throws(() => state.set("reset", null), /unknown/);
  assert.throws(() => state.reset("missing"), /Unknown/);
  assert.equal(typeof state.reset, "function");
});
it("keeps scope identity and model selection owned by distinct domains", () => {
  const api = load(), chat = api.create("chat"), labels = api.create("labels");
  const portrait = api.create("portrait"), settings = api.create("settings");
  chat.currentAccount = "a"; chat.currentUser = "c";
  settings.modelSourceSnapshot = { mode: "api", sourceId: "api-a" };
  assert.equal(Object.hasOwn(labels, "currentAccount"), false);
  assert.equal(Object.hasOwn(portrait, "modelSourceSnapshot"), false);
  assert.equal(Object.hasOwn(settings, "currentUser"), false);
  labels.apiInsightCache.set("a/c/api-a", { result: "one" });
  labels.apiInsightCache.set("a/c/api-b", { result: "two" });
  labels.apiInsightCache.delete("a/c/api-a");
  assert.equal(labels.apiInsightCache.get("a/c/api-b").result, "two");
});
