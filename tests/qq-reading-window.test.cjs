// Actual latest-page loader under a sliding 80-row response and scoped view state.
const assert = require("node:assert/strict");
const { it } = require("node:test");
const { readFileSync } = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");
const { seed } = require("./helpers/view-state-harness.cjs");
const source = readFileSync(process.env.QQVIBE_READING_TEST_SOURCE || path.join(__dirname, "../chatui/app.js"), "utf8");
function section(start, end, optional = false) {
  const first = source.indexOf(start), last = source.indexOf(end, first);
  if (optional && first < 0) return "";
  assert.ok(first >= 0 && last > first, start);
  return source.slice(first, last);
}
const row = n => ({ id: String(n), side: "other", kind: "text", text: `合成消息 ${n}`, historyCursor: `cursor-${n}` });
function harness(next, options = {}) {
  const counters = { renders: 0, cacheWrites: 0, readMarks: 0, analysisReads: 0, requests: 0 };
  const context = vm.createContext({
    window: { ProductConfig: { key: "qq" } }, clearTimeout() {},
    historyAnchor: () => ({ id: "1", top: 20 }),
    clearInlineIntentPending() {}, refreshLabels() {}, updateHistoryNavigation() {}, ensureApiInsights() {},
    api: async () => { counters.requests++; return { account: "synthetic-account", messages: next, hasMoreBefore: true }; },
    acceptResponseAccount: account => account === "synthetic-account",
    unchangedMessageResults: () => ({}), visibleResults: value => value,
    renderMessages: messages => { counters.renders++; context.chatState.messages = messages; },
    cacheCurrentSession: () => counters.cacheWrites++, markSessionAsRead: () => counters.readMarks++,
    renderSessions() {}, sessionSummarySignature: () => "synthetic", status() {}, text() {},
    byId: () => ({ scrollTop: 100 }),
    loadAnalysis: async () => { counters.analysisReads++; return null; },
    scheduleIncremental() {}, scheduleRecent() {}, handleAccountBoundaryError: () => false,
  });
  seed(context, { currentUser: "synthetic-peer", currentAccount: "synthetic-account", generation: 1,
    controller: new AbortController(), messages: Array.from({ length: 80 }, (_, n) => row(n + 1)),
    followLatest: false, currentHasMoreBefore: false, view: "chat", ...options });
  vm.runInContext(section("function labelSideEligible(", "const svgIcon") +
    section("function retainQQReadingWindow(", "function updateHistoryNavigation(", true) +
    section("function enterHistoryView(", "function historyResponseValid(") +
    section("async function loadMessages(", "function switchSession(") +
    "globalThis.load = loadMessages;", context);
  return { context, counters };
}

it("a sliding page cannot remove the visible old message or mark unseen rows read", async () => {
  const next = Array.from({ length: 80 }, (_, n) => row(n + 81));
  const { context, counters } = harness(next);
  await context.load(1, true);
  assert.equal(context.chatState.messages[0].id, "1");
  assert.equal(context.chatState.messages.at(-1).id, "80");
  assert.equal(context.chatState.historyState.afterCursor, "cursor-80");
  assert.equal(context.chatState.historyState.hasMoreAfter, true);
  assert.equal(context.chatState.historyState.hasMoreBefore, false);
  assert.equal(context.chatState.followLatest, false);
  assert.equal(context.chatState.messagePending, false);
  assert.deepEqual(counters, { renders: 0, cacheWrites: 0, readMarks: 0, analysisReads: 0, requests: 1 });
  await context.load(1, true); await context.load(1, true);
  assert.equal(counters.requests, 1, "pinned history stops replacing the visible window");
});

it("following the latest page continues to receive new rows normally", async () => {
  const next = Array.from({ length: 80 }, (_, n) => row(n + 81));
  const { context, counters } = harness(next, { followLatest: true });
  await context.load(1, true);
  assert.equal(context.chatState.historyState, null);
  assert.equal(context.chatState.messages[0].id, "81");
  assert.equal(counters.renders, 1); assert.equal(counters.readMarks, 1);
});

it("visible text corrections still arrive when the reading anchor remains in the page", async () => {
  const next = Array.from({ length: 80 }, (_, n) => row(n + 1)); next[0].text = "已更正的合成文字";
  const { context, counters } = harness(next);
  await context.load(1, true);
  assert.equal(context.chatState.historyState, null);
  assert.equal(context.chatState.messages[0].text, "已更正的合成文字");
  assert.equal(counters.renders, 1);
});

it("the QQ reading change does not alter the upstream WeChat path", async () => {
  const next = Array.from({ length: 80 }, (_, n) => row(n + 81));
  const { context } = harness(next); context.window.ProductConfig.key = "wechat";
  await context.load(1, true);
  assert.equal(context.chatState.historyState, null);
  assert.equal(context.chatState.messages[0].id, "81");
});

it("a response for a previous conversation generation cannot pin or overwrite the current view", async () => {
  const { context, counters } = harness([row(999)]);
  let finish; context.api = () => new Promise(resolve => { finish = resolve; });
  const pending = context.load(1, true);
  context.chatState.generation = 2; context.chatState.currentUser = "new-peer";
  finish({ account: "synthetic-account", messages: [row(999)] }); await pending;
  assert.equal(context.chatState.historyState, null); assert.equal(context.chatState.messages[0].id, "1");
  assert.equal(counters.renders, 0); assert.equal(counters.readMarks, 0);
});
