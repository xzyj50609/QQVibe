"use strict";
const chatState = window.ViewState.create("chat");
const labelState = window.ViewState.create("labels");
const portraitState = window.ViewState.create("portrait");
const settingsState = window.ViewState.create("settings");
const byId = id => document.getElementById(id);
const element = (tag, className = "", text) => {
  const node = document.createElement(tag);
  node.className = className;
  if (text !== undefined && text !== null) node.textContent = String(text);
  return node;
};
const text = (id, value) => { byId(id).textContent = value == null ? "" : String(value); };
const setStripStatus = (message = "") => {
  text("stripStatusText", message);
  byId("stripStatusText").title = "";
  const item = byId("stripStatusItem");
  if (item) item.style.display = message ? "" : "none";
};
function labelSideEligible(message) {
  return message.side === "other" || (window.ProductConfig?.key === "qq" && message.side === "self");
}
const svgIcon = (pathD, className = "", viewBox = "0 0 24 24") => {
  const svg = document.createElementNS("http://www.w3.org/2000/svg", "svg");
  if (className) svg.setAttribute("class", className);
  svg.setAttribute("viewBox", viewBox);
  const path = document.createElementNS("http://www.w3.org/2000/svg", "path");
  path.setAttribute("d", pathD);
  svg.appendChild(path);
  return svg;
};
function getWatermarks() {
  const key = `read-watermark:${chatState.currentAccount || "default"}`;
  try { return JSON.parse(localStorage.getItem(key) || "{}"); }
  catch { return {}; }
}
function markSessionAsRead(username) {
  if (!username) return;
  const key = `read-watermark:${chatState.currentAccount || "default"}`;
  const marks = getWatermarks();
  const session = chatState.sessions.get(username);
  marks[username] = {
    time: Number(session?.sortTimestamp || session?.time) || 0,
    unreadCount: Number(session?.unreadCount) || 0,
    preview: String(session?.preview || ""),
    readAt: Date.now()
  };
  try { localStorage.setItem(key, JSON.stringify(marks)); } catch {}
}
function getVisibleUnreadCount(session) {
  if (!session) return 0;
  const serverUnread = Number(session.unreadCount) || 0;
  if (serverUnread <= 0) return 0;
  if (session.username === chatState.currentUser) return 0;
  const marks = getWatermarks();
  const wm = marks[session.username];
  if (!wm) return serverUnread;
  const curTime = Number(session.sortTimestamp || session.time) || 0;
  const curPreview = String(session.preview || "");
  const hasNewTime = wm.time > 0 && curTime > wm.time;
  const hasIncreasedUnread = serverUnread > wm.unreadCount;
  const hasNewPreview = (curTime === wm.time && curPreview !== wm.preview && curPreview !== "");
  if (hasIncreasedUnread) return serverUnread - wm.unreadCount;
  if (hasNewTime || hasNewPreview) return serverUnread;
  return 0;
}
const defaults = { theme: "light", zoom: "1.0", intent: true, labelDetails: window.ProductConfig?.key === "qq" };
const CURRENT_LABEL_SCHEMA = "generic-v9";
const GENERIC_INTENT_LABELS = Object.freeze({
  small_talk: "闲聊", share_news: "分享", ask_question: "提问", seek_help: "求助", deny: "否认",
  give_comfort: "安慰", agree: "同意", invite: "邀约", show_affection: "表达好感",
  complain: "抱怨", apologize: "道歉", joke: "玩笑", reject: "拒绝",
  distance: "保持距离", thank: "感谢", greet: "问候", confirm: "确认", inspect: "查看",
  suggest_action: "建议或指令", explain: "解释", inform: "告知事实",
  plan: "计划", correct: "纠正或异议", status_report: "状态报告",
  follow_up: "追问", clarify: "澄清", share_feeling: "表达感受", confide: "倾诉",
  seek_comfort: "求安慰", seek_company: "求陪伴", show_care: "关心",
  encourage: "鼓励", praise: "称赞", celebrate: "祝贺", miss_you: "表达想念",
  test_feelings: "探询心意", set_boundary: "设定边界", reconcile: "缓和关系",
  show_material: "展示内容", offer_help: "提供帮助", tease: "调侃", close_chat: "告别",
  general_exchange: "一般交流",
});
const GROUNDED_EVIDENCE = Object.freeze({
  greet: ["greeting_phrase"], thank: ["thanks_phrase"],
  confirm: ["short_acknowledgement"], inspect: ["first_person_inspection"],
  agree: ["explicit_acceptance"],
  reject: ["explicit_refusal"], invite: ["inclusive_invitation"],
  ask_question: ["answer_seeking_question"], seek_help: ["action_request"],
  suggest_action: ["advice_marker", "negative_imperative", "imperative_adjustment", "delegated_action"],
  plan: ["first_person_intention"], correct: ["explicit_correction"],
  explain: ["causal_explanation", "process_explanation"], complain: ["negative_evaluation"],
  status_report: ["progress_statement"], share_news: ["sharing_announcement"],
  deny: ["contextual_denial"],
  // A deferral grounded by the preceding invitation is an explicit refusal signal.
  reject: ["explicit_refusal", "contextual_deferral"],
});
settingsState.settings = undefined;
try { settingsState.settings = { ...defaults, ...JSON.parse(localStorage.getItem("real-ui-settings-1") || "{}") }; }
catch { settingsState.settings = { ...defaults }; }
delete settingsState.settings.historyLimit;
if (!["dark", "light"].includes(settingsState.settings.theme)) settingsState.settings.theme = defaults.theme;
if (!["0.9", "1.0", "1.1", "1.25", "1.5"].includes(settingsState.settings.zoom)) settingsState.settings.zoom = "1.0";
if (typeof settingsState.settings.intent !== "boolean") settingsState.settings.intent = true;
if (typeof settingsState.settings.labelDetails !== "boolean") settingsState.settings.labelDetails = defaults.labelDetails;
const save = () => localStorage.setItem("real-ui-settings-1", JSON.stringify(settingsState.settings));
chatState.sessions = new Map();
chatState.selectedConversations = new Set();
chatState.selectionLoadedAccount = null;
chatState.conversationSelectionBusy = false;
chatState.sessionCache = new Map();
portraitState.profileCache = new Map();
const sessionCacheMessageLimit = 80;
const historyWindowLimit = 240;
chatState.historyState = null;
chatState.historyRequest = 0;
chatState.historyController = null;
chatState.historySearchRequest = 0;
chatState.historySearchController = null;
chatState.historySearchPending = false;
chatState.historySearchPage = 0;
chatState.historySearchPageStarts = [null];
chatState.historySearchQuery = { q: "", date: "" };
chatState.self = null;
chatState.sessionSignature = null;
chatState.sessionRequest = 0;
chatState.sessionLoading = false;
chatState.sessionRefreshQueued = false;
chatState.windowRequestSerial = 0;
chatState.preloadDone = 0;
chatState.preloadTotal = 0;
chatState.currentAccount = null;
chatState.currentUser = null;
chatState.currentHasMoreBefore = null;
chatState.messageSourceReady = false;
portraitState.profileSnapshotsRequireRefresh = new Set();
chatState.view = "chat";
chatState.generation = 0;
portraitState.profileGeneration = 0;
portraitState.analysisGeneration = 0;
portraitState.autoIncrementalState = new Map();
portraitState.activeAnalysisScope = null;
portraitState.currentAnalysisJob = null;
labelState.currentRecentJob = null;
chatState.controller = null;
chatState.messages = [];
labelState.results = {};
labelState.inlineIntentPending = new Map();
labelState.inlineIntentJobId = null;
labelState.recentFailed = false;
labelState.requestedRecentSignatures = new Set();
labelState.recentPending = false;
labelState.intentActionState = "idle";
labelState.intentFeedbackTimer = null;
labelState.manualRecentAwaitingPost = false;
labelState.manualRecentJobId = null;
labelState.manualRecentDeferred = false;
portraitState.incrementalFailed = false;
portraitState.analysisNetworkFailed = false;
labelState.recentNetworkFailed = false;
chatState.messagePending = false;
chatState.messageRequest = 0;
chatState.messageRefreshQueued = false;
chatState.emptyMessagePolls = 0;
labelState.selectedAnalysisTimer = null;
chatState.conversationMood = null;
chatState.followLatest = true;
chatState.lastChatScrollTop = 0;
labelState.catalogReady = false;
labelState.intentDisplayAliases = new Map();
labelState.emotionDisplayAliases = new Map();
labelState.catalogLabelRevision = "";
let toastTimer;
let accountUnavailable = false;
let accountClearedExiting = false;
let replyPredictionRequest = 0;
let replyPredictionController = null;
let startupActive = true;
let startupStage = "account";
let startupStageEpoch = 0;
let startupAttempt = 0;
let startupWatchdog = null;
let startupAccountRetryUsed = false;
let startupAccountRetryTimer = null;
function accountCheckStatus(message, retry = false) {
  text("accountCheckSummary", message);
  text("accountCheckStatus", message);
  const ready = message.startsWith("聊天记录就绪") || message === "账号已就绪";
  byId("accountValidation").hidden = ready;
  byId("accountCheckSummary").dataset.state = ready ? "ready" : retry ? "attention" : "working";
  byId("accountCheckSummary").title = message;
  byId("btnRetryAccountCheck").hidden = !retry;
}
function showStartup(stage, message, options = {}) {
  accountCheckStatus(message, options.retry === true);
  if (!startupActive) return;
  startupStage = stage;
  const epoch = ++startupStageEpoch;
  const steps = ["account", "sessions", "messages"];
  for (const item of byId("startupOverlay").querySelectorAll(".startup-step")) {
    const position = steps.indexOf(item.dataset.step);
    item.classList.toggle("done", position < steps.indexOf(stage));
    item.classList.toggle("active", item.dataset.step === stage);
  }
  text("startupStatus", message);
  byId("startupRetry").hidden = !options.retry;
  byId("startupContinue").hidden = !options.continueEmpty;
  clearTimeout(startupWatchdog);
  if (!options.retry) startupWatchdog = setTimeout(() => {
    if (startupActive && startupStageEpoch === epoch) byId("startupRetry").hidden = false;
  }, 12000);
}
function unlockStartupUi() {
  byId("startupOverlay").hidden = true;
  byId("appWindow").removeAttribute("inert");
}
function completeStartup() {
  accountCheckStatus(chatState.currentAccount ? (chatState.preloadTotal ? `聊天记录就绪 ${chatState.preloadDone}/${chatState.preloadTotal}` : "账号已就绪") : "尚未选择账号", !chatState.currentAccount);
  if (chatState.currentAccount) window.QQStartup?.ready();
  if (!startupActive) return;
  startupActive = false;
  startupAttempt++;
  clearTimeout(startupWatchdog);
  clearTimeout(startupAccountRetryTimer);
  startupAccountRetryTimer = null;
  startupAccountRetryUsed = false;
  unlockStartupUi();
}
function retryStartup() {
  if (!startupActive || accountClearedExiting) return;
  startupAttempt++;
  clearTimeout(startupAccountRetryTimer);
  startupAccountRetryTimer = null;
  startupAccountRetryUsed = true;
  showStartup(chatState.currentAccount ? "messages" : "sessions", chatState.currentAccount ? `正在准备聊天记录 ${chatState.preloadDone}/${chatState.preloadTotal}` : "正在读取会话列表…");
  void loadSessions();
}
function sessionCacheKey(account, user) {
  return JSON.stringify([account, user]);
}
function sessionSummarySignature(session) {
  return JSON.stringify(session);
}
function cacheCurrentSession(update) {
  if (!chatState.currentAccount || !chatState.currentUser) return;
  const key = sessionCacheKey(chatState.currentAccount, chatState.currentUser);
  const cached = chatState.sessionCache.get(key);
  if (!cached && !Array.isArray(update.messages)) return;
  if (update.windowSerial && cached?.windowSerial > update.windowSerial) return;
  const entry = { ...(cached || { account: chatState.currentAccount, user: chatState.currentUser, messages: null, results: {}, mood: null, scrollTop: 0, followLatest: true }), ...update };
  if (Array.isArray(entry.messages)) {
    entry.messages = entry.messages.slice(-sessionCacheMessageLimit);
    entry.results = visibleResults(entry.results || {}, entry.messages);
  }
  chatState.sessionCache.set(key, entry);
}
function cacheSessionWindow(account, session, next, serial, hasMoreBefore) {
  const key = sessionCacheKey(account, session.username);
  const cached = chatState.sessionCache.get(key);
  if (serial < (cached?.windowSerial || 0)) return;
  const selected = next.slice(-sessionCacheMessageLimit);
  chatState.sessionCache.set(key, {
    ...(cached || { account, user: session.username, mood: null, scrollTop: 0, followLatest: true }),
    messages: selected,
    results: unchangedMessageResults(cached?.messages || [], selected, cached?.results || {}),
    summarySignature: sessionSummarySignature(session),
    ...(typeof hasMoreBefore === "boolean" ? { hasMoreBefore } : {}),
    windowSerial: serial
  });
}
function sessionWindowReady(account, session) {
  const cached = chatState.sessionCache.get(sessionCacheKey(account, session.username));
  return Array.isArray(cached?.messages) &&
    (session.username === chatState.currentUser && !startupActive || cached.summarySignature === sessionSummarySignature(session));
}
function pruneSessionCache(account, nextSessions) {
  for (const [key, entry] of chatState.sessionCache) {
    if (entry.account !== account || !nextSessions.has(entry.user)) chatState.sessionCache.delete(key);
  }
  for (const key of portraitState.profileCache.keys()) {
    const [cachedAccount, user] = JSON.parse(key);
    if (cachedAccount !== account || !nextSessions.has(user)) portraitState.profileCache.delete(key);
  }
  pruneStoredProfiles(account, nextSessions);
}
function visibleResults(source, list) {
  const selected = {};
  for (const message of list) {
    const id = String(message.id);
    if (Object.prototype.hasOwnProperty.call(source, id) && fineMessageResult(source[id])) selected[id] = source[id];
  }
  return selected;
}
function fineMessageResult(value) {
  return !!value && typeof value === "object" && !value.batchId && !value.batch && value.state !== "batch-covered";
}
function unchangedMessageResults(previous, next, source) {
  const previousById = new Map(previous.map(message => [String(message.id), JSON.stringify(message)]));
  const selected = {};
  for (const message of next) {
    const id = String(message.id);
    if (previousById.get(id) === JSON.stringify(message) && Object.prototype.hasOwnProperty.call(source, id) && fineMessageResult(source[id])) selected[id] = source[id];
  }
  return selected;
}
const percent = value => typeof value === "number" && Number.isFinite(value) && value >= 0 && value <= 1 ? `${(value * 100).toFixed(1).replace(/\.0$/, "")}%` : "";
const time = value => { const number = Number(value); if (!Number.isFinite(number) || number <= 0) return ""; const date = new Date(number); return Number.isFinite(date.getTime()) ? date.toLocaleString("zh-CN", { month: "numeric", day: "numeric", hour: "2-digit", minute: "2-digit" }) : ""; };
function toast(value) {
  text("toastMsg", value);
  byId("toastMsg").classList.add("show");
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => byId("toastMsg").classList.remove("show"), 2300);
}
function avatarUrls(value, candidates = []) {
  return [value, ...(Array.isArray(candidates) ? candidates : [])].map(candidate => {
    try { const url = new URL(candidate); return ["http:", "https:"].includes(url.protocol) && !url.username && !url.password ? url.href : null; }
    catch { return null; }
  }).filter(Boolean);
}
function avatar(value, className, candidates = [], name = "", group = false) {
  const frame = element("span", `${className === "msg-avatar" ? "msg-avatar-frame" : className} avatar-frame`);
  const fallback = element("span", "avatar-fallback", group ? "群" : Array.from(String(name || "·").trim())[0] || "·");
  frame.appendChild(fallback);
  const urls = avatarUrls(value, candidates);
  if (urls.length) {
    const image = element("img", `${className === "msg-avatar" ? "msg-avatar " : ""}avatar-image`);
    image.alt = "";
    if (className === "session-avatar" || className === "msg-avatar") {
      image.loading = "lazy";
      image.decoding = "async";
    }
    let index = 0;
    image.onload = () => frame.classList.add("loaded");
    image.onerror = () => { frame.classList.remove("loaded"); index++; if (index < urls.length) image.src = urls[index]; else image.remove(); };
    frame.appendChild(image);
    image.src = urls[index];
  }
  return frame;
}
function setAvatar(id, value, candidates = [], name = "", group = false) {
  const target = byId(id);
  if (id === "selfAvatar") {
    const fallback = byId("selfAvatarFallback");
    fallback.textContent = Array.from(String(name || "我").trim())[0] || "我";
    fallback.style.display = "grid";
    target.style.display = "none";
    const urls = avatarUrls(value, candidates);
    let index = 0;
    target.onload = () => { target.style.display = "block"; fallback.style.display = "none"; };
    target.onerror = () => { index++; if (index < urls.length) target.src = urls[index]; else { target.removeAttribute("src"); target.style.display = "none"; } };
    if (urls.length) target.src = urls[index];
    else target.removeAttribute("src");
    return;
  }
  target.replaceChildren(avatar(value, "avatar-inner", candidates, name, group));
}
async function api(path, options = {}, signal) {
  const response = await fetch(path, { ...options, signal, headers: { ...(options.body ? { "Content-Type": "application/json" } : {}) } });
  if (!response.ok) {
    const error = new Error(`HTTP ${response.status}`);
    error.status = response.status;
    try {
      const body = await response.json();
      error.code = typeof body?.error === "string" ? body.error : "";
      if (typeof body?.message === "string") error.message = body.message.slice(0, 200);
      else if (error.code === "stale-history-cursor") error.message = "聊天记录已更新，请重新打开会话";
    } catch { }
    throw error;
  }
  const data = await response.json();
  if (data.error) throw new Error(String(data.error));
  return data;
}
function status(container, message, retry) {
  container.replaceChildren(element("div", "ui-state", message));
  if (retry) {
    const button = element("button", "ui-retry", "重试");
    button.addEventListener("click", retry);
    container.appendChild(button);
  }
}
const isNetworkFailure = error => error instanceof TypeError;
function canPredictReply() {
  return chatState.messageSourceReady && !!chatState.currentUser && chatState.currentAccount != null && chatState.sessions.has(chatState.currentUser) &&
    !chatState.sessions.get(chatState.currentUser).isGroup && chatState.messages.length > 0;
}
function updatePredictReplyAvailability() {
  const button = byId("btnPredictReply");
  if (!button) return;
  button.disabled = !canPredictReply() || !!replyPredictionController;
  button.title = chatState.sessions.get(chatState.currentUser)?.isGroup ? "群聊暂不支持预测" : chatState.messages.length ? "预测对方可能的回应" : "暂无消息";
}
function clearReplyPrediction() {
  replyPredictionController?.abort();
  replyPredictionController = null;
  replyPredictionRequest++;
  const card = byId("replyPrediction");
  card.hidden = true;
  card.dataset.lastMessageId = "";
  byId("replyPredictionBody").replaceChildren();
  const dock = byId("chatInput").closest(".chat-input-pane");
  if (card.parentElement !== dock) dock.prepend(card);
  updatePredictReplyAvailability();
}
function placeReplyPrediction(scroll = true) {
  const card = byId("replyPrediction");
  const items = [...byId("chatMessages").querySelectorAll(".msg-item")];
  const host = [...items].reverse().find(item => item.querySelector(".inline-intent-row")) || items.at(-1);
  const wrap = host?.querySelector(".msg-content-wrap");
  if (!wrap) return;
  const intent = wrap.querySelector(".inline-intent-row");
  if (intent) intent.after(card);
  else wrap.appendChild(card);
  if (scroll) requestAnimationFrame(() => { if (!card.hidden && card.isConnected) card.scrollIntoView({ block: "nearest" }); });
}
function resetAccountView(message = "当前账号未就绪", preserveOtherCaches = false) {
  labelState.messageLabels?.closeDetails();
  cancelApiInsightWork();
  cancelApiPortraitPoll();
  clearTimeout(startupAccountRetryTimer);
  startupAccountRetryTimer = null;
  clearInlineIntentPending();
  clearTimeout(labelState.selectedAnalysisTimer);
  labelState.selectedAnalysisTimer = null;
  cancelHistoryRequest();
  chatState.historyState = null;
  resetHistorySearch();
  if (!startupActive) startupActive = true;
  showStartup("account", message, { retry: message === "当前账号未就绪", continueEmpty: message === "当前账号未就绪" });
  accountUnavailable = false;
  chatState.sessionRequest++;
  chatState.controller?.abort();
  chatState.controller = null;
  chatState.advance("generation");
  portraitState.advance("profileGeneration");
  portraitState.advance("analysisGeneration");
  chatState.messageRequest++;
  chatState.currentAccount = null;
  chatState.selectionLoadedAccount = null;
  chatState.selectedConversations.clear();
  chatState.currentUser = null;
  chatState.currentHasMoreBefore = null;
  window.QQSupportController?.scopeChanged();
  chatState.messageSourceReady = false;
  chatState.self = null;
  chatState.sessionSignature = null;
  chatState.preloadDone = 0;
  chatState.preloadTotal = 0;
  chatState.sessions.clear();
  if (!preserveOtherCaches) {
    chatState.sessionCache.clear();
    portraitState.profileCache.clear();
    labelState.apiInsightCache.clear();
  }
  chatState.messages = [];
  labelState.results = {};
  chatState.conversationMood = null;
  labelState.recentFailed = false;
  labelState.recentPending = false;
  portraitState.incrementalFailed = false;
  portraitState.analysisNetworkFailed = false;
  labelState.recentNetworkFailed = false;
  if (!preserveOtherCaches) portraitState.autoIncrementalState.clear();
  chatState.messagePending = false;
  chatState.messageRefreshQueued = false;
  labelState.manualRecentAwaitingPost = false;
  labelState.manualRecentJobId = null;
  labelState.manualRecentDeferred = false;
  setIntentActionState("idle");
  portraitState.profilePending = false;
  labelState.requestedRecentSignatures.clear();
  portraitState.activeAnalysisScope = null;
  portraitState.currentAnalysisJob = null;
  labelState.currentRecentJob = null;
  portraitState.activeMember = "";
  portraitState.groupMembers = [];
  portraitState.renderedProfileKey = null;
  portraitState.renderedProfileSignature = null;
  portraitState.memberRenderedScope = null;
  chatState.followLatest = true;
  chatState.lastChatScrollTop = 0;
  byId("chatInput").value = "";
  byId("btnSend").classList.remove("ready");
  byId("searchInput").value = "";
  clearReplyPrediction();
  setAvatar("selfAvatar", null, [], "我");
  text("chatTitle", "聊天");
  byId("chatStatusPill").style.display = "none";
  text("analysisStatus", "");
  byId("btnRetryAnalysis").hidden = true;
  byId("btnRetryProfile").hidden = true;
  status(byId("chatMessages"), message);
  status(byId("sessionList"), message);
  text("personaHeaderTitle", "人物画像分析");
  text("heroName", message);
  text("heroRelationBadge", "");
  byId("heroAvatar").replaceChildren();
  byId("heroMbtiRow").hidden = true;
  text("heroArchetype", "");
  byId("heroArchetype").style.display = "none";
  byId("heroMetricBox").replaceChildren();
  text("heroMbti", "");
  byId("mbtiCard").hidden = true;
  byId("mbtiScalesList").replaceChildren();
  byId("mbtiSources").replaceChildren();
  byId("radarContainer").replaceChildren();
  byId("tagCloud").replaceChildren();
  text("botSummaryText", "");
  text("stripDbPath", "待读取");
  text("stripMsgCount", "待读取");
  text("stripMessageLabel", "消息：");
  text("stripTextLabel", "文本：");
  text("stripConfidence", "");
  text("stripAnalysisLabel", "已分析：");
  setStripStatus("");
  byId("groupMemberTabs").replaceChildren();
  byId("groupMemberTabs").style.display = "none";
  portraitState.renderedApiPortraitKey = null;
  clearApiPortraitView();
  updateHistoryNavigation();
}
function accountUnavailableError(error) {
  return error?.status === 503 && error?.code === "AccountUnavailableError";
}
function accountChangedError(error) {
  return error?.status === 503 && error?.code === "AccountChangedError";
}
function contactSnapshotStaleError(error) {
  return error?.status === 503 && error?.code === "ContactSnapshotStaleError";
}
function handleAccountBoundaryError(error) {
  if (accountChangedError(error)) {
    resetAccountView("账号已变化，正在读取会话…");
    void loadSessions();
    return true;
  }
  if (accountUnavailableError(error)) {
    resetAccountView("当前账号未就绪");
    accountUnavailable = true;
    return true;
  }
  return false;
}
function acceptResponseAccount(account) {
  if (account === undefined || account === chatState.currentAccount) return true;
  resetAccountView("账号已变化，正在读取会话…");
  void loadSessions();
  return false;
}
function predictionState(message, retry = false) {
  const body = byId("replyPredictionBody");
  const state = element("div", "reply-prediction-state", message);
  if (retry) {
    const button = element("button", "reply-prediction-retry", "重试");
    button.type = "button";
    button.addEventListener("click", () => { void requestReplyPrediction(); });
    state.appendChild(button);
  }
  body.replaceChildren(state);
}
async function predictionApi(payload, signal) {
  const response = await fetch("/api/predict-reply", {
    method: "POST", signal, headers: { "Content-Type": "application/json" }, body: JSON.stringify(payload)
  });
  let data = null;
  try { data = await response.json(); } catch { }
  if (!response.ok || data?.error) {
    const error = new Error("Prediction request failed");
    error.status = response.status;
    error.code = typeof data?.code === "string" ? data.code : typeof data?.error === "string" ? data.error :
      typeof data?.error?.code === "string" ? data.error.code : "";
    throw error;
  }
  return data;
}
async function requestReplyPrediction() {
  if (!canPredictReply() || chatState.view !== "chat") return;
  clearReplyPrediction();
  const token = ++replyPredictionRequest;
  const user = chatState.currentUser;
  const account = chatState.currentAccount;
  const lastMessageId = String(chatState.messages.at(-1).id);
  const draft = byId("chatInput").value.trim();
  const requestId = globalThis.crypto?.randomUUID?.() || `predict-${Date.now()}-${token}`;
  const payload = { user, account, requestId, expectedLastMessageId: lastMessageId };
  if (draft) payload.draft = draft;
  const requestController = new AbortController();
  replyPredictionController = requestController;
  const card = byId("replyPrediction");
  card.hidden = false;
  card.dataset.lastMessageId = lastMessageId;
  placeReplyPrediction();
  predictionState("正在预测…");
  updatePredictReplyAvailability();
  try {
    const data = await predictionApi(payload, requestController.signal);
    if (token !== replyPredictionRequest || requestController.signal.aborted) return;
    if (chatState.currentUser !== user || chatState.currentAccount !== account || String(chatState.messages.at(-1)?.id ?? "") !== lastMessageId ||
      byId("chatInput").value.trim() !== draft) { clearReplyPrediction(); return; }
    if (data.user !== user || data.account !== account || data.requestId !== requestId ||
      String(data.lastMessageId) !== lastMessageId || typeof data.basisFingerprint !== "string" || !data.basisFingerprint ||
      !Array.isArray(data.candidates) || data.candidates.length !== 3 ||
      data.candidates.some(item => !item || typeof item.label !== "string" || !item.label.trim() ||
        typeof item.description !== "string" || !item.description.trim())) throw new Error("Invalid prediction response");
    const body = byId("replyPredictionBody");
    body.replaceChildren(...data.candidates.map(candidate => {
      const row = element("div", "reply-prediction-item");
      row.append(element("span", "reply-prediction-label", candidate.label),
        element("span", "reply-prediction-description", candidate.description));
      return row;
    }));
  } catch (error) {
    if (error.name !== "AbortError" && token === replyPredictionRequest && chatState.currentUser === user && chatState.currentAccount === account) {
      if (error.code === "account-changed" || accountChangedError(error) || accountUnavailableError(error)) {
        if (error.code === "account-changed") {
          resetAccountView("账号已变化，正在读取会话…");
          void loadSessions();
        } else handleAccountBoundaryError(error);
        return;
      }
      const message = error.code === "content-too-long" ? "内容太长，请缩短草稿" :
        ["no-context", "no-text-context"].includes(error.code) ? "暂无可用文字" :
          error.status === 409 ? "消息已变化，请重试" : "预测失败，请重试";
      predictionState(message, !["content-too-long", "no-context", "no-text-context"].includes(error.code));
    }
  } finally {
    if (token === replyPredictionRequest) {
      replyPredictionController = null;
      updatePredictReplyAvailability();
    }
  }
}
function renderSessions() {
  const container = byId("sessionList");
  const query = byId("searchInput").value.trim().toLowerCase();
  const existing = new Map([...container.children].filter(node => node.classList.contains("session-item")).map(node => [node.dataset.id, node]));
  let visible = 0;
  for (const session of chatState.sessions.values()) {
    if (!chatState.selectedConversations.has(session.username)) continue;
    if (!`${session.name || ""} ${session.preview || ""}`.toLowerCase().includes(query)) continue;
    let item = existing.get(session.username);
    if (!item) {
      item = element("div", "session-item");
      item.dataset.id = session.username;
      const avatarWrap = element("div", "session-avatar-wrap");
      const info = element("div", "session-info");
      const top = element("div", "session-top");
      top.append(element("span", "session-name"), element("span", "session-time"));
      const bottom = element("div", "session-bottom");
      bottom.appendChild(element("span", "session-preview"));
      info.append(top, bottom);
      item.append(avatarWrap, info);
      item.addEventListener("click", () => {
        if (!chatState.messageSourceReady) {
          text("chatTitle", chatState.sessions.get(item.dataset.id)?.name || item.dataset.id);
          status(byId("chatMessages"), "聊天记录尚未就绪，正在重试…");
          return;
        }
        markSessionAsRead(item.dataset.id);
        switchSession(item.dataset.id);
      });
    }
    item.classList.toggle("active", session.username === chatState.currentUser);
    const avatarWrap = item.querySelector(".session-avatar-wrap");
    const avatarSignature = JSON.stringify([session.avatar, session.avatarCandidates, session.name || session.username, session.isGroup]);
    if (item.dataset.avatarSignature !== avatarSignature) {
      avatarWrap.querySelector(".avatar-frame")?.remove();
      avatarWrap.prepend(avatar(session.avatar, "session-avatar", session.avatarCandidates, session.name || session.username, session.isGroup));
      item.dataset.avatarSignature = avatarSignature;
    }
    const unread = getVisibleUnreadCount(session);
    const badge = avatarWrap.querySelector(".session-unread-dot");
    if (unread > 0) {
      const label = unread > 99 ? "99+" : unread > 9 ? "9+" : String(unread);
      if (badge) { if (badge.textContent !== label) badge.textContent = label; }
      else avatarWrap.appendChild(element("span", "session-unread-dot", label));
    } else badge?.remove();
    const name = item.querySelector(".session-name");
    const displayName = session.name || session.username;
    if (name.dataset.label !== displayName || name.dataset.pinned !== String(!!session.pinned)) {
      name.replaceChildren(document.createTextNode(displayName));
      if (session.pinned) name.appendChild(element("span", "session-pinned", "置顶"));
      name.dataset.label = displayName;
      name.dataset.pinned = String(!!session.pinned);
    }
    const timeNode = item.querySelector(".session-time");
    const displayTime = time(session.time);
    if (timeNode.textContent !== displayTime) timeNode.textContent = displayTime;
    const preview = item.querySelector(".session-preview");
    if (preview.textContent !== (session.preview || "")) preview.textContent = session.preview || "";
    if (container.children[visible] !== item) container.insertBefore(item, container.children[visible] || null);
    visible++;
  }
  while (container.children.length > visible) container.lastElementChild.remove();
  if (!visible && chatState.sessions.size && !chatState.selectedConversations.size) {
    container.replaceChildren();
    const empty = element("div", "session-empty");
    empty.appendChild(element("strong", "", "还没有添加会话"));
    empty.appendChild(element("span", "", "从信息列表选择要查看的聊天"));
    const button = element("button", "settings-action-btn", "打开信息列表");
    button.type = "button";
    button.addEventListener("click", openConversationManager);
    empty.appendChild(button);
    container.appendChild(empty);
  } else if (!visible) status(container, chatState.sessions.size ? "没有匹配的会话" : "暂无会话");
}
async function preloadSessionWindows(account, nextSessions, request) {
  const list = [...nextSessions.values()].filter(session => chatState.selectedConversations.has(session.username));
  chatState.preloadTotal = list.length;
  if (!list.length) { chatState.preloadDone = 0; return true; }
  const missing = list.filter(session => !sessionWindowReady(account, session));
  chatState.preloadDone = list.length - missing.length;
  if (startupActive) showStartup("messages", `正在准备聊天记录 ${chatState.preloadDone}/${chatState.preloadTotal}`);
  for (let offset = 0; offset < missing.length; offset += 64) {
    const batch = missing.slice(offset, offset + 64);
    const serial = ++chatState.windowRequestSerial;
    const data = await api("/api/messages/batch", { method: "POST", body: JSON.stringify({ account, users: batch.map(session => session.username) }) });
    if (request !== chatState.sessionRequest || chatState.currentAccount !== account) return false;
    if (data.account !== account) {
      const error = new Error("Batch account changed");
      error.status = 503;
      error.code = "AccountChangedError";
      throw error;
    }
    if (!Array.isArray(data.windows)) throw new Error("Invalid batch response");
    const expected = new Map(batch.map(session => [session.username, session]));
    const seen = new Set();
    for (const window of data.windows) {
      if (!window || !expected.has(window.user) || seen.has(window.user) || !Array.isArray(window.messages)) throw new Error("Invalid batch window");
      seen.add(window.user);
      cacheSessionWindow(account, expected.get(window.user), window.messages, serial, window.hasMoreBefore);
    }
    chatState.preloadDone = list.filter(session => sessionWindowReady(account, session)).length;
    if (startupActive) showStartup("messages", `正在准备聊天记录 ${chatState.preloadDone}/${chatState.preloadTotal}`);
    if (seen.size !== batch.length) throw new Error("Incomplete batch response");
  }
  return true;
}
async function loadConversationSelection(account, request) {
  if (chatState.selectionLoadedAccount === account) return;
  const state = await api("/api/conversation-selection");
  if (request !== chatState.sessionRequest || accountClearedExiting) return;
  if (state?.account !== account || !Array.isArray(state.selectedSessions) ||
      state.selectedSessions.some(id => typeof id !== "string"))
    throw new Error("Invalid conversation selection response");
  chatState.selectedConversations.clear();
  for (const id of state.selectedSessions) chatState.selectedConversations.add(id);
  chatState.selectionLoadedAccount = account;
}
function clearUnselectedConversation() {
  if (chatState.currentUser) {
    cacheCurrentSession({ messages: chatState.messages, results: labelState.results, mood: chatState.conversationMood,
      scrollTop: byId("chatMessages").scrollTop, followLatest: chatState.followLatest });
    cancelHistoryRequest();
    chatState.historyState = null;
    resetHistorySearch();
    clearReplyPrediction();
    chatState.controller?.abort();
    chatState.controller = null;
    chatState.advance("generation");
    portraitState.profileGeneration++;
    cancelApiInsightWork();
    cancelApiPortraitPoll();
    chatState.currentUser = null;
    chatState.messages = [];
    labelState.results = {};
    chatState.conversationMood = null;
    portraitState.activeMember = "";
    clearProfileView("人物画像");
  }
  text("chatTitle", "聊天");
  status(byId("chatMessages"), "从信息列表选择会话");
  byId("btnChatHistory").disabled = true;
  updateHistoryNavigation();
  renderMood();
  switchView("chat");
}
function renderConversationManager() {
  const list = byId("conversationManagerList");
  const query = byId("conversationSearch").value.trim().toLowerCase();
  text("conversationManagerCount", `已添加 ${chatState.selectedConversations.size} / ${chatState.sessions.size} 个会话`);
  list.replaceChildren();
  for (const session of chatState.sessions.values()) {
    if (!`${session.name || ""} ${session.username}`.toLowerCase().includes(query)) continue;
    const row = element("div", "conversation-manager-row");
    row.appendChild(avatar(session.avatar, "session-avatar", session.avatarCandidates,
      session.name || session.username, session.isGroup));
    row.appendChild(element("strong", "", session.name || session.username));
    const selected = chatState.selectedConversations.has(session.username);
    const button = element("button", "settings-action-btn", selected ? "从列表移除" : "添加");
    button.type = "button";
    button.disabled = chatState.conversationSelectionBusy;
    button.addEventListener("click", () => { void toggleConversationSelected(session.username); });
    row.appendChild(button);
    list.appendChild(row);
  }
  for (const id of chatState.selectedConversations) if (!chatState.sessions.has(id) &&
      (!query || id.toLowerCase().includes(query))) {
    const row = element("div", "conversation-manager-row");
    row.appendChild(element("strong", "", "已保存但当前不可见的会话"));
    const button = element("button", "settings-action-btn", "从列表移除");
    button.type = "button";
    button.disabled = chatState.conversationSelectionBusy;
    button.addEventListener("click", () => { void toggleConversationSelected(id); });
    row.appendChild(button);
    list.appendChild(row);
  }
  if (!list.children.length) status(list, chatState.sessions.size ? "没有匹配的会话" : "会话目录尚未就绪");
}
function openConversationManager() {
  if (!byId("settingsModal").classList.contains("show")) byId("btnSettings").click();
  document.querySelector('.settings-tab-btn[data-tab="general"]')?.click();
  byId("conversationManager").hidden = false;
  byId("settingsModal").querySelector(".settings-modal-card").classList.add("conversation-open");
  text("btnManageConversations", "收起");
  renderConversationManager();
}
async function toggleConversationSelected(user) {
  const account = chatState.currentAccount;
  if (!account || chatState.conversationSelectionBusy || !chatState.selectionLoadedAccount ||
      (!chatState.sessions.has(user) && !chatState.selectedConversations.has(user))) return;
  const selected = !chatState.selectedConversations.has(user);
  chatState.conversationSelectionBusy = true;
  text("conversationManagerStatus", selected ? "正在添加…" : "正在移除…");
  renderConversationManager();
  try {
    const state = await api("/api/conversation-selection", { method: "POST", body: JSON.stringify({
      expectedAccount: account, session: user, selected,
    }) });
    if (account !== chatState.currentAccount || state?.account !== account ||
        !Array.isArray(state.selectedSessions) ||
        state.selectedSessions.includes(user) !== selected) throw new Error("选择结果不匹配");
    chatState.selectedConversations.clear();
    for (const id of state.selectedSessions) chatState.selectedConversations.add(id);
    renderSessions();
    if (selected && chatState.messageSourceReady && chatState.sessions.has(user)) {
      if (!chatState.currentUser) switchSession(user);
      else void preloadSessionWindows(account, new Map([[user, chatState.sessions.get(user)]]), chatState.sessionRequest)
        .catch(() => text("conversationManagerStatus", "已添加，消息将在点开会话时读取"));
    } else if (!selected && chatState.currentUser === user) {
      const next = [...chatState.sessions.keys()].find(id => chatState.selectedConversations.has(id));
      if (next) switchSession(next);
      else clearUnselectedConversation();
    }
    text("conversationManagerStatus", selected ? "已添加" : "已从列表移除");
  } catch {
    text("conversationManagerStatus", "操作失败，请重试");
  } finally {
    chatState.conversationSelectionBusy = false;
    renderConversationManager();
  }
}
async function loadSessions(retryChanged = true) {
  if (accountClearedExiting) return;
  if (chatState.sessionLoading) { chatState.sessionRefreshQueued = true; return; }
  chatState.sessionLoading = true;
  let request = ++chatState.sessionRequest;
  let followup = null;
  try {
    const data = await api("/api/sessions");
    if (request !== chatState.sessionRequest || accountClearedExiting) return;
    if (typeof data.account !== "string" || !data.account || !Array.isArray(data.sessions)) throw new Error("Invalid sessions response");
    const signature = JSON.stringify([data.self, data.sessions, data.account]);
    if (data.messagesReady === false) {
      for (const key of portraitState.storedProfileSnapshots.keys()) {
        try {
          const scope = JSON.parse(key);
          if (scope[0] === data.account && scope.length === 3) portraitState.profileSnapshotsRequireRefresh.add(key);
        }
        catch { }
      }
      if (chatState.messageSourceReady || chatState.currentAccount !== data.account)
        resetAccountView("聊天记录尚未就绪", false);
      request = chatState.sessionRequest;
      await loadConversationSelection(data.account, request);
      if (request !== chatState.sessionRequest || accountClearedExiting) return;
      accountUnavailable = false;
      chatState.currentAccount = data.account;
      chatState.messageSourceReady = false;
      clearTimeout(startupAccountRetryTimer);
      startupAccountRetryTimer = null;
      chatState.self = data.self || null;
      setAvatar("selfAvatar", chatState.self?.avatar, chatState.self?.avatarCandidates, chatState.self?.name || "我");
      chatState.sessions.clear();
      for (const session of data.sessions) if (session?.username) chatState.sessions.set(session.username, session);
      chatState.sessionSignature = signature;
      chatState.currentUser = null;
      byId("btnChatHistory").disabled = true;
      chatState.messages = [];
      labelState.results = {};
      chatState.conversationMood = null;
      status(byId("chatMessages"), "聊天记录尚未就绪，正在重试…");
      text("analysisStatus", "");
      switchView("chat");
      renderSessions();
      if (!byId("conversationManager").hidden) renderConversationManager();
      completeStartup();
      accountCheckStatus("账号已连接，聊天记录校验中", true);
      return;
    }
    const wasPartial = !chatState.messageSourceReady;
    const accountChanged = chatState.currentAccount !== data.account;
    if (accountChanged && chatState.currentAccount !== null) {
      resetAccountView("正在读取会话…");
      request = ++chatState.sessionRequest;
    }
    await loadConversationSelection(data.account, request);
    if (request !== chatState.sessionRequest || accountClearedExiting) return;
    accountUnavailable = false;
    chatState.currentAccount = data.account;
    chatState.messageSourceReady = true;
    clearTimeout(startupAccountRetryTimer);
    startupAccountRetryTimer = null;
    const nextSessions = new Map();
    for (const session of data.sessions) if (session?.username) nextSessions.set(session.username, session);
    if (!wasPartial && signature === chatState.sessionSignature &&
        [...nextSessions.values()].filter(session => chatState.selectedConversations.has(session.username))
          .every(session => sessionWindowReady(data.account, session))) {
      completeStartup();
      return;
    }
    const scroll = byId("sessionList").scrollTop;
    chatState.self = data.self || null;
    setAvatar("selfAvatar", chatState.self?.avatar, chatState.self?.avatarCandidates, chatState.self?.name || "我");
    if (!nextSessions.size) {
      chatState.sessions.clear();
      chatState.sessionSignature = signature;
      renderSessions();
      if (!byId("conversationManager").hidden) renderConversationManager();
      clearUnselectedConversation();
      completeStartup();
      return;
    }
    chatState.sessions.clear();
    for (const [user, session] of nextSessions) chatState.sessions.set(user, session);
    chatState.sessionSignature = signature;
    pruneSessionCache(data.account, nextSessions);
    renderSessions();
    if (!byId("conversationManager").hidden) renderConversationManager();
    byId("sessionList").scrollTop = scroll;
    if (!await preloadSessionWindows(data.account, nextSessions, request)) return;
    if (request !== chatState.sessionRequest || chatState.currentAccount !== data.account) return;
    let remembered = null;
    try { remembered = localStorage.getItem(`last-conversation:${chatState.currentAccount}`); } catch {}
    const selected = chatState.selectedConversations.has(chatState.currentUser) && chatState.sessions.has(chatState.currentUser) ? chatState.currentUser :
      chatState.selectedConversations.has(remembered) && chatState.sessions.has(remembered) ? remembered :
      [...chatState.sessions.keys()].find(id => chatState.selectedConversations.has(id));
    if (!selected) clearUnselectedConversation();
    else if (selected !== chatState.currentUser) switchSession(selected, accountChanged);
    if (chatState.currentUser && chatState.sessions.has(chatState.currentUser)) text("chatTitle", chatState.sessions.get(chatState.currentUser).name || chatState.currentUser);
    if (!accountChanged && chatState.currentUser && chatState.view === "persona") void loadProfile(portraitState.activeMember);
    completeStartup();
  } catch (error) {
    if (request !== chatState.sessionRequest) return;
    if (accountChangedError(error)) {
      resetAccountView("账号已变化，正在读取会话…");
      if (retryChanged !== false) followup = false;
      else showStartup("account", "账号已变化，请重试", { retry: true });
    } else if (accountUnavailableError(error)) {
      const autoRetry = startupActive && !startupAccountRetryUsed;
      if (!accountUnavailable || chatState.currentAccount !== null || chatState.sessions.size) resetAccountView("当前账号未就绪");
      accountUnavailable = true;
      status(byId("sessionList"), "当前账号未就绪", () => { void loadSessions(); });
      if (typeof window !== "undefined" && window.QQStartup) {
        showStartup("account", "选择本地 QQ 账号，或进入设置", { retry: true, continueEmpty: true });
        await window.QQStartup.show(api);
        return;
      }
      if (autoRetry) {
        startupAccountRetryUsed = true;
        showStartup("account", "正在重新读取账号…");
        const attempt = startupAttempt;
        const pendingRequest = chatState.sessionRequest;
        startupAccountRetryTimer = setTimeout(() => {
          startupAccountRetryTimer = null;
          if (startupActive && startupAttempt === attempt && chatState.sessionRequest === pendingRequest &&
              accountUnavailable && !accountClearedExiting) {
            void loadSessions();
          }
        }, 2500);
      } else showStartup("account", "当前账号未就绪", { retry: true, continueEmpty: true });
    } else if (contactSnapshotStaleError(error)) {
      if (chatState.currentAccount !== null || chatState.sessions.size || !startupActive) {
        resetAccountView("联系人资料更新中…", true);
      }
      status(byId("sessionList"), "联系人资料更新中…", () => { void loadSessions(); });
      showStartup("sessions", "联系人资料更新中…", { retry: true });
      const pendingRequest = chatState.sessionRequest;
      setTimeout(() => {
        if (pendingRequest === chatState.sessionRequest && !accountClearedExiting) void loadSessions();
      }, 2000);
    } else {
      if (!chatState.sessions.size && !accountUnavailable) status(byId("sessionList"), "会话读取失败，请重试", () => { void loadSessions(); });
      showStartup(chatState.currentAccount && chatState.preloadTotal ? "messages" : "sessions",
        chatState.currentAccount && chatState.preloadTotal ? `聊天记录准备失败 ${chatState.preloadDone}/${chatState.preloadTotal}，请重试` : "会话读取失败，请重试", { retry: true });
    }
  } finally {
    chatState.sessionLoading = false;
    const queued = chatState.sessionRefreshQueued;
    chatState.sessionRefreshQueued = false;
    if (followup !== null) void loadSessions(followup);
    else if (queued) void loadSessions();
  }
}
function renderMood() {
  const mood = chatState.conversationMood;
  const moodLabel = displayEmotionLabel(mood);
  const face = mood?.label ? window.Kaomoji.pick(mood, `${chatState.currentUser}:history:${mood.label}`) || String(mood.kaomoji || "").trim() : "";
  byId("chatStatusPill").style.display = settingsState.settings.intent && mood?.label ? "inline-flex" : "none";
  text("headerMoodLabel", chatState.sessions.get(chatState.currentUser)?.isGroup ? "群聊氛围" : "人物情绪");
  text("headerMoodKaomoji", face || moodLabel);
  byId("chatStatusPill").title = moodLabel ? `${moodLabel} · ${Number(mood.sampleCount) || 0} 条已分析文本` : "";
}
function rankedScores(values) {
  if (!Array.isArray(values)) return [];
  return values.map((item, index) => ({ item, index, probability: Number(item?.probability) }))
    .filter(({ item, probability }) => item?.label && Number.isFinite(probability) && probability > 0 && probability <= 1)
    .sort((left, right) => right.probability - left.probability || left.index - right.index);
}
function intentAlias(value) {
  return String(value || "").trim().toLowerCase().replace(/\s+/g, "_");
}
function displayEmotionLabel(item) {
  for (const value of [item?.rawLabel, item?.id, item?.modelLabel, item?.label]) {
    const display = labelState.emotionDisplayAliases.get(intentAlias(value));
    if (display) return display;
  }
  return String(item?.label || item?.rawLabel || "").trim();
}
function rankedEmotionScores(values) {
  return rankedScores(values).map(({ item, ...rank }) => ({
    ...rank, item: { ...item, label: displayEmotionLabel(item) },
  }));
}
const ROUTINE_MESSAGE_TEXT = /^(?:收到(?:了)?|明白(?:了)?|知道(?:了)?|完成了|搞定(?:了|啦)?|同意|确认|状态报告|告知事实|分享|闲聊|一般交流|好(?:的|呀|啊|了)?|行(?:的|呀|啊|了)?|嗯+)(?:[。！!，,~～\s]*)$/u;
function displayedEmotion(values, messageText) {
  if (ROUTINE_MESSAGE_TEXT.test(String(messageText || "").trim())) return [];
  const hidden = new Set(["自然", "随和", "坦诚"]);
  const scores = rankedEmotionScores(values).filter(({ item }) => !hidden.has(String(item.label)));
  const top = scores[0];
  if (!top || top.probability < 0.30) return [];
  const second = scores[1];
  if (second && top.probability - second.probability < 0.15) return [];
  return [top];
}
function hasIntentContent(messageText) {
  // Punctuation-only messages can carry tone or intent (for example “？” or
  // “。。”). Treat every non-whitespace message as analyzable; the model and
  // the existing evidence rules decide whether a label is warranted.
  return String(messageText || "").trim().length > 0;
}
function isIncompleteFragment(messageText) {
  return /^(?:这|那|我|你|你这|这个|那个)(?:就)?是[，,。！!…\s]*$/u.test(String(messageText || "").trim());
}
// Plain acknowledgements / status reports display blank unless the text turns or asks for
// something. This is an explicit short-text rule, not a global label blacklist: a normal
// message keeps its candidates, and only pure acknowledgement labels stay hideable.
const PLAIN_ACK_TEXT = /^(?:嗯+|哦+|好(?:的|呀|啊|了|吧)?|行(?:的|呀|啊|了|吧)?|可以(?:的|呀|啊|吧)?|收到(?:了)?|明白(?:了)?|知道(?:了)?|搞定(?:了|啦)?|没问题|ok|okay)(?:[。！!，,~～\s]*)$/iu;
const PLAIN_STATUS_TEXT = /^(?:已经)?(?:完成|做好|处理完|弄好|弄完|上传|提交|发送|到了|搞定)(?:了)?(?:[。！!，,~～\s]*)$/u;
const PLAIN_ACTION_LABELS = new Set(["确认", "同意", "状态报告", "一般交流"]);
// These are useful internal candidates, but ordinary messages must not expose them
// as if they carried a meaningful social decision. A salient cue keeps real sharing,
// questions, requests and decisions visible.
const LOW_SIGNAL_INTENT_LABELS = new Set(["闲聊", "一般交流", "确认", "同意", "状态报告", "告知事实", "分享", "表达感受"]);
const SALIENT_INTENT_CUE = /[？?]|帮我|请|能不能|可不可以|麻烦|要不要|一起|改天|下次|不行|不了|拒绝|不想|不愿|建议|因为|所以|解释|澄清|计划|准备|打算|邀请|邀约|改期|发给我|给你看|喜欢|在乎|抱歉|谢谢|求助|安慰|陪我|检查|查看|看看|确认一下|\bcheck\b/iu;
const PLAIN_TURN = /[？?]|但是|不过|可是|其实|然而|而且|顺便|另外|请你|帮我|能不能|可不可以|麻烦|建议|要不要|一起/u;
function plainAcknowledgementOnly(messageText, candidates) {
  const text = String(messageText || "").trim();
  if (!text || PLAIN_TURN.test(text)) return false;
  if (!PLAIN_ACK_TEXT.test(text) && !PLAIN_STATUS_TEXT.test(text)) return false;
  return candidates.length > 0 &&
    candidates.every(item => PLAIN_ACTION_LABELS.has(String(item.label)));
}
function lowSignalIntentOnly(messageText, candidates) {
  const text = String(messageText || "").trim();
  return !!text && !SALIENT_INTENT_CUE.test(text) && candidates.length > 0 &&
    candidates.every(item => LOW_SIGNAL_INTENT_LABELS.has(String(item.label)));
}
function displayedIntent(result, messageText) {
  if (!hasIntentContent(messageText) || isIncompleteFragment(messageText)) return [];
  const ranked = Array.isArray(result.intent) ? result.intent
    .filter(item => typeof item?.rawLabel === "string" &&
      Object.prototype.hasOwnProperty.call(GENERIC_INTENT_LABELS, item.rawLabel) &&
      typeof item.probability === "number" && Number.isFinite(item.probability) &&
      item.probability >= 0 && item.probability <= 1)
    .sort((left, right) => right.probability - left.probability) : [];
  const modelCandidates = ranked.map(item => ({
    label: GENERIC_INTENT_LABELS[item.rawLabel], probability: item.probability,
  }));
  const grounded = result.groundedIntent;
  const shown = grounded && Object.prototype.hasOwnProperty.call(GROUNDED_EVIDENCE, grounded.label) &&
      GROUNDED_EVIDENCE[grounded.label].includes(grounded.evidenceKind)
    ? [{ label: GENERIC_INTENT_LABELS[grounded.label], probability: null }]
    : modelCandidates.length && modelCandidates[0].probability >= 0.50
      ? [modelCandidates[0]] : [];
  return plainAcknowledgementOnly(messageText, shown) || lowSignalIntentOnly(messageText, shown) ? [] : shown;
}
labelState.messageLabels = null;
function messageLabelsApi() {
  if (!labelState.messageLabels) {
    labelState.messageLabels = window.MessageLabels.create({
      element,
      showDetails: window.ProductConfig?.key === "qq",
      detailedMode: () => settingsState.settings.labelDetails,
      pickKaomoji: (item, id) => window.Kaomoji?.pick(item, id) || "",
      sourceLabel: source => source.kind === "laya" ? "本地 Laya" :
        "API · " + (settingsState.modelSourceSnapshot.api?.model || "已配置模型"),
      isCurrentScope: detail => settingsState.modelSourceResolved && detail.scope.accountKey === chatState.currentAccount &&
        detail.scope.conversationKey === chatState.currentUser &&
        detail.modelSource.id === settingsState.modelSourceSnapshot.sourceId,
    });
  }
  return labelState.messageLabels;
}
function appendScoreLine(container, label, scores, messageId, emotion = false) {
  return messageLabelsApi().appendScoreLine(container, label, scores, messageId, emotion);
}
function appendIntentLine(container, candidates) {
  return messageLabelsApi().appendIntentLine(container, candidates);
}
function messageInsightView(result, text) {
  const emotionPicker = typeof displayedEmotion === "function" ? displayedEmotion : rankedEmotionScores;
  return window.MessageInsightAdapters.localView(result, text, {
    rankedEmotionScores, displayedEmotion: emotionPicker, displayedIntent, hasIntentContent, isIncompleteFragment,
    labelSchema: CURRENT_LABEL_SCHEMA,
  });
}
function messageDetailContext(kind, id) {
  return { modelSource: { kind, id: settingsState.modelSourceSnapshot.sourceId,
    status: settingsState.modelSourceSnapshot.status || "active" },
    scope: { accountKey: chatState.currentAccount, conversationKey: chatState.currentUser, messageKey: String(id) } };
}
function clearInlineIntentPending() {
  labelState.inlineIntentPending.clear();
  labelState.inlineIntentJobId = null;
}
function startInlineIntentPending(window) {
  for (const message of uncoveredMessages(window)) {
    const id = String(message.id);
    labelState.inlineIntentPending.set(id, message.text);
  }
  labelState.inlineIntentJobId = null;
  refreshLabels();
}
function settleInlineIntentPending(job) {
  if (!labelState.inlineIntentPending.size) return;
  let changed = false;
  const visible = new Map(chatState.messages.map(message => [String(message.id), message]));
  const recentFinished = !!labelState.inlineIntentJobId && job?.recent?.id === labelState.inlineIntentJobId &&
    ["done", "error"].includes(job.recent.status);
  const jobFailed = job?.status === "error" && !["queued", "running"].includes(job.recent?.status);
  const terminal = recentFinished || jobFailed;
  for (const [id, pendingText] of labelState.inlineIntentPending) {
    const state = fineMessageResult(labelState.results[id]) ? labelState.results[id].state : null;
    if (terminal || state === "skipped" ||
        (state === "done" && labelState.results[id]?.labelSchema === CURRENT_LABEL_SCHEMA) ||
        visible.get(id)?.text !== pendingText) {
      labelState.inlineIntentPending.delete(id);
      changed = true;
    }
  }
  if (terminal || !labelState.inlineIntentPending.size) labelState.inlineIntentJobId = null;
  if (changed) refreshLabels();
}
function updateLabel(message, node) {
  const wrap = node.querySelector(".msg-content-wrap");
  if (!settingsState.modelSourceResolved || settingsState.modelSourceSnapshot.mode === "api") {
    updateApiInsightLabel(message, node, wrap);
    return;
  }
  const result = labelState.results[message.id];
  const eligible = settingsState.settings.intent && labelSideEligible(message) && message.kind === "text" &&
    typeof message.text === "string" && !!message.text.trim() && !isIncompleteFragment(message.text);
  const pendingText = labelState.inlineIntentPending.get(String(message.id));
  const pending = eligible && !chatState.historyState && pendingText === message.text &&
    !(fineMessageResult(result) && (result.state === "skipped" ||
      result.state === "done" && result.labelSchema === CURRENT_LABEL_SCHEMA));
  const signature = pending ? "local:pending" : eligible && fineMessageResult(result) && result.state === "done" ?
    `local:${JSON.stringify([result.emotion, result.intentBroad, result.intent, result.groundedIntent,
      result.labelSchema, labelState.catalogReady, labelState.catalogLabelRevision, !!settingsState.settings.labelDetails])}` : "";
  if (node.dataset.analysisSignature === signature) return;
  const revealing = signature !== "local:pending" && !!wrap.querySelector(".inline-intent-pending");
  node.querySelector(".msg-avatar-column .msg-mood")?.remove();
  wrap.querySelector(".inline-expression-row")?.remove();
  wrap.querySelector(".inline-intent-row")?.remove();
  wrap.querySelector(".inline-intent-pending")?.remove();
  node.dataset.analysisSignature = signature;
  if (!signature) return;
  if (signature === "local:pending") {
    wrap.appendChild(element("div", "inline-intent-pending", "分析中"));
    return;
  }
  const row = messageLabelsApi().render(messageInsightView(result, message.text), message.id,
    window.MessageInsightAdapters.localDetails(result, messageDetailContext("laya", message.id)));
  if (row.childNodes.length) {
    if (revealing) row.classList.add("inline-intent-revealed");
    wrap.appendChild(row);
  }
}
function messageBubble(message) {
  const emptyQQText = window.ProductConfig?.key === "qq" && message.kind === "text" &&
    (typeof message.text !== "string" || !message.text.trim());
  const legacy = message.kind === "image" ? "[图片]" :
    message.kind === "text" ? emptyQQText ? "[空白文本]" : message.text || "" : message.text || "[不支持的消息]";
  const details = message.qqDisplay;
  if (window.ProductConfig?.key !== "qq" || !details || !Array.isArray(details.parts))
    return element("div", "msg-bubble", legacy);
  const bubble = element("div", "msg-bubble qq-message-bubble");
  if (details.hasQuote === true) {
    const quote = element("div", "qq-message-quote");
    quote.appendChild(element("div", "qq-message-quote-title", "引用"));
    quote.appendChild(element("div", "qq-message-quote-text", typeof details.quoteText === "string"
      ? Array.from(details.quoteText).slice(0, 4000).join("") : "引用内容未保存在本机"));
    if (details.quoteTruncated) quote.appendChild(element("div", "qq-message-note", "引用过长，仅显示前 4000 字"));
    bubble.appendChild(quote);
  }
  const names = { image: "图片", audio: "语音", file: "文件", video: "视频", face: "表情", unknown: "未知类型" };
  for (const kind of [...new Set(details.parts)].slice(0, 6)) {
    if (!Object.hasOwn(names, kind)) continue;
    bubble.appendChild(element("div", "qq-message-placeholder",
      `[${names[kind]}]${kind === "unknown" ? " · 内容暂不支持" : " · 内容未读取"}`));
  }
  if (message.kind === "text" && !emptyQQText && typeof message.text === "string" && message.text)
    bubble.appendChild(element("div", "qq-message-body", message.text));
  if (!bubble.childNodes.length) bubble.textContent = legacy;
  return bubble;
}
function messageNode(message) {
  const session = chatState.sessions.get(chatState.currentUser);
  const item = element("div", `msg-item ${message.side === "self" ? "outgoing" : "incoming"}`);
  item.dataset.messageId = String(message.id);
  const avatarColumn = element("div", "msg-avatar-column");
  avatarColumn.appendChild(message.side === "self" ? avatar(chatState.self?.avatar, "msg-avatar", chatState.self?.avatarCandidates, chatState.self?.name || "我") : avatar(message.senderAvatar || (session?.isGroup ? null : session?.avatar), "msg-avatar", message.senderAvatarCandidates || (session?.isGroup ? [] : session?.avatarCandidates), message.senderName || session?.name || message.senderId, session?.isGroup && !message.senderId));
  item.appendChild(avatarColumn);
  const wrap = element("div", "msg-content-wrap");
  if (window.ChatBeanUI) wrap.appendChild(window.ChatBeanUI.messageHeading(message, session, chatState.self));
  else if (session?.isGroup && message.side !== "self") wrap.appendChild(element("span", "msg-sender", message.senderName || message.senderId || "未知成员"));
  wrap.appendChild(messageBubble(message));
  if (window.ProductConfig?.key === "qq" && window.QQProvenanceUI && message.historyCursor)
    wrap.appendChild(window.QQProvenanceUI.create({ document, api,
      getScope: () => ({ account: chatState.currentAccount, user: chatState.currentUser, generation: chatState.generation }),
      cursor: message.historyCursor }));
  item.appendChild(wrap);
  updateLabel(message, item);
  return item;
}
function renderMessages(next, restoreScroll = null, historyAnchor = null) {
  const container = byId("chatMessages");
  const previousMessages = chatState.messages;
  const oldIds = chatState.messages.map(message => String(message.id));
  const nextIds = next.map(message => String(message.id));
  const appendOnly = oldIds.length && oldIds.length <= nextIds.length && oldIds.every((id, index) => id === nextIds[index] && JSON.stringify(chatState.messages[index]) === JSON.stringify(next[index])) && container.querySelectorAll(".msg-item").length === oldIds.length;
  const atBottom = container.scrollHeight - container.scrollTop - container.clientHeight < 80;
  chatState.followLatest = historyAnchor ? false : restoreScroll ? restoreScroll.followLatest : atBottom || !oldIds.length;
  const scroll = container.scrollTop;
  const anchor = !atBottom && !appendOnly ? [...container.querySelectorAll(".msg-item")].find(node => node.getBoundingClientRect().bottom >= container.getBoundingClientRect().top) : null;
  const anchorId = anchor?.dataset.messageId;
  const anchorOffset = anchor ? anchor.getBoundingClientRect().top - container.getBoundingClientRect().top : 0;
  const start = appendOnly ? oldIds.length : 0;
  const fragment = document.createDocumentFragment();
  try {
    chatState.messages = next;
    for (let index = start; index < next.length; index++) {
      const message = next[index];
      const previous = next[index - 1];
      if (!previous || Number(message.time) - Number(previous.time) > 300000) {
        const row = element("div", "msg-time-row");
        row.appendChild(element("span", "msg-time", time(message.time)));
        fragment.appendChild(row);
      }
      fragment.appendChild(messageNode(message));
    }
  } catch (error) {
    chatState.messages = previousMessages;
    throw error;
  }
  if (!byId("replyPrediction").hidden) clearReplyPrediction();
  updatePredictReplyAvailability();
  if (!next.length) status(container, "暂无消息");
  else if (appendOnly) container.appendChild(fragment);
  else container.replaceChildren(fragment);
  if (next.length && historyAnchor) {
    const anchor = [...container.querySelectorAll(".msg-item")].find(node => node.dataset.messageId === historyAnchor.id);
    if (anchor) container.scrollTop += anchor.getBoundingClientRect().top - container.getBoundingClientRect().top - historyAnchor.top;
    chatState.lastChatScrollTop = container.scrollTop;
  } else if (next.length && restoreScroll) {
    const token = chatState.generation;
    const user = chatState.currentUser;
    requestAnimationFrame(() => {
      if (token !== chatState.generation || user !== chatState.currentUser || chatState.view !== "chat") return;
      container.scrollTop = restoreScroll.followLatest ? container.scrollHeight : restoreScroll.scrollTop;
      chatState.lastChatScrollTop = container.scrollTop;
    });
  } else if (next.length && (atBottom || !oldIds.length)) scrollToLatest();
  else if (next.length && anchorId && !appendOnly) {
    const replacement = [...container.querySelectorAll(".msg-item")].find(node => node.dataset.messageId === anchorId);
    container.scrollTop = replacement ? scroll + replacement.getBoundingClientRect().top - container.getBoundingClientRect().top - anchorOffset : scroll;
  } else container.scrollTop = scroll;
  renderMood();
  updateHistoryNavigation();
  ensureApiInsights();
}
function scrollToLatest() {
  chatState.followLatest = true;
  const token = chatState.generation;
  const user = chatState.currentUser;
  requestAnimationFrame(() => { if (chatState.view === "chat" && token === chatState.generation && user === chatState.currentUser) { const container = byId("chatMessages"); container.scrollTop = container.scrollHeight; } });
}
function refreshLabels() {
  const container = byId("chatMessages");
  const atBottom = container.scrollHeight - container.scrollTop - container.clientHeight < 80;
  const nodes = new Map([...byId("chatMessages").querySelectorAll(".msg-item")].map(node => [node.dataset.messageId, node]));
  for (const message of chatState.messages) if (nodes.has(String(message.id))) updateLabel(message, nodes.get(String(message.id)));
  if (!byId("replyPrediction").hidden) placeReplyPrediction(false);
  if (atBottom && !chatState.historyState) scrollToLatest();
  renderMood();
}
function historyAnchor(fromEnd = false) {
  const container = byId("chatMessages");
  const bounds = container.getBoundingClientRect();
  const visible = [...container.querySelectorAll(".msg-item")].filter(node => {
    const rect = node.getBoundingClientRect();
    return rect.bottom > bounds.top && rect.top < bounds.bottom;
  });
  const node = fromEnd ? visible.at(-1) : visible[0];
  return node ? { id: node.dataset.messageId, top: node.getBoundingClientRect().top - bounds.top } : null;
}
function retainQQReadingWindow(next) {
  if (window.ProductConfig?.key !== "qq" || chatState.view !== "chat" ||
      chatState.followLatest || chatState.historyState || !chatState.messages.length || !next.length) return false;
  const anchor = historyAnchor();
  if (!anchor || next.some(message => String(message.id) === anchor.id)) return false;
  const afterCursor = chatState.messages.at(-1)?.historyCursor;
  if (!afterCursor) return false;
  // The latest page has moved beyond the visible message. Keep the already read
  // page and use the formal history controls rather than replacing its contents.
  enterHistoryView();
  Object.assign(chatState.historyState, { afterCursor,
    hasMoreBefore: chatState.currentHasMoreBefore !== false, hasMoreAfter: true,
    notice: "记录已更新，已保留当前阅读位置；返回最新查看。" });
  updateHistoryNavigation();
  return true;
}
function updateHistoryNavigation() {
  const firstCursor = chatState.messages[0]?.historyCursor;
  const state = chatState.historyState;
  const container = byId("chatMessages");
  const atTop = container.scrollTop <= 80;
  const atBottom = container.scrollHeight - container.scrollTop - container.clientHeight <= 80;
  byId("historyNavigation").hidden = !chatState.currentUser || !(firstCursor || state);
  byId("btnHistoryEarlier").hidden = !firstCursor || state?.hasMoreBefore === false ||
    (!state && chatState.currentHasMoreBefore === false);
  byId("btnHistoryEarlier").disabled = !!chatState.historyController || !atTop;
  byId("btnHistoryEarlier").title = atTop ? "" : "滚动到顶部后加载";
  byId("btnHistoryNewer").hidden = !state?.hasMoreAfter;
  byId("btnHistoryNewer").disabled = !!chatState.historyController || !atBottom;
  byId("btnHistoryNewer").title = atBottom ? "" : "滚动到底部后加载";
  byId("btnReturnLatest").hidden = !state;
  text("historyNavStatus", chatState.historyController ? "读取中…" : state?.error || state?.notice ||
    (!state && firstCursor && chatState.currentHasMoreBefore === false ? "已到本机最早消息" : ""));
}
function cancelHistoryRequest() {
  chatState.historyRequest++;
  chatState.historyController?.abort();
  chatState.historyController = null;
}
function enterHistoryView() {
  if (chatState.historyState) return;
  clearTimeout(labelState.selectedAnalysisTimer);
  labelState.selectedAnalysisTimer = null;
  chatState.historyState = { beforeCursor: chatState.messages[0]?.historyCursor || null, hasMoreBefore: true, hasMoreAfter: false, error: "" };
  chatState.messageRequest++;
  chatState.messagePending = false;
  chatState.messageRefreshQueued = false;
  portraitState.analysisGeneration++;
  chatState.followLatest = false;
  clearInlineIntentPending();
  refreshLabels();
  updateHistoryNavigation();
  ensureApiInsights();
}
function historyResponseValid(data, account, user) {
  if (data?.account !== account || data?.user !== user || !Array.isArray(data.messages)) throw new Error("Invalid history response");
}
function historyUrl(account, user, key, cursor) {
  const params = new URLSearchParams({ account, user, limit: "80" });
  params.set(key, cursor);
  return `/api/history?${params}`;
}
async function loadOlderHistory() {
  if (!chatState.currentAccount || !chatState.currentUser || chatState.historyController || chatState.historyState?.hasMoreBefore === false) return;
  if (byId("chatMessages").scrollTop > 80) return;
  const initialCursor = chatState.historyState?.beforeCursor || chatState.messages[0]?.historyCursor;
  if (!initialCursor) return;
  enterHistoryView();
  const account = chatState.currentAccount, user = chatState.currentUser, token = chatState.generation, request = ++chatState.historyRequest;
  const controller = new AbortController();
  chatState.historyController = controller;
  chatState.historyState.error = "";
  updateHistoryNavigation();
  try {
    let before = initialCursor, data;
    for (;;) {
      data = await api(historyUrl(account, user, "before", before), {}, controller.signal);
      if (request !== chatState.historyRequest || token !== chatState.generation || account !== chatState.currentAccount || user !== chatState.currentUser) return;
      historyResponseValid(data, account, user);
      if (data.messages.length || !data.hasMoreBefore) break;
      if (!data.nextCursor || data.nextCursor === before) throw new Error("History cursor did not advance");
      before = data.nextCursor;
    }
    const existing = new Set(chatState.messages.map(message => String(message.id)));
    const older = data.messages.filter(message => !existing.has(String(message.id)));
    const combined = [...older, ...chatState.messages];
    const removedFromEnd = Math.max(0, combined.length - historyWindowLimit);
    const next = combined.slice(0, historyWindowLimit);
    const anchor = historyAnchor();
    labelState.results = visibleResults({ ...labelState.results, ...(data.results || {}) }, next);
    chatState.historyState.beforeCursor = data.nextCursor || older[0]?.historyCursor || before;
    chatState.historyState.hasMoreBefore = !!data.hasMoreBefore;
    chatState.historyState.hasMoreAfter ||= removedFromEnd > 0;
    chatState.historyState.notice = data.hasMoreBefore ? "" : "已到本机最早消息";
    if (older.length) renderMessages(next, null, anchor);
  } catch (error) {
    if (error.name !== "AbortError" && request === chatState.historyRequest && token === chatState.generation) chatState.historyState.error = "读取失败，请重试";
  } finally {
    if (request === chatState.historyRequest) {
      chatState.historyController = null;
      updateHistoryNavigation();
    }
  }
}
async function loadNewerHistory() {
  if (!chatState.historyState?.hasMoreAfter || chatState.historyController || !chatState.messages.length) return;
  const container = byId("chatMessages");
  if (container.scrollHeight - container.scrollTop - container.clientHeight > 80) return;
  const marker = chatState.messages.at(-1)?.historyCursor;
  if (!marker) return;
  const account = chatState.currentAccount, user = chatState.currentUser, token = chatState.generation, request = ++chatState.historyRequest;
  const controller = new AbortController();
  chatState.historyController = controller;
  chatState.historyState.error = "";
  updateHistoryNavigation();
  try {
    const data = await api(historyUrl(account, user, "around", marker), {}, controller.signal);
    if (request !== chatState.historyRequest || token !== chatState.generation || account !== chatState.currentAccount || user !== chatState.currentUser) return;
    historyResponseValid(data, account, user);
    const position = data.messages.findIndex(message => message.historyCursor === marker);
    if (position < 0) throw new Error("History anchor missing");
    const existing = new Set(chatState.messages.map(message => String(message.id)));
    const newer = data.messages.slice(position + 1).filter(message => !existing.has(String(message.id)));
    const combined = [...chatState.messages, ...newer];
    const removedFromStart = Math.max(0, combined.length - historyWindowLimit);
    const next = combined.slice(removedFromStart);
    const anchor = historyAnchor(true);
    labelState.results = visibleResults({ ...labelState.results, ...(data.results || {}) }, next);
    chatState.historyState.hasMoreBefore ||= removedFromStart > 0;
    chatState.historyState.beforeCursor = next[0]?.historyCursor || chatState.historyState.beforeCursor;
    chatState.historyState.hasMoreAfter = !!data.hasMoreAfter && newer.length > 0;
    if (newer.length) renderMessages(next, null, anchor);
  } catch (error) {
    if (error.name !== "AbortError" && request === chatState.historyRequest && token === chatState.generation) chatState.historyState.error = "读取失败，请重试";
  } finally {
    if (request === chatState.historyRequest) {
      chatState.historyController = null;
      updateHistoryNavigation();
    }
  }
}
async function jumpToHistory(cursor, messageId) {
  if (!chatState.currentAccount || !chatState.currentUser || !cursor) return;
  const previous = chatState.historyState;
  enterHistoryView();
  cancelHistoryRequest();
  const account = chatState.currentAccount, user = chatState.currentUser, token = chatState.generation, request = ++chatState.historyRequest;
  const controller = new AbortController();
  chatState.historyController = controller;
  chatState.historyState.error = "";
  updateHistoryNavigation();
  try {
    const data = await api(historyUrl(account, user, "around", cursor), {}, controller.signal);
    if (request !== chatState.historyRequest || token !== chatState.generation || account !== chatState.currentAccount || user !== chatState.currentUser) return;
    historyResponseValid(data, account, user);
    const target = data.messages.find(message => String(message.id) === String(messageId));
    if (!target) throw new Error("History target missing");
    chatState.historyState = { beforeCursor: data.nextCursor || data.messages[0]?.historyCursor || null,
      hasMoreBefore: !!data.hasMoreBefore, hasMoreAfter: !!data.hasMoreAfter, error: "" };
    labelState.results = visibleResults(data.results || {}, data.messages);
    renderMessages(data.messages, null, { id: String(messageId), top: byId("chatMessages").clientHeight / 2 });
    const hit = [...byId("chatMessages").querySelectorAll(".msg-item")].find(node => node.dataset.messageId === String(messageId));
    hit?.classList.add("history-hit");
    setTimeout(() => hit?.classList.remove("history-hit"), 2500);
  } catch (error) {
    if (error.name !== "AbortError" && request === chatState.historyRequest && token === chatState.generation) {
      chatState.historyState = previous;
      if (!previous) void loadMessages(token, true);
      toast("记录定位失败，请重试");
    }
  } finally {
    if (request === chatState.historyRequest) {
      chatState.historyController = null;
      updateHistoryNavigation();
    }
  }
}
function returnToLatest() {
  if (!chatState.historyState) return;
  cancelHistoryRequest();
  chatState.historyState = null;
  const cached = chatState.sessionCache.get(sessionCacheKey(chatState.currentAccount, chatState.currentUser));
  labelState.results = visibleResults(cached?.results || {}, cached?.messages || []);
  chatState.conversationMood = cached?.mood || null;
  if (Array.isArray(cached?.messages)) renderMessages(cached.messages, { followLatest: true, scrollTop: 0 });
  else status(byId("chatMessages"), "正在读取消息…");
  chatState.followLatest = true;
  updateHistoryNavigation();
  void loadMessages(chatState.generation, !Array.isArray(cached?.messages));
}
function cancelHistorySearch(showCancelled = false) {
  chatState.historySearchRequest++;
  chatState.historySearchController?.abort();
  chatState.historySearchController = null;
  chatState.historySearchPending = false;
  byId("btnRunHistorySearch").disabled = false;
  byId("btnCancelHistorySearch").hidden = true;
  if (showCancelled) text("historySearchStatus", "已取消");
}
function closeHistorySearch() {
  cancelHistorySearch();
  byId("historySearchPanel").hidden = true;
  byId("btnChatHistory").setAttribute("aria-expanded", "false");
}
function resetHistorySearch() {
  closeHistorySearch();
  byId("historyKeyword").value = "";
  byId("historyDate").value = "";
  byId('historyMember').replaceChildren(element('option','','全部成员'));
  byId('historyMember').firstChild.value='';
  byId('historyMember').hidden=true;
  byId("historySearchResults").replaceChildren();
  text("historySearchStatus", "");
  byId("btnHistoryPrevResults").hidden = true;
  byId("btnHistoryNextResults").hidden = true;
  chatState.historySearchPage = 0;
  chatState.historySearchPageStarts = [null];
  chatState.historySearchQuery = { q: "", date: "" };
}
function openHistorySearch() {
  if (!chatState.currentUser || !chatState.currentAccount) { toast("请先选择会话"); return; }
  byId("historySearchPanel").hidden = false;
  byId("btnChatHistory").setAttribute("aria-expanded", "true");
  byId("historyKeyword").focus();
  if(window.ProductConfig?.key==='qq' && chatState.sessions.get(chatState.currentUser)?.isGroup) {
    const account=chatState.currentAccount,user=chatState.currentUser,token=chatState.generation;
    const select=byId('historyMember');select.hidden=false;
    void api('/api/qq/members?'+new URLSearchParams({account,user})).then(data=>{
      if(token!==chatState.generation || account!==chatState.currentAccount || user!==chatState.currentUser || data.account!==account || data.user!==user) return;
      const selected=select.value;
      select.replaceChildren(element('option','','全部成员'));select.firstChild.value='';
      for(const member of data.members||[]) {
        const option=element('option','',`${member.isSelf?'我 · ':''}${member.name||member.id} · 已读范围${member.count||0}条`);
        option.value=member.id;select.appendChild(option);
      }
      select.value=selected;
    }).catch(()=>text('historySearchStatus','成员资料读取失败，可继续按关键词或日期查找'));
  }
}
function renderHistorySearchResults(items, pageIndex, hasMore) {
  const container = byId("historySearchResults");
  container.replaceChildren();
  for (const message of items) {
    if (!message?.historyCursor) continue;
    const button = element("button", "history-result");
    button.type = "button";
    const sender = message.side === "self" ? "我" : message.senderName || message.senderId || chatState.sessions.get(chatState.currentUser)?.name || "对方";
    button.appendChild(element("span", "history-result-meta", `${sender} · ${time(message.time)}`));
    button.appendChild(element("span", "history-result-text", message.text || "[非文字消息]"));
    button.addEventListener("click", () => {
      const cursor = message.historyCursor, id = message.id;
      closeHistorySearch();
      void jumpToHistory(cursor, id);
    });
    container.appendChild(button);
  }
  chatState.historySearchPage = pageIndex;
  byId("btnHistoryPrevResults").hidden = pageIndex === 0;
  byId("btnHistoryNextResults").hidden = !hasMore;
  text("historySearchStatus", items.length ? `第 ${pageIndex + 1} 页` : "没有找到记录");
  container.scrollTop = 0;
}
async function loadHistorySearchPage(pageIndex) {
  const cursor = chatState.historySearchPageStarts[pageIndex];
  if (pageIndex > 0 && !cursor || chatState.historySearchPending || !chatState.currentAccount || !chatState.currentUser) return;
  cancelHistorySearch();
  const account = chatState.currentAccount, user = chatState.currentUser, token = chatState.generation, request = ++chatState.historySearchRequest;
  const controller = new AbortController();
  chatState.historySearchController = controller;
  chatState.historySearchPending = true;
  byId("btnRunHistorySearch").disabled = true;
  byId("btnCancelHistorySearch").hidden = false;
  byId("btnHistoryPrevResults").hidden = true;
  byId("btnHistoryNextResults").hidden = true;
  text("historySearchStatus", "搜索中…");
  try {
    const found = [];
    let before = cursor || null, hasMore = true;
    while (!found.length && hasMore) {
      const params = new URLSearchParams({ account, user, limit: String(50 - found.length) });
      if (chatState.historySearchQuery.q) params.set("q", chatState.historySearchQuery.q);
      if (chatState.historySearchQuery.date) params.set("date", chatState.historySearchQuery.date);
      if (chatState.historySearchQuery.member) params.set('member',chatState.historySearchQuery.member);
      if (before) params.set("before", before);
      const data = await api(`/api/history/search?${params}`, {}, controller.signal);
      if (request !== chatState.historySearchRequest || token !== chatState.generation || account !== chatState.currentAccount || user !== chatState.currentUser) return;
      if (data.account !== account || data.user !== user || !Array.isArray(data.messages)) throw new Error("Invalid search response");
      found.push(...data.messages);
      hasMore = !!data.hasMore;
      if (hasMore && (!data.nextCursor || data.nextCursor === before)) throw new Error("Search cursor did not advance");
      before = data.nextCursor || null;
    }
    chatState.historySearchPageStarts[pageIndex + 1] = before;
    renderHistorySearchResults(found.slice(0, 50), pageIndex, hasMore);
  } catch (error) {
    if (error.name !== "AbortError" && request === chatState.historySearchRequest && token === chatState.generation) text("historySearchStatus", "搜索失败，请重试");
  } finally {
    if (request === chatState.historySearchRequest) {
      chatState.historySearchController = null;
      chatState.historySearchPending = false;
      byId("btnRunHistorySearch").disabled = false;
      byId("btnCancelHistorySearch").hidden = true;
    }
  }
}
function startHistorySearch() {
  const q = byId("historyKeyword").value.trim();
  const date = byId("historyDate").value;
  const member=byId('historyMember').hidden?'':byId('historyMember').value;
  if (!q && !date && !member) { text("historySearchStatus", "输入关键词、选择日期或群成员"); return; }
  cancelHistorySearch();
  chatState.historySearchQuery = { q, date, ...(member?{member}:{}) };
  chatState.historySearchPageStarts = [null];
  chatState.historySearchPage = 0;
  byId("historySearchResults").replaceChildren();
  void loadHistorySearchPage(0);
}
function renderJob(job, settlePending = true) {
  if (!job) return;
  portraitState.currentAnalysisJob = job;
  if (job.recent) labelState.currentRecentJob = job.recent;
  if (settlePending) settleInlineIntentPending(job);
  if (job.status === "error" && job.requested?.mode !== "recent") portraitState.incrementalFailed = true;
  if (!labelState.manualRecentAwaitingPost) {
    if (job.recent?.status === "error") labelState.recentFailed = true;
    else if ((job.recent?.status === "done" || !job.recent && job.status === "done") && !uncoveredMessages().length) labelState.recentFailed = false;
  }
  renderRecentAction(job);
  const retry = portraitState.incrementalFailed || usingLocalFine() && labelState.recentFailed;
  byId("btnRetryAnalysis").hidden = !retry;
  byId("btnRetryProfile").hidden = !portraitState.incrementalFailed;
  text("analysisStatus", "");
  byId("analysisStatus").title = "";
  renderApiInsightStatus();
  if (job.publicationUnit === 'message') {
    const status = ['queued', 'running'].includes(job.status) ? '正在分析' :
      job.status === 'error' ? '分析失败，可重试' : job.pending ? '有新增或失效结果待分析' : '当前范围已完成';
    const detail = [`已发布 ${job.published || 0} 条`, `当前待算 ${job.pending || 0} 条`];
    if (job.backlog) detail.push(`队列积压 ${job.backlog} 条`);
    if (job.invalidated) detail.push(`输入失效 ${job.invalidated} 次`);
    if (job.recomputed) detail.push(`已安排重算 ${job.recomputed} 次`);
    text('analysisStatus', `${status} · ${detail.join(' · ')}`);
  }
  updateProfileProgress();
}
function setIntentActionState(state) {
  if (labelState.intentActionState === state && state !== "idle") return;
  labelState.intentActionState = state;
  clearTimeout(labelState.intentFeedbackTimer);
  const button = byId("btnToggleIntent");
  button.textContent = !settingsState.settings.intent ? "情绪 / 意图" : {
    idle: "情绪 / 意图", submitting: "正在提交…", queued: "识别排队中",
    running: "识别中…", done: "识别完成", error: "识别失败 · 重试"
  }[state];
  button.dataset.state = settingsState.settings.intent ? state : "idle";
  if (settingsState.settings.intent && ["submitting", "queued", "running"].includes(state)) button.setAttribute("aria-busy", "true");
  else button.removeAttribute("aria-busy");
  if (state === "done") labelState.intentFeedbackTimer = setTimeout(() => {
    if (labelState.intentActionState === "done") setIntentActionState("idle");
  }, 2300);
}
function renderRecentAction(job) {
  if (!usingLocalFine()) return;
  if (!settingsState.settings.intent || labelState.intentActionState === "idle" || labelState.manualRecentAwaitingPost) return;
  if (!job?.recent || labelState.manualRecentJobId && job.recent.id !== labelState.manualRecentJobId) return;
  const status = job.recent.status;
  if (["queued", "running", "done", "error"].includes(status)) setIntentActionState(status);
}
function submitManualRecent() {
  if (settingsState.suppressedLocalAccounts.has(chatState.currentAccount) || !canAnalyzeLocal() || !settingsState.settings.intent || !chatState.currentUser || !chatState.controller || chatState.historyState || labelState.recentPending ||
      ["queued", "running"].includes(labelState.currentRecentJob?.status)) return;
  if (!portraitState.activeAnalysisScope) {
    labelState.manualRecentDeferred = true;
    return;
  }
  const window = fineWindow();
  if (!uncoveredMessages(window).length) return;
  labelState.recentFailed = false;
  labelState.manualRecentAwaitingPost = true;
  labelState.manualRecentJobId = null;
  portraitState.analysisGeneration++;
  setIntentActionState("submitting");
  void analyzeRecent(chatState.currentUser, chatState.generation, chatState.controller.signal, fineWindowSignature(window), window.limit, window);
}
function fineWindow() {
  if (!chatState.messages.length) return { limit: 0, candidates: [] };
  const container = byId("chatMessages");
  const bounds = container.getBoundingClientRect();
  const positions = new Map(chatState.messages.map((message, index) => [String(message.id), index]));
  let first = -1;
  const pendingLatestScroll = chatState.followLatest && container.scrollHeight - container.scrollTop - container.clientHeight > 80;
  if (bounds.height > 0 && !pendingLatestScroll) for (const node of container.querySelectorAll(".msg-item")) {
    const rect = node.getBoundingClientRect();
    if (rect.bottom <= bounds.top || rect.top >= bounds.bottom) continue;
    const index = positions.get(node.dataset.messageId);
    if (index !== undefined && (first < 0 || index < first)) first = index;
  }
  const limit = Math.min(80, first < 0 ? Math.min(12, chatState.messages.length) : chatState.messages.length - first);
  const candidates = chatState.messages.slice(-Math.max(1, limit)).filter(message => labelSideEligible(message) &&
    message.kind === "text" && typeof message.text === "string" && message.text.trim());
  return { limit: Math.max(1, limit), candidates };
}
function analyzableMessages(window = fineWindow()) {
  return window.candidates;
}
function uncoveredMessages(window = fineWindow()) {
  return analyzableMessages(window).filter(message => {
    const result = fineMessageResult(labelState.results[message.id]) ? labelState.results[message.id] : null;
    return result?.state !== "skipped" &&
      !(result?.state === "done" && result.labelSchema === CURRENT_LABEL_SCHEMA);
  });
}
function fineWindowSignature(window) {
  return JSON.stringify([window.limit, portraitState.activeAnalysisScope,
    analyzableMessages(window).map(message => [message.id, message.text, message.senderId,
      message.time, message.kind, message.quote, message.mentions])]);
}
function scheduleRecent(user, token, signal, changedOther) {
  if (settingsState.suppressedLocalAccounts.has(chatState.currentAccount) || !canAnalyzeLocal() || !settingsState.settings.intent || chatState.view !== "chat" || document.hidden || startupActive || !changedOther ||
      labelState.recentPending || labelState.recentFailed || !portraitState.activeAnalysisScope) return;
  const window = fineWindow();
  if (!uncoveredMessages(window).length) return;
  const signature = fineWindowSignature(window);
  if (!labelState.requestedRecentSignatures.has(signature)) void analyzeRecent(user, token, signal, signature, window.limit, window);
}
function incrementalState(key) {
  let state = portraitState.autoIncrementalState.get(key);
  if (!state) {
    state = { requestedSignature: null, bootstrapRequested: false, pending: false, queued: false, failed: false, networkFailed: false };
    portraitState.autoIncrementalState.set(key, state);
  }
  return state;
}
async function startIncremental(user, token, signal, key, state) {
  const account = chatState.currentAccount;
  if (!canAnalyzeLocal() || state.pending || !account || token !== chatState.generation || user !== chatState.currentUser ||
      settingsState.suppressedLocalAccounts.has(account)) return;
  state.pending = true;
  let refresh = false;
  try {
    const data = await api("/api/analyze", { method: "POST", body: JSON.stringify({
      account, user, mode: "incremental",
    }) }, signal);
    if (portraitState.autoIncrementalState.get(key) !== state || !canAnalyzeLocal()) return;
    state.failed = data.job?.status === "error";
    state.networkFailed = false;
    if (token === chatState.generation && key === portraitState.activeAnalysisScope) {
      portraitState.incrementalFailed = state.failed;
      renderJob(data.job);
      refresh = !state.failed;
    }
  } catch (error) {
    if (portraitState.autoIncrementalState.get(key) !== state || !canAnalyzeLocal()) return;
    if (error.name === "AbortError") {
      state.requestedSignature = null;
      state.bootstrapRequested = false;
      return;
    }
    if (token === chatState.generation && key === portraitState.activeAnalysisScope && handleAccountBoundaryError(error)) return;
    state.failed = true;
    state.networkFailed = isNetworkFailure(error);
    if (token === chatState.generation && key === portraitState.activeAnalysisScope) {
      portraitState.incrementalFailed = true;
      renderJob({ status: "error", error: error.message });
    }
  } finally {
    state.pending = false;
    if (refresh && token === chatState.generation && key === portraitState.activeAnalysisScope) void loadAnalysis(user, token, signal);
  }
}
function scheduleIncremental(user, token, signal, data, changed, signature) {
  if(typeof window!=='undefined' && window.ProductConfig?.key==='qq' && chatState.sessions.get(user)?.isGroup) return;
  if (!canAnalyzeLocal() || settingsState.suppressedLocalAccounts.has(chatState.currentAccount)) return;
  const key = portraitState.activeAnalysisScope;
  if (!key) return;
  const state = incrementalState(key);
  const job = data.job || { status: "idle", checkpointComplete: false };
  if (job.status === "error") {
    state.failed = true;
    portraitState.incrementalFailed = true;
    return;
  }
  if (state.failed) return;
  if (state.pending || ["queued", "running"].includes(job.status)) {
    if (changed && signature !== state.requestedSignature) state.queued = true;
    return;
  }
  const checkpointNeeded = !state.bootstrapRequested || job.status === "idle";
  const newWindow = changed && signature !== state.requestedSignature;
  if (!checkpointNeeded && !newWindow && !state.queued) return;
  state.queued = false;
  state.bootstrapRequested = true;
  state.requestedSignature = signature;
  void startIncremental(user, token, signal, key, state);
}
function localAnalysisScopeKey(account, user, data) {
  return JSON.stringify([account, user, data.analysisVersion || "current",
    ...(Number.isSafeInteger(data.dataRevision) ? [data.dataRevision] : [])]);
}
async function loadAnalysis(user, token, signal) {
  if (chatState.historyState || !canAnalyzeLocal()) return;
  const request = ++portraitState.analysisGeneration;
  try {
    const range=window.ProductConfig?.key==='qq' && chatState.sessions.get(user)?.isGroup?`&limit=${Math.max(1,fineWindow().limit)}`:'';
    const data = await api(`/api/analysis?user=${encodeURIComponent(user)}${range}`, {}, signal);
    if (token !== chatState.generation || request !== portraitState.analysisGeneration || chatState.historyState || !canAnalyzeLocal()) return;
    if (!acceptResponseAccount(data.account)) return;
    portraitState.analysisNetworkFailed = false;
    const key = localAnalysisScopeKey(chatState.currentAccount, user, data);
    portraitState.activeAnalysisScope = key;
    if (data.job?.status !== "error" && !portraitState.autoIncrementalState.get(key)?.failed) portraitState.incrementalFailed = false;
    const incoming = visibleResults(data.results || {}, chatState.messages);
    const verifiedGroupLabels = window.ProductConfig?.key === 'qq' && chatState.sessions.get(user)?.isGroup;
    labelState.results = !verifiedGroupLabels && ["queued", "running"].includes(data.job?.status) ? { ...visibleResults(labelState.results, chatState.messages), ...incoming } : incoming;
    chatState.conversationMood = data.mood || null;
    cacheCurrentSession({ results: labelState.results, mood: chatState.conversationMood });
    refreshLabels();
    renderJob(data.job || { status: "idle", checkpointComplete: false });
    if (data.stale === true && data.hasPublishedAnalysis === true) {
      text("analysisStatus", data.job?.status === "error" ?
        "记录已更新，重算失败；当前显示旧分析" : "记录已更新，旧分析将在重算完成后替换");
    }
    if (labelState.manualRecentDeferred) {
      labelState.manualRecentDeferred = false;
      submitManualRecent();
    } else scheduleRecent(user, token, signal, true);
    if (chatState.view === "persona" && !portraitState.profilePending && (data.job?.status === "running" || data.job?.status === "done")) loadProfile(portraitState.activeMember);
    return data;
  } catch (error) {
    if (error.name !== "AbortError" && token === chatState.generation && request === portraitState.analysisGeneration && canAnalyzeLocal()) {
      if (handleAccountBoundaryError(error)) return;
      portraitState.incrementalFailed = true;
      portraitState.analysisNetworkFailed = isNetworkFailure(error);
      renderJob({ status: "error", error: `读取失败：${error.message}` });
      setStripStatus("分析读取失败，请重试");
    }
  }
}
async function analyzeRecent(user, token, signal, signature, limit, window) {
  const account = chatState.currentAccount;
  if (!canAnalyzeLocal() || labelState.recentPending || labelState.recentFailed || !account || token !== chatState.generation || user !== chatState.currentUser) return;
  labelState.requestedRecentSignatures.add(signature);
  while (labelState.requestedRecentSignatures.size > 64) labelState.requestedRecentSignatures.delete(labelState.requestedRecentSignatures.values().next().value);
  labelState.recentPending = true;
  startInlineIntentPending(window);
  try {
    const data = await api("/api/analyze", { method: "POST", body: JSON.stringify({
      account, user, mode: "recent", limit,
    }) }, signal);
    if (token === chatState.generation && canAnalyzeLocal()) {
      labelState.recentNetworkFailed = false;
      if (labelState.manualRecentAwaitingPost) labelState.manualRecentJobId = data.job?.recent?.id || null;
      labelState.manualRecentAwaitingPost = false;
      labelState.inlineIntentJobId = data.job?.recent?.id || null;
      renderJob(data.job, false);
      await loadAnalysis(user, token, signal);
    }
  } catch (error) {
    if (error.name !== "AbortError" && token === chatState.generation && canAnalyzeLocal()) {
      labelState.manualRecentAwaitingPost = false;
      if (handleAccountBoundaryError(error)) return;
      labelState.recentFailed = true;
      labelState.recentNetworkFailed = isNetworkFailure(error);
      if (labelState.intentActionState !== "idle") setIntentActionState("error");
      clearInlineIntentPending();
      refreshLabels();
      byId("btnRetryAnalysis").hidden = false;
      updateProfileProgress();
      toast("分析失败，可重试");
    }
  }
  finally {
    if (token === chatState.generation) {
      labelState.recentPending = false;
    }
  }
}
async function loadMessages(token = chatState.generation, poll = false, refresh = false) {
  const qualityNotice=byId('qqLocalQualityNotice');
  if(qualityNotice)qualityNotice.hidden=!(window.ProductConfig?.key==='qq' && chatState.sessions.get(chatState.currentUser)?.isGroup && settingsState.modelSourceSnapshot.mode!=='api');
  if (!chatState.currentUser || token !== chatState.generation || chatState.historyState) return;
  if (chatState.messagePending) {
    if (refresh) chatState.messageRefreshQueued = true;
    return;
  }
  chatState.messagePending = true;
  const request = ++chatState.messageRequest;
  const windowSerial = ++chatState.windowRequestSerial;
  const user = chatState.currentUser;
  const signal = chatState.controller.signal;
  try {
    const data = await api(`/api/messages?user=${encodeURIComponent(user)}&limit=80`, {}, signal);
    if (token !== chatState.generation || request !== chatState.messageRequest || chatState.historyState) return;
    if (!acceptResponseAccount(data.account)) return;
    if (!Array.isArray(data.messages)) throw new Error("Invalid messages response");
    const next = data.messages;
    if (poll && !next.length && chatState.messages.length && ++chatState.emptyMessagePolls < 2) return;
    if (next.length) chatState.emptyMessagePolls = 0;
    const signature = JSON.stringify(next);
    const changed = signature !== JSON.stringify(chatState.messages);
    if (poll && changed && retainQQReadingWindow(next)) return;
    if (typeof data.hasMoreBefore === "boolean") chatState.currentHasMoreBefore = data.hasMoreBefore;
    const previousOther = new Map(chatState.messages.filter(message => labelSideEligible(message) && message.kind === "text")
      .map(message => [String(message.id), message.text]));
    const changedOther = next.some(message => labelSideEligible(message) && message.kind === "text" &&
      typeof message.text === "string" && message.text.trim() && previousOther.get(String(message.id)) !== message.text);
    if (changed) labelState.results = unchangedMessageResults(chatState.messages, next, labelState.results);
    if (!poll || changed) renderMessages(next);
    cacheCurrentSession({ messages: next, results: visibleResults(labelState.results, next),
      ...(typeof data.hasMoreBefore === "boolean" ? { hasMoreBefore: data.hasMoreBefore } : {}),
      summarySignature: sessionSummarySignature(chatState.sessions.get(user)), windowSerial });
    markSessionAsRead(user);
    renderSessions();
    const analysis = await loadAnalysis(user, token, signal);
    if (token === chatState.generation && request === chatState.messageRequest && !chatState.historyState && analysis) {
      scheduleIncremental(user, token, signal, analysis, changed, signature);
      scheduleRecent(user, token, signal, changedOther);
    }
  } catch (error) {
    if (error.name !== "AbortError" && token === chatState.generation && request === chatState.messageRequest) {
      if (handleAccountBoundaryError(error)) return;
      if (!poll) status(byId("chatMessages"), "消息读取失败，请重试", () => loadMessages(token));
      else text("stripStatusText", "消息读取失败，请重试");
    }
  } finally {
    if (token === chatState.generation && request === chatState.messageRequest) {
      chatState.messagePending = false;
      if (chatState.messageRefreshQueued) {
        chatState.messageRefreshQueued = false;
        void loadMessages(token, true);
      }
    }
  }
}
function switchSession(user, force = false) {
  if (!chatState.sessions.has(user)) return;
  if (!chatState.messageSourceReady) {
    text("chatTitle", chatState.sessions.get(user).name || user);
    status(byId("chatMessages"), "聊天记录尚未就绪，正在重试…");
    return;
  }
  try { localStorage.setItem(`last-conversation:${chatState.currentAccount}`, user); } catch {}
  markSessionAsRead(user);
  if (user === chatState.currentUser && !force) {
    renderSessions();
    return;
  }
  labelState.messageLabels?.closeDetails();
  cacheCurrentSession({ scrollTop: chatState.historyState ? 0 : byId("chatMessages").scrollTop, followLatest: chatState.historyState ? true : chatState.followLatest });
  const cacheKey = sessionCacheKey(chatState.currentAccount, user);
  const cached = chatState.sessionCache.get(cacheKey);
  cancelApiInsightWork();
  clearInlineIntentPending();
  clearTimeout(labelState.selectedAnalysisTimer);
  labelState.selectedAnalysisTimer = null;
  cancelHistoryRequest();
  chatState.historyState = null;
  resetHistorySearch();
  clearReplyPrediction();
  if (byId("btnPredictReply")) byId("btnPredictReply").disabled = true;
  chatState.controller?.abort();
  chatState.controller = new AbortController();
  chatState.advance("generation");
  portraitState.advance("analysisGeneration");
  portraitState.advance("profileGeneration");
  portraitState.profilePending = false;
  chatState.messagePending = false;
  chatState.messageRefreshQueued = false;
  chatState.emptyMessagePolls = 0;
  labelState.manualRecentAwaitingPost = false;
  labelState.manualRecentJobId = null;
  labelState.manualRecentDeferred = false;
  setIntentActionState("idle");
  labelState.recentPending = false;
  portraitState.incrementalFailed = false;
  labelState.recentFailed = false;
  portraitState.analysisNetworkFailed = false;
  labelState.recentNetworkFailed = false;
  labelState.requestedRecentSignatures.clear();
  portraitState.activeMember = chatState.sessions.get(user)?.isGroup || window.ProductConfig?.key === "qq" ? portraitState.storedProfileSelections.get(sessionCacheKey(chatState.currentAccount, user)) || "" : "";
  portraitState.groupMembers = [];
  portraitState.renderedProfileKey = null;
  portraitState.renderedProfileSignature = null;
  portraitState.memberRenderedScope = null;
  portraitState.reset("activeAnalysisScope", "currentAnalysisJob");
  labelState.currentRecentJob = null;
  chatState.currentUser = user;
  window.QQSupportController?.scopeChanged();
  byId("btnChatHistory").disabled = false;
  chatState.currentHasMoreBefore = typeof cached?.hasMoreBefore === "boolean" ? cached.hasMoreBefore : null;
  chatState.messages = [];
  labelState.results = visibleResults(cached?.results || {}, cached?.messages || []);
  chatState.conversationMood = cached?.mood || null;
  chatState.followLatest = cached?.followLatest ?? true;
  chatState.lastChatScrollTop = cached?.scrollTop || 0;
  byId("chatStatusPill").style.display = "none";
  byId("btnRetryAnalysis").hidden = true;
  byId("btnRetryProfile").hidden = true;
  text("analysisStatus", "");
  text("chatTitle", chatState.sessions.get(user).name || user);
  if (Array.isArray(cached?.messages)) renderMessages(cached.messages, cached);
  else status(byId("chatMessages"), "正在读取消息…");
  updateHistoryNavigation();
  const snapshotSourceId = settingsState.modelSourceResolved && settingsState.modelSourceSnapshot.mode === "api" ? settingsState.modelSourceSnapshot.sourceId : undefined;
  let profileKey = profileCacheKey(chatState.currentAccount, user, portraitState.activeMember, snapshotSourceId);
  let cachedProfile = cachedProfileFor(chatState.currentAccount, user, portraitState.activeMember, snapshotSourceId);
  if (!cachedProfile && portraitState.activeMember) {
    portraitState.activeMember = "";
    profileKey = profileCacheKey(chatState.currentAccount, user, "", snapshotSourceId);
    cachedProfile = cachedProfileFor(chatState.currentAccount, user, "", snapshotSourceId);
  }
  if (cachedProfile && usingLocalFine()) {
    renderProfile(cachedProfile);
    portraitState.renderedProfileKey = profileKey;
    portraitState.renderedProfileSignature = JSON.stringify(cachedProfile);
  } else if (settingsState.modelSourceResolved && settingsState.modelSourceSnapshot.mode === "api") {
    // Reuse the Laya snapshot store for the API portrait of this source; the persona
    // refresh below replaces it once the same-scope response arrives.
    if (!renderCachedApiProfile(chatState.currentAccount, user, portraitState.activeMember, settingsState.modelSourceSnapshot.sourceId))
      clearProfileView(chatState.sessions.get(user).name || user);
  } else clearProfileView(chatState.sessions.get(user).name || user);
  if (chatState.view === "persona") loadProfile(portraitState.activeMember);
  renderSessions();
  const token = chatState.generation, account = chatState.currentAccount, signal = chatState.controller.signal;
  if (canAnalyzeLocal() && sessionWindowReady(account, chatState.sessions.get(user))) {
    labelState.selectedAnalysisTimer = setTimeout(async () => {
      labelState.selectedAnalysisTimer = null;
      if (token !== chatState.generation || account !== chatState.currentAccount || user !== chatState.currentUser || chatState.historyState) return;
      const analysis = await loadAnalysis(user, token, signal);
      if (analysis && token === chatState.generation && !chatState.historyState) scheduleIncremental(user, token, signal, analysis, false, JSON.stringify(chatState.messages));
    }, 120);
  } else void loadMessages(token, !!cached);
}
function switchView(target) {
  if (target === "persona" && (!chatState.messageSourceReady || !chatState.currentUser)) return;
  if (target === "persona" && chatState.view === "chat") chatState.returnChatScroll = {
    account: chatState.currentAccount, user: chatState.currentUser, top: byId("chatMessages").scrollTop,
  };
  if (target !== "chat") clearReplyPrediction();
  if (target !== "persona") cancelApiPortraitPoll();
  chatState.view = target;
  byId("chatView").classList.toggle("active", target === "chat");
  byId("personaView").classList.toggle("active", target === "persona");
  byId("navChat").classList.toggle("active", target === "chat");
  byId("navPersona").classList.toggle("active", target === "persona");
  window.ChatBeanUI?.setView(target);
  if (target === "persona") loadProfile(portraitState.activeMember);
  else {
    const saved = chatState.returnChatScroll;
    chatState.returnChatScroll = null;
    if (saved?.account === chatState.currentAccount && saved?.user === chatState.currentUser)
      byId("chatMessages").scrollTop = saved.top;
    else if (!chatState.historyState) scrollToLatest();
  }
}
portraitState.activeMember = "";
portraitState.profilePending = false;
portraitState.groupMembers = [];
portraitState.renderedProfileKey = null;
portraitState.renderedProfileSignature = null;
portraitState.memberRenderedScope = null;
function profileCacheKey(account, user, member, sourceId) {
  // Local Laya keeps its legacy 3-part key; an API source appends its id so local and
  // different API models never share a snapshot entry.
  return sourceId ? JSON.stringify([account, user, member, sourceId])
    : JSON.stringify([account, user, member]);
}
const profileSnapshotStorageKey = "real-ui-profile-snapshots-v1";
const profileSelectionStorageKey = "real-ui-profile-selection-v1";
const profileSnapshotLimit = 128;
const profileSelectionLimit = 64;
const profileSnapshotMaxBytes = 1000000;
const profileSnapshotEncoder = new TextEncoder();
function storedArray(key) {
  try { const value = JSON.parse(localStorage.getItem(key) || "[]"); return Array.isArray(value) ? value : []; }
  catch { return []; }
}
portraitState.storedProfileSnapshots = new Map();
for (const entry of storedArray(profileSnapshotStorageKey).slice(0, profileSnapshotLimit)) {
  try {
    const scope = JSON.parse(entry.key);
    const [account, user, member, sourceId] = scope;
    if (typeof account === "string" && account && typeof user === "string" && user &&
        typeof member === "string" && entry.profile?.account === account &&
        entry.profile.username === (member || user) &&
        (scope.length === 3 || scope.length === 4 && typeof sourceId === "string" && sourceId) &&
        (entry.profile.apiSource === true) === (scope.length === 4) &&
        (scope.length === 3 || entry.profile.apiSourceId === sourceId) &&
        Number.isFinite(entry.at)) {
      portraitState.storedProfileSnapshots.set(entry.key, entry);
    }
  } catch { }
}
portraitState.storedProfileSelections = new Map();
for (const entry of storedArray(profileSelectionStorageKey).slice(0, profileSelectionLimit)) {
  try {
    const [account, user] = JSON.parse(entry.key);
    if (typeof account === "string" && account && typeof user === "string" && user &&
        typeof entry.member === "string") portraitState.storedProfileSelections.set(entry.key, entry.member);
  } catch { }
}
function saveStoredProfiles() {
  const entries = [...portraitState.storedProfileSnapshots.values()].sort((a, b) => b.at - a.at).slice(0, profileSnapshotLimit);
  while (entries.length && profileSnapshotEncoder.encode(JSON.stringify(entries)).byteLength > profileSnapshotMaxBytes) entries.pop();
  portraitState.storedProfileSnapshots.clear();
  for (const entry of entries) portraitState.storedProfileSnapshots.set(entry.key, entry);
  try { localStorage.setItem(profileSnapshotStorageKey, JSON.stringify(entries)); } catch { }
}
function saveStoredSelections() {
  const entries = [...portraitState.storedProfileSelections].slice(-profileSelectionLimit).map(([key, member]) => ({ key, member }));
  try { localStorage.setItem(profileSelectionStorageKey, JSON.stringify(entries)); } catch { }
}
function rememberProfileMember(account, user, member) {
  if (!account || !user) return;
  const key = sessionCacheKey(account, user);
  if (portraitState.storedProfileSelections.get(key) === member) return;
  portraitState.storedProfileSelections.delete(key);
  portraitState.storedProfileSelections.set(key, member);
  while (portraitState.storedProfileSelections.size > profileSelectionLimit) portraitState.storedProfileSelections.delete(portraitState.storedProfileSelections.keys().next().value);
  saveStoredSelections();
}
function profileSnapshot(profile) {
  const stats = profile.stats || {};
  const count = value => Math.max(0, Math.floor(Number(value) || 0));
  const avatarUrl = value => typeof value === "string" && value.length <= 512 && /^https?:\/\//i.test(value) ? value : "";
  const score = value => Number.isInteger(value) && value >= 0 && value <= 100 ? value : null;
  const phrases = (value, max) => Array.isArray(value) ?
    value.filter(entry => typeof entry === "string" && entry).slice(0, 6).map(entry => entry.slice(0, max)) : [];
  const inference = profile.mbtiInference;
  const apiSource = profile.apiSource === true;
  const apiPortrait = apiSource ? profile.apiPortrait : null;
  const apiAxes = apiPortrait?.mbtiAxes || {};
  const apiTraits = apiPortrait?.traits || {};
  return {
    account: profile.account, username: profile.username,
    name: String(profile.name || profile.username || "").slice(0, 128), isGroup: !!profile.isGroup,
    ...(window.ProductConfig?.key === "qq" ? {targetSide: profile.targetSide, targetId: profile.targetId, analysisVersion:profile.analysisVersion,
      objectiveOverview:profile.objectiveOverview===true} : {}),
    avatar: avatarUrl(profile.avatar), avatarCandidates: Array.isArray(profile.avatarCandidates) ?
      profile.avatarCandidates.map(avatarUrl).filter(Boolean).slice(0, 2) : [],
    stats: { messageCount: apiSource && stats.messageCount === null ? null : count(stats.messageCount), textCount: count(stats.textCount),
      analyzedCount: count(stats.analyzedCount), participantCount: count(stats.participantCount) },
    affinity: Number.isFinite(profile.affinity) ? profile.affinity : null,
    traits: Array.isArray(profile.traits) ? profile.traits.slice(0, 6).map(item => ({
      key: String(item.key || ""), label: String(item.label || "").slice(0, 24),
      val: Number(item.val), sampleCount: count(item.sampleCount) })) : [],
    keywords: Array.isArray(profile.keywords) ? profile.keywords.slice(0, 6).map(item => ({
      word: String(item.word || "").slice(0, 24), count: count(item.count) })) : [],
    summary: String(profile.summary || "").slice(0, 600),
    mbtiInference: inference && typeof inference === "object" ? {
      eligibleMessages: count(inference.eligibleMessages), minMessages: count(inference.minMessages) || 100,
      axes: inference.axes && typeof inference.axes === "object" ? Object.fromEntries(
        ["EI", "SN", "TF", "JP"].map(axis => [axis, inference.axes[axis] || {}])) : {}
    } : null,
    members: profile.isGroup && Array.isArray(profile.members) ? profile.members.slice(0, 64).map(item => ({
      id: String(item.id || "").slice(0, 256), name: String(item.name || item.id || "").slice(0, 64) })) : [],
    dataStatus: profile.dataStatus, analysisUnit: profile.analysisUnit,
    stale: profile.stale === true, analysisRevision: profile.analysisRevision ?? null,
    hasPublishedAnalysis: profile.hasPublishedAnalysis === true,
    job: { status: "idle" },
    // API display state (only meaningful when apiSource). Bounded and derived from the
    // already-validated backend portrait; no key, raw chat, or raw provider response.
    apiSource,
    apiSourceId: apiSource && typeof profile.apiSourceId === "string" ? profile.apiSourceId.slice(0, 64) : "",
    apiTargetTexts: count(profile.apiTargetTexts),
    apiProgressProcessed: count(profile.apiProgressProcessed),
    apiProgressTotal: count(profile.apiProgressTotal),
    apiComplete: profile.apiComplete === true,
    apiRunning: profile.apiRunning === true,
    apiRebuildError: profile.apiRebuildError === true,
    apiMbtiAxes: apiPortrait ? { EI: score(apiAxes.EI), SN: score(apiAxes.SN),
      TF: score(apiAxes.TF), JP: score(apiAxes.JP) } : null,
    apiPortrait: apiPortrait ? {
      summary: String(apiPortrait.summary || "").slice(0, 240),
      communication: String(apiPortrait.communication || "").slice(0, 120),
      emotionExpression: String(apiPortrait.emotionExpression || "").slice(0, 120),
      interactionPreferences: String(apiPortrait.interactionPreferences || "").slice(0, 120),
      topics: phrases(apiPortrait.topics, 30), patterns: phrases(apiPortrait.patterns, 80),
      boundaries: phrases(apiPortrait.boundaries, 80), uncertain: phrases(apiPortrait.uncertain, 80),
      affinity: score(apiPortrait.affinity),
      mbtiAxes: { EI: score(apiAxes.EI), SN: score(apiAxes.SN), TF: score(apiAxes.TF), JP: score(apiAxes.JP) },
      traits: { socialEnergy: score(apiTraits.socialEnergy), humor: score(apiTraits.humor),
        composure: score(apiTraits.composure), initiative: score(apiTraits.initiative),
        care: score(apiTraits.care), affection: score(apiTraits.affection) } } : null,
  };
}
function cachedProfileFor(account, user, member, sourceId) {
  const key = profileCacheKey(account, user, member, sourceId);
  if (!sourceId && portraitState.profileSnapshotsRequireRefresh.has(key)) return null;
  const stored = portraitState.storedProfileSnapshots.get(key);
  const cached = portraitState.profileCache.get(key) || stored?.profile;
  if (!cached || cached.account !== account || cached.username !== (member || user) ||
      !!cached.isGroup !== !!chatState.sessions.get(user)?.isGroup || !cached.stats ||
      !Array.isArray(cached.traits) || !Array.isArray(cached.keywords) ||
      !Array.isArray(cached.members) || (cached.apiSource === true) !== !!sourceId ||
      sourceId && cached.apiSourceId !== sourceId) return null;
  portraitState.profileCache.set(key, cached);
  if (stored && Date.now() - stored.at > 5 * 60 * 1000) {
    portraitState.storedProfileSnapshots.delete(key);
    portraitState.storedProfileSnapshots.set(key, { ...stored, at: Date.now() });
    saveStoredProfiles();
  }
  return cached;
}
function renderCachedApiProfile(account, user, member, sourceId) {
  const cached = cachedProfileFor(account, user, member, sourceId);
  if (!cached) return false;
  renderProfile(cached);
  portraitState.renderedProfileKey = profileCacheKey(account, user, member, sourceId);
  portraitState.renderedProfileSignature = JSON.stringify(cached);
  return true;
}
function rememberProfile(profile, account, user, member, sourceId) {
  if (profile.account !== account || profile.username !== (member || user)) return;
  const key = profileCacheKey(account, user, member, sourceId);
  // Re-entry reads memory before disk. Keep both on the same revision, including
  // the transition from an initial empty portrait to its completed result.
  portraitState.profileCache.set(key, profile);
  const snapshot = profileSnapshot(profile);
  const previous = portraitState.storedProfileSnapshots.get(key);
  if (previous && Date.now() - previous.at < 24 * 60 * 60 * 1000 &&
      JSON.stringify(previous.profile) === JSON.stringify(snapshot)) return;
  portraitState.storedProfileSnapshots.delete(key);
  portraitState.storedProfileSnapshots.set(key, { key, at: Date.now(), profile: snapshot });
  saveStoredProfiles();
}
function pruneStoredProfiles(account, nextSessions) {
  let changedProfiles = false, changedSelections = false;
  for (const key of portraitState.storedProfileSnapshots.keys()) {
    const [cachedAccount, user] = JSON.parse(key);
    if (cachedAccount === account && !nextSessions.has(user)) { portraitState.storedProfileSnapshots.delete(key); changedProfiles = true; }
  }
  for (const key of portraitState.storedProfileSelections.keys()) {
    const [cachedAccount, user] = JSON.parse(key);
    if (cachedAccount === account && !nextSessions.has(user)) { portraitState.storedProfileSelections.delete(key); changedSelections = true; }
  }
  if (changedProfiles) saveStoredProfiles();
  if (changedSelections) saveStoredSelections();
}
async function clearStoredProfilesForAccount(accountId) {
  const maps = [portraitState.storedProfileSnapshots, portraitState.storedProfileSelections, portraitState.profileCache,
    portraitState.profileRateSamples, chatState.sessionCache, portraitState.autoIncrementalState, labelState.apiInsightCache];
  const storages = [localStorage];
  if (typeof sessionStorage !== "undefined") storages.push(sessionStorage);
  const accounts = new Set();
  if (typeof chatState.currentAccount === "string" && chatState.currentAccount) accounts.add(chatState.currentAccount);
  for (const map of maps) for (const key of map.keys()) {
    try {
      const [account] = JSON.parse(key);
      if (typeof account === "string" && account) accounts.add(account);
    } catch { }
  }
  for (const storage of storages) try {
    for (let index = 0; index < storage.length; index++) {
      const key = storage.key(index);
      if (key?.startsWith("read-watermark:")) accounts.add(key.slice("read-watermark:".length));
      else if (key?.startsWith("last-conversation:")) accounts.add(key.slice("last-conversation:".length));
      else if (key?.startsWith("mbti-unlocked:")) accounts.add(key.slice("mbti-unlocked:".length).split(":", 1)[0]);
    }
  } catch { }
  const matching = new Set();
  for (const account of accounts) {
    const digest = await crypto.subtle.digest("SHA-256", profileSnapshotEncoder.encode(account));
    const id = [...new Uint8Array(digest)].map(byte => byte.toString(16).padStart(2, "0")).join("");
    if (id === accountId) matching.add(account);
  }
  for (const map of maps) for (const key of map.keys()) {
    try { if (matching.has(JSON.parse(key)[0])) map.delete(key); } catch { }
  }
  for (const account of matching) for (const storage of storages) try {
    storage.removeItem(`read-watermark:${account}`);
    storage.removeItem(`last-conversation:${account}`);
    for (let index = storage.length - 1; index >= 0; index--) {
      const key = storage.key(index);
      if (key?.startsWith(`mbti-unlocked:${account}:`)) storage.removeItem(key);
    }
  } catch { }
  saveStoredProfiles();
  saveStoredSelections();
  return matching;
}
portraitState.profileRateSamples = new Map();
function observeProfileRate(profile, key) {
  const count = Number(profile.stats?.analyzedCount);
  if (!Number.isFinite(count) || count < 0) return;
  const now = performance.now();
  let samples = portraitState.profileRateSamples.get(key) || [];
  if (samples.length && (count < samples[samples.length - 1].count ||
      now - samples[samples.length - 1].at > 30000)) samples = [];
  samples.push({ at: now, count });
  // Retain the observation just before the window so a long model batch is not
  // mistaken for an instantaneous burst when its completed texts arrive together.
  while (samples.length > 2 && samples[1].at < now - 30000) samples.shift();
  portraitState.profileRateSamples.delete(key);
  portraitState.profileRateSamples.set(key, samples);
  while (portraitState.profileRateSamples.size > 64) portraitState.profileRateSamples.delete(portraitState.profileRateSamples.keys().next().value);
}
function profileRateText() {
  const samples = portraitState.profileRateSamples.get(profileCacheKey(chatState.currentAccount, chatState.currentUser, portraitState.activeMember));
  if (!samples || samples.length < 2) return "— 条/秒";
  const first = samples[0], last = samples[samples.length - 1];
  const elapsed = (last.at - first.at) / 1000;
  return elapsed > 0 ? `${((last.count - first.count) / elapsed).toFixed(1)} 条/秒` : "— 条/秒";
}
function updateProfileProgress(profile = portraitState.profileCache.get(profileCacheKey(chatState.currentAccount, chatState.currentUser, portraitState.activeMember))) {
  if(profile?.objectiveOverview) {
    byId('stripConfidenceItem').style.display='none';
    byId('btnRetryProfile').hidden=true;
    setStripStatus('已读范围概览');
    return;
  }
  if (profile?.apiSource) {
    const analyzed = profile.apiProgressProcessed;
    const total = profile.apiProgressTotal;
    text("stripConfidence", `${analyzed} / ${total} 条文本`);
    byId("stripConfidenceItem").style.display = profile.isGroup ? "none" : "";
    byId("btnRetryProfile").hidden = true;
    setStripStatus(profile.apiComplete ? "已完成" : profile.apiRunning ? "API 分析中" : "待继续");
    if (profile.stale && profile.hasPublishedAnalysis) {
      setStripStatus(profile.apiRebuildError ? "重算失败，保留旧 API 画像" : "保留旧 API 画像，等待重算完成");
      text("summaryBadge", "旧记录分析");
    }
    return;
  }
  if (settingsState.modelSourceResolved && settingsState.modelSourceSnapshot.mode === "api") return;
  const ownJob = profile?.job;
  const job = ownJob || (portraitState.activeMember ? null : portraitState.currentAnalysisJob);
  const profileFailed = ownJob ? ownJob.status === "error" :
    !portraitState.activeMember && (portraitState.incrementalFailed || job?.status === "error" && job.requested?.mode !== "recent");
  if (profile) {
    const analyzed = Number(profile.stats?.analyzedCount) || 0;
    const total = Number(profile.stats?.textCount) || 0;
    const partial = (profile.dataStatus && profile.dataStatus !== "analyzed") || analyzed < total || job?.checkpointComplete === false;
    text("stripConfidence", `${analyzed} / ${total} 条文本`);
    byId("stripConfidenceItem").style.display = profile.isGroup ? "none" : "";
    text("summaryBadge", analyzed ? partial ? "阶段性结果" : "基于聊天汇总" : "暂无分析结果");
  }
  byId("btnRetryProfile").hidden = !profileFailed;
  const active = status => status === "queued" || status === "running";
  let state = "";
  if (job?.status === "missing-model") state = "未安装 Laya 模型，请在设置下载模型";
  else if (profileFailed) state = "分析失败，请重试";
  else if (active(job?.status)) state = profileRateText();
  else if (job?.checkpointComplete === false) state = "待继续";
  else if (profile && job?.checkpointComplete) state = "已完成";
  if (profile?.stale === true && profile.hasPublishedAnalysis === true) {
    state = profileFailed ? "重算失败，保留旧画像；可重试" : "记录已更新，保留旧画像至重算完成";
    text("summaryBadge", "旧记录分析");
  }
  setStripStatus(state);
  if (state.endsWith("条/秒")) byId("stripStatusText").title = "最近30秒新增已分析文本的实际速率";
}
function clearProfileView(name = "正在读取画像…") {
  text("personaHeaderTitle", name);
  text("heroName", name);
  text("heroRelationBadge", "待分析");
  byId("heroAvatar").replaceChildren();
  byId("heroMbtiRow").hidden = true;
  text("heroArchetype", "");
  byId("heroArchetype").style.display = "none";
  byId("heroMetricBox").replaceChildren();
  text("heroMbti", "");
  byId("mbtiCard").hidden = true;
  text("mbtiScaleBadge", "正在读取");
  byId("mbtiScalesList").replaceChildren();
  byId("mbtiSources").replaceChildren();
  byId("radarContainer").replaceChildren();
  byId("tagCloud").replaceChildren();
  text("botSummaryText", "");
  byId("apiPortraitDetails").replaceChildren();
  byId("apiPortraitDetails").hidden = true;
  text("stripDbPath", "正在读取");
  text("stripMsgCount", "正在读取");
  text("stripMessageLabel", "消息：");
  text("stripTextLabel", "文本：");
  text("stripConfidence", "");
  byId("stripConfidenceItem").style.display = "none";
  text("summaryBadge", "基于聊天汇总");
  setStripStatus("读取画像中");
  byId("groupMemberTabs").replaceChildren();
  byId("groupMemberTabs").style.display = "none";
  portraitState.memberRenderedScope = null;
  portraitState.renderedProfileKey = null;
  portraitState.renderedProfileSignature = null;
  // The API portrait shares this DOM, so its render memo must be invalidated too;
  // otherwise a same-scope re-entry skips re-rendering and the strip stays blank.
  portraitState.renderedApiProfileScope = null;
  portraitState.renderedApiProfileSignature = null;
  portraitState.apiPortraitProgressNode = null;
}
const preferenceAxes = [
  { key: "EI", left: "E", right: "I", meaning: "注意力与能量：外向互动 / 内向反思" },
  { key: "SN", left: "S", right: "N", meaning: "信息偏好：具体经验事实 / 模式与可能性" },
  { key: "TF", left: "T", right: "F", meaning: "决策偏好：客观逻辑原则 / 价值与对人的影响" },
  { key: "JP", left: "J", right: "P", meaning: "对外界的方式：结构收敛 / 保留选项、灵活探索" }
];
const officialSources = [
  "https://www.myersbriggs.org/my-mbti-personality-type/the-mbti-preferences/",
  "https://www.themyersbriggs.com/en-US/Products-and-Services/Myers-Briggs"
];
function renderMbti(profile) {
  const container = byId("mbtiScalesList");
  const sources = byId("mbtiSources");
  container.replaceChildren();
  sources.replaceChildren();
  const groupOverall = profile.isGroup && !portraitState.activeMember;
  byId("mbtiCard").hidden = groupOverall;
  byId("heroMbtiRow").hidden = groupOverall;
  if (groupOverall) {
    text("heroMbti", "");
    return;
  }

  if (profile.apiSource) {
    // API portrait reuses the Laya card: same threshold, lock panel and evidence
    // section. Axis shares arrive as 0-100 favoring the left letter.
    const eligible = Number(profile.apiTargetTexts) || 0;
    const minMessages = 100;
    const axes = {};
    for (const axis of preferenceAxes) {
      const share = apiScore(profile.apiMbtiAxes?.[axis.key]);
      axes[axis.key] = share === null ? null :
        { leftShare: share / 100, rightShare: (100 - share) / 100, evidenceCount: 1 };
    }
    profile = { ...profile, mbtiInference: { eligibleMessages: eligible, minMessages,
      axes, sources: officialSources } };
  }

  const inference = profile.mbtiInference;
  const eligible = Number(inference?.eligibleMessages) || 0;
  const minMessages = Number(inference?.minMessages) || 100;
  const axes = inference?.axes || {};
  // Match the existing local backend's MIN_AXIS_EVIDENCE / MIN_AXIS_MARGIN.
  // API axes are provider estimates and retain their separate existing contract.
  const validAxis = evidence => Number(evidence?.evidenceCount) >= (profile.apiSource ? 1 : 30) &&
    Number.isFinite(evidence?.leftShare) && Number.isFinite(evidence?.rightShare) &&
    evidence.leftShare >= 0 && evidence.leftShare <= 1 &&
    evidence.rightShare >= 0 && evidence.rightShare <= 1;

  if (eligible < minMessages) {
    text("heroMbti", `${Math.max(0, eligible)}/${minMessages} 条`);
    text("mbtiScaleBadge", "未解锁");
    const lockPanel = element("div", "mbti-lock-panel");
    const iconWrap = element("div", "mbti-lock-icon-wrap");
    iconWrap.appendChild(svgIcon("M18 8h-1V6c0-2.76-2.24-5-5-5S7 3.24 7 6v2H6c-1.1 0-2 .9-2 2v10c0 1.1.9 2 2 2h12c1.1 0 2-.9 2-2V10c0-1.1-.9-2-2-2zm-6 9c-1.1 0-2-.9-2-2s.9-2 2-2 2 .9 2 2-.9 2-2 2zm3.1-9H8.9V6c0-1.71 1.39-3.1 3.1-3.1 1.71 0 3.1 1.39 3.1 3.1v2z", "mbti-lock-icon"));
    lockPanel.appendChild(iconWrap);
    lockPanel.appendChild(element("div", "mbti-lock-title", "人格推测未解锁"));
    lockPanel.appendChild(element("div", "mbti-lock-desc", `需积累 ${minMessages} 条该人物有效文本以进行四维偏好推测`));
    const progWrap = element("div", "mbti-lock-progress-wrap");
    const progBar = element("div", "mbti-lock-progress-bar");
    const fill = element("div", "mbti-lock-progress-fill");
    fill.style.width = `${Math.min(100, Math.round(eligible / minMessages * 100))}%`;
    progBar.appendChild(fill);
    progWrap.appendChild(progBar);
    progWrap.appendChild(element("div", "mbti-lock-progress-text", `目标人物文本 ${eligible} / ${minMessages} 条`));
    lockPanel.appendChild(progWrap);
    const btn = element("button", "mbti-unlock-btn disabled", `积累 ${minMessages} 条后解锁`);
    btn.disabled = true;
    lockPanel.appendChild(btn);
    container.appendChild(lockPanel);
  } else {
    const inclination = preferenceAxes.map(axis => {
      const evidence = axes[axis.key];
      if (!validAxis(evidence) || evidence.leftShare === evidence.rightShare) return "?";
      if (!profile.apiSource && Math.abs(evidence.leftShare - evidence.rightShare) < .2) return "?";
      return evidence.leftShare > evidence.rightShare ? axis.left : axis.right;
    }).join("");
    const determined = Array.from(inclination).filter(value => value !== "?").length;
    text("heroMbti", determined ? inclination : "待判断");
    text("mbtiScaleBadge", inclination.includes("?") ? determined ? "部分维度待定" : "偏好证据待积累" : `${inclination} · 聊天倾向`);
    for (const axis of preferenceAxes) {
      const evidence = axes[axis.key];
      const row = element("div", "mbti-scale-row");
      row.appendChild(element("div", "mbti-scale-meta", axis.key));
      const leftShare = evidence?.leftShare;
      const rightShare = evidence?.rightShare;
      if (validAxis(evidence)) {
        row.appendChild(element("div", "mbti-axis-values", `${axis.left} ${Math.round(leftShare * 100)}% · ${axis.right} ${Math.round(rightShare * 100)}%`));
        const track = element("div", "mbti-track-wrap");
        const fill = element("div", "mbti-track-fill");
        fill.style.width = `${leftShare * 100}%`;
        track.appendChild(fill);
        row.appendChild(track);
      } else row.appendChild(element("div", "mbti-axis-empty", `${axis.left}/${axis.right} · 尚无足够证据`));
      container.appendChild(row);
    }
  }

  const provided = Array.isArray(inference?.sources) ? inference.sources : [];
  const urls = provided.filter(value => officialSources.includes(value));
  const details = element("details", "mbti-details");
  details.appendChild(element("summary", "", "依据与说明"));
  details.appendChild(element("div", "mbti-sample", `${eligible} 条目标文本 · ${minMessages} 条展示门槛`));
  for (const [index, url] of (urls.length ? urls : officialSources).entries()) {
    const link = element("a", "mbti-source", index ? "Myers & Briggs 官方资料" : "MBTI 官方偏好理论");
    link.href = url;
    link.target = "_blank";
    link.rel = "noopener noreferrer";
    details.appendChild(link);
  }
  details.appendChild(element("span", "mbti-source-note", "聊天证据推测，非标准量表。"));
  sources.appendChild(details);
}
function renderRadar(traits, apiSource = false) {
  const container = byId("radarContainer");
  container.replaceChildren();
  const keys = ["socialEnergy", "humor", "composure", "initiative", "care", "affection"];
  const values = keys.map(key => Array.isArray(traits) ? traits.find(item => item?.key === key) : null);
  if (values.some(item => !item || !Number.isFinite(item.val) || item.val < 0 || item.val > 100)) {
    if (apiSource) {
      const legend = element("div", "radar-values");
      values.forEach((item, index) => {
        const row = element("div", "radar-value");
        row.append(element("span", "", item?.label || apiTraitLabels[keys[index]]),
          element("strong", "", apiScore(item?.val) ?? "待判断"));
        legend.appendChild(row);
      });
      container.appendChild(legend);
    } else container.appendChild(element("div", "radar-empty", "暂无互动风格证据"));
    return;
  }
  const svg = document.createElementNS("http://www.w3.org/2000/svg", "svg");
  svg.setAttribute("viewBox", "0 0 360 280");
  svg.setAttribute("class", "radar-chart");
  svg.setAttribute("role", "img");
  svg.setAttribute("aria-label", "六维互动风格雷达");
  const point = (index, radius) => {
    const angle = -Math.PI / 2 + index * Math.PI / 3;
    return `${(180 + Math.cos(angle) * radius).toFixed(1)},${(140 + Math.sin(angle) * radius).toFixed(1)}`;
  };
  const shape = (tag, className, points) => {
    const node = document.createElementNS("http://www.w3.org/2000/svg", tag);
    node.setAttribute("class", className);
    node.setAttribute("points", points);
    svg.appendChild(node);
  };
  for (const radius of [25, 50, 75, 100]) shape("polygon", "radar-ring", keys.map((_, index) => point(index, radius)).join(" "));
  for (let index = 0; index < keys.length; index++) shape("polyline", "radar-axis", `180,140 ${point(index, 100)}`);
  shape("polygon", "radar-area", values.map((item, index) => point(index, item.val)).join(" "));
  values.forEach((item, index) => {
    const [x, y] = point(index, 100).split(",").map(Number);
    const side = index === 1 || index === 2 ? 1 : index === 4 || index === 5 ? -1 : 0;
    const label = document.createElementNS("http://www.w3.org/2000/svg", "text");
    label.setAttribute("class", "radar-label");
    label.setAttribute("x", String(x + side * 14));
    label.setAttribute("y", String(y + (index === 0 ? -18 : index === 3 ? 20 : 0)));
    label.setAttribute("text-anchor", side > 0 ? "start" : side < 0 ? "end" : "middle");
    label.setAttribute("dominant-baseline", "middle");
    label.textContent = item.label;
    svg.appendChild(label);
  });
  const legend = element("div", "radar-values");
  for (const item of values) {
    const row = element("div", "radar-value");
    row.append(element("span", "", item.label), element("strong", "", `${Math.round(item.val)}`));
    legend.appendChild(row);
  }
  container.append(svg, legend);
}
function renderMembers(profile) {
  const members = byId("groupMemberTabs");
  const scope = `${chatState.currentUser}\u0000${portraitState.activeMember}`;
  const previousSearch = members.querySelector(".member-search");
  const previousPicker = members.querySelector(".member-picker");
  const previous = scope === portraitState.memberRenderedScope && previousSearch ? {
    open: previousPicker?.classList.contains("open"), query: previousSearch.value,
    start: previousSearch.selectionStart, end: previousSearch.selectionEnd,
    focused: document.activeElement === previousSearch,
    page: Number(previousPicker?.dataset.page) || 0
  } : null;
  members.replaceChildren();
  portraitState.memberRenderedScope = profile.isGroup ? scope : null;
  members.style.display = profile.isGroup || window.ProductConfig?.key === "qq" ? "flex" : "none";
  if (!profile.isGroup && window.ProductConfig?.key === "qq") {
    for (const [id, name] of [["", "对方"], ["self", "我"]]) {
      const button = element("button", `member-chip${portraitState.activeMember === id ? " active" : ""}`, name);
      button.type = "button";
      button.addEventListener("click", () => { if (portraitState.activeMember !== id) void loadProfile(id); });
      members.appendChild(button);
    }
    return;
  }
  if (!profile.isGroup) return;
  if (Array.isArray(profile.members) && profile.members.length) portraitState.groupMembers = profile.members;
  const overall = element("button", `member-chip${portraitState.activeMember ? "" : " active"}`, window.ProductConfig?.key==='qq'?'群概览':'群整体');
  overall.type = "button";
  overall.addEventListener("click", () => { closeMemberPicker(); if (portraitState.activeMember) void loadProfile(""); });
  members.appendChild(overall);
  if (portraitState.activeMember) members.appendChild(element("span", "member-current", portraitState.groupMembers.find(item => item.id === portraitState.activeMember)?.name || profile.name || portraitState.activeMember));
  const picker = element("div", "member-picker");
  const trigger = element("button", "member-picker-trigger", "选择成员");
  trigger.type = "button";
  trigger.setAttribute("aria-expanded", "false");
  const panel = element("div", "member-picker-panel");
  const search = element("input", "member-search");
  search.type = "search";
  search.placeholder = "搜索成员";
  search.setAttribute("aria-label", "搜索群成员");
  const list = element("div", "member-options");
  const pager = element("div", "member-pager");
  const previousPage = element("button", "member-page-btn", "上一页");
  const pageText = element("span", "member-page-text");
  const nextPage = element("button", "member-page-btn", "下一页");
  previousPage.type = nextPage.type = "button";
  pager.append(previousPage, pageText, nextPage);
  let page = previous?.page ?? Math.max(0, Math.floor(portraitState.groupMembers.findIndex(item => item.id === portraitState.activeMember) / 6));
  function renderPage() {
    const query = search.value.trim().toLowerCase();
    const filtered = portraitState.groupMembers.filter(member => `${member.name || ""} ${member.id || ""}`.toLowerCase().includes(query));
    const pageCount = Math.max(1, Math.ceil(filtered.length / 6));
    page = Math.min(Math.max(0, page), pageCount - 1);
    picker.dataset.page = String(page);
    list.replaceChildren();
    for (const member of filtered.slice(page * 6, page * 6 + 6)) {
      const option = element("button", `member-option${member.id === portraitState.activeMember ? " active" : ""}`, member.name || member.id);
      option.type = "button";
      option.addEventListener("click", () => { closeMemberPicker(); void loadProfile(member.id); });
      list.appendChild(option);
    }
    if (!filtered.length) list.appendChild(element("div", "member-empty", "没有匹配成员"));
    pager.hidden = pageCount <= 1;
    pageText.textContent = `${page + 1} / ${pageCount}`;
    previousPage.disabled = page === 0;
    nextPage.disabled = page >= pageCount - 1;
    list.scrollTop = 0;
  }
  search.addEventListener("input", () => { page = 0; renderPage(); });
  previousPage.addEventListener("click", () => { page--; renderPage(); });
  nextPage.addEventListener("click", () => { page++; renderPage(); });
  trigger.addEventListener("click", () => { const open = picker.classList.toggle("open"); trigger.setAttribute("aria-expanded", String(open)); if (open) search.focus(); });
  panel.append(search, list, pager);
  picker.append(trigger, panel);
  members.appendChild(picker);
  if (previous) {
    search.value = previous.query;
    picker.classList.toggle("open", !!previous.open);
    trigger.setAttribute("aria-expanded", String(!!previous.open));
    if (previous.focused && previous.open) {
      search.focus();
      search.setSelectionRange(previous.start, previous.end);
    }
  }
  renderPage();
}
function renderProfile(profile) {
  const group = !!profile.isGroup;
  const own = !group && profile.targetSide === "self";
  portraitState.apiPortraitProgressNode = null;
  text("personaHeaderTitle", profile.name || profile.username || "人物画像");
  text("heroName", profile.name || profile.username || "未知");
  setAvatar("heroAvatar", profile.avatar, profile.avatarCandidates, profile.name || profile.username, group && !portraitState.activeMember);
  renderMbti(profile);
  byId("heroArchetype").style.display = "none";
  text("heroRelationBadge", own ? "我在此单聊的画像" : group ? portraitState.activeMember ? "群成员画像" : profile.objectiveOverview?'群概览':'群画像' : profile.affinity == null ?
    profile.apiSource ? "好感待判断" : "好感待分析" : `好感 ${profile.affinity}`);
  const stats = profile.stats || {};
  text("stripMessageLabel", own ? "我的消息：" : group ? portraitState.activeMember ? "成员消息：" : "群消息：" :
    profile.apiSource && window.ProductConfig?.key !== "qq" ? "会话消息：" : "对方消息：");
  text("stripTextLabel", own ? "我的文本：" : group ? portraitState.activeMember ? "成员文本：" : "群文本：" : "对方文本：");
  text("stripAnalysisLabel", "已分析：");
  const messageCount = profile.apiSource && stats.messageCount === null ? "—" : Number(stats.messageCount) || 0;
  text("stripDbPath", group && !portraitState.activeMember ?
    `${messageCount} 条 · ${Number(stats.participantCount) || 0} 人参与` :
    `${messageCount} 条`);
  text("stripMsgCount", `${Number(stats.textCount) || 0} 条`);
  updateProfileProgress(profile);
  const metric = byId("heroMetricBox");
  metric.replaceChildren();
  if (group || own) {
    const grid = element("div", "group-activity-grid");
    const figures = profile.objectiveOverview ? [["已读范围参与人数",stats.participantCount],["已读消息",messageCount],
      ["可分析文本",stats.textCount],["画像对象","选择成员"]] : own ? [["本单聊我的消息", messageCount], ["我的文本", stats.textCount],
      ["已分析文本", `${Number(stats.analyzedCount) || 0} / ${Number(stats.textCount) || 0}`]] :
      [["参与人数", stats.participantCount], ["消息数", messageCount], ["文本消息", stats.textCount],
        ["已分析文本", `${Number(stats.analyzedCount) || 0} / ${Number(stats.textCount) || 0}`]];
    for (const [label, value] of figures) {
      const card = element("div", "group-stat-card");
      const valueNode = element("span", "group-stat-val", value);
      if (profile.apiSource && label === "已分析文本") portraitState.apiPortraitProgressNode = valueNode;
      card.append(element("span", "group-stat-label", label), valueNode);
      grid.appendChild(card);
    }
    metric.appendChild(grid);
  } else {
    const score = profile.affinity;
    const header = element("div", "game-favor-header");
    header.append(element("div", "game-favor-title-wrap", "♥ 好感度等级"), element("div", "game-favor-score",
      score == null ? profile.apiSource ? "待判断" : "待分析" : String(score)));
    metric.appendChild(header);
    if (typeof score === "number" && score >= 0 && score <= 100) {
      const names = ["素昧平生", "泛泛之交", "初识相知", "友善默契", "亲密无间"];
      const level = Math.min(5, Math.floor(score / 20) + 1);
      const track = element("div", "favor-heart-track");
      names.forEach((name, index) => {
        const stage = index + 1;
        const node = element("div", `heart-stage-node${stage <= level ? " unlocked" : ""}${stage === level ? " current" : ""}`);
        node.append(element("div", "heart-icon-wrap", stage === level ? "❤️" : stage < level ? "💖" : "🤍"), element("span", "heart-stage-name", name));
        track.appendChild(node);
      });
      const progress = element("div", "game-favor-progress-wrap");
      const bar = element("div", "game-favor-progress-bar");
      const fill = element("div", "game-favor-progress-fill");
      fill.style.width = `${score}%`;
      bar.appendChild(fill);
      progress.appendChild(bar);
      metric.append(track, progress);
    }
  }
  renderRadar(profile.traits, !!profile.apiSource);
  text("tagCardTitle", profile.apiSource ? "常见话题" : "高频词");
  text("tagCardBadge", profile.apiSource ? "API 画像" : "词频统计 Top 6");
  const tags = byId("tagCloud");
  tags.replaceChildren();
  for (const keyword of (profile.keywords || []).map((value, index) => ({ value, index })).sort((left, right) => (Number(right.value.count) || 0) - (Number(left.value.count) || 0) || left.index - right.index).slice(0, 6).map(item => item.value)) {
    const tag = element("span", "keyword-tag", keyword.word);
    if (!profile.apiSource) tag.appendChild(element("span", "tag-count", ` ${keyword.count}`));
    tags.appendChild(tag);
  }
  if (!tags.childNodes.length) tags.textContent = profile.apiSource ? "待判断" : "暂无关键词";
  renderPortraitSummary(profile);
  renderApiPortraitDetails(null);
  renderMembers(profile);
}
portraitState.apiPortraitRequest = 0;
portraitState.apiPortraitPollTimer = null;
portraitState.apiPortraitSnapshot = null;
portraitState.apiPortraitBusy = false;
portraitState.renderedApiPortraitKey = null;
portraitState.apiPortraitLoadingKey = null;
portraitState.apiPortraitSubmitErrors = new Map();
function rememberApiPortraitSubmitError(key, message) {
  portraitState.apiPortraitSubmitErrors.delete(key);
  portraitState.apiPortraitSubmitErrors.set(key, message);
  while (portraitState.apiPortraitSubmitErrors.size > 64)
    portraitState.apiPortraitSubmitErrors.delete(portraitState.apiPortraitSubmitErrors.keys().next().value);
}
portraitState.apiPortraitReadFailures = 0;
portraitState.renderedApiPortraitScopeKey = null;
portraitState.renderedApiProfileScope = null;
portraitState.renderedApiProfileSignature = null;
portraitState.apiPortraitProgressNode = null;
function renderPortraitSummary(localProfile) {
  text("botSummaryText", localProfile?.summary || (localProfile?.apiSource ?
    localProfile.apiPortrait ? "待判断" : "待分析" : "暂无摘要"));
  text("summaryBadge", localProfile?.objectiveOverview?'已读范围':localProfile?.apiSource ? `API · ${settingsState.modelSourceSnapshot.api?.model || "模型"}` : "本地 Laya");
}
const apiTraitLabels = {
  socialEnergy: "表达活力", humor: "幽默表达", composure: "情绪平和",
  initiative: "话题主动", care: "关怀支持", affection: "亲近表达",
};
function apiScore(value) {
  return Number.isInteger(value) && value >= 0 && value <= 100 ? value : null;
}
function apiPortraitHasContent(portrait) {
  if (!portrait || typeof portrait !== "object") return false;
  return ["summary", "communication", "emotionExpression", "interactionPreferences"]
    .some(key => typeof portrait[key] === "string" && portrait[key].trim()) ||
    ["topics", "patterns", "boundaries", "uncertain"]
      .some(key => Array.isArray(portrait[key]) && portrait[key].length) ||
    apiScore(portrait.affinity) !== null ||
    Object.values(portrait.mbtiAxes || {}).some(value => apiScore(value) !== null) ||
    Object.values(portrait.traits || {}).some(value => apiScore(value) !== null);
}
function renderApiPortraitDetails(portrait) {
  const container = byId("apiPortraitDetails");
  container.replaceChildren();
  container.hidden = !portrait;
  if (!portrait) return;
  for (const [label, value] of [["交流方式", portrait.communication], ["情绪表达", portrait.emotionExpression],
    ["互动偏好", portrait.interactionPreferences], ["交流模式", portrait.patterns],
    ["明确边界", portrait.boundaries], ["证据不足", portrait.uncertain]]) {
    const content = Array.isArray(value) ? value.join("、") : value;
    const row = element("div", "api-portrait-detail");
    row.append(element("strong", "", label), element("span", "", content || "待判断"));
    container.appendChild(row);
  }
}
function apiProcessedTargets(available, progress, fallback = 0) {
  const target = Number(available?.targetTextCount) || 0;
  if (Number.isSafeInteger(progress?.processedTargetTexts) && progress.processedTargetTexts >= 0)
    return Math.min(target || fallback, progress.processedTargetTexts);
  const total = Number(progress?.total) || 0;
  const processed = Number(progress?.processed) || 0;
  if (progress?.complete === true || total <= 0) return target || fallback;
  // Only the processed share of target texts counts as MBTI evidence, so an
  // unfinished portrait cannot unlock from messages it has not analyzed yet.
  return Math.min(target, Math.floor(target * Math.max(0, Math.min(1, processed / total))));
}
function renderApiProfile(data) {
  const identity = data.identity;
  const available = data.available || {};
  const progress = (data.stale && data.hasPublishedAnalysis ? data.publishedProgress : data.progress) || data.progress || {};
  const portrait = data.portrait;
  const group = identity.isGroup;
  const groupOverall = group && !portraitState.activeMember;
  const analyzedTargets = apiProcessedTargets(available, progress);
  const analyzed = groupOverall ? Number(progress.processed) || 0 : analyzedTargets;
  const analysisTotal = groupOverall ? Number(progress.total) || Number(available.textCount) || 0 :
    Number(progress.totalTargetTexts) || Number(available.targetTextCount) || 0;
  const messageCount = group || window.ProductConfig?.key === "qq" ? identity.messageCount : available.messageCount;
  const cached = cachedProfileFor(data.account, chatState.currentUser, portraitState.activeMember, data.sourceId);
  const stats = {
    messageCount: Number.isSafeInteger(messageCount) && messageCount >= 0 ? messageCount :
      cached?.stats?.messageCount ?? null,
    textCount: group ? Number(identity.textCount) || 0 : Number(available.targetTextCount) || 0,
    analyzedCount: analyzed,
    participantCount: group ? identity.members.length : 1,
  };
  const profile = {
    account: data.account, ...identity, isGroup: group, members: identity.members, stats,
    apiSource: true, apiSourceId: data.sourceId, apiPortrait: portrait,
    apiMbtiAxes: portrait?.mbtiAxes || null,
    apiTargetTexts: analyzedTargets,
    apiProgressProcessed: analyzed,
    apiProgressTotal: analysisTotal,
    apiComplete: progress.complete === true,
    apiRunning: ["queued", "running"].includes(data.job?.status),
    stale: data.stale === true, hasPublishedAnalysis: data.hasPublishedAnalysis === true,
    analysisRevision: data.analysisRevision ?? null, apiRebuildError: data.job?.status === "error",
    affinity: group || identity.targetSide === 'self' ? null : apiScore(portrait?.affinity),
    traits: Object.entries(apiTraitLabels).map(([key, label]) =>
      ({ key, label, val: apiScore(portrait?.traits?.[key]) })),
    keywords: Array.isArray(portrait?.topics) ? portrait.topics.map(word => ({ word })) : [],
    summary: portrait?.summary || "",
  };
  portraitState.renderedProfileKey = null;
  portraitState.renderedProfileSignature = null;
  renderProfile(profile);
  // Persist the same bounded display snapshot the local Laya path uses, keyed by the
  // active source id so a cold start or A->B->A renders before the slow GET.
  rememberProfile(profile, data.account, chatState.currentUser, portraitState.activeMember, data.sourceId);
}
function updateApiProfileProgress(data) {
  const progress = data.progress || {};
  const groupOverall = !!data.identity.isGroup && !portraitState.activeMember;
  const processed = groupOverall ? Number(progress.processed) || 0 :
    apiProcessedTargets(data.available, progress);
  const total = groupOverall ? Number(progress.total) || 0 :
    Number(progress.totalTargetTexts) || Number(data.available?.targetTextCount) || 0;
  const progressFields = {
    apiSource: true, isGroup: !!data.identity.isGroup,
    apiProgressProcessed: processed, apiProgressTotal: total,
    apiComplete: progress.complete === true,
    apiRunning: ["queued", "running"].includes(data.job?.status),
    stale: data.stale === true, hasPublishedAnalysis: data.hasPublishedAnalysis === true,
    analysisRevision: data.analysisRevision ?? null, apiRebuildError: data.job?.status === "error",
  };
  const cached = cachedProfileFor(data.account, chatState.currentUser, portraitState.activeMember, data.sourceId);
  if (cached) rememberProfile({
    ...cached, ...progressFields,
    apiTargetTexts: apiProcessedTargets(data.available, progress),
    stats: { ...cached.stats, analyzedCount: processed },
  }, data.account, chatState.currentUser, portraitState.activeMember, data.sourceId);
  updateProfileProgress(progressFields);
  if (portraitState.apiPortraitProgressNode) portraitState.apiPortraitProgressNode.textContent = `${processed} / ${total}`;
}
function clearApiPortraitView() {
  portraitState.apiPortraitSnapshot = null;
  portraitState.renderedApiProfileScope = null;
  portraitState.renderedApiProfileSignature = null;
  portraitState.apiPortraitProgressNode = null;
  clearProfileView(chatState.sessions.get(chatState.currentUser)?.name || chatState.currentUser || "正在读取画像…");
  text("tagCardTitle", "常见话题");
  text("tagCardBadge", "API 画像");
  byId("apiPortraitStatus").hidden = false;
  byId("btnRetryApiPortrait").hidden = true;
  text("apiPortraitStatus", "正在读取会话消息");
}
function cancelApiPortraitPoll() {
  ++portraitState.apiPortraitRequest;
  clearTimeout(portraitState.apiPortraitPollTimer);
  portraitState.apiPortraitPollTimer = null;
}
function syncPortraitMode() {
  const apiMode = settingsState.modelSourceResolved && settingsState.modelSourceSnapshot.mode === "api";
  byId("personaDashboard").hidden = false;
  byId("apiPortraitStatus").hidden = !apiMode;
  if (!apiMode) byId("btnRetryApiPortrait").hidden = true;
  text("portraitSourceBadge", apiMode ? `API ${settingsState.modelSourceSnapshot.api?.model || "模型"}` : "本地 Laya");
  if (!apiMode) cancelApiPortraitPoll();
  return apiMode;
}
function renderApiPortrait(data) {
  const scope = JSON.stringify([data.account, chatState.currentUser, data.sourceId, data.subject]);
  if(data.objectiveOverview && !portraitState.activeMember) {
    const overview={...data.overview,apiSource:true,apiSourceId:data.sourceId,apiComplete:true,apiProgressProcessed:0,apiProgressTotal:0};
    renderProfile(overview);
    rememberProfile(overview,data.account,chatState.currentUser,'',data.sourceId);
    portraitState.apiPortraitSnapshot=data;
    portraitState.renderedApiProfileScope=scope;
    byId('apiPortraitStatus').hidden=true;
    byId('btnRetryApiPortrait').hidden=true;
    return;
  }
  const previous = portraitState.apiPortraitSnapshot;
  const cached = cachedProfileFor(data.account, chatState.currentUser, portraitState.activeMember, data.sourceId);
  const portrait = data.portrait == null && portraitState.renderedApiProfileScope === scope &&
    apiPortraitHasContent(previous?.portrait) ? previous.portrait :
    data.portrait == null && apiPortraitHasContent(cached?.apiPortrait) ? cached.apiPortrait : data.portrait;
  const displayData = portrait === data.portrait ? data : { ...data, portrait };
  const signature = JSON.stringify([scope, displayData.identity,
    displayData.available?.messageCount, displayData.available?.targetTextCount,
    displayData.portrait, displayData.dataRevision, displayData.analysisRevision, displayData.stale]);
  portraitState.apiPortraitSnapshot = displayData;
  if ((!cached || data.inventoryReady && data.available) &&
      (scope !== portraitState.renderedApiProfileScope || signature !== portraitState.renderedApiProfileSignature)) {
    renderApiProfile(displayData);
    portraitState.renderedApiProfileScope = scope;
    portraitState.renderedApiProfileSignature = signature;
  } else if (!cached || data.inventoryReady && data.available) updateApiProfileProgress(displayData);
  const available = data.available;
  const progress = data.progress || {};
  const job = data.job || {};
  const ready = data.inventoryReady === true && !!available;
  const targetTexts = Number(available?.targetTextCount) || 0;
  const total = Number(available?.textCount) || Number(progress.total) || 0;
  const running = ["queued", "running"].includes(job.status);
  const upToDate = ready && progress.complete === true && Number(progress.processed) >= total;
  const contextReady = Number.isSafeInteger(settingsState.modelSourceSnapshot.api?.contextTokens) &&
    settingsState.modelSourceSnapshot.api.contextTokens >= 4096;
  const portraitErrors = {
    "context-too-long": "模型不支持当前上下文大小，请在设置中调低",
    "invalid-output": "模型返回格式不正确", "invalid-portrait": "模型画像结果不完整",
    "timeout": "模型响应超时", "rate-limit": "接口请求受限",
    "empty-response": "模型未返回内容", "response-too-large": "模型返回内容过长",
    "auth": "API Key 无效", "network": "网络连接失败",
    "provider-error": "模型服务返回错误",
  };
  let state = "";
  if (data.suspended) state = "API 画像缓存已暂停";
  else if (job.status === "error") state = portraitErrors[job.error] || "API 画像分析失败";
  else if (running && job.phase === "preparing") state = "正在准备待分析文本";
  else if (!ready) state = data.inventoryStatus === "error" ? "会话读取失败，正在重试" : "正在读取会话消息";
  else if (targetTexts < 3) state = "目标发言不足 3 条，等待更多消息";
  else if (!contextReady) state = "请在设置中填写模型上下文大小";
  else if (running) state = "API 分析中";
  else if (upToDate) state = "API 画像已更新";
  else state = "正在准备 API 画像";
  const autoKey = portraitState.renderedApiPortraitKey + ":" + total + ":" + (Number(available?.totalChars) || 0) +
    (Number.isSafeInteger(data.dataRevision) ? ":revision:" + data.dataRevision : "");
  const submitError = portraitState.apiPortraitSubmitErrors.get(autoKey);
  if (submitError && !running && job.status !== "error") state = "API 画像提交失败（" + submitError + "）";
  if (job.retry && running) {
    const remaining = Math.max(0, Math.ceil((Number(job.retry.nextAtMs) - Date.now()) / 1000));
    const reason = portraitErrors[job.retry.reason] || "模型响应失败";
    state = `${reason}，${remaining} 秒后自动重试 ${job.retry.attempt}/${job.retry.max}`;
    setStripStatus(`自动重试 ${job.retry.attempt}/${job.retry.max} · ${remaining} 秒`);
  } else if (running && job.phase === "preparing") {
    setStripStatus("正在准备待分析文本");
  } else if (running) {
    const elapsed = Math.max(0, Math.floor((Date.now() - Number(job.batchStartedAtMs || job.startedAtMs || Date.now())) / 1000));
    const rate = Number(job.rateTextsPerSecond);
    const speed = Number.isFinite(rate) && rate > 0 ? `上批 ${rate.toFixed(1)} 条/秒` : "等待首批结果";
    setStripStatus(`API 分析中 · ${elapsed} 秒 · ${speed}`);
  } else if (job.status === "error" || submitError) {
    setStripStatus("分析失败，请重试");
  }
  if (data.stale && data.hasPublishedAnalysis) {
    const retained = job.status === "error" || submitError ? "重算失败，保留旧 API 画像" : "保留旧 API 画像，等待重算完成";
    setStripStatus(retained);
    text("summaryBadge", "旧记录分析");
    state = retained + (state ? " · " + state : "");
  }
  byId("apiPortraitStatus").hidden = false;
  text("apiPortraitStatus", state);
  // Like local Laya, entering/switching back to a conversation automatically continues
  // the saved incremental analysis. The backend resumes from the persisted cursor and
  // sends only new/unprocessed text, and dedupes an in-flight job. A terminal error stops
  // the loop and keeps the prior portrait with an actionable Manual retry.
  const canContinue = ready && targetTexts >= 3 && contextReady && !data.suspended && !running &&
    !upToDate && job.status !== "error" && !submitError;
  const action = byId("btnRetryApiPortrait");
  action.hidden = !(job.status === "error" && ready || submitError);
  action.textContent = "重试分析";
  if (canContinue && !portraitState.apiPortraitBusy) {
    void startApiPortrait(autoKey);
  }
}
async function loadApiPortrait(member = "") {
  if (!chatState.currentUser || !chatState.currentAccount || !settingsState.modelSourceResolved ||
      settingsState.modelSourceSnapshot.mode !== "api" || member !== portraitState.activeMember) return;
  const account = chatState.currentAccount, user = chatState.currentUser, sourceId = settingsState.modelSourceSnapshot.sourceId;
  const subject = member || user;
  const scopeKey = JSON.stringify([account, user, sourceId, subject]);
  const cacheKey = profileCacheKey(account, user, member, sourceId);
  const portraitKey = JSON.stringify([account, user, sourceId, subject, settingsState.modelSourceSnapshot.api?.contextTokens]);
  if (portraitState.apiPortraitLoadingKey === portraitKey) return;
  clearTimeout(portraitState.apiPortraitPollTimer);
  const token = ++portraitState.apiPortraitRequest;
  portraitState.apiPortraitLoadingKey = portraitKey;
  if (portraitKey !== portraitState.renderedApiPortraitKey) {
    portraitState.renderedApiPortraitKey = portraitKey;
    portraitState.apiPortraitReadFailures = 0;
    if (scopeKey !== portraitState.renderedApiPortraitScopeKey) {
      portraitState.renderedApiPortraitScopeKey = scopeKey;
      // Reuse the Laya snapshot store: render this account/conversation/source's saved
      // portrait immediately; only a first visit with no snapshot shows the loading state.
      if (!renderCachedApiProfile(account, user, member, sourceId)) clearApiPortraitView();
    }
  }
  if (portraitState.renderedApiProfileScope !== scopeKey && portraitState.renderedProfileKey !== cacheKey)
    renderCachedApiProfile(account, user, member, sourceId);
  const params = new URLSearchParams({ user });
  if (member) params.set("member", member);
  try {
    const data = await api("/api/model-portrait?" + params, {}, chatState.controller?.signal);
    if (token !== portraitState.apiPortraitRequest || account !== chatState.currentAccount || user !== chatState.currentUser ||
        sourceId !== settingsState.modelSourceSnapshot.sourceId || member !== portraitState.activeMember || chatState.view !== "persona") return;
    if (data?.account !== account || data.sourceId !== sourceId || data.subject !== subject ||
        !data.identity || data.identity.username !== subject ||
        typeof data.identity.isGroup !== "boolean" || !Array.isArray(data.identity.members) ||
        typeof data.inventoryReady !== "boolean" ||
        (data.inventoryReady && (!data.available ||
          !(data.available.totalChars === null || Number.isSafeInteger(data.available.totalChars) && data.available.totalChars >= 0) ||
          !Number.isSafeInteger(data.available.textCount) || data.available.textCount < 0)))
      throw new Error("画像数据无效");
    renderApiPortrait(data);
    if (data.inventoryReady) portraitState.profileSnapshotsRequireRefresh.delete(cacheKey);
    portraitState.apiPortraitReadFailures = 0;
    if (!data.inventoryReady || ["queued", "running"].includes(data.job?.status))
      portraitState.apiPortraitPollTimer = setTimeout(() => { void loadApiPortrait(member); }, 2200);
  } catch (error) {
    if (token === portraitState.apiPortraitRequest && error?.name !== "AbortError") {
      text("apiPortraitStatus", "画像读取失败，请稍后重试");
      portraitState.apiPortraitReadFailures++;
      if (portraitState.apiPortraitReadFailures <= 3)
        portraitState.apiPortraitPollTimer = setTimeout(() => { void loadApiPortrait(member); },
          Math.min(5000, 1500 * portraitState.apiPortraitReadFailures));
      else byId("btnRetryApiPortrait").hidden = false;
    }
  } finally {
    if (portraitState.apiPortraitLoadingKey === portraitKey) portraitState.apiPortraitLoadingKey = null;
  }
}
async function startApiPortrait(autoKey, force = false, refreshAxes = false) {
  const account = chatState.currentAccount, user = chatState.currentUser, sourceId = settingsState.modelSourceSnapshot.sourceId;
  const member = portraitState.activeMember;
  if (!account || !user || settingsState.modelSourceSnapshot.mode !== "api" || portraitState.apiPortraitBusy ||
      portraitState.apiPortraitSubmitErrors.has(autoKey) && !force) return;
  if (force) portraitState.apiPortraitSubmitErrors.delete(autoKey);
  portraitState.apiPortraitBusy = true;
  byId("btnRetryApiPortrait").hidden = true;
  setStripStatus("API 分析中");
  text("apiPortraitStatus", refreshAxes ? "正在根据已保存画像重新评估 MBTI" : "正在提交 API 画像");
  try {
    const body = { account, user, ...(member ? { member } : {}),
      ...(refreshAxes ? { refreshAxes: true } : {}) };
    const data = await api("/api/model-portrait", { method: "POST", body: JSON.stringify(body) });
    if (account !== chatState.currentAccount || user !== chatState.currentUser ||
        sourceId !== settingsState.modelSourceSnapshot.sourceId || member !== portraitState.activeMember) return;
    if (data?.account !== account || data.sourceId !== sourceId) throw new Error("画像任务不匹配");
    portraitState.apiPortraitSubmitErrors.delete(autoKey);
    void loadApiPortrait(member);
  } catch (error) {
    if (account !== chatState.currentAccount || user !== chatState.currentUser ||
        sourceId !== settingsState.modelSourceSnapshot.sourceId || member !== portraitState.activeMember) return;
    const reason = modelSourceRequestError(error);
    rememberApiPortraitSubmitError(autoKey, reason);
    setStripStatus("分析失败，请重试");
    text("apiPortraitStatus", "API 画像提交失败（" + reason + "）");
    byId("btnRetryApiPortrait").hidden = false;
  } finally {
    portraitState.apiPortraitBusy = false;
    if (account === chatState.currentAccount && user === chatState.currentUser && member !== portraitState.activeMember &&
        chatState.view === "persona" && settingsState.modelSourceSnapshot.mode === "api") void loadApiPortrait(portraitState.activeMember);
  }
}
function apiAxesMissing() {
  const snapshot = portraitState.apiPortraitSnapshot;
  const axes = snapshot?.portrait?.mbtiAxes;
  if (!axes || snapshot?.progress?.complete !== true) return false;
  if (!["EI", "SN", "TF", "JP"].every(key => apiScore(axes[key]) === null)) return false;
  return (Number(snapshot.available?.targetTextCount) || 0) >= 100;
}
byId("btnRetryApiPortrait").addEventListener("click", () => {
  const available = portraitState.apiPortraitSnapshot?.available;
  if (!available || !portraitState.renderedApiPortraitKey) {
    portraitState.apiPortraitReadFailures = 0;
    byId("btnRetryApiPortrait").hidden = true;
    void loadApiPortrait(portraitState.activeMember);
    return;
  }
  const autoKey = portraitState.renderedApiPortraitKey + ":" +
    (Number(available.textCount) || 0) + ":" + (Number(available.totalChars) || 0);
  // A finished portrait whose axes are still empty re-evaluates them from the saved
  // cumulative portrait; everything else keeps the normal resume behavior.
  void startApiPortrait(autoKey, true, apiAxesMissing());
});
async function loadProfile(member = "", retry = false) {
  if (!chatState.currentUser || !settingsState.modelSourceResolved) return;
  const apiMode = syncPortraitMode();
  const token = ++portraitState.profileGeneration;
  const account = chatState.currentAccount;
  const user = chatState.currentUser;
  const key = profileCacheKey(account, user, member);
  const previousMember = portraitState.activeMember;
  portraitState.activeMember = member;
  if (chatState.sessions.get(user)?.isGroup || window.ProductConfig?.key === "qq") rememberProfileMember(account, user, member);
  if (apiMode) {
    portraitState.profilePending = false;
    void loadApiPortrait(member);
    return;
  }
  if (key !== portraitState.renderedProfileKey) {
    const cached = cachedProfileFor(account, user, member);
    if (cached) {
      renderProfile(cached);
      portraitState.renderedProfileKey = key;
      portraitState.renderedProfileSignature = JSON.stringify(cached);
    } else {
      const members = previousMember !== member ? portraitState.groupMembers : null;
      clearProfileView(chatState.sessions.get(user)?.name || user);
      if (members) portraitState.groupMembers = members;
    }
  }
  portraitState.profilePending = true;
  setStripStatus(key === portraitState.renderedProfileKey ? "刷新画像中" : "读取画像中");
  try {
    const data = await api(`/api/profile?user=${encodeURIComponent(user)}${member ? `&member=${encodeURIComponent(member)}` : ""}${retry ? "&retry=1" : ""}`, {}, chatState.controller.signal);
    if (token === portraitState.profileGeneration && account === chatState.currentAccount && user === chatState.currentUser && member === portraitState.activeMember) {
      if (!acceptResponseAccount(data.account)) return;
      observeProfileRate(data, key);
      const signature = JSON.stringify(data);
      if (key !== portraitState.renderedProfileKey || signature !== portraitState.renderedProfileSignature) {
        const previous = portraitState.profileCache.get(key);
        try { renderProfile(data); }
        catch (error) {
          if (previous && portraitState.renderedProfileKey === key) {
            try { renderProfile(previous); } catch { }
          }
          console.error("画像显示失败", error);
          setStripStatus("画像显示失败");
          return;
        }
        portraitState.renderedProfileKey = key;
        portraitState.renderedProfileSignature = signature;
      }
      portraitState.profileCache.set(key, data);
      rememberProfile(data, account, user, member);
      portraitState.profileSnapshotsRequireRefresh.delete(key);
      updateProfileProgress(data);
      if (portraitState.currentAnalysisJob) renderJob(portraitState.currentAnalysisJob);
    }
  } catch (error) {
    if (error.name !== "AbortError" && token === portraitState.profileGeneration) {
      if (handleAccountBoundaryError(error)) return;
      const hasVisibleProfile = portraitState.renderedProfileKey === key && portraitState.profileCache.has(key);
      setStripStatus(hasVisibleProfile ? "画像刷新失败" : "画像读取失败，请重试");
      if (!hasVisibleProfile) status(byId("heroMetricBox"), "画像读取失败", () => loadProfile(member));
    }
  } finally { if (token === portraitState.profileGeneration) portraitState.profilePending = false; }
}
function applySettings() {
  document.body.classList.toggle("theme-light", settingsState.settings.theme === "light");
  document.documentElement.classList.toggle("desktop-host", !!window.desktopHost);
  document.documentElement.style.zoom = settingsState.settings.zoom;
  document.documentElement.style.setProperty("--zoom-inverse", String(1 / Number(settingsState.settings.zoom)));
  window.desktopHost?.setTheme?.(settingsState.settings.theme);
  byId("selectThemeMode").value = settingsState.settings.theme;
  byId("selectZoomLevel").value = settingsState.settings.zoom;
  byId("btnToggleIntent").classList.toggle("active", settingsState.settings.intent);
  byId("btnToggleIntent").setAttribute("aria-pressed", String(settingsState.settings.intent));
  const detailToggle = byId("btnToggleLabelView");
  if (detailToggle) {
    detailToggle.hidden = window.ProductConfig?.key !== "qq";
    detailToggle.disabled = !settingsState.settings.intent;
    detailToggle.textContent = settingsState.settings.labelDetails ? "切回简洁" : "百分比详情";
    detailToggle.classList.toggle("active", settingsState.settings.labelDetails);
    detailToggle.setAttribute("aria-pressed", String(settingsState.settings.labelDetails));
  }
  refreshLabels();
}
settingsState.runtimeSnapshot = null;
settingsState.runtimeRequest = 0;
settingsState.runtimeBusy = false;
settingsState.runtimePollTimer = null;
function validRuntime(data) {
  return data && ["cpu", "gpu"].includes(data.requestedProvider) &&
    [null, "cpu", "webgpu"].includes(data.modelProvider) &&
    ["ready", "loading", "idle", "missing", "error"].includes(data.status) &&
    (data.status !== "ready" || data.modelProvider !== null);
}
function showRuntime(data) {
  settingsState.runtimeSnapshot = data;
  byId("selectRuntimeProvider").value = data.requestedProvider;
  const actual = data.modelProvider === "webgpu" ? "GPU" : data.modelProvider === "cpu" ? "CPU" : "";
  text("runtimeStatus", data.status === "ready" ? `当前 ${actual}` :
    data.status === "loading" ? "正在加载…" : data.status === "missing" ? "未安装模型" :
    data.status === "error" ? "加载失败" : "待加载");
  clearTimeout(settingsState.runtimePollTimer);
  if (["loading", "idle"].includes(data.status) && byId("settingsModal").classList.contains("show") &&
      byId("selectModelSource").value === "local")
    settingsState.runtimePollTimer = setTimeout(() => { void loadRuntime(true); }, 1200);
}
async function loadRuntime(silent = false) {
  if (settingsState.runtimeBusy) return;
  const request = ++settingsState.runtimeRequest;
  const select = byId("selectRuntimeProvider");
  if (!silent) {
    select.disabled = true;
    text("runtimeStatus", "读取中…");
  }
  try {
    const data = await api("/api/runtime");
    if (request !== settingsState.runtimeRequest) return;
    if (!validRuntime(data)) throw new Error("运行状态无效");
    showRuntime(data);
  } catch {
    if (request === settingsState.runtimeRequest) text("runtimeStatus", "读取失败");
  } finally {
    if (request === settingsState.runtimeRequest) syncRuntimeControl();
  }
}
async function changeRuntime(provider) {
  const select = byId("selectRuntimeProvider");
  const previous = settingsState.runtimeSnapshot?.requestedProvider;
  if (!previous || settingsState.runtimeBusy || settingsState.modelSourceSnapshot.mode !== "local" ||
      byId("selectModelSource").value !== "local" || !["cpu", "gpu"].includes(provider)) {
    if (previous) select.value = previous;
    return;
  }
  settingsState.runtimeBusy = true;
  const request = ++settingsState.runtimeRequest;
  clearTimeout(settingsState.runtimePollTimer);
  select.disabled = true;
  text("runtimeStatus", "正在切换…");
  try {
    const data = await api("/api/runtime", { method: "POST", body: JSON.stringify({ provider }) });
    if (request !== settingsState.runtimeRequest) return;
    if (!validRuntime(data) || data.requestedProvider !== provider || data.status === "error") throw new Error("切换失败");
    showRuntime(data);
  } catch {
    if (request === settingsState.runtimeRequest) {
      select.value = previous;
      text("runtimeStatus", "切换失败");
    }
  } finally {
    settingsState.runtimeBusy = false;
    if (request === settingsState.runtimeRequest) syncRuntimeControl();
  }
}
settingsState.localModelRequest = 0;
settingsState.localModelDownloadBusy = false;
settingsState.localModelReady = false;
settingsState.localModelResolved = false;
function showLocalModel(data) {
  const becameReady = !settingsState.localModelResolved || !settingsState.localModelReady;
  settingsState.localModelResolved = true;
  settingsState.localModelReady = data.state === "ready";
  const labels = { bundled: "内置模型已就绪", downloaded: "本机模型已就绪", custom: "自选模型已就绪" };
  text("localModelStatus", data.state === "ready" ? labels[data.source] || "已就绪" :
    data.source === "custom" ? "所选目录不可用" : "未安装");
  byId("localModelStatus").title = data.path || "";
  byId("btnChooseLocalModelDir").title = data.path ? `当前目录：${data.path}` : "选择 Laya 模型目录";
  byId("btnDownloadLocalModel").hidden = settingsState.localModelReady;
  byId("btnDownloadLocalModel").disabled = settingsState.localModelDownloadBusy || settingsState.localModelReady;
  if (usingLocalFine()) {
    renderApiInsightStatus();
    if (becameReady && settingsState.localModelReady) {
      resumeLocalAnalysis();
      if (chatState.view === "persona" && chatState.currentUser) void loadProfile(portraitState.activeMember);
    }
  }
}
async function loadLocalModel() {
  const request = ++settingsState.localModelRequest;
  try {
    const data = await api("/api/local-model");
    if (request !== settingsState.localModelRequest) return;
    if (!data || !["ready", "missing"].includes(data.state) ||
        !["bundled", "downloaded", "custom", "none"].includes(data.source) ||
        typeof data.path !== "string") throw new Error("模型状态无效");
    showLocalModel(data);
  } catch {
    if (request === settingsState.localModelRequest) {
      settingsState.localModelResolved = false;
      text("localModelStatus", "读取失败");
    }
  }
}
async function selectLocalModel(value) {
  text("localModelStatus", "正在校验模型…");
  try {
    const data = await api("/api/local-model", { method: "POST", body: JSON.stringify({ path: value }) });
    if (data.state !== "ready") throw new Error("模型未就绪");
    showLocalModel(data);
    void loadRuntime();
  } catch {
    text("localModelStatus", "目录不包含完整的 Laya 模型");
  }
}
function showLocalModelDownload(state) {
  if (!state || typeof state !== "object") return;
  const progress = byId("localModelDownloadProgress");
  settingsState.localModelDownloadBusy = state.phase === "downloading" || state.phase === "installing";
  byId("btnDownloadLocalModel").disabled = settingsState.localModelDownloadBusy || settingsState.localModelReady;
  progress.hidden = !settingsState.localModelDownloadBusy && state.phase !== "failed";
  text("localModelDownloadProgress", state.phase === "downloading" ?
    `正在下载模型 ${Math.round(100 * (state.received || 0) / (state.total || 1))}%` :
    state.phase === "installing" ? "正在校验并安装模型…" :
    state.phase === "failed" ?
      (state.stage === "installing" ? "模型安装失败，请重试" : "模型下载失败，请重试") : "");
}
window.addEventListener("wechatvibe-model-download-state", event => showLocalModelDownload(event.detail));
byId("btnDownloadLocalModel").addEventListener("click", async () => {
  if (typeof window.desktopHost?.downloadLayaModel !== "function") return;
  showLocalModelDownload({ phase: "downloading", received: 0, total: 1 });
  try {
    const result = await window.desktopHost.downloadLayaModel();
    showLocalModelDownload(result);
    if (result?.phase === "ready") await selectLocalModel("downloaded");
  } catch { showLocalModelDownload({ phase: "failed" }); }
});
byId("btnChooseLocalModelDir").addEventListener("click", async () => {
  if (typeof window.desktopHost?.chooseModelDirectory !== "function") return;
  const directory = await window.desktopHost.chooseModelDirectory();
  if (directory) await selectLocalModel(directory);
});
const MODEL_SOURCE_PROTOCOLS = new Set(["anthropic", "responses", "chat_completions", "gemini", "ollama"]);
settingsState.modelSourceSnapshot = { mode: "local", api: null, sourceId: "local", status: "idle" };
settingsState.modelSourceResolved = false;
settingsState.modelSourceReadRequest = 0;
settingsState.modelSourceLoadController = null;
settingsState.modelSourceRevision = 0;
settingsState.modelListRequest = 0;
settingsState.modelListController = null;
settingsState.modelTestRequest = 0;
settingsState.modelTestController = null;
settingsState.modelSourceLoading = false;
settingsState.modelSourceBusy = false;
settingsState.modelListBusy = false;
settingsState.modelTestBusy = false;
settingsState.modelSourceDraftDirty = false;
function validModelSource(data) {
  return !!data && ["local", "api"].includes(data.mode) &&
    typeof data.sourceId === "string" && !!data.sourceId &&
    (data.mode !== "api" || !!data.api) &&
    (data.api === null || (!!data.api && MODEL_SOURCE_PROTOCOLS.has(data.api.protocol) &&
      typeof data.api.baseUrl === "string" && typeof data.api.model === "string" &&
      (data.api.contextTokens == null || Number.isSafeInteger(data.api.contextTokens) &&
        data.api.contextTokens >= 4096 && data.api.contextTokens <= 1000000) &&
      typeof data.api.hasKey === "boolean"));
}
function usingLocalFine() {
  return settingsState.modelSourceResolved && settingsState.modelSourceSnapshot.mode === "local";
}
function canAnalyzeLocal() {
  return usingLocalFine() && settingsState.localModelResolved && settingsState.localModelReady;
}
function resumeLocalAnalysis() {
  if (!canAnalyzeLocal() || !chatState.currentUser || !chatState.controller || !chatState.messageSourceReady || chatState.historyState) return;
  const user = chatState.currentUser, token = chatState.generation, signal = chatState.controller.signal;
  void loadAnalysis(user, token, signal).then(analysis => {
    if (analysis && token === chatState.generation && !chatState.historyState)
      scheduleIncremental(user, token, signal, analysis, false, JSON.stringify(chatState.messages));
  });
}
function applyActiveModelSource(data) {
  const changed = !settingsState.modelSourceResolved || settingsState.modelSourceSnapshot.mode !== data.mode ||
    settingsState.modelSourceSnapshot.sourceId !== data.sourceId;
  const portraitSourceChanged = settingsState.modelSourceSnapshot.mode !== data.mode ||
    settingsState.modelSourceSnapshot.sourceId !== data.sourceId;
  const portraitBudgetChanged = settingsState.modelSourceResolved && !changed && data.mode === "api" &&
    settingsState.modelSourceSnapshot.api?.contextTokens !== data.api?.contextTokens;
  settingsState.modelSourceSnapshot = data;
  settingsState.modelSourceResolved = true;
  syncPortraitMode();
  if (changed) {
    labelState.messageLabels?.closeDetails();
    cancelApiPortraitPoll();
    if (portraitSourceChanged) {
      // A source switch only swaps the visible source; each source keeps its own
      // persisted snapshot (the key includes the source id), so none is erased here.
      clearApiPortraitView();
      if (data.mode !== "api") byId("apiPortraitStatus").hidden = true;
    }
    cancelApiInsightWork();
    clearInlineIntentPending();
    setIntentActionState("idle");
    refreshLabels();
    if (data.mode === "api") {
      portraitState.incrementalFailed = false;
      portraitState.analysisNetworkFailed = false;
      portraitState.currentAnalysisJob = null;
      ensureApiInsights();
    } else {
      submitManualRecent();
      resumeLocalAnalysis();
    }
    if (chatState.view === "persona" && chatState.currentUser) void loadProfile(portraitState.activeMember);
  } else if (portraitBudgetChanged) {
    cancelApiPortraitPoll();
    portraitState.apiPortraitSubmitErrors.clear();
    if (chatState.view === "persona" && chatState.currentUser) void loadProfile(portraitState.activeMember);
  }
  renderApiInsightStatus();
}
function beginUnknownModelSource() {
  const key = activeApiInsightKey();
  if (key && labelState.apiInsightCache.has(key)) {
    const entry = labelState.apiInsightCache.get(key);
    entry.requestedSignature = null;
    entry.job = null;
    entry.error = "";
  }
  settingsState.modelSourceResolved = false;
  if (!(settingsState.modelSourceSnapshot.mode === "api" && portraitState.apiPortraitSnapshot && chatState.currentUser))
    clearProfileView("正在读取模型来源…");
  cancelApiInsightWork();
  clearInlineIntentPending();
  refreshLabels();
  renderApiInsightStatus();
  updateModelSourceControls();
}
function syncRuntimeControl() {
  byId("selectRuntimeProvider").disabled = !settingsState.runtimeSnapshot || settingsState.runtimeBusy || settingsState.modelSourceLoading ||
    settingsState.modelSourceBusy || !settingsState.modelSourceResolved || settingsState.modelSourceSnapshot.mode !== "local" ||
    byId("selectModelSource").value !== "local";
}
function updateModelSourceControls() {
  byId("selectModelSource").disabled = !settingsState.modelSourceResolved || settingsState.modelSourceLoading || settingsState.modelSourceBusy;
  byId("btnReloadModelSource").hidden = settingsState.modelSourceResolved || settingsState.modelSourceLoading;
  byId("localModelActions").hidden = !settingsState.modelSourceResolved || settingsState.modelSourceSnapshot.mode !== "api" ||
    byId("selectModelSource").value !== "local";
  byId("btnActivateLocal").disabled = !settingsState.modelSourceResolved || settingsState.modelSourceLoading || settingsState.modelSourceBusy || settingsState.modelSourceSnapshot.mode === "local";
  for (const id of ["selectApiProtocol", "inputApiBaseUrl", "inputApiKey", "selectApiModel", "inputApiModelId", "inputApiContextTokens"])
    byId(id).disabled = settingsState.modelSourceLoading || settingsState.modelSourceBusy ||
      (id === "selectApiModel" && byId(id).options.length < 2);
  byId("btnFetchApiModels").disabled = !settingsState.modelSourceResolved || settingsState.modelSourceLoading || settingsState.modelSourceBusy || settingsState.modelListBusy;
  byId("btnTestApiModel").disabled = !settingsState.modelSourceResolved || settingsState.modelSourceLoading || settingsState.modelSourceBusy || settingsState.modelTestBusy;
  byId("btnActivateApi").disabled = !settingsState.modelSourceResolved || settingsState.modelSourceLoading || settingsState.modelSourceBusy || settingsState.modelTestBusy;
  byId("btnClearApiKey").disabled = !settingsState.modelSourceResolved || settingsState.modelSourceLoading || settingsState.modelSourceBusy;
  syncRuntimeControl();
}
function showModelSourceMode() {
  const isApi = byId("selectModelSource").value === "api";
  byId("localModelSettings").hidden = isApi;
  byId("apiModelSettings").hidden = !isApi;
  byId("settingsModal").querySelector(".settings-modal-card").classList.toggle("api-source-open", isApi);
  updateModelSourceControls();
}
function clearModelList() {
  const select = byId("selectApiModel");
  select.replaceChildren();
  const option = document.createElement("option");
  option.value = "";
  option.textContent = "获取列表后选择";
  select.appendChild(option);
  select.value = "";
  select.disabled = true;
}
function invalidateModelDiscovery() {
  settingsState.modelListController?.abort();
  settingsState.modelListController = null;
  settingsState.modelTestController?.abort();
  settingsState.modelTestController = null;
  ++settingsState.modelSourceRevision;
  ++settingsState.modelListRequest;
  ++settingsState.modelTestRequest;
  settingsState.modelListBusy = false;
  settingsState.modelTestBusy = false;
  clearModelList();
  text("apiModelCount", "");
  text("apiModelTestStatus", "");
  text("modelSourceStatus", "");
  updateModelSourceControls();
}
function invalidateModelTest() {
  settingsState.modelTestController?.abort();
  settingsState.modelTestController = null;
  ++settingsState.modelSourceRevision;
  ++settingsState.modelTestRequest;
  settingsState.modelTestBusy = false;
  text("apiModelTestStatus", "");
  text("modelSourceStatus", "");
  updateModelSourceControls();
}
function syncSavedApiKeyHint() {
  const saved = settingsState.modelSourceSnapshot.api;
  const reusable = !!saved?.hasKey && saved.protocol === byId("selectApiProtocol").value &&
    saved.baseUrl.replace(/\/+$/, "") === byId("inputApiBaseUrl").value.trim().replace(/\/+$/, "");
  byId("apiKeySaved").hidden = !reusable;
  byId("btnClearApiKey").hidden = !saved?.hasKey;
  byId("inputApiKey").placeholder = reusable ? "留空沿用已保存密钥" : "按服务要求填写 API Key";
}
function showModelSource(data) {
  applyActiveModelSource(data);
  settingsState.modelSourceDraftDirty = false;
  text("modelSourceActive", data.mode === "api" ? "当前 API" : "当前本地");
  byId("selectModelSource").value = data.mode;
  byId("selectApiProtocol").value = data.api?.protocol || "responses";
  byId("inputApiBaseUrl").value = data.api?.baseUrl || "";
  byId("inputApiModelId").value = data.api?.model || "";
  byId("inputApiContextTokens").value = data.api?.contextTokens || "";
  byId("inputApiKey").value = "";
  syncSavedApiKeyHint();
  invalidateModelDiscovery();
  showModelSourceMode();
}
async function loadModelSource(preserveDraft = false) {
  settingsState.modelSourceLoadController?.abort();
  const request = ++settingsState.modelSourceReadRequest;
  const abortController = new AbortController();
  settingsState.modelSourceLoadController = abortController;
  const timeoutId = setTimeout(() => abortController.abort(), 15_000);
  settingsState.modelSourceLoading = true;
  text("modelSourceStatus", "读取中…");
  updateModelSourceControls();
  try {
    const data = await api("/api/model-source", {}, abortController.signal);
    if (request !== settingsState.modelSourceReadRequest) return;
    if (!validModelSource(data)) throw new Error("invalid model source");
    if (settingsState.modelSourceDraftDirty) {
      applyActiveModelSource(data);
      text("modelSourceActive", data.mode === "api" ? "当前 API" : "当前本地");
      syncSavedApiKeyHint();
    } else showModelSource(data);
    text("modelSourceStatus", "");
  } catch {
    if (request === settingsState.modelSourceReadRequest)
      text("modelSourceStatus", abortController.signal.aborted ? "模型来源读取超时" : "模型来源读取失败");
  } finally {
    clearTimeout(timeoutId);
    if (settingsState.modelSourceLoadController === abortController) settingsState.modelSourceLoadController = null;
    if (request === settingsState.modelSourceReadRequest) {
      settingsState.modelSourceLoading = false;
      updateModelSourceControls();
    }
  }
}
function modelSourceRequestError(error) {
  const reasons = {
    auth: "密钥或访问权限有误", "rate-limit": "请求过于频繁",
    timeout: "连接超时", unsupported: "接口不支持",
    network: "无法连接服务", "invalid-url": "地址格式有误",
    "response-too-large": "服务响应过大", "empty-response": "模型未返回内容",
    "provider-error": "模型服务返回错误", "invalid-output": "模型返回格式不正确",
  };
  if (typeof error?.code === "string" && reasons[error.code]) return reasons[error.code];
  return Number.isInteger(error?.status) ? `HTTP ${error.status}` : "网络或服务错误";
}
function apiModelDraft(requireModel, requireContext = false) {
  const protocol = byId("selectApiProtocol").value;
  const baseUrl = byId("inputApiBaseUrl").value.trim();
  const model = byId("inputApiModelId").value.trim();
  if (!MODEL_SOURCE_PROTOCOLS.has(protocol)) throw new Error("请选择接口协议");
  let url;
  try { url = new URL(baseUrl); } catch { throw new Error("请输入有效的 Base URL"); }
  if (!["http:", "https:"].includes(url.protocol)) throw new Error("Base URL 须使用 HTTP 或 HTTPS");
  if (url.username || url.password || url.search || url.hash || /[\s\\]/.test(baseUrl))
    throw new Error("Base URL 不能包含账号、查询参数或空格");
  if (requireModel && !model) throw new Error("请输入模型 ID");
  const rawContext = byId("inputApiContextTokens").value.trim();
  const contextTokens = rawContext ? Number(rawContext) : null;
  if (requireContext && contextTokens === null) throw new Error("请填写模型上下文大小");
  if (contextTokens !== null && (!Number.isSafeInteger(contextTokens) ||
      contextTokens < 4096 || contextTokens > 1000000)) throw new Error("上下文大小须为 4096～1000000 tokens");
  const draft = { protocol, baseUrl };
  const apiKey = byId("inputApiKey").value.trim();
  if (apiKey) draft.apiKey = apiKey;
  if (requireModel) {
    draft.model = model;
    if (contextTokens !== null) draft.contextTokens = contextTokens;
  }
  return draft;
}
async function fetchApiModels() {
  let draft;
  try { draft = apiModelDraft(false); }
  catch (error) { text("apiModelCount", error.message); return; }
  const request = ++settingsState.modelListRequest;
  const revision = settingsState.modelSourceRevision;
  const abortController = new AbortController();
  settingsState.modelListController = abortController;
  const timeoutId = setTimeout(() => abortController.abort(), 20_000);
  settingsState.modelListBusy = true;
  clearModelList();
  text("apiModelCount", "正在获取…");
  text("apiModelTestStatus", "");
  updateModelSourceControls();
  try {
    const result = await api("/api/model-source/list", { method: "POST", body: JSON.stringify(draft) },
      abortController.signal);
    if (request !== settingsState.modelListRequest || revision !== settingsState.modelSourceRevision) return;
    if (!result || typeof result.supported !== "boolean" || !Array.isArray(result.models))
      throw new Error("invalid model list");
    clearModelList();
    if (!result.supported) {
      text("apiModelCount", "此接口不提供模型列表，可手动填写模型 ID");
      return;
    }
    const select = byId("selectApiModel");
    const known = new Set();
    for (const item of result.models) {
      if (!item || typeof item.id !== "string" || !item.id.trim() || known.has(item.id)) continue;
      known.add(item.id);
      const option = document.createElement("option");
      option.value = item.id;
      option.textContent = typeof item.name === "string" && item.name ? item.name : item.id;
      if (Number.isSafeInteger(item.contextTokens) && item.contextTokens >= 4096 &&
          item.contextTokens <= 1000000) option.dataset.contextTokens = String(item.contextTokens);
      select.appendChild(option);
    }
    const model = byId("inputApiModelId").value.trim();
    select.value = known.has(model) ? model : "";
    const selected = Array.from(select.options).find(option => option.value === model);
    if (selected?.dataset.contextTokens) byId("inputApiContextTokens").value = selected.dataset.contextTokens;
    text("apiModelCount", `${known.size} 个模型可用`);
  } catch (error) {
    if (request === settingsState.modelListRequest && revision === settingsState.modelSourceRevision)
      text("apiModelCount", abortController.signal.aborted
        ? "获取超时，可手动填写模型 ID"
        : `获取失败（${modelSourceRequestError(error)}），可手动填写模型 ID`);
  } finally {
    clearTimeout(timeoutId);
    if (settingsState.modelListController === abortController) settingsState.modelListController = null;
    if (request === settingsState.modelListRequest) {
      settingsState.modelListBusy = false;
      updateModelSourceControls();
    }
  }
}
async function testApiModel() {
  if (settingsState.modelSourceBusy || settingsState.modelTestBusy) return;
  let draft;
  try { draft = apiModelDraft(true); }
  catch (error) { text("apiModelTestStatus", error.message); return; }
  const request = ++settingsState.modelTestRequest;
  const revision = settingsState.modelSourceRevision;
  const abortController = new AbortController();
  settingsState.modelTestController = abortController;
  const timeoutId = setTimeout(() => abortController.abort(), 20_000);
  settingsState.modelTestBusy = true;
  text("apiModelTestStatus", "正在测试…");
  updateModelSourceControls();
  try {
    const result = await api("/api/model-source/test", { method: "POST", body: JSON.stringify(draft) },
      abortController.signal);
    if (request !== settingsState.modelTestRequest || revision !== settingsState.modelSourceRevision) return;
    if (result?.ok !== true) throw new Error("connection test failed");
    const latency = Number.isFinite(result.latencyMs) ? ` · ${Math.round(result.latencyMs)} ms` : "";
    text("apiModelTestStatus", `连接成功${latency}`);
  } catch (error) {
    if (request === settingsState.modelTestRequest && revision === settingsState.modelSourceRevision)
      text("apiModelTestStatus", abortController.signal.aborted ? "连接测试超时" :
        `连接失败（${modelSourceRequestError(error)}）`);
  } finally {
    clearTimeout(timeoutId);
    if (settingsState.modelTestController === abortController) settingsState.modelTestController = null;
    if (request === settingsState.modelTestRequest) {
      settingsState.modelTestBusy = false;
      updateModelSourceControls();
    }
  }
}
async function activateModelSource(mode) {
  if (settingsState.modelSourceBusy || settingsState.modelTestBusy || !["local", "api"].includes(mode)) return;
  let payload = { mode };
  if (mode === "api") {
    try { payload = { ...payload, ...apiModelDraft(true, true) }; }
    catch (error) { text("modelSourceStatus", error.message); return; }
  }
  settingsState.modelSourceBusy = true;
  const abortController = new AbortController();
  const timeoutId = setTimeout(() => abortController.abort(), mode === "local" ? 15_000 : 30_000);
  text("modelSourceStatus", "正在启用…");
  updateModelSourceControls();
  try {
    const data = await api("/api/model-source/activate", { method: "POST", body: JSON.stringify(payload) },
      abortController.signal);
    if (!validModelSource(data) || data.mode !== mode) throw new Error("activation failed");
    showModelSource(data);
    text("modelSourceStatus", mode === "api" ? "API 模型已启用" : "本地模型已启用");
  } catch (error) {
    if (!Number.isInteger(error?.status)) {
      beginUnknownModelSource();
      settingsState.modelSourceDraftDirty = true;
      void loadModelSource(true);
    }
    text("modelSourceStatus", settingsState.modelSourceResolved ?
      `启用失败（${abortController.signal.aborted ? "连接超时" : modelSourceRequestError(error)}），当前仍为${settingsState.modelSourceSnapshot.mode === "api" ? " API" : "本地"}` :
      "启用状态待读取");
  } finally {
    clearTimeout(timeoutId);
    settingsState.modelSourceBusy = false;
    updateModelSourceControls();
  }
}
async function clearStoredApiKey() {
  if (settingsState.modelSourceBusy || !settingsState.modelSourceSnapshot.api?.hasKey) return;
  settingsState.modelSourceBusy = true;
  text("modelSourceStatus", "正在清除密钥…");
  updateModelSourceControls();
  let cleared = false;
  try {
    await api("/api/model-source/clear-key", { method: "POST", body: "{}" });
    cleared = true;
    byId("inputApiKey").value = "";
    const data = await api("/api/model-source");
    if (!validModelSource(data)) throw new Error("invalid model source");
    showModelSource(data);
    text("modelSourceStatus", "密钥已清除");
  } catch (error) {
    if (cleared) {
      settingsState.modelSourceSnapshot = { ...settingsState.modelSourceSnapshot, api: settingsState.modelSourceSnapshot.api ?
        { ...settingsState.modelSourceSnapshot.api, hasKey: false } : null };
      beginUnknownModelSource();
      byId("apiKeySaved").hidden = true;
      byId("btnClearApiKey").hidden = true;
      text("modelSourceActive", "状态待读取");
      text("modelSourceStatus", "密钥已清除，状态读取失败");
    } else text("modelSourceStatus", `清除失败（${modelSourceRequestError(error)}）`);
  } finally {
    settingsState.modelSourceBusy = false;
    updateModelSourceControls();
  }
}
labelState.apiInsightCache = new Map();
settingsState.suppressedApiSources = new Set();
settingsState.suppressedLocalAccounts = new Set();
labelState.apiInsightWork = null;
labelState.apiInsightViewportTimer = null;
labelState.apiInsightStatusRendered = false;
function apiInsightKey(account, user, sourceId) {
  return JSON.stringify([account, user, sourceId]);
}
function activeApiInsightKey() {
  return settingsState.modelSourceResolved && settingsState.modelSourceSnapshot.mode === "api" && chatState.currentAccount && chatState.currentUser ?
    apiInsightKey(chatState.currentAccount, chatState.currentUser, settingsState.modelSourceSnapshot.sourceId) : null;
}
function activeApiInsightEntry() {
  const key = activeApiInsightKey();
  return key ? labelState.apiInsightCache.get(key) : null;
}
const API_INSIGHT_LABEL = /^\p{Script=Han}{1,8}$/u;
const API_AFFECT_KEYS = ["tone", "feeling", "interaction"];
function validApiInsightLabel(value) {
  return typeof value === "string" && API_INSIGHT_LABEL.test(value);
}
// Accepts the S1 affect/intents shape, the legacy scalar {emotion,intent} shape, and the
// routine/uncertain/insufficient success terminal states. It never coerces a failure.
function validApiInsight(value, id) {
  if (!value || String(value.id) !== id) return false;
  if (value.status === "insufficient" || value.status === "routine" || value.status === "uncertain") return true;
  if (value.status !== "ok") return false;
  if (typeof value.emotion === "string" || typeof value.intent === "string") {
    return validApiInsightLabel(value.emotion) && validApiInsightLabel(value.intent);
  }
  const affect = value.affect;
  const seen = new Set();
  if (affect !== undefined && affect !== null) {
    if (typeof affect !== "object" || Array.isArray(affect)) return false;
    for (const key of Object.keys(affect)) {
      if (!API_AFFECT_KEYS.includes(key)) return false;
      const label = affect[key];
      if (label === undefined || label === null) continue;
      if (!validApiInsightLabel(label) || seen.has(label)) return false;
      seen.add(label);
    }
  }
  if (value.intents === undefined || value.intents === null) return true;
  if (!Array.isArray(value.intents) || value.intents.length > 3) return false;
  const seenIntents = new Set();
  for (const label of value.intents) {
    if (!validApiInsightLabel(label) || seenIntents.has(label) || seen.has(label)) return false;
    seenIntents.add(label);
  }
  return true;
}
// Parse only completed emotion/intent pairs from the text received so far. A
// label is committed after its line (or the next label marker) is complete;
// half a streamed word never reaches the message row. The provider may add
// JSON, Markdown, thoughts, or a trailing summary, so the parser deliberately
// ignores everything except the two requested markers and uses target order.
function parseApiPartialLabels(raw, ids) {
  const targetIds = Array.isArray(ids) ? ids.map(id => String(id)) : [];
  if (!targetIds.length || typeof raw !== "string" || !raw) return {};
  const summary = raw.search(/(?:^|\n)\s*(?:整体|总体)?总结\s*[:：]?/u);
  const body = (summary >= 0 ? `${raw.slice(0, summary)}\n` : raw).replace(/\r/g, "");
  const marker = /(?:情感|情绪|emotion|意图|intent)\s*[:：]\s*/giu;
  const matches = [...body.matchAll(marker)];
  const emotions = [];
  const intents = [];
  for (let index = 0; index < matches.length; index++) {
    const current = matches[index];
    const start = (current.index ?? 0) + current[0].length;
    const next = matches[index + 1];
    const end = next?.index ?? body.length;
    const value = body.slice(start, end);
    const trimmed = value.trim();
    // When there is no next marker, require a visible line/JSON/punctuation
    // boundary so a currently streamed prefix such as “关” stays pending.
    const complete = !!next || /\n/u.test(value) ||
      /[}\]，,。！？!?；;:"'”」』]\s*$/u.test(trimmed);
    if (!complete) continue;
    const label = trimmed.match(/\p{Script=Han}{1,4}/u)?.[0];
    if (!label) continue;
    const kind = current[0].match(/^(?:情感|情绪|emotion)/iu) ? "emotion" : "intent";
    (kind === "emotion" ? emotions : intents).push(label);
  }
  const count = Math.min(targetIds.length, emotions.length, intents.length);
  const result = {};
  for (let index = 0; index < count; index++) {
    const id = targetIds[index];
    const emotion = emotions[index];
    const intent = intents[index];
    if (!emotion || !intent) continue;
    result[id] = { id, status: "ok", affect: { feeling: emotion }, intents: [intent] };
  }
  return result;
}
function apiInsightCandidates() {
  if (!chatState.messages.length) return [];
  const eligible = message => labelSideEligible(message) && message.kind === "text" &&
    typeof message.text === "string" && !!message.text.trim() &&
    hasIntentContent(message.text) && !isIncompleteFragment(message.text) &&
    (!chatState.historyState || typeof message.historyCursor === "string");
  // Analyze the entire message window already loaded for this conversation.
  // Clicking “load more” is the explicit boundary for expanding that window.
  return chatState.messages.filter(eligible).slice(-500);
}
function apiInsightSignature(candidates) {
  const qq = typeof window !== "undefined" && window.ProductConfig?.key === "qq";
  return JSON.stringify(candidates.map(message => [String(message.id), message.text,
    ...(qq ? [message.historyCursor || null] : [])]));
}
function acceptApiInsightBasis(entry, data) {
  if (typeof data.contextHash !== "string") return;
  if (entry.contextHash !== data.contextHash || entry.dataRevision !== data.dataRevision) {
    entry.results = {};
    entry.sessionReady = false;
    entry.requestedSignature = null;
  }
  entry.contextHash = data.contextHash;
  entry.dataRevision = data.dataRevision;
  entry.previousResults = Object.fromEntries(Object.entries(data.previousResults || {})
    .filter(([id, value]) => validApiInsight(value, id)).slice(-320));
  entry.stale = data.stale === true && data.hasPublishedAnalysis === true;
}
function apiInsightWorkCurrent(work) {
  return labelState.apiInsightWork === work && settingsState.modelSourceResolved && settingsState.modelSourceSnapshot.mode === "api" &&
    settingsState.settings.intent && chatState.currentAccount === work.account && chatState.currentUser === work.user &&
    settingsState.modelSourceSnapshot.sourceId === work.sourceId && chatState.generation === work.generation;
}
function cancelApiInsightWork() {
  clearTimeout(labelState.apiInsightViewportTimer);
  labelState.apiInsightViewportTimer = null;
  if (!labelState.apiInsightWork) return;
  clearTimeout(labelState.apiInsightWork.timer);
  labelState.apiInsightWork.controller.abort();
  labelState.apiInsightWork = null;
}
function renderApiInsightStatus() {
  const node = byId("analysisStatus");
  const retry = byId("btnRetryAnalysis");
  retry.textContent = "分析失败 · 重试";
  if (!settingsState.modelSourceResolved || settingsState.modelSourceSnapshot.mode !== "api" || !settingsState.settings.intent || !chatState.currentUser) {
    if (labelState.apiInsightStatusRendered ||
        settingsState.modelSourceResolved && settingsState.modelSourceSnapshot.mode === "api") node.textContent = "";
    labelState.apiInsightStatusRendered = false;
    retry.hidden = !(portraitState.incrementalFailed || usingLocalFine() && labelState.recentFailed);
    if (usingLocalFine() && settingsState.localModelResolved && !settingsState.localModelReady) {
      node.textContent = "未安装 Laya 模型，请在设置下载模型";
      retry.hidden = true;
    }
    if (settingsState.modelSourceResolved && settingsState.modelSourceSnapshot.mode === "api") setIntentActionState("idle");
    return;
  }
  labelState.apiInsightStatusRendered = true;
  const entry = activeApiInsightEntry();
  if (entry?.error) {
    node.textContent = entry.error;
    retry.hidden = false;
    setIntentActionState("error");
  } else if (["queued", "running"].includes(entry?.job?.status) || labelState.apiInsightWork?.postPending) {
    const total = Number(entry?.job?.total) || 0;
    const processed = Number(entry?.job?.processed) || 0;
    const retry = entry?.job?.retry;
    if (retry) {
      const remaining = Math.max(0, Math.ceil((Number(retry.nextAtMs) - Date.now()) / 1000));
      const reason = ({ "invalid-output": "模型返回格式不正确", timeout: "模型响应超时",
        "invalid-insights": "模型结果不完整", network: "网络连接失败",
        "rate-limit": "接口请求受限" })[retry.reason] || "模型响应失败";
      node.textContent = `${reason}，${remaining} 秒后自动重试 ${retry.attempt}/${retry.max}`;
    } else {
      const elapsed = Math.max(0, Math.floor((Date.now() - Number(entry?.job?.startedAtMs || Date.now())) / 1000));
      const firstBodyMs = Number(entry?.job?.timings?.firstBodyMs);
      const first = Number.isFinite(firstBodyMs) && firstBodyMs >= 0 ?
        `首段 ${(firstBodyMs / 1000).toFixed(1)} 秒 · ` : "";
      node.textContent = first + (total > 0 ? `分析中 ${Math.min(processed, total)}/${total} · ${elapsed} 秒` : `分析中 · ${elapsed} 秒`);
    }
    setIntentActionState(labelState.apiInsightWork?.postPending ? "submitting" : entry?.job?.status || "queued");
  } else if (entry?.job?.status === "done") {
    // Real wall-clock total from the backend job, never a poll count.
    const totalMs = Number(entry.job.timings?.totalMs);
    const firstBodyMs = Number(entry.job.timings?.firstBodyMs);
    const total = Number(entry.job.total) || 0;
    node.textContent = Number.isFinite(totalMs) && totalMs >= 0 ?
      (Number.isFinite(firstBodyMs) && firstBodyMs >= 0 ? `首段 ${(firstBodyMs / 1000).toFixed(1)} 秒 · ` : "") +
      `完成 · ${(totalMs / 1000).toFixed(1)} 秒` + (total > 0 ? ` · ${total} 条` : "") : "";
    setIntentActionState("done");
  } else {
    node.textContent = "";
    setIntentActionState("idle");
  }
  if (!entry?.error) retry.hidden = !portraitState.incrementalFailed;
  if (entry?.stale) node.textContent = `显示旧标签 · ${node.textContent || "等待重新分析"}`;
}
function renderApiInsightResult(result, text = "") {
  return messageLabelsApi().render(window.MessageInsightAdapters.apiView(result, text), String(result?.id || ""),
    window.MessageInsightAdapters.apiDetails(result, messageDetailContext("api", result?.id || "")));
}
function updateApiInsightLabel(message, node, wrap) {
  const id = String(message.id);
  const eligible = settingsState.modelSourceResolved && settingsState.modelSourceSnapshot.mode === "api" && settingsState.settings.intent &&
    labelSideEligible(message) && message.kind === "text" &&
    typeof message.text === "string" && !!message.text.trim() &&
    hasIntentContent(message.text) && !isIncompleteFragment(message.text);
  const entry = activeApiInsightEntry();
  const value = entry?.results?.[id] || entry?.previousResults?.[id];
  const result = eligible && validApiInsight(value, id) ? value : null;
  const pending = eligible && !result && labelState.apiInsightWork?.key === activeApiInsightKey() &&
    labelState.apiInsightWork.pendingIds.has(id);
  const signature = result ? `api:${settingsState.modelSourceSnapshot.sourceId}:${JSON.stringify(result)}` :
    pending ? `api:${settingsState.modelSourceSnapshot.sourceId}:pending:${id}` : "";
  if (node.dataset.analysisSignature === signature) return;
  const revealing = !!wrap.querySelector(".inline-intent-pending") && result?.status === "ok";
  node.querySelector(".msg-avatar-column .msg-mood")?.remove();
  wrap.querySelector(".inline-expression-row")?.remove();
  wrap.querySelector(".inline-intent-row")?.remove();
  wrap.querySelector(".inline-intent-pending")?.remove();
  node.dataset.analysisSignature = signature;
  if (pending) wrap.appendChild(element("div", "inline-intent-pending", "分析中"));
  else if (result?.status === "ok") {
    const row = messageLabelsApi().render(window.MessageInsightAdapters.apiView(result, message.text || ""), id,
      window.MessageInsightAdapters.apiDetails(result, messageDetailContext("api", id)));
    if (revealing) row.classList.add("inline-intent-revealed");
    wrap.appendChild(row);
  }
}
// API insight job polling. The base interval sits in the 250-500 ms band so a fast job
// is picked up quickly; while the job stays queued/running it backs off up to a bounded
// cap so a slow provider does not cause a poll storm. Not a model-speed measurement.
const API_INSIGHT_POLL_BASE_MS = 400;
const API_INSIGHT_POLL_MAX_MS = 2000;
function apiInsightPollDelay(attempt) {
  const step = Math.max(0, (Number(attempt) || 1) - 1);
  return Math.min(API_INSIGHT_POLL_MAX_MS, API_INSIGHT_POLL_BASE_MS * Math.pow(2, step));
}
function scheduleApiInsightPoll(work) {
  clearTimeout(work.timer);
  if (!apiInsightWorkCurrent(work)) return;
  work.pollAttempt = (Number(work.pollAttempt) || 0) + 1;
  work.timer = setTimeout(() => { void fetchApiInsightResults(work); }, apiInsightPollDelay(work.pollAttempt));
}
async function fetchApiInsightResults(work) {
  if (!apiInsightWorkCurrent(work) || work.getPending) return;
  work.getPending = true;
  const entry = labelState.apiInsightCache.get(work.key);
  let terminal = false;
  try {
    const query = new URLSearchParams({ user: work.user });
    const qq = window.ProductConfig?.key === "qq";
    const candidates = apiInsightCandidates();
    const requestBasis = apiInsightSignature(candidates);
    if (qq) {
      work.around = candidates[Math.floor(candidates.length / 2)]?.historyCursor;
      if (work.around) query.set("around", work.around);
      query.set("ids", JSON.stringify(candidates.map(message => String(message.id))));
    }
    if (chatState.historyState) {
     const ids = apiInsightCandidates().map(message => String(message.id));
      if (ids.length) query.set("ids", JSON.stringify(ids));
    }
    const data = await api(`/api/model-insights?${query}`, {}, work.controller.signal);
    if (!apiInsightWorkCurrent(work)) return;
    if (qq && requestBasis !== apiInsightSignature(apiInsightCandidates())) {
      work.basisPending = true;
      work.hydrated = false;
      terminal = true;
      return;
    }
    if (data?.account !== work.account || data.sourceId !== work.sourceId) {
      beginUnknownModelSource();
      void loadModelSource(true);
      if (data?.account !== work.account) void loadSessions();
      return;
    }
    if (data.refreshRequired === true) {
      entry.results = {};
      entry.sessionReady = false;
      entry.requestedSignature = null;
      cancelApiInsightWork();
      if (chatState.historyState) returnToLatest();
      else void loadMessages(chatState.generation, true, true);
      return;
    }
    if (!data.results || typeof data.results !== "object" || Array.isArray(data.results))
      throw new Error("model insights scope mismatch");
    acceptApiInsightBasis(entry, data);
    const accepted = {};
    for (const [id, value] of Object.entries(data.results)) if (validApiInsight(value, id)) accepted[id] = value;
    entry.results = { ...entry.results, ...accepted };
    const storedIds = Object.keys(entry.results);
    for (const id of storedIds.slice(0, Math.max(0, storedIds.length - 320))) delete entry.results[id];
    entry.job = data.job || { status: "idle" };
    if (["queued", "running"].includes(entry.job.status) &&
        (!qq || entry.job.contextHash === data.contextHash)) {
      const partialIds = Array.isArray(entry.job.targetIds) && entry.job.targetIds.length ?
        entry.job.targetIds : [...work.pendingIds];
      const partial = parseApiPartialLabels(entry.job.partialText, partialIds);
      // Reparse the complete streamed text on every poll. Newer output replaces
      // an earlier partial label; the final validated result above wins at done.
      entry.results = { ...entry.results, ...partial };
    }
    if (entry.job.status === "done") entry.sessionReady = true;
    const insightErrors = {
      "invalid-output": "模型返回格式不正确", "invalid-insights": "模型结果不完整",
      "context-too-long": "上下文超过模型上限", "auth": "API Key 无效",
      "rate-limit": "接口请求受限", "timeout": "模型响应超时",
      "network": "网络连接失败", "provider-error": "模型服务返回错误",
      "response-too-large": "模型返回内容过长", "empty-response": "模型未返回内容",
      "unsupported": "当前接口不支持分析", "model-source-changed": "模型来源已切换",
    };
    entry.error = entry.job.status === "error" ? insightErrors[entry.job.error] || "分析失败" : "";
    if (entry.error && !work.force) entry.requestedSignature = null;
    work.hydrated = true;
    if (!["queued", "running"].includes(entry.job.status)) work.pendingIds.clear();
    refreshLabels();
    renderApiInsightStatus();
    if (["queued", "running"].includes(entry.job.status)) {
      work.force = false;
      scheduleApiInsightPoll(work);
    }
    else terminal = true;
  } catch (error) {
    if (apiInsightWorkCurrent(work) && error.name !== "AbortError") {
      if (handleAccountBoundaryError(error)) return;
      work.hydrated = true;
      work.force = false;
      work.pendingIds.clear();
      entry.requestedSignature = null;
      entry.error = "分析结果读取失败";
      refreshLabels();
      renderApiInsightStatus();
    }
  } finally {
    work.getPending = false;
    if (terminal && apiInsightWorkCurrent(work)) {
      const force = work.force;
      work.force = false;
      ensureApiInsights(force);
    }
  }
}
async function submitApiInsightJob(work, candidates, signature) {
  if (!apiInsightWorkCurrent(work) || work.postPending) return;
  const entry = labelState.apiInsightCache.get(work.key);
  work.postPending = true;
  work.pendingIds = new Set(candidates.map(message => String(message.id)));
  // A new text signature is a new analysis turn. Remove stale labels for its
  // targets immediately so the row cannot show an older answer while the
  // streamed replacement is being parsed.
  for (const id of work.pendingIds) delete entry.results[id];
  entry.requestedSignature = signature;
  entry.error = "";
  renderApiInsightStatus();
  refreshLabels();
  try {
    // The visible recent window also has stable cursors. Keep the exact visible
    // targets resolvable when new messages arrive or the user scrolls upward.
    const around = work.around || candidates[Math.floor(candidates.length / 2)]?.historyCursor;
    const data = await api("/api/model-insights", { method: "POST", body: JSON.stringify({
       account: work.account, user: work.user, limit: Math.max(1, candidates.length),
      targetIds: candidates.map(message => String(message.id)),
      ...(around ? { around } : {}),
    }) }, work.controller.signal);
    if (!apiInsightWorkCurrent(work)) return;
    if (data?.account !== work.account || data.sourceId !== work.sourceId) {
      beginUnknownModelSource();
      void loadModelSource(true);
      if (data?.account !== work.account) void loadSessions();
      return;
    }
    if (!data.job?.id)
      throw new Error("model insights scope mismatch");
    entry.job = data.job;
    work.pollAttempt = 0;
    renderApiInsightStatus();
    void fetchApiInsightResults(work);
  } catch (error) {
    if (apiInsightWorkCurrent(work) && error.name !== "AbortError") {
      if (handleAccountBoundaryError(error)) return;
      work.pendingIds.clear();
      entry.error = "分析失败";
      renderApiInsightStatus();
      refreshLabels();
    }
  } finally {
    work.postPending = false;
    if (apiInsightWorkCurrent(work) && work.hydrated &&
        !["queued", "running"].includes(entry.job?.status)) ensureApiInsights();
  }
}
function ensureApiInsights(force = false) {
  const key = settingsState.settings.intent && activeApiInsightKey();
  const sourceKey = JSON.stringify([chatState.currentAccount, settingsState.modelSourceSnapshot.sourceId]);
  if (settingsState.suppressedApiSources.has(sourceKey)) {
    cancelApiInsightWork();
    renderApiInsightStatus();
    return;
  }
  if (!key || !chatState.controller) {
    cancelApiInsightWork();
    renderApiInsightStatus();
    return;
  }
  if (!labelState.apiInsightCache.has(key)) {
     labelState.apiInsightCache.set(key, { results: {}, job: null, requestedSignature: null,
       error: "", sessionReady: false });
    while (labelState.apiInsightCache.size > 64) labelState.apiInsightCache.delete(labelState.apiInsightCache.keys().next().value);
  }
  const entry = labelState.apiInsightCache.get(key);
  if (window.ProductConfig?.key === "qq") {
    const basis = apiInsightSignature(apiInsightCandidates());
    if (entry.windowSignature !== basis) {
      entry.windowSignature = basis;
      entry.error = "";
      entry.requestedSignature = null;
      if (labelState.apiInsightWork?.key === key) labelState.apiInsightWork.basisPending = true;
    }
  }
  if (entry.error && !force) {
    renderApiInsightStatus();
    return;
  }
  if (!labelState.apiInsightWork || labelState.apiInsightWork.key !== key || labelState.apiInsightWork.generation !== chatState.generation) {
    cancelApiInsightWork();
    labelState.apiInsightWork = { key, account: chatState.currentAccount, user: chatState.currentUser,
      sourceId: settingsState.modelSourceSnapshot.sourceId, generation: chatState.generation, controller: new AbortController(),
      timer: null, pollAttempt: 0, pendingIds: new Set(), getPending: false, postPending: false,
      hydrated: false, force };
    void fetchApiInsightResults(labelState.apiInsightWork);
    return;
  }
  const work = labelState.apiInsightWork;
  if (work.basisPending && !work.getPending && !work.postPending) {
    work.basisPending = false;
    work.hydrated = false;
    void fetchApiInsightResults(work);
    return;
  }
  if (force) {
    work.force = true;
    entry.requestedSignature = null;
    entry.error = "";
  }
  if (!work.hydrated || work.getPending || work.postPending ||
      ["queued", "running"].includes(entry.job?.status)) return;
  const candidates = apiInsightCandidates();
  if (!candidates.length) return;
   const pending = entry.sessionReady ? candidates.filter(message => !validApiInsight(
     entry.results[String(message.id)], String(message.id))) : candidates;
  if (!pending.length) return;
  const signature = apiInsightSignature(pending);
  if (signature !== entry.requestedSignature || work.force)
    void submitApiInsightJob(work, pending, signature);
  work.force = false;
}
let managedAccounts = [];
let managedCurrentAccountId = null;
let selectedManagedAccountId = null;
let pendingDeleteAccountId = null;
let accountManagerRequest = 0;
let accountDeleteBusy = false;
let accountManagementOpen = false;
function managedAccountIsCurrent(account) {
  return !!account && (account.current || account.accountId === managedCurrentAccountId);
}
async function activateManagedAccount(account) {
  if (accountDeleteBusy || account.platform !== "qq" || account.deletionPending) return;
  accountDeleteBusy = true;
  text("accountManagerStatus", "正在打开本地记录…");
  renderManagedAccounts();
  try {
    const result = await api("/api/accounts/activate", { method: "POST",
      headers: { "Content-Type": "application/json" }, body: JSON.stringify({ accountId: account.accountId }) });
    if (result.accountId !== account.accountId) throw new Error("账号切换结果不匹配");
    window.location.reload();
  } catch (error) {
    text("accountManagerStatus", error.message || "无法打开本地记录");
    accountDeleteBusy = false;
    renderManagedAccounts();
  }
}
function renderManagedAccounts() {
  const list = byId("accountList");
  list.replaceChildren();
  if (!managedAccounts.length) status(list, "暂无已保存账号");
  for (const account of managedAccounts) {
    const row = element("div", "account-row");
    const item = element("button", `account-item${account.accountId === selectedManagedAccountId ? " active" : ""}`);
    item.type = "button";
    item.setAttribute("aria-pressed", String(account.accountId === selectedManagedAccountId));
    item.appendChild(element("span", "account-wechat-id", account.displayId || account.wechatId));
    if (account.deletionPending) item.appendChild(element("span", "account-current", "待完成清理"));
    else if (managedAccountIsCurrent(account)) item.appendChild(element("span", "account-current", "正在使用"));
    item.addEventListener("click", () => {
      if (accountDeleteBusy) return;
      selectedManagedAccountId = account.accountId;
      pendingDeleteAccountId = null;
      byId("accountDeleteConfirm").hidden = true;
      text("accountManagerStatus", managedAccountIsCurrent(account) ? "正在使用" : "");
      renderManagedAccounts();
    });
    row.appendChild(item);
    if (account.platform === "qq" && !managedAccountIsCurrent(account) && !account.deletionPending) {
      const open = element("button", "settings-action-btn", "查看");
      open.type = "button";
      open.disabled = accountDeleteBusy;
      open.addEventListener("click", () => { void activateManagedAccount(account); });
      row.appendChild(open);
    }
    if (accountManagementOpen) {
      const clear = element("button", "settings-danger-btn account-clear-btn", "清除");
      clear.type = "button";
      clear.disabled = accountDeleteBusy;
      clear.title = "清除本软件中的账号数据";
      clear.addEventListener("click", () => {
        if (clear.disabled) return;
        selectedManagedAccountId = account.accountId;
        pendingDeleteAccountId = account.accountId;
        const displayId = account.displayId || account.wechatId;
        const platformName = account.platform === "qq" ? "QQ" : "微信";
        text("accountDeleteQuestion", `${account.deletionPending ? "继续清除" : "清除"}账号 ${displayId} 在本软件中的聊天记录副本、分析与画像、运行缓存？${platformName}原始记录不会删除。${managedAccountIsCurrent(account) ? "若清除时仍为当前账号，软件将退出；下次启动重新初始化。" : "若清除时仍非当前账号，软件继续运行。"}`);
        text("btnConfirmDeleteAccount", managedAccountIsCurrent(account) ? "清除并退出" : "确认清除");
        byId("accountDeleteConfirm").hidden = false;
        renderManagedAccounts();
      });
      row.appendChild(clear);
    }
    list.appendChild(row);
  }
}
async function loadAccounts() {
  if (accountDeleteBusy) return;
  const request = ++accountManagerRequest;
  status(byId("accountList"), "正在读取账号…");
  try {
    const data = await api("/api/accounts");
    if (request !== accountManagerRequest) return;
    if (!Array.isArray(data.accounts) || !data.accounts.every(account => account &&
      typeof account.accountId === "string" && account.accountId &&
      typeof (account.displayId || account.wechatId) === "string" &&
      (account.displayId || account.wechatId) && typeof account.current === "boolean") ||
      !(data.currentAccountId === null || typeof data.currentAccountId === "string") ||
      new Set(data.accounts.map(account => account.accountId)).size !== data.accounts.length) throw new Error("账号列表无效");
    managedAccounts = data.accounts;
    managedCurrentAccountId = data.currentAccountId;
    if (!managedAccounts.some(account => account.accountId === selectedManagedAccountId)) selectedManagedAccountId = null;
    pendingDeleteAccountId = null;
    byId("accountDeleteConfirm").hidden = true;
    renderManagedAccounts();
    text("accountManagerStatus", "");
  } catch (error) {
    if (request !== accountManagerRequest) return;
    managedAccounts = [];
    managedCurrentAccountId = null;
    selectedManagedAccountId = null;
    pendingDeleteAccountId = null;
    byId("accountDeleteConfirm").hidden = true;
    status(byId("accountList"), "账号读取失败", () => { void loadAccounts(); });
    text("accountManagerStatus", error.message);
  }
}
let analysisCacheRequest = 0;
let analysisCacheBusy = false;
let pendingAnalysisCacheClear = null;
function renderAnalysisCache(data) {
  const list = byId("analysisCacheList");
  list.replaceChildren();
  if (!data.sources.length) status(list, "暂无分析缓存");
  for (const source of data.sources) {
    const apiSourceKey = source.kind === "api" ? JSON.stringify([data.account, source.sourceId]) : null;
    if (source.suspended) {
      if (apiSourceKey) settingsState.suppressedApiSources.add(apiSourceKey);
      else settingsState.suppressedLocalAccounts.add(data.account);
    } else if (apiSourceKey) {
      settingsState.suppressedApiSources.delete(apiSourceKey);
    } else {
      settingsState.suppressedLocalAccounts.delete(data.account);
    }
    const card = element("div", "analysis-cache-card");
    const main = element("div", "analysis-cache-card-main");
    const label = source.kind === "local" ? "本地 Laya" : source.label || "API 模型";
    const title = element("strong", "", label);
    const count = element("span", "", `消息分析 ${source.messageCount} · 画像 ${source.portraitCount}${source.suspended ? " · 已暂停" : ""}`);
    main.append(title, count);
    const clear = element("button", "settings-danger-btn", "清除");
    clear.type = "button";
    // An API source with a saved record can still hold stale job/config state, so
    // its Clear stays available even at 0/0. Only a truly empty local cache is a no-op.
    const clearable = source.kind === "api" || source.suspended ||
      source.messageCount + source.portraitCount > 0;
    clear.disabled = analysisCacheBusy || !clearable;
    clear.title = clearable ? "" : "该来源当前没有可清除的分析缓存";
    clear.addEventListener("click", () => {
      if (clear.disabled || !chatState.currentAccount || data.account !== chatState.currentAccount) return;
      pendingAnalysisCacheClear = { account: data.account, sourceId: source.sourceId,
        kind: source.kind, label };
      text("analysisCacheQuestion", `清除当前账号的「${label}」消息分析和画像缓存？聊天记录会保留。`);
      byId("analysisCacheConfirm").hidden = false;
    });
    card.append(main, clear);
    list.appendChild(card);
  }
}
async function loadAnalysisCache() {
  const request = ++analysisCacheRequest;
  const account = chatState.currentAccount;
  pendingAnalysisCacheClear = null;
  byId("analysisCacheConfirm").hidden = true;
  if (!account) {
    status(byId("analysisCacheList"), "当前账号未就绪");
    text("analysisCacheStatus", "");
    return;
  }
  text("analysisCacheStatus", "正在读取…");
  try {
    const data = await api("/api/analysis-cache");
    if (request !== analysisCacheRequest || account !== chatState.currentAccount) return;
    if (data?.account !== account || !Array.isArray(data.sources) ||
        !data.sources.every(source => source && typeof source.sourceId === "string" && source.sourceId &&
          ["local", "api"].includes(source.kind) && typeof source.label === "string" &&
          Number.isSafeInteger(source.messageCount) && source.messageCount >= 0 &&
          Number.isSafeInteger(source.portraitCount) && source.portraitCount >= 0 &&
          typeof source.suspended === "boolean"))
      throw new Error("分析缓存状态无效");
    renderAnalysisCache(data);
    text("analysisCacheStatus", "");
  } catch {
    if (request === analysisCacheRequest) text("analysisCacheStatus", "缓存读取失败，请重试");
  }
}
function clearLocalUiAnalysis(account) {
  for (const map of [portraitState.storedProfileSnapshots, portraitState.profileCache, portraitState.profileRateSamples])
    for (const key of map.keys()) try {
      const scope = JSON.parse(key);
      if (scope[0] === account && scope.length === 3) map.delete(key);
    } catch { }
  for (const key of portraitState.autoIncrementalState.keys()) try {
    if (JSON.parse(key)[0] === account) portraitState.autoIncrementalState.delete(key);
  } catch { }
  for (const entry of chatState.sessionCache.values()) if (entry.account === account) {
    entry.results = {};
    entry.mood = null;
  }
  for (const key of portraitState.profileSnapshotsRequireRefresh) try {
    const scope = JSON.parse(key);
    if (scope[0] === account && scope.length === 3) portraitState.profileSnapshotsRequireRefresh.delete(key);
  } catch { }
  saveStoredProfiles();
  for (let index = localStorage.length - 1; index >= 0; index--) {
    const key = localStorage.key(index);
    if (key?.startsWith(`mbti-unlocked:${account}:`)) localStorage.removeItem(key);
  }
  if (chatState.currentAccount === account) {
    labelState.results = {};
    chatState.conversationMood = null;
    labelState.requestedRecentSignatures.clear();
    clearProfileView(chatState.sessions.get(chatState.currentUser)?.name || "人物画像");
    text("stripDbPath", "—");
    text("stripMsgCount", "—");
    setStripStatus("画像缓存已清除");
    if (chatState.messages.length) renderMessages(chatState.messages);
  }
}
byId("btnManageAnalysisCache").addEventListener("click", () => {
  const panel = byId("analysisCacheManager");
  panel.hidden = !panel.hidden;
  text("btnManageAnalysisCache", panel.hidden ? "查看缓存" : "收起");
  if (!panel.hidden) void loadAnalysisCache();
});
byId("btnCancelAnalysisCacheClear").addEventListener("click", () => {
  pendingAnalysisCacheClear = null;
  byId("analysisCacheConfirm").hidden = true;
});
byId("btnConfirmAnalysisCacheClear").addEventListener("click", async () => {
  const pending = pendingAnalysisCacheClear;
  if (!pending || analysisCacheBusy || pending.account !== chatState.currentAccount) return;
  analysisCacheBusy = true;
  byId("btnConfirmAnalysisCacheClear").disabled = true;
  text("analysisCacheStatus", "正在清除…");
  try {
    const result = await api("/api/analysis-cache/clear", {
      method: "POST", body: JSON.stringify({ account: pending.account, sourceId: pending.sourceId }),
    });
    if (result?.cleared !== true || result.account !== pending.account || result.sourceId !== pending.sourceId)
      throw new Error("清理结果不匹配");
    if (pending.kind === "api") {
      settingsState.suppressedApiSources.delete(JSON.stringify([pending.account, pending.sourceId]));
      cancelApiInsightWork();
      portraitState.apiPortraitSubmitErrors.clear();
      for (const key of labelState.apiInsightCache.keys()) try {
        const [account, _user, sourceId] = JSON.parse(key);
        if (account === pending.account && sourceId === pending.sourceId) labelState.apiInsightCache.delete(key);
      } catch { }
      for (const map of [portraitState.storedProfileSnapshots, portraitState.profileCache, portraitState.profileRateSamples])
        for (const key of map.keys()) try {
          const scope = JSON.parse(key);
          if (scope[0] === pending.account && scope[3] === pending.sourceId) map.delete(key);
        } catch { }
      for (const key of portraitState.profileSnapshotsRequireRefresh) try {
        const scope = JSON.parse(key);
        if (scope[0] === pending.account && scope[3] === pending.sourceId)
          portraitState.profileSnapshotsRequireRefresh.delete(key);
      } catch { }
      saveStoredProfiles();
      if (settingsState.modelSourceSnapshot.sourceId === pending.sourceId && chatState.view === "persona") {
        cancelApiPortraitPoll();
        clearApiPortraitView();
        void loadApiPortrait(portraitState.activeMember);
      }
      if (settingsState.modelSourceSnapshot.sourceId === pending.sourceId && chatState.messages.length) renderMessages(chatState.messages);
    } else {
      settingsState.suppressedLocalAccounts.delete(pending.account);
      clearLocalUiAnalysis(pending.account);
    }
    pendingAnalysisCacheClear = null;
    byId("analysisCacheConfirm").hidden = true;
    analysisCacheBusy = false;
    await loadAnalysisCache();
    text("analysisCacheStatus", "已清除");
  } catch { text("analysisCacheStatus", "清除失败，请重试"); }
  finally {
    analysisCacheBusy = false;
    byId("btnConfirmAnalysisCacheClear").disabled = false;
  }
});
function closeSettingsModal() {
  settingsState.modelSourceLoadController?.abort();
  settingsState.modelSourceLoadController = null;
  settingsState.modelListController?.abort();
  settingsState.modelListController = null;
  settingsState.modelTestController?.abort();
  settingsState.modelTestController = null;
  byId("settingsModal").classList.remove("show");
  clearTimeout(settingsState.runtimePollTimer);
  ++analysisCacheRequest;
  pendingAnalysisCacheClear = null;
  byId("analysisCacheConfirm").hidden = true;
  byId("analysisCacheManager").hidden = true;
  text("btnManageAnalysisCache", "查看缓存");
  byId("conversationManager").hidden = true;
  byId("settingsModal").querySelector(".settings-modal-card").classList.remove("conversation-open");
  text("btnManageConversations", "管理会话");
  text("conversationManagerStatus", "");
  ++settingsState.modelSourceReadRequest;
  ++settingsState.modelListRequest;
  ++settingsState.modelTestRequest;
  settingsState.modelSourceLoading = false;
  settingsState.modelListBusy = false;
  settingsState.modelTestBusy = false;
  byId("inputApiKey").value = "";
  accountManagementOpen = false;
  text("btnToggleAccountManagement", "管理");
  byId("btnToggleAccountManagement").setAttribute("aria-pressed", "false");
  pendingDeleteAccountId = null;
  byId("accountDeleteConfirm").hidden = true;
  renderManagedAccounts();
}
async function deleteManagedAccount() {
  const account = managedAccounts.find(item => item.accountId === pendingDeleteAccountId);
  if (!account || accountDeleteBusy) return;
  const accountId = account.accountId;
  accountDeleteBusy = true;
  byId("btnConfirmDeleteAccount").disabled = true;
  renderManagedAccounts();
  text("accountManagerStatus", "正在删除…");
  try {
    const response = await fetch(`/api/accounts/${encodeURIComponent(accountId)}`, { method: "DELETE" });
    let data = null;
    try { data = await response.json(); } catch { }
    if (!response.ok) throw new Error(typeof data?.message === "string" && data.message ? data.message :
      typeof data?.error === "string" && data.error ? data.error : `删除失败（HTTP ${response.status}）`);
    if (data?.deleted !== accountId) throw new Error("删除结果不匹配");
    const exitAfterDelete = data.exitApp === true || data.current === true;
    if (exitAfterDelete) {
      accountClearedExiting = true;
      chatState.sessionRequest++;
      chatState.advance("generation");
      portraitState.advance("profileGeneration");
      portraitState.advance("analysisGeneration");
      chatState.controller?.abort();
      chatState.currentUser = null;
    }
    let cacheCleared = true;
    try { await clearStoredProfilesForAccount(accountId); }
    catch { cacheCleared = false; }
    if (exitAfterDelete) {
      resetAccountView(cacheCleared ? "账号数据已清除，正在退出…" : "账号已清除，本地缓存清理失败，请关闭软件", true);
      clearTimeout(startupWatchdog);
      byId("startupRetry").hidden = true;
      byId("startupContinue").hidden = true;
      text("accountManagerStatus", cacheCleared ? "已清除，正在退出…" : "账号已清除，本地缓存清理失败");
      try {
        if (typeof window.desktopHost?.exitApp !== "function") throw new Error("exitApp unavailable");
        await window.desktopHost.exitApp();
      } catch { text("startupStatus", "账号已清除，请关闭软件。"); }
      return;
    }
    selectedManagedAccountId = null;
    pendingDeleteAccountId = null;
    byId("accountDeleteConfirm").hidden = true;
    accountDeleteBusy = false;
    await loadAccounts();
    text("accountManagerStatus", cacheCleared ? "已清除" : "账号已清除，本地画像缓存清理失败");
    void loadSessions();
  } catch (error) {
    text("accountManagerStatus", error.message);
  } finally {
    accountDeleteBusy = false;
    byId("btnConfirmDeleteAccount").disabled = false;
    if (!accountClearedExiting) renderManagedAccounts();
  }
}
async function copyDraft() {
  const value = byId("chatInput").value;
  if (!value.trim()) return;
  let copied = false;
  if (window.desktopHost?.copyDraft) {
    try { copied = await window.desktopHost.copyDraft(value); } catch { }
  }
  if (!copied && navigator.clipboard?.writeText) {
    try { await navigator.clipboard.writeText(value); copied = true; } catch { }
  }
  if (!copied) {
    const input = byId("chatInput");
    const focus = document.activeElement;
    const start = input.selectionStart;
    const end = input.selectionEnd;
    try {
      input.focus();
      input.select();
      copied = document.execCommand("copy");
    } catch { }
    finally {
      input.setSelectionRange(start, end);
      if (focus && focus !== input && typeof focus.focus === "function") focus.focus();
    }
  }
  toast(copied ? "草稿已复制，未发送" : "复制失败，请手动复制草稿");
}
byId("searchInput").addEventListener("input", renderSessions);
byId("chatMessages").addEventListener("scroll", event => {
  const container = event.currentTarget;
  if (chatState.historyState) {
    chatState.followLatest = false;
    chatState.lastChatScrollTop = container.scrollTop;
    updateHistoryNavigation();
    clearTimeout(labelState.apiInsightViewportTimer);
    labelState.apiInsightViewportTimer = setTimeout(() => ensureApiInsights(), 250);
    return;
  }
  if (container.scrollHeight - container.scrollTop - container.clientHeight < 80) chatState.followLatest = true;
  else if (container.scrollTop < chatState.lastChatScrollTop - 1) chatState.followLatest = false;
  chatState.lastChatScrollTop = container.scrollTop;
  updateHistoryNavigation();
  if (settingsState.modelSourceResolved && settingsState.modelSourceSnapshot.mode === "api" && settingsState.settings.intent) {
    clearTimeout(labelState.apiInsightViewportTimer);
    labelState.apiInsightViewportTimer = setTimeout(() => ensureApiInsights(), 250);
  }
});
byId("btnHistoryEarlier").addEventListener("click", () => void loadOlderHistory());
byId("btnHistoryNewer").addEventListener("click", () => void loadNewerHistory());
byId("btnReturnLatest").addEventListener("click", returnToLatest);
byId("btnChatHistory").addEventListener("click", () => byId("historySearchPanel").hidden ? openHistorySearch() : closeHistorySearch());
byId("btnCloseHistorySearch").addEventListener("click", closeHistorySearch);
byId("historySearchForm").addEventListener("submit", event => { event.preventDefault(); startHistorySearch(); });
byId("btnCancelHistorySearch").addEventListener("click", () => cancelHistorySearch(true));
byId("btnHistoryPrevResults").addEventListener("click", () => void loadHistorySearchPage(chatState.historySearchPage - 1));
byId("btnHistoryNextResults").addEventListener("click", () => void loadHistorySearchPage(chatState.historySearchPage + 1));
byId("btnSend").addEventListener("click", copyDraft);
byId("chatInput").addEventListener("input", () => {
  byId("btnSend").classList.toggle("ready", !!byId("chatInput").value.trim());
  if (!byId("replyPrediction").hidden) clearReplyPrediction();
});
byId("btnPredictReply")?.addEventListener("click", () => {
  closeSettingsModal();
  switchView("chat");
  void requestReplyPrediction();
});
byId("btnClosePrediction").addEventListener("click", clearReplyPrediction);
byId("chatInput").addEventListener("keydown", event => { if (event.key === "Enter" && !event.shiftKey && !event.isComposing) { event.preventDefault(); copyDraft(); } });
byId("navChat").addEventListener("click", () => switchView("chat"));
byId("navPersona").addEventListener("click", () => switchView("persona"));
byId("btnToolbarPersona").addEventListener("click", () => switchView("persona"));
byId("btnBackToChat").addEventListener("click", () => switchView("chat"));
function retryAnalysis() {
  if (!chatState.currentUser || !chatState.controller) return;
  if (settingsState.suppressedLocalAccounts.has(chatState.currentAccount)) {
    setStripStatus("分析已暂停，请在设置中清除该来源缓存后重试");
    return;
  }
  if (settingsState.modelSourceResolved && settingsState.modelSourceSnapshot.mode === "api") {
    if (activeApiInsightEntry()?.error) activeApiInsightEntry().error = "";
    ensureApiInsights(true);
    renderApiInsightStatus();
    return;
  }
  if (!canAnalyzeLocal()) {
    text("analysisStatus", settingsState.localModelResolved ? "未安装 Laya 模型，请在设置下载模型" : "正在检查 Laya 模型");
    return;
  }
  const key = portraitState.activeAnalysisScope || JSON.stringify([chatState.currentAccount, chatState.currentUser, "current"]);
  const state = incrementalState(key);
  if (state.pending) return;
  byId("btnRetryAnalysis").hidden = true;
  byId("btnRetryProfile").hidden = true;
  setStripStatus("");
  text("analysisStatus", "");
  labelState.recentFailed = false;
  portraitState.incrementalFailed = false;
  portraitState.activeAnalysisScope = key;
  state.failed = false;
  state.queued = false;
  state.bootstrapRequested = true;
  state.requestedSignature = JSON.stringify(chatState.messages);
  void startIncremental(chatState.currentUser, chatState.generation, chatState.controller.signal, key, state);
}
byId("btnRetryAnalysis").addEventListener("click", retryAnalysis);
let qqAnalysisControlBusy = false;
function renderQQAnalysisControl(state) {
  byId('btnPauseQQAnalysis').hidden = state !== 'running';
  byId('btnResumeQQAnalysis').hidden = state === 'running';
  byId('btnCancelQQAnalysis').hidden = state === 'cancelled';
  text('qqAnalysisControlStatus', ({paused:'已暂停 · 阅读和同步继续', cancelled:'已取消 · 已完成结果保留'})[state] || '');
}
async function refreshQQAnalysisControl() {
  const active = window.ProductConfig?.key === 'qq' && chatState.currentAccount && chatState.currentUser;
  byId('qqAnalysisControls').hidden = !active;
  if (!active || qqAnalysisControlBusy) return;
  const account = chatState.currentAccount, user = chatState.currentUser;
  try {
    const data = await api(`/api/qq/analysis/control?account=${encodeURIComponent(account)}&user=${encodeURIComponent(user)}`);
    if (account === chatState.currentAccount && user === chatState.currentUser) {
      renderQQAnalysisControl(data.analysisControl);
      byId('qqAnalysisBudget').hidden = data.modelMode !== 'api';
      const usage = data.apiUsage;
      if (usage) text('qqAnalysisUsage', `当前来源已调用 ${usage.usedRequests} 次（含重试）· 剩余 ${usage.remainingRequests} 次${usage.remainingRequests === 0 ? ' · 额度用尽，设置后重试' : ''} · 未提供费率，未估算金额`);
    }
  } catch {
    if (account === chatState.currentAccount && user === chatState.currentUser) text('qqAnalysisControlStatus', '分析控制暂不可用，请重试');
  }
}
async function controlQQAnalysis(action) {
  if (qqAnalysisControlBusy || !chatState.currentUser) return;
  const account = chatState.currentAccount, user = chatState.currentUser;
  qqAnalysisControlBusy = true;
  const buttons = ['btnPauseQQAnalysis','btnResumeQQAnalysis','btnCancelQQAnalysis'].map(byId);
  buttons.forEach(button => { button.disabled = true; });
  try {
    const data = await api('/api/qq/analysis/control', {method:'POST',body:JSON.stringify({account,user,action})});
    if (account !== chatState.currentAccount || user !== chatState.currentUser) return;
    renderQQAnalysisControl(data.analysisControl);
    if (action === 'resume') {
      if (settingsState.modelSourceSnapshot?.mode === 'api') retryAnalysis();
      else await api('/api/analyze', {method:'POST',body:JSON.stringify({account,user,mode:'recent',limit:80})});
    }
  } catch {
    if (account === chatState.currentAccount && user === chatState.currentUser) text('qqAnalysisControlStatus','操作未完成，请重试');
  } finally {
    qqAnalysisControlBusy = false;
    buttons.forEach(button => { button.disabled = false; });
  }
}
byId('btnPauseQQAnalysis').addEventListener('click', () => { void controlQQAnalysis('pause'); });
byId('btnResumeQQAnalysis').addEventListener('click', () => { void controlQQAnalysis('resume'); });
byId('btnCancelQQAnalysis').addEventListener('click', () => { void controlQQAnalysis('cancel'); });
byId('btnQQAnalysisBudget').addEventListener('click', async () => {
  const account = chatState.currentAccount, user = chatState.currentUser;
  const requests = Number(byId('qqAnalysisRequestLimit').value);
  if (!account || !user || !Number.isInteger(requests) || requests < 1 || requests > 1000) {
    text('qqAnalysisUsage','请输入 1 至 1000 的整数请求上限'); return;
  }
  const button = byId('btnQQAnalysisBudget');
  if (button.disabled) return;
  button.disabled = true;
  try {
    await api('/api/qq/analysis/budget',{method:'POST',body:JSON.stringify({account,user,requests})});
    if (account === chatState.currentAccount && user === chatState.currentUser) await refreshQQAnalysisControl();
  } catch {
    if (account === chatState.currentAccount && user === chatState.currentUser) text('qqAnalysisUsage','设置未完成，请重试');
  } finally { button.disabled = false; }
});
setInterval(() => { void refreshQQAnalysisControl(); }, 2000);
byId("btnRetryProfile").addEventListener("click", () => {
  if (chatState.view === "persona" && portraitState.activeMember) void loadProfile(portraitState.activeMember, true);
  else retryAnalysis();
});
byId("btnToggleLabelView").addEventListener("click", () => {
  settingsState.settings.labelDetails = !settingsState.settings.labelDetails;
  save();
  applySettings();
});
byId("btnToggleIntent").addEventListener("click", () => {
  settingsState.settings.intent = !settingsState.settings.intent;
  save();
  if (!settingsState.settings.intent) {
    labelState.manualRecentDeferred = false;
    setIntentActionState("idle");
    cancelApiInsightWork();
  }
  applySettings();
  if (settingsState.settings.intent) {
    if (settingsState.modelSourceSnapshot.mode === "api") ensureApiInsights(true);
    else submitManualRecent();
  }
});
for (const [id, key] of [["selectThemeMode", "theme"], ["selectZoomLevel", "zoom"]]) byId(id).addEventListener("change", event => { settingsState.settings[key] = event.target.value; save(); applySettings(); });
byId("selectRuntimeProvider").addEventListener("change", event => { void changeRuntime(event.target.value); });
byId("selectModelSource").addEventListener("change", () => {
  settingsState.modelSourceDraftDirty = true;
  invalidateModelDiscovery();
  showModelSourceMode();
});
for (const id of ["selectApiProtocol", "inputApiBaseUrl", "inputApiKey"])
  byId(id).addEventListener(id === "selectApiProtocol" ? "change" : "input", () => {
    settingsState.modelSourceDraftDirty = true;
    invalidateModelDiscovery();
    syncSavedApiKeyHint();
  });
