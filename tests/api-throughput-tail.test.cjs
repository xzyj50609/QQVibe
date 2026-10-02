const assert = require("node:assert/strict");
const { readFileSync } = require("node:fs");
const path = require("node:path");
const { it } = require("node:test");
const vm = require("node:vm");

// Offline tail-latency contract: the UI polls an API job within a bounded band and
// backs off while it stays queued/running, and the completed status reports the
// backend's real wall-clock total (never a poll count). The backend timing segments
// themselves are covered by bridge/test_api_insights.py.
const source = readFileSync(path.join(__dirname, "../chatui/app.js"), "utf8");

function section(start, end) {
  const first = source.indexOf(start);
  const last = source.indexOf(end, first);
  assert.ok(first >= 0 && last > first, `Missing UI section: ${start}`);
  return source.slice(first, last);
}

function pollHarness() {
  const timers = [];
  const context = vm.createContext({
    setTimeout: (fn, ms) => { timers.push({ fn, ms }); return timers.length; },
    clearTimeout: () => {},
    apiInsightWorkCurrent: () => true,
  });
  vm.runInContext(section("const API_INSIGHT_POLL_BASE_MS", "function scheduleApiInsightPoll(") +
    section("function scheduleApiInsightPoll(", "async function fetchApiInsightResults(") +
    "globalThis.poll = { API_INSIGHT_POLL_BASE_MS, API_INSIGHT_POLL_MAX_MS, apiInsightPollDelay, scheduleApiInsightPoll };",
  context);
  return { poll: context.poll, timers, context };
}

it("keeps the API job poll interval in the 250-500 ms band and backs off while running", () => {
  const { poll, timers } = pollHarness();
  assert.ok(poll.API_INSIGHT_POLL_BASE_MS >= 250 && poll.API_INSIGHT_POLL_BASE_MS <= 500,
    `base poll ${poll.API_INSIGHT_POLL_BASE_MS} ms must stay in the bounded band`);
  const delays = [1, 2, 3, 4, 5, 6].map(attempt => poll.apiInsightPollDelay(attempt));
  assert.equal(delays[0], poll.API_INSIGHT_POLL_BASE_MS);
  for (let index = 1; index < delays.length; index++) {
    assert.ok(delays[index] >= delays[index - 1], "backoff must not shrink");
  }
  assert.ok(delays.every(delay => delay <= poll.API_INSIGHT_POLL_MAX_MS));
  assert.ok(delays.at(-1) > delays[0], "a slow job must back off, not hammer at the base rate");

  const work = { timer: null, pollAttempt: 0 };
  poll.scheduleApiInsightPoll(work);
  assert.equal(timers.length, 1);
  assert.ok(timers[0].ms >= 250 && timers[0].ms <= 500);
  assert.equal(work.pollAttempt, 1);
  poll.scheduleApiInsightPoll(work);
  assert.equal(timers.length, 2);
  assert.ok(timers[1].ms >= timers[0].ms);
  assert.equal(work.pollAttempt, 2);
});

it("does not schedule a poll once the work is no longer current", () => {
  const { poll, timers, context } = pollHarness();
  const work = { timer: 7, pollAttempt: 0 };
  context.apiInsightWorkCurrent = () => false;
  poll.scheduleApiInsightPoll(work);
  assert.equal(timers.length, 0);
});

it("removed the old fixed 1500 ms tail interval from the scheduling path", () => {
  const schedule = section("function scheduleApiInsightPoll(", "async function fetchApiInsightResults(");
  assert.doesNotMatch(schedule, /1500/u);
  assert.match(schedule, /apiInsightPollDelay/u);
});

function statusHarness(entry) {
  const nodes = new Map();
  const byId = id => {
    if (!nodes.has(id)) nodes.set(id, { textContent: "", hidden: true });
    return nodes.get(id);
  };
  const context = vm.createContext({
    byId,
    settingsState: { modelSourceResolved: true, modelSourceSnapshot: { mode: "api" },
      settings: { intent: true }, localModelResolved: false, localModelReady: false },
    chatState: { currentUser: "chat" },
    labelState: { apiInsightStatusRendered: false, apiInsightWork: null },
    portraitState: { incrementalFailed: false },
    usingLocalFine: () => false,
    activeApiInsightEntry: () => entry,
    setIntentActionState: () => {},
  });
  vm.runInContext(section("function renderApiInsightStatus(", "function renderApiInsightResult("), context);
  context.renderApiInsightStatus();
  return byId("analysisStatus");
}

it("shows the backend wall-clock total on a completed job, not a poll count", () => {
  const node = statusHarness({ error: "",
    job: { status: "done", total: 2, processed: 2, startedAtMs: Date.now(),
      timings: { prepareMs: 3, queueMs: 1, providerMs: 900, validateMs: 1, saveMs: 2, totalMs: 1234 } } });
  assert.equal(node.textContent, "完成 · 1.2 秒 · 2 条");
  const legacy = statusHarness({ error: "", job: { status: "done", total: 2, processed: 2 } });
  assert.equal(legacy.textContent, "", "a job without timing data must not invent a duration");
});

it("still shows elapsed time and retry state while a job is running", () => {
  const running = statusHarness({ error: "",
    job: { status: "running", total: 2, processed: 1, startedAtMs: Date.now() - 2500 } });
  assert.match(running.textContent, /分析中 1\/2 · \d+ 秒/u);
  const retry = statusHarness({ error: "",
    job: { status: "running", total: 2, processed: 0, startedAtMs: Date.now(),
      retry: { reason: "timeout", nextAtMs: Date.now() + 4000, attempt: 1, max: 5 } } });
  assert.match(retry.textContent, /自动重试 1\/5/u);
});
