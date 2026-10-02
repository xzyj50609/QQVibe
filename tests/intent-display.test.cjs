const assert = require("node:assert/strict");
const { readFileSync } = require("node:fs");
const path = require("node:path");
const { it } = require("node:test");
const vm = require("node:vm");
const { installViewState } = require("./helpers/view-state-harness.cjs");

// Load the pure decision and rendering functions from the desktop's classic script.
const source = readFileSync(path.join(__dirname, "../chatui/app.js"), "utf8");
const labelsSource = readFileSync(path.join(__dirname, "../chatui/message-labels.js"), "utf8");
function section(start, end) {
  const first = source.indexOf(start);
  const last = source.indexOf(end, first);
  assert.ok(first >= 0 && last > first, `Missing UI section: ${start}`);
  return source.slice(first, last);
}
function element(tag, className, textContent = "") {
  return {
    tag, className, textContent, children: [],
    appendChild(child) { this.children.push(child); },
  };
}
const context = vm.createContext({
  element,
  percent: value => `${value * 100}%`,
  window: { Kaomoji: { pick: () => null } },
});
installViewState(context);
vm.runInContext(labelsSource, context);
vm.runInContext(section("const GENERIC_INTENT_LABELS", "settingsState.settings = undefined;") +
  section("function hasIntentContent(", "function clearInlineIntentPending(") +
  "globalThis.displayedIntentForTest = displayedIntent;" +
  "globalThis.appendScoreLineForTest = appendScoreLine;" +
  "globalThis.appendIntentLineForTest = appendIntentLine;", context);
const select = context.displayedIntentForTest;
const plain = candidates => Array.from(candidates, candidate => ({ ...candidate }));

it("shows evidence-backed intent plus distinct scored model alternatives", () => {
  const result = select({
    groundedIntent: { label: "status_report", evidenceKind: "progress_statement" },
    intent: [{ rawLabel: "complain", probability: 0.49 },
      { rawLabel: "status_report", probability: 0.31 },
      { rawLabel: "inform", probability: 0.2 }],
  }, "文件已上传到共享盘。");
  assert.deepEqual(plain(result), []);
});

it("shows grounded inspection and explanation but blanks a plain acknowledgement", () => {
  for (const [text, label, evidenceKind, expected] of [
    ["好了", "confirm", "short_acknowledgement", []],
    ["我看看", "inspect", "first_person_inspection", [{ label: "查看", probability: null }]],
    ["因为线路故障，所以暂时断开。", "explain", "process_explanation", [{ label: "解释", probability: null }]],
  ]) {
    const result = select({ groundedIntent: { label, evidenceKind }, intent: [] }, text);
    assert.deepEqual(plain(result), expected);
  }
});

it("leaves plain acknowledgements and status reports blank unless the text turns or asks", () => {
  const ack = { groundedIntent: null, intent: [
    { rawLabel: "confirm", probability: 0.7 },
    { rawLabel: "agree", probability: 0.2 },
    { rawLabel: "general_exchange", probability: 0.1 },
  ] };
  for (const text of ["嗯", "好的", "收到", "可以", "完成了", "搞定啦"]) {
    assert.deepEqual(plain(select(ack, text)), [], `${text} should stay blank`);
  }
  for (const text of ["好的，但是我想改天", "可以吗？", "收到，帮我看看", "完成了，你 check 一下"]) {
    assert.ok(select(ack, text).length > 0, `${text} should keep its candidates`);
  }
  const status = { groundedIntent: null, intent: [
    { rawLabel: "status_report", probability: 0.6 },
    { rawLabel: "general_exchange", probability: 0.4 },
  ] };
  assert.deepEqual(plain(select(status, "已经完成了")), []);
  const action = { groundedIntent: null, intent: [
    { rawLabel: "status_report", probability: 0.6 },
    { rawLabel: "suggest_action", probability: 0.4 },
  ] };
  assert.ok(select(action, "已经完成了，请你确认").length > 0,
    "a status report with an extra action keeps its candidates");
});