byId("selectApiModel").addEventListener("change", event => {
  if (event.target.value) {
    byId("inputApiModelId").value = event.target.value;
    byId("inputApiContextTokens").value = event.target.selectedOptions?.[0]?.dataset.contextTokens || "";
  }
  settingsState.modelSourceDraftDirty = true;
  invalidateModelTest();
});
byId("inputApiModelId").addEventListener("input", () => {
  const select = byId("selectApiModel");
  const model = byId("inputApiModelId").value.trim();
  select.value = Array.from(select.options).some(option => option.value === model) ? model : "";
  byId("inputApiContextTokens").value = select.selectedOptions?.[0]?.dataset.contextTokens || "";
  settingsState.modelSourceDraftDirty = true;
  invalidateModelTest();
});
byId("inputApiContextTokens").addEventListener("input", () => {
  settingsState.modelSourceDraftDirty = true;
  text("modelSourceStatus", "");
});
byId("btnFetchApiModels").addEventListener("click", () => { void fetchApiModels(); });
byId("btnReloadModelSource").addEventListener("click", () => { void loadModelSource(true); });
byId("btnTestApiModel").addEventListener("click", () => { void testApiModel(); });
byId("btnActivateLocal").addEventListener("click", () => { void activateModelSource("local"); });
byId("btnActivateApi").addEventListener("click", () => { void activateModelSource("api"); });
byId("btnClearApiKey").addEventListener("click", () => { void clearStoredApiKey(); });
const OFFICIAL_RELEASES_URL = window.ProductConfig?.key === "qq" ? "https://github.com/xzyj50609/QQVibe/releases" : "https://github.com/tswawa/WechatVibe/releases";
const UPDATE_BUSY_PHASES = new Set(["downloading", "verifying", "extracting", "installing", "restarting"]);
let aboutVersionPromise = null;
let versionLoadFailed = false;
let updateState = { phase: "idle" };
let updateStateSequence = 0;
let updateCheckPending = false;
let updateActionPending = false;
let updateOperationStatus = "";
let updateCheckedOnce = false;
let updatePreviousFocus = null;

