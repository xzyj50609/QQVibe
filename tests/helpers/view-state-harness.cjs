"use strict";
// A4 test-only bridge: lets existing assertions keep referring to the old bare
// names while the real ViewState instances own the values. Production code never
// uses this; it exists only inside the vm test contexts.
const { readFileSync } = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");

const VIEW_STATE_SOURCE = readFileSync(path.join(__dirname, "..", "..", "chatui/view-state.js"), "utf8");

const DOMAIN_FIELDS = {
  chat: ["sessions", "selectedConversations", "selectionLoadedAccount", "conversationSelectionBusy",
    "sessionCache", "historyState", "historyRequest", "historyController", "historySearchRequest",
    "historySearchController", "historySearchPending", "historySearchPage", "historySearchPageStarts",
    "historySearchQuery", "self", "sessionSignature", "sessionRequest", "sessionLoading",
    "sessionRefreshQueued", "windowRequestSerial", "preloadDone", "preloadTotal", "currentAccount",
    "currentUser", "currentHasMoreBefore", "messageSourceReady", "view", "generation", "controller",
    "messages", "messagePending", "messageRequest", "messageRefreshQueued", "emptyMessagePolls",
    "conversationMood", "followLatest", "lastChatScrollTop"],
  labels: ["results", "currentRecentJob", "inlineIntentPending", "inlineIntentJobId", "recentFailed",
    "requestedRecentSignatures", "recentPending", "intentActionState", "intentFeedbackTimer",
    "manualRecentAwaitingPost", "manualRecentJobId", "manualRecentDeferred", "recentNetworkFailed",
    "selectedAnalysisTimer", "catalogReady", "intentDisplayAliases", "emotionDisplayAliases",
    "catalogLabelRevision", "messageLabels", "apiInsightCache", "apiInsightWork",
    "apiInsightViewportTimer", "apiInsightStatusRendered"],
  portrait: ["profileCache", "profileSnapshotsRequireRefresh", "profileGeneration", "analysisGeneration",
    "autoIncrementalState", "activeAnalysisScope", "currentAnalysisJob", "incrementalFailed",
    "analysisNetworkFailed", "activeMember", "profilePending", "groupMembers", "renderedProfileKey",
    "renderedProfileSignature", "memberRenderedScope", "storedProfileSnapshots",
    "storedProfileSelections", "profileRateSamples", "apiPortraitRequest", "apiPortraitPollTimer",
    "apiPortraitSnapshot", "apiPortraitBusy", "renderedApiPortraitKey", "apiPortraitLoadingKey",
    "apiPortraitSubmitErrors", "apiPortraitReadFailures", "renderedApiPortraitScopeKey",
    "renderedApiProfileScope", "renderedApiProfileSignature", "apiPortraitProgressNode"],
  settings: ["settings", "runtimeSnapshot", "runtimeRequest", "runtimeBusy", "runtimePollTimer", "localModelRequest",
    "localModelDownloadBusy", "localModelReady", "localModelResolved", "modelSourceSnapshot",
    "modelSourceResolved", "modelSourceReadRequest", "modelSourceLoadController", "modelSourceRevision",
    "modelListRequest", "modelListController", "modelTestRequest", "modelTestController",
    "modelSourceLoading", "modelSourceBusy", "modelListBusy", "modelTestBusy", "modelSourceDraftDirty",
    "suppressedApiSources", "suppressedLocalAccounts"],
};

const DOMAIN_FOR = {};
for (const [domain, fields] of Object.entries(DOMAIN_FIELDS)) {
  for (const field of fields) DOMAIN_FOR[field] = domain;
}

// Install the real ViewState into an existing vm context and alias old names.
function installViewState(context) {
  if (context.__viewStateInstances) return context.__viewStateInstances;
  if (!context.window) context.window = context;
  vm.runInContext(VIEW_STATE_SOURCE, context);
  context.ViewState = context.window.ViewState;
  const states = {
    chat: vm.runInContext('ViewState.create("chat")', context),
    labels: vm.runInContext('ViewState.create("labels")', context),
    portrait: vm.runInContext('ViewState.create("portrait")', context),
    settings: vm.runInContext('ViewState.create("settings")', context),
  };
  context.chatState = states.chat;
  context.labelState = states.labels;
  context.portraitState = states.portrait;
  context.settingsState = states.settings;
  context.ViewState = vm.runInContext("ViewState", context);
  for (const [name, domain] of Object.entries(DOMAIN_FOR)) {
    const state = states[domain];
    if (Object.prototype.hasOwnProperty.call(context, name)) state[name] = context[name];
    // Alias reads/writes so a bare `currentUser` in the test maps to chatState.currentUser.
    Object.defineProperty(context, name, {
      configurable: true,
      enumerable: true,
      get() { return state[name]; },
      set(value) { state[name] = value; },
    });
  }
  context.__viewStateInstances = states;
  return states;
}

// Apply the values the test wants to seed, then alias; never two sources of truth.
function seed(context, values) {
  const states = installViewState(context);
  for (const [name, value] of Object.entries(values)) {
    const domain = DOMAIN_FOR[name];
    if (!domain) throw new Error(`unknown view-state seed: ${name}`);
    states[domain][name] = value;
  }
  return states;
}

module.exports = { installViewState, seed, DOMAIN_FIELDS, DOMAIN_FOR };
