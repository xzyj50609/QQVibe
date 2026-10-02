"use strict";
// A4: the four front-end state domains as independent, plain instances.
// This module owns initialization only: it never reads storage, fetches, sets a
// timer, or touches the DOM. Every Map/Set/array is created per instance, so two
// instances never share a reference. Static defaults match the original app.js
// initializers; dynamic values (settings from localStorage, view from the URL)
// stay in app.js at their original position.
(function () {
  const BUILDERS = {
    chat: function () {
      return {
        sessions: new Map(),
        selectedConversations: new Set(),
        selectionLoadedAccount: null,
        conversationSelectionBusy: false,
        sessionCache: new Map(),
        historyState: null,
        historyRequest: 0,
        historyController: null,
        historySearchRequest: 0,
        historySearchController: null,
        historySearchPending: false,
        historySearchPage: 0,
        historySearchPageStarts: [null],
        historySearchQuery: { q: "", date: "" },
        self: null,
        sessionSignature: null,
        sessionRequest: 0,
        sessionLoading: false,
        sessionRefreshQueued: false,
        windowRequestSerial: 0,
        preloadDone: 0,
        preloadTotal: 0,
        currentAccount: null,
        currentUser: null,
        currentHasMoreBefore: null,
        messageSourceReady: false,
        view: "chat",
        generation: 0,
        controller: null,
        messages: [],
        messagePending: false,
        messageRequest: 0,
        messageRefreshQueued: false,
        emptyMessagePolls: 0,
        conversationMood: null,
        followLatest: true,
        lastChatScrollTop: 0,
      };
    },
    labels: function () {
      return {
        results: {},
        currentRecentJob: null,
        inlineIntentPending: new Map(),
        inlineIntentJobId: null,
        recentFailed: false,
        requestedRecentSignatures: new Set(),
        recentPending: false,
        intentActionState: "idle",
        intentFeedbackTimer: null,
        manualRecentAwaitingPost: false,
        manualRecentJobId: null,
        manualRecentDeferred: false,
        recentNetworkFailed: false,
        selectedAnalysisTimer: null,
        catalogReady: false,
        intentDisplayAliases: new Map(),
        emotionDisplayAliases: new Map(),
        catalogLabelRevision: "",
        messageLabels: null,
        apiInsightCache: new Map(),
        apiInsightWork: null,
        apiInsightViewportTimer: null,
        apiInsightStatusRendered: false,
      };
    },
    portrait: function () {
      return {
        profileCache: new Map(),
        profileSnapshotsRequireRefresh: new Set(),
        profileGeneration: 0,
        analysisGeneration: 0,
        autoIncrementalState: new Map(),
        activeAnalysisScope: null,
        currentAnalysisJob: null,
        incrementalFailed: false,
        analysisNetworkFailed: false,
        activeMember: "",
        profilePending: false,
        groupMembers: [],
        renderedProfileKey: null,
        renderedProfileSignature: null,
        memberRenderedScope: null,
        storedProfileSnapshots: new Map(),
        storedProfileSelections: new Map(),
        profileRateSamples: new Map(),
        apiPortraitRequest: 0,
        apiPortraitPollTimer: null,
        apiPortraitSnapshot: null,
        apiPortraitBusy: false,
        renderedApiPortraitKey: null,
        apiPortraitLoadingKey: null,
        apiPortraitSubmitErrors: new Map(),
        apiPortraitReadFailures: 0,
        renderedApiPortraitScopeKey: null,
        renderedApiProfileScope: null,
        renderedApiProfileSignature: null,
        apiPortraitProgressNode: null,
      };
    },
    settings: function () {
      return {
        // The dynamic localStorage load stays in app.js; this field owns its result.
        settings: undefined,
        runtimeSnapshot: null,
        runtimeRequest: 0,
        runtimeBusy: false,
        runtimePollTimer: null,
        localModelRequest: 0,
        localModelDownloadBusy: false,
        localModelReady: false,
        localModelResolved: false,
        modelSourceSnapshot: { mode: "local", api: null, sourceId: "local", status: "idle" },
        modelSourceResolved: false,
        modelSourceReadRequest: 0,
        modelSourceLoadController: null,
        modelSourceRevision: 0,
        modelListRequest: 0,
        modelListController: null,
        modelTestRequest: 0,
        modelTestController: null,
        modelSourceLoading: false,
        modelSourceBusy: false,
        modelListBusy: false,
        modelTestBusy: false,
        modelSourceDraftDirty: false,
        suppressedApiSources: new Set(),
        suppressedLocalAccounts: new Set(),
      };
    },
  };
  // Narrow, actually-called transition helpers. They never zero a generation
  // counter; `advance` only increments.
  function create(domain) {
    const builder = BUILDERS[domain];
    if (!builder) throw new Error(`unknown view state domain: ${domain}`);
    const state = builder();
    const fields = new Set(Object.keys(state));
    state.set = function set(key, value) {
      if (!fields.has(key)) throw new Error(`unknown ${domain} state key: ${key}`);
      this[key] = value;
    };
    state.advance = function advance(key) {
      if (!Object.prototype.hasOwnProperty.call(this, key) || typeof this[key] !== "number") {
        throw new Error(`cannot advance ${domain} state key: ${key}`);
      }
      this[key] += 1;
      return this[key];
    };
    state.reset = function reset(...keys) {
      const counters = {
        chat: ["generation", "historyRequest", "historySearchRequest", "sessionRequest", "windowRequestSerial", "messageRequest"],
        labels: [],
        portrait: ["profileGeneration", "analysisGeneration", "apiPortraitRequest"],
        settings: ["runtimeRequest", "localModelRequest", "modelSourceReadRequest", "modelSourceRevision", "modelListRequest", "modelTestRequest"],
      };
      if (keys.some(key => counters[domain].includes(key))) throw new Error("Request counters must never reset");
      if (keys.some(key => !fields.has(key))) throw new Error("Unknown state field");
      const defaults = builder();
      for (const key of keys) state.set(key, defaults[key]);
    };
    return state;
  }
  window.ViewState = Object.freeze({ create });
})();