function displayVersion(value) {
  const version = typeof value === "string" ? value.trim() : "";
  return version ? (version.startsWith("v") ? version : `v${version}`) : "";
}
function setDisplayedVersion(value) {
  const version = displayVersion(value);
  if (!version) return;
  text("aboutCurrentVersion", version);
  text("updateCurrentVersion", version);
  byId("btnAboutVersion").setAttribute("aria-label", `查看软件更新，当前版本 ${version}`);
}
function loadAboutVersion() {
  if (aboutVersionPromise) return aboutVersionPromise;
  if (typeof window.desktopHost?.getAppVersion !== "function") {
    text("aboutCurrentVersion", "--");
    text("updateCurrentVersion", "--");
    text("updateStatus", "仅桌面版可用");
    return Promise.resolve();
  }
  aboutVersionPromise = window.desktopHost.getAppVersion().then(version => {
    if (!displayVersion(version)) throw new Error("Invalid app version");
    versionLoadFailed = false;
    setDisplayedVersion(version);
  }).catch(() => {
    versionLoadFailed = true;
    text("aboutCurrentVersion", "读取失败");
    text("updateCurrentVersion", "读取失败");
    renderUpdateState();
    aboutVersionPromise = null;
  });
  return aboutVersionPromise;
}
function officialReleaseUrl(candidate) {
  try {
    const url = new URL(candidate);
    if (url.href === OFFICIAL_RELEASES_URL) return url.href;
  } catch { }
  return OFFICIAL_RELEASES_URL;
}
function updatePhase(state) {
  return typeof state?.phase === "string" ? state.phase :
    (typeof state?.status === "string" ? state.status : "");
}
function updateStatusMessage(state) {
  const latest = displayVersion(state.latestVersion);
  const error = typeof state.error === "string" ? state.error.trim().slice(0, 180) : "";
  return {
    idle: "准备检查更新",
    current: "已是最新版本",
    "preview-current": "已是最新版本",
    available: latest ? `发现新版本 ${latest}${state.prerelease ? "（预发布）" : ""}` : "发现新版本",
    downloading: "正在下载更新",
    verifying: "正在校验更新包",
    extracting: "正在解压更新包",
    ready: error || "更新已下载，点击重启并更新",
    "no-space": "磁盘空间不足，请释放空间后重试",
    installing: "正在安装更新",
    restarting: "正在重启",
    rolled_back: "已回退到上一版本",
    failed: error ? `更新失败：${error}` : "更新失败，请重试",
    "incomplete-release": latest ? `发现 ${latest}，发布文件尚未齐全` : "发布文件尚未齐全",
    "no-release": "所选渠道暂无发布版本",
    "invalid-current": "当前版本信息异常",
    "invalid-release": "发布信息异常，请稍后重试",
    "rate-limited": "检查次数受限，请稍后重试",
    timeout: "检查超时，请重试",
    offline: "网络不可用，请重试",
    "server-error": "暂时无法检查更新",
    unconfigured: "尚未配置独立更新源",
  }[updatePhase(state)] || "暂时无法检查更新";
}
function renderUpdateState() {
  const phase = updatePhase(updateState);
  const latest = displayVersion(updateState.latestVersion);
  byId("updateLatestRow").hidden = !latest;
  text("updateLatestVersion", latest);
  text("updateStatus", updateCheckPending ? "正在检查更新" :
    (updateOperationStatus || (!window.desktopHost ? "仅桌面版可用" :
      (versionLoadFailed && phase === "idle" ? "版本读取失败" : updateStatusMessage(updateState)))));
  byId("updateReleaseLink").hidden = phase === "unconfigured";
  byId("updateReleaseLink").href = officialReleaseUrl(updateState.releaseUrl);

  const downloading = phase === "downloading";
  const downloaded = Number(updateState.downloadedBytes);
  const total = Number(updateState.totalBytes);
  byId("updateProgress").hidden = !downloading;
  if (downloading) {
    const knownTotal = Number.isFinite(total) && total > 0;
    const safeDownloaded = Number.isFinite(downloaded) ? Math.max(0, downloaded) : 0;
    const percent = knownTotal ? Math.min(100, Math.round(safeDownloaded / total * 100)) : 0;
    const bar = byId("updateProgressBar");
    bar.classList.toggle("indeterminate", !knownTotal);
    if (knownTotal) bar.setAttribute("aria-valuenow", String(percent));
    else bar.removeAttribute("aria-valuenow");
    byId("updateProgressFill").style.width = `${percent}%`;
    text("updateProgressText", knownTotal ? `${percent}%` : `${(safeDownloaded / 1048576).toFixed(1)} MB`);
  }

  const canRollback = !!displayVersion(updateState.rollbackVersion) &&
    typeof window.desktopHost?.rollbackUpdate === "function";
  byId("updateRollback").hidden = !canRollback;
  text("updateRollbackVersion", displayVersion(updateState.rollbackVersion));
  byId("btnRollbackUpdate").disabled = updateActionPending || updateCheckPending || UPDATE_BUSY_PHASES.has(phase);

  const canCheck = typeof window.desktopHost?.checkForUpdates === "function" &&
    phase !== "unconfigured";
  byId("btnCheckUpdates").disabled = !canCheck || updateCheckPending || updateActionPending ||
    UPDATE_BUSY_PHASES.has(phase) || phase === "ready";
  const canBegin = typeof window.desktopHost?.beginUpdate === "function" &&
    (phase === "available" || phase === "ready");
  byId("btnBeginUpdate").hidden = !canBegin;
  byId("btnBeginUpdate").disabled = updateCheckPending || updateActionPending;
  text("btnBeginUpdate", phase === "ready" ? "重启并更新" : window.ProductConfig?.key === "qq" ? "下载更新" : "下载并安装");
  if (byId("updateNotes")) {
    byId("updateNotes").hidden = !updateState.notes;
    text("updateNotes", typeof updateState.notes === "string" ? updateState.notes : "");
  }
  if (byId("updateDownloadSize")) {
    byId("updateDownloadSize").hidden = !Number.isFinite(total) || total <= 0;
    text("updateDownloadSize", total > 0 ? `更新包 ${(total / 1000000).toFixed(1)} MB，已有本地模型将保留。` : "");
  }
  if (["available", "ready"].includes(phase)) {
    text("aboutCurrentVersion", phase === "ready" ? "更新已就绪" : "有新版本");
  } else if (updateState.currentVersion) setDisplayedVersion(updateState.currentVersion);
}
function applyUpdateState(next) {
  if (!updatePhase(next)) return false;
  updateState = { ...updateState, ...next, phase: updatePhase(next) };
  updateOperationStatus = "";
  updateStateSequence++;
  renderUpdateState();
  return true;
}
async function checkForUpdates() {
  if (updateCheckPending || updateActionPending || typeof window.desktopHost?.checkForUpdates !== "function" ||
      UPDATE_BUSY_PHASES.has(updatePhase(updateState))) return;
  updateCheckedOnce = true;
  updateCheckPending = true;
  renderUpdateState();
  const before = updateStateSequence;
  try {
    const result = await window.desktopHost.checkForUpdates();
    if (before === updateStateSequence) applyUpdateState(result);
  } catch {
    if (before === updateStateSequence) applyUpdateState({ phase: "server-error" });
  } finally {
    updateCheckPending = false;
    renderUpdateState();
  }
}
async function refreshUpdateState() {
  if (typeof window.desktopHost?.getUpdateState === "function") {
    const before = updateStateSequence;
    try {
      const state = await window.desktopHost.getUpdateState();
      if (before === updateStateSequence) applyUpdateState(state);
    } catch { /* The check action remains available. */ }
  }
  if (!updateCheckedOnce && ["idle", "current", "preview-current"].includes(updatePhase(updateState)))
    void checkForUpdates();
}
function openUpdateModal() {
  updatePreviousFocus = document.activeElement;
  byId("updateModal").classList.add("show");
  byId("btnCloseUpdate").focus();
  void loadAboutVersion();
  void refreshUpdateState();
  void loadUpdatePreferences();
  renderUpdateState();
}
function closeUpdateModal() {
  byId("updateModal").classList.remove("show");
  if (updatePreviousFocus?.isConnected) updatePreviousFocus.focus();
}
async function loadUpdatePreferences() {
  if (window.ProductConfig?.key !== "qq" || typeof window.desktopHost?.getUpdatePreferences !== "function") return;
  const prefs = await window.desktopHost.getUpdatePreferences().catch(() => null);
  if (!prefs) return;
  byId("updatePreferences").hidden = false;
  byId("updateAutoCheck").checked = prefs.autoCheck;
  byId("updateAutoDownload").checked = prefs.autoDownload;
  byId("updateChannel").value = prefs.channel;
}
for (const id of ["updateAutoCheck", "updateAutoDownload", "updateChannel"]) {
  byId(id)?.addEventListener("change", async () => {
    const patch = { autoCheck: byId("updateAutoCheck").checked, autoDownload: byId("updateAutoDownload").checked, channel: byId("updateChannel").value };
    const saved = await window.desktopHost?.setUpdatePreferences(patch).catch(() => null);
    text("updatePreferenceStatus", saved && saved.autoCheck === patch.autoCheck && saved.autoDownload === patch.autoDownload && saved.channel === patch.channel ? "设置已保存" : "设置未保存，更新完成后再试");
    await loadUpdatePreferences();
    if (id === "updateChannel" && saved?.channel === patch.channel) void checkForUpdates();
  });
}
byId("btnAboutVersion").addEventListener("click", openUpdateModal);
byId("btnCloseUpdate").addEventListener("click", closeUpdateModal);
byId("updateModal").addEventListener("click", event => { if (event.target === byId("updateModal")) closeUpdateModal(); });
document.addEventListener("keydown", event => { if (event.key === "Escape" && byId("updateModal").classList.contains("show")) closeUpdateModal(); });
window.addEventListener("wechatvibe-update-state", event => { applyUpdateState(event.detail); });
byId("btnCheckUpdates").addEventListener("click", () => { void checkForUpdates(); });
byId("btnBeginUpdate").addEventListener("click", async () => {
  if (updateActionPending || !["available", "ready"].includes(updatePhase(updateState))) return;
  updateActionPending = true;
  updateOperationStatus = updatePhase(updateState) === "ready" ? "正在安装更新" : "正在启动更新";
  renderUpdateState();
  const before = updateStateSequence;
  try {
    const result = await window.desktopHost.beginUpdate();
    if (result === false) throw new Error("更新未启动");
    if (before === updateStateSequence && !applyUpdateState(result)) void refreshUpdateState();
  } catch (error) {
    if (before === updateStateSequence) applyUpdateState({ phase: "failed", error: error?.message || "请重试" });
  } finally {
    updateActionPending = false;
    updateOperationStatus = "";
    renderUpdateState();
  }
});
byId("btnRollbackUpdate").addEventListener("click", async () => {
  if (updateActionPending || !updateState.rollbackVersion ||
      typeof window.desktopHost?.rollbackUpdate !== "function") return;
  updateActionPending = true;
  updateOperationStatus = "正在回退版本";
  renderUpdateState();
  const before = updateStateSequence;
  try {
    const result = await window.desktopHost.rollbackUpdate();
    if (result === false) throw new Error("回退未启动");
    if (before === updateStateSequence && !applyUpdateState(result)) void refreshUpdateState();
  } catch (error) {
    if (before === updateStateSequence) applyUpdateState({ phase: "failed", error: error?.message || "回退失败" });
  } finally {
    updateActionPending = false;
    updateOperationStatus = "";
    renderUpdateState();
  }
});
void loadAboutVersion();
renderUpdateState();
byId("btnSettings").addEventListener("click", () => {
  byId("settingsModal").classList.add("show");
  void loadRuntime();
  void loadModelSource();
  void loadLocalModel();
  if (typeof window.desktopHost?.getModelDownloadState === "function")
    void window.desktopHost.getModelDownloadState().then(showLocalModelDownload);
});
byId("btnManageConversations").addEventListener("click", () => {
  const panel = byId("conversationManager");
  if (panel.hidden) openConversationManager();
  else {
    panel.hidden = true;
    byId("settingsModal").querySelector(".settings-modal-card").classList.remove("conversation-open");
    text("btnManageConversations", "管理会话");
  }
});
byId("conversationSearch").addEventListener("input", renderConversationManager);
byId("btnCloseSettings").addEventListener("click", closeSettingsModal);
byId("settingsModal").addEventListener("click", event => { if (event.target === byId("settingsModal")) closeSettingsModal(); });
byId("btnManageAccounts").addEventListener("click", () => {
  const panel = byId("accountManager");
  panel.hidden = !panel.hidden;
  byId("settingsModal").querySelector(".settings-modal-card").classList.toggle("account-open", !panel.hidden);
  text("btnManageAccounts", panel.hidden ? "查看账号" : "收起");
  if (!panel.hidden) void loadAccounts();
});
byId("btnRefreshAccounts").addEventListener("click", () => { void loadAccounts(); });
if (window.ProductConfig?.key === "qq") {
  byId('qqBackupPanel').hidden=false;
  byId('btnBackupQQData').addEventListener('click',async()=>{
    if(!window.desktopHost?.backupQQData){text('qqBackupStatus','请在 QQVibe 独立桌面包中使用备份。');return;}
    byId('btnBackupQQData').disabled=true;text('qqBackupStatus','请选择备份位置，正在保存全部数据…');
    try{
      const result=await window.desktopHost.backupQQData(settingsState.settings);
      text('qqBackupStatus',result.state==='complete'?`备份完成：${result.counts.messages} 条消息，已校验文件内容。`:
        result.state==='cancelled'?'已取消备份。':result.reason||'备份尚未完成。');
    }finally{byId('btnBackupQQData').disabled=false;}
  });
  byId('btnRestoreQQData').addEventListener('click',async()=>{
    if(!window.desktopHost?.restoreQQData){text('qqBackupStatus','请在 QQVibe 独立桌面包中恢复备份。');return;}
    byId('btnRestoreQQData').disabled=true;text('qqBackupStatus','请选择备份，校验后会显示恢复范围。');
    try{
      const result=await window.desktopHost.restoreQQData();
      text('qqBackupStatus',result.state==='restoring'?'正在安全退出并恢复，完成后自动重启。':
        result.state==='cancelled'?'已取消恢复。':result.reason||'恢复尚未开始。');
    }finally{byId('btnRestoreQQData').disabled=false;}
  });
  void api('/api/qq/data/restore-status').then(result=>{
    if(result.state==='failed'){text('qqBackupStatus','恢复未完成，旧数据已保留或回退；请重新选择经过校验的备份。');return;}
    if(result.state!=='complete'||localStorage.getItem('qq-restored-preferences-operation')===result.operation)return;
    settingsState.settings={...settingsState.settings,...result.uiPreferences};save();applySettings();
    localStorage.setItem('qq-restored-preferences-operation',result.operation);
    text('qqBackupStatus',`已恢复 ${result.counts.messages} 条消息与分析缓存；请确认连接和模型来源。`);
  }).catch(()=>{});
  window.QQSupportController = window.QQSupportUI.mount({ document, api,
    getScope: () => ({ account: chatState.currentAccount, user: chatState.currentUser,
      generation: chatState.generation }),onConversationChange:async()=>{
        chatState.selectionLoadedAccount=null;await loadSessions();
      } });
  const qqConnectionUI = window.QQSyncUI.mount({ document, api, getAccount: () => chatState.currentAccount,
    onStatus: sync => window.QQStartup?.setStatus({ localReady: Boolean(chatState.currentAccount),
      connection: sync?.state === "online" ? "online" : "offline", sync }),
    onChange: async () => {
      // Connecting/add-contact changes server-side selection in the same account.
      // Reject any older in-flight list and reload that selection before rendering.
      chatState.selectionLoadedAccount = null;
      chatState.sessionRequest++;
      await loadAccounts();
      await loadSessions();
    } });
  setInterval(() => { void qqConnectionUI.load(); }, 5000);
  window.ChatBeanUI?.mount({ switchView, addConversation: qqConnectionUI.addConversation });
  window.QQImportUI.mount({ document, api,
    chooseFile: typeof window.desktopHost?.chooseQQExport === "function" ?
      () => window.desktopHost.chooseQQExport() : undefined,
    onComplete: async () => { await loadAccounts(); await loadSessions(); },
    openAccount: result => activateManagedAccount({ accountId: result.accountId, platform: "qq" }),
  });
}
byId("btnRetryAccountCheck").addEventListener("click", () => { void loadSessions(); });
byId("btnToggleAccountManagement").addEventListener("click", () => {
  accountManagementOpen = !accountManagementOpen;
  text("btnToggleAccountManagement", accountManagementOpen ? "完成" : "管理");
  byId("btnToggleAccountManagement").setAttribute("aria-pressed", String(accountManagementOpen));
  if (!accountManagementOpen) {
    pendingDeleteAccountId = null;
    byId("accountDeleteConfirm").hidden = true;
  }
  renderManagedAccounts();
});
byId("btnCancelDeleteAccount").addEventListener("click", () => {
  pendingDeleteAccountId = null;
  byId("accountDeleteConfirm").hidden = true;
});
byId("btnConfirmDeleteAccount").addEventListener("click", () => { void deleteManagedAccount(); });
document.querySelectorAll(".settings-tab-btn").forEach(tab => tab.addEventListener("click", () => {
  document.querySelectorAll(".settings-tab-btn").forEach(node => node.classList.toggle("active", node === tab));
  document.querySelectorAll(".settings-panel").forEach(node => node.classList.toggle("active", node.id === ({ general: "panelGeneral", about: "panelAbout" })[tab.dataset.tab]));
  byId("settingsModal").querySelector(".settings-modal-card").classList.toggle("account-open", tab.dataset.tab === "general" && !byId("accountManager").hidden);
  byId("settingsModal").querySelector(".settings-modal-card").classList.toggle("conversation-open", tab.dataset.tab === "general" && !byId("conversationManager").hidden);
  if (tab.dataset.tab === "about") void loadAboutVersion();
}));
byId("btnEmoji").addEventListener("click", event => { event.stopPropagation(); byId("emojiPopover").classList.toggle("show"); });
document.querySelectorAll(".popover-tab").forEach(tab => tab.addEventListener("click", event => {
  event.stopPropagation();
  document.querySelectorAll(".popover-tab").forEach(node => node.classList.toggle("active", node === tab));
  byId("emojiGrid").classList.toggle("active", tab.dataset.tab === "emoji");
  byId("kaomojiGrid").classList.toggle("active", tab.dataset.tab === "kaomoji");
}));
function insertKaomoji(value) {
  const input = byId("chatInput");
  input.setRangeText(value, input.selectionStart, input.selectionEnd, "end");
  input.dispatchEvent(new Event("input"));
  input.focus();
  byId("emojiPopover").classList.remove("show");
}
document.querySelectorAll(".emoji-btn").forEach(button => button.addEventListener("click", () => insertKaomoji(button.textContent)));
byId("kaomojiGrid").addEventListener("click", event => {
  const button = event.target.closest(".kaomoji-btn");
  if (button) insertKaomoji(button.textContent);
});
function renderKaomojiPanel() {
  const grid = byId("kaomojiGrid");
  grid.replaceChildren();
  for (const group of window.Kaomoji.panelItems()) {
    const section = element("section", "kaomoji-category");
    section.appendChild(element("div", "kaomoji-category-title", group.category));
    const items = element("div", "kaomoji-category-items");
    for (const item of group.items) for (const variant of item.variants) {
      const button = element("button", "kaomoji-btn", variant);
      button.type = "button";
      button.title = item.label;
      items.appendChild(button);
    }
    section.appendChild(items);
    grid.appendChild(section);
  }
}
async function loadCatalog() {
  try {
    const response = await fetch("data/analysis-catalog.json", { cache: "no-store" });
    if (!response.ok) return;
    const catalog = await response.json();
    const coverage = window.Kaomoji.installCatalog(catalog);
    if (coverage.missing.length) { console.error("Missing canonical kaomoji mapping:", coverage.missing); return; }
    const addAlias = (map, alias, display) => {
      const key = intentAlias(alias);
      if (!key || !display) return;
      if (!map.has(key)) map.set(key, display);
      else if (map.get(key) !== display) map.set(key, null);
    };
    const legacyIntentAliases = new Map();
    for (const [alias, display] of Object.entries(catalog.legacyIntentLabels || {})) {
      addAlias(legacyIntentAliases, alias, String(display || "").trim());
    }
    const canonicalIntentAliases = new Map();
    for (const entry of catalog.intents || []) {
      const display = String(entry.displayLabel || entry.label || "").trim();
      for (const alias of [entry.id, entry.modelLabel, entry.label, display]) {
        addAlias(canonicalIntentAliases, alias, display);
      }
    }
    const legacyEmotionAliases = new Map();
    const canonicalEmotionAliases = new Map();
    for (const entry of catalog.emotions || []) {
      const display = String(entry.label || "").trim();
      for (const alias of Array.isArray(entry.aliases) ? entry.aliases : []) {
        addAlias(legacyEmotionAliases, alias, display);
      }
      for (const alias of [entry.id, entry.modelLabel, display]) {
        addAlias(canonicalEmotionAliases, alias, display);
      }
    }
    labelState.intentDisplayAliases = new Map([...legacyIntentAliases, ...canonicalIntentAliases]);
    labelState.emotionDisplayAliases = new Map([...legacyEmotionAliases, ...canonicalEmotionAliases]);
    labelState.catalogLabelRevision = String(catalog.labelRevision || catalog.intentDisplayVersion || catalog.version || "");
    labelState.catalogReady = true;
    renderKaomojiPanel();
    refreshLabels();
    if (chatState.view === "persona") loadProfile(portraitState.activeMember);
  } catch { }
}
document.addEventListener("click", event => { if (!event.target.closest("#emojiPopover, #btnEmoji")) byId("emojiPopover").classList.remove("show"); });
function closeMemberPicker() {
  byId("groupMemberTabs").querySelector(".member-picker")?.classList.remove("open");
  byId("groupMemberTabs").querySelector(".member-picker-trigger")?.setAttribute("aria-expanded", "false");
}
document.addEventListener("click", event => { if (!event.target.closest("#groupMemberTabs")) closeMemberPicker(); });
document.addEventListener("keydown", event => { if (event.key === "Escape") { closeMemberPicker(); closeHistorySearch(); byId("emojiPopover").classList.remove("show"); } });
renderKaomojiPanel();
applySettings();
loadCatalog();
setInterval(() => { if (!labelState.catalogReady && !document.hidden) loadCatalog(); }, 30000);
async function startInitialLoad() {
  const attempt = ++startupAttempt;
  clearTimeout(startupAccountRetryTimer);
  startupAccountRetryTimer = null;
  startupAccountRetryUsed = false;
  showStartup("account", "正在读取当前账号…");
  let healthReady = false;
  try {
    const data = await api("/api/health");
    if (attempt !== startupAttempt || !startupActive) return;
    window.QQStartup?.setStatus(data.source);
    healthReady = data.data?.state === "ready";
    if (data.data?.state === "error") setStripStatus("数据源不可用");
  } catch { }
  if (attempt !== startupAttempt || !startupActive) return;
  showStartup(healthReady ? "sessions" : "account", healthReady ? "正在读取会话列表…" : "正在确认当前账号…");
  void loadSessions();
}
byId("startupRetry").addEventListener("click", retryStartup);
byId("startupContinue").addEventListener("click", () => { if (startupActive && accountUnavailable) completeStartup(); });
const updateValidationMode = window.desktopHost?.updateValidationMode === true;
const updateFinalReadyMode = window.desktopHost?.updateFinalReadyMode === true;
let updateCommitReady = !updateFinalReadyMode;
if (updateValidationMode) {
  // The updater checks that this script and its desktop bridge actually run
  // before opening a writable chat session in the new installation.
  byId("startupOverlay").hidden = false;
  byId("appWindow").setAttribute("inert", "");
  byId("startupOverlay").classList.add("update-validation");
  text("startupStatus", "正在验证新版本…");
  void window.desktopHost.reportUiReady().catch(() => {});
} else if (updateFinalReadyMode) {
  byId("startupOverlay").hidden = false;
  byId("appWindow").setAttribute("inert", "");
  text("startupStatus", "正在完成更新…");
  void window.desktopHost.reportUiReady().then(ready => {
    if (!ready) return;
    updateCommitReady = true;
    unlockStartupUi();
    void startInitialLoad();
  }).catch(() => {});
} else {
  if (window.QQStartup) {
    byId("startupOverlay").hidden = false;
    byId("appWindow").setAttribute("inert", "");
  }
  void startInitialLoad();
}
if (!updateValidationMode) {
  void loadModelSource();
  void loadLocalModel();
}
window.addEventListener("wechatvibe-service-restored", () => {
  if (updateValidationMode || !updateCommitReady) return;
  // A new bridge has no in-memory jobs, even if the earlier POST succeeded.
  // Keep all saved UI/results and let the persisted server cursor resume the job.
  portraitState.autoIncrementalState.clear();
  labelState.requestedRecentSignatures.clear();
  portraitState.incrementalFailed = portraitState.analysisNetworkFailed = labelState.recentFailed = labelState.recentNetworkFailed = false;
  settingsState.localModelResolved = false;
  beginUnknownModelSource();
  if (byId("settingsModal").classList.contains("show")) {
    void loadRuntime();
    invalidateModelDiscovery();
  }
  void loadModelSource(true);
  void loadLocalModel();
  void loadSessions();
  if (chatState.currentUser && chatState.controller) {
    // Refresh in place: a bridge recovery is not an account/conversation/source
    // change, so the visible portrait and counts must not be cleared.
    if (!chatState.historyState) void loadMessages(chatState.generation, true, true);
    if (chatState.view === "persona") void loadProfile(portraitState.activeMember);
  }
});
if (!updateValidationMode) {
  setInterval(() => { if (updateCommitReady && chatState.currentUser && !document.hidden) loadMessages(chatState.generation, true); }, 4000);
  setInterval(() => { if (updateCommitReady && !document.hidden) loadSessions(); }, 15000);
}
document.addEventListener("visibilitychange", () => {
  if (document.hidden || updateValidationMode || !updateCommitReady) return;
  void loadSessions();
  if (chatState.currentUser) void loadMessages(chatState.generation, true, true);
  if (chatState.currentUser && chatState.view === "persona") void loadProfile(portraitState.activeMember);
});