it("suppresses generic candidates for an ordinary factual sentence", () => {
  const result = select({ groundedIntent: null, intent: [
    { rawLabel: "inform", probability: 0.24 },
    { rawLabel: "general_exchange", probability: 0.41 },
    { rawLabel: "small_talk", probability: 0.35 },
    { rawLabel: "share_news", probability: 0 },
  ] }, "今天天气不错。");
  assert.deepEqual(plain(result), []);
  assert.ok(select({ groundedIntent: null, intent: [
    { rawLabel: "share_news", probability: 0.61 },
    { rawLabel: "inform", probability: 0.27 },
  ] }, "请把通知发给我").length > 0);
});

it("renders nuanced generic-v9 labels using their original model probabilities", () => {
  const result = select({ groundedIntent: null, intent: [
    { rawLabel: "confide", probability: 0.72 },
    { rawLabel: "seek_comfort", probability: 0.2 },
    { rawLabel: "share_feeling", probability: 0.08 },
  ] }, "我今天有点难过，想跟你说说。");
  assert.deepEqual(plain(result), [{ label: "倾诉", probability: 0.72 }]);
});

it("allows punctuation and quote-only text to carry an intent", () => {
  const result = {
    groundedIntent: { label: "greet", evidenceKind: "greeting_phrase" },
    intent: [{ rawLabel: "greet", probability: 0.99 }],
  };
  for (const text of ['"', "“”", "！？…", "  '  "]) {
    const candidates = select(result, text);
    assert.ok(plain(candidates).length > 0, `${text} should remain analyzable`);
    const row = element("div", "inline-intent-row");
    context.appendScoreLineForTest(row, "情绪", [{ item: { label: "平静", probability: 0.7 } }], "synthetic", true);
    context.appendIntentLineForTest(row, candidates);
    assert.equal(row.children.length, 2);
    assert.match(row.children[0].className, /emotion-line/);
    assert.match(row.children[1].className, /intent-line/);
  }
});

it("does not present a forced intent for unfinished or ordinary fragments", () => {
  const result = { groundedIntent: null, intent: [
    { rawLabel: "status_report", probability: 0.64 },
    { rawLabel: "inform", probability: 0.36 },
  ] };
  assert.deepEqual(plain(select(result, "这就是")), []);
  assert.deepEqual(plain(select(result, "这是昨天说的文件")), []);
});

it("renders model labels without percentages or grounded metadata", () => {
  const modelRow = element("div", "inline-intent-row");
  context.appendIntentLineForTest(modelRow, select({ groundedIntent: null, intent: [
    { rawLabel: "share_news", probability: 0.61 },
    { rawLabel: "inform", probability: 0.27 },
    { rawLabel: "small_talk", probability: 0.12 },
  ] }, "请把今天收到的通知发给我。"));
  assert.equal(modelRow.children.length, 1);
  assert.equal(modelRow.children[0].children.length, 2); // title and the primary candidate
  assert.deepEqual(modelRow.children[0].children.slice(1).map(item => item.children[0].textContent),
    ["分享"]);
  assert.doesNotMatch(modelRow.children.map(item => item.textContent).join(" "), /%/u);

  const groundedRow = element("div", "inline-intent-row");
  context.appendIntentLineForTest(groundedRow, select({
    groundedIntent: { label: "thank", evidenceKind: "thanks_phrase" }, intent: [],
  }, "谢谢你。"));
  assert.equal(groundedRow.children[0].children.length, 2);
  assert.equal(groundedRow.children[0].children[1].children.length, 1);
  assert.doesNotMatch(groundedRow.children[0].children[1].className, /grounded/);
});

it("ignores malformed model candidates without inventing a fallback score", () => {
  assert.deepEqual(plain(select({ groundedIntent: null, intent: [
    { rawLabel: "inform", probability: "0.99" },
    { rawLabel: "unknown", probability: 0.8 },
    { label: "分享", probability: 0.7 },
  ] }, "文件在共享盘。")), []);
});
