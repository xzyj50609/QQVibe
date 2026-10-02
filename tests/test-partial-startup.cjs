const assert = require("node:assert/strict");
const { readFileSync } = require("node:fs");
const path = require("node:path");
const { test } = require("node:test");
const vm = require("node:vm");
const { seed } = require("./helpers/view-state-harness.cjs");

const source = readFileSync(path.join(__dirname, "../chatui/app.js"), "utf8");
const start = source.indexOf("async function loadSessions(");
const end = source.indexOf("function renderMood(", start);
assert.ok(start >= 0 && end > start);
const loadSessionsSource = source.slice(start, end);
const selectionStart = source.indexOf("async function loadConversationSelection(");
const selectionEnd = source.indexOf("function clearUnselectedConversation(", selectionStart);
assert.ok(selectionStart >= 0 && selectionEnd > selectionStart);
const loadSelectionSource = source.slice(selectionStart, selectionEnd);

function harness(responses, selectedByAccount = {}) {
  const context = vm.createContext({ responses: [...responses], selectedByAccount, AbortController });
  seed(context, { messages: [{ id: "stale" }], results: { stale: true }, conversationMood: "stale" });
  vm.runInContext(`
    let accountClearedExiting = false, accountUnavailable = false, startupAccountRetryTimer = null;
    const elements = new Map(), events = [];
    const byId = id => {
      if (!elements.has(id)) elements.set(id, { scrollTop: 0 });
      return elements.get(id);
    };
    let lastAccount = null;
    const api = async path => {
      events.push(path);
      if (path === "/api/conversation-selection")
        return { account: lastAccount, selectedSessions: selectedByAccount[lastAccount] || [] };
      const response = responses.shift();
      lastAccount = response?.account;
      return response;
    };
    const clearTimeout = () => {};
    function resetAccountView() {
      events.push("reset");
      sessionRequest++;
      currentAccount = null;
      selectionLoadedAccount = null;
      selectedConversations.clear();
      currentUser = null;
      messageSourceReady = false;
      sessions.clear();
      messages = [];
      results = {};
      conversationMood = null;
    }
    const setAvatar = () => {}, text = () => {};
    const status = (_node, message) => events.push(message);
    const switchView = target => events.push("view:" + target);
    const renderSessions = () => events.push("render:" + sessions.size);
    const completeStartup = () => events.push("complete");
    const accountCheckStatus = message => events.push("status:" + message);
    const sessionWindowReady = () => false;
    const preloadSessionWindows = async (_account, nextSessions) => {
      events.push("preload:" + [...nextSessions.keys()].filter(id => selectedConversations.has(id)).length);
      return true;
    };
    const pruneSessionCache = () => {};
    const localStorage = { getItem: () => null };
    const switchSession = user => { events.push("switch:" + user); currentUser = user; };
    const clearUnselectedConversation = () => {
      events.push("clear-unselected");
      currentUser = null;
      messages = [];
      results = {};
      conversationMood = null;
    };
    const loadProfile = () => { throw new Error("profile must not run in chat view"); };
    const renderConversationManager = () => events.push("manager");
    const accountChangedError = () => false;
    const accountUnavailableError = () => false;
    const contactSnapshotStaleError = () => false;
    const showStartup = (_stage, message) => events.push("startup:" + message);
    let startupActive = false;
    ${loadSelectionSource}
    ${loadSessionsSource}
    globalThis.check = () => ({ events: [...events], account: currentAccount, user: currentUser,
      ready: messageSourceReady, sessions: [...sessions.keys()], messages: [...messages],
      results: { ...results }, mood: conversationMood, refresh: [...profileSnapshotsRequireRefresh],
      selected: [...selectedConversations] });
  `, context);
  return context;
}

const session = account => ({ account, self: { name: "我" },
  sessions: [{ username: "friend", name: "朋友", preview: "合成会话" }] });

test("partial startup renders sessions and keeps the chat empty when none were selected", async () => {
  const context = harness([{ ...session("account-a"), messagesReady: false },
    { ...session("account-a"), messagesReady: false },
    { ...session("account-a"), messagesReady: true }]);
  await context.loadSessions();
  let state = context.check();
  assert.equal(state.ready, false);
  assert.equal(state.account, "account-a");
  assert.equal(state.user, null);
  assert.equal(state.sessions.join(","), "friend");
  assert.equal(state.messages.length, 0);
  assert.equal(Object.keys(state.results).length, 0);
  assert.equal(state.mood, null);
  assert.equal(state.events.some(value => value.startsWith("preload:")), false);
  assert.equal(state.events.includes("switch:friend"), false);
  assert.ok(state.events.includes("render:1"));
  await context.loadSessions();
  state = context.check();
  assert.equal(state.events.filter(value => value === "reset").length, 1);
  assert.equal(state.events.some(value => value.startsWith("preload:")), false);
  await context.loadSessions();
  state = context.check();
  assert.equal(state.ready, true);
  assert.equal(state.selected.length, 0);
  assert.ok(state.events.includes("preload:0"));
  assert.ok(state.events.includes("clear-unselected"));
  assert.equal(state.events.includes("switch:friend"), false);
  assert.equal(state.events.filter(value => value === "/api/conversation-selection").length, 1);
});

test("a saved selection alone enables its chat window and first session", async () => {
  const context = harness([{ ...session("account-a"), messagesReady: true }],
    { "account-a": ["friend"] });
  await context.loadSessions();
  const state = context.check();
  assert.equal(state.selected.join(","), "friend");
  assert.equal(state.user, "friend");
  assert.ok(state.events.includes("preload:1"));
  assert.ok(state.events.includes("switch:friend"));
});

test("partial account change drops the previous account view", async () => {
  const context = harness([{ ...session("account-a"), messagesReady: false },
    { ...session("account-b"), messagesReady: false }]);
  await context.loadSessions();
  await context.loadSessions();
  const state = context.check();
  assert.equal(state.account, "account-b");
  assert.equal(state.user, null);
  assert.equal(state.ready, false);
  assert.equal(state.events.filter(value => value === "reset").length, 2);
  assert.equal(state.events.some(value => value.startsWith("preload:")), false);
  assert.equal(state.selected.length, 0);
});

test("final update unlocks the UI after commit while account validation can continue", async () => {
  const unlockStart = source.indexOf("function unlockStartupUi()");
  const unlockEnd = source.indexOf("function completeStartup()", unlockStart);
  const bootStart = source.indexOf("const updateValidationMode =");
  const bootEnd = source.indexOf('window.addEventListener("wechatvibe-service-restored"', bootStart);
  assert.ok(unlockStart >= 0 && unlockEnd > unlockStart && bootStart >= 0 && bootEnd > bootStart);
  const nodes = new Map();
  const byId = id => {
    if (!nodes.has(id)) nodes.set(id, {
      hidden: true, inert: false,
      classList: { add() {} },
      setAttribute(name) { if (name === "inert") this.inert = true; },
      removeAttribute(name) { if (name === "inert") this.inert = false; },
    });
    return nodes.get(id);
  };
  let finishCommit;
  const commit = new Promise(resolve => { finishCommit = resolve; });
  const events = [];
  const context = vm.createContext({
    byId, text() {},
    window: { desktopHost: { updateValidationMode: false, updateFinalReadyMode: true,
      reportUiReady: () => commit } },
    startInitialLoad: () => { events.push("account-validation-started"); return new Promise(() => {}); },
    loadModelSource: () => events.push("model-source-load-started"),
    loadLocalModel: () => events.push("local-model-load-started"),
  });
  vm.runInContext(source.slice(unlockStart, unlockEnd) + source.slice(bootStart, bootEnd), context);
  assert.equal(byId("startupOverlay").hidden, false);
  assert.equal(byId("appWindow").inert, true);
  assert.equal(events.includes("account-validation-started"), false);
  finishCommit(true);
  await Promise.resolve();
  assert.equal(byId("startupOverlay").hidden, true);
  assert.equal(byId("appWindow").inert, false);
  assert.equal(events.includes("account-validation-started"), true);

  const validationContext = vm.createContext({
    byId, text() {},
    window: { desktopHost: { updateValidationMode: true, updateFinalReadyMode: false,
      reportUiReady: () => Promise.resolve(true) } },
    startInitialLoad: () => { throw new Error("validation must not open account data"); },
    loadModelSource: () => { throw new Error("validation must not load model settings"); },
    loadLocalModel: () => { throw new Error("validation must not start local model loading"); },
  });
  vm.runInContext(source.slice(unlockStart, unlockEnd) + source.slice(bootStart, bootEnd), validationContext);
  await Promise.resolve();
  assert.equal(byId("startupOverlay").hidden, false);
  assert.equal(byId("appWindow").inert, true);
});
