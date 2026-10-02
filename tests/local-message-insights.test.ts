import assert from "node:assert/strict";
import { it } from "node:test";

import { ANALYSIS_QUESTIONS, FINE_DISPLAY_QUESTIONS } from "../electron/laya/options";
import { generateFineMessageInsight } from "../electron/local-message-insights";
import type { Question } from "../electron/laya/types";

function answersFor(overrides: Record<string, unknown> = {}) {
  return {
    emotion: { type: "choice" as const, probabilities: { "俏皮": 0.6, "坦诚": 0.4 } },
    intent: { type: "choice" as const, probabilities: { "ask question": 0.7, inform: 0.3 } },
    relationship: { type: "choice" as const, probabilities: { warm: 0.5, neutral: 0.5 } },
    ...overrides,
  };
}

it("returns message-only label scores and ranks them by probability", async () => {
  const calls: Array<Record<string, Question>> = [];
  const insight = await generateFineMessageInsight(
    async (questions) => { calls.push(questions); return answersFor(); }, "other", "周六见吗？");
  assert.equal(calls.length, 1);
  assert.deepEqual(insight.emotion, [
    { label: "俏皮", probability: 0.6 },
    { label: "坦诚", probability: 0.4 },
  ]);
  assert.deepEqual(insight.intent, [
    { label: "ask_question", probability: 0.7 },
    { label: "inform", probability: 0.3 },
  ]);
  assert.equal(insight.intentBroad, insight.intent, "intentBroad keeps the same general-intent scores");
  assert.deepEqual(insight.relationship, [
    { label: "warm", probability: 0.5 },
    { label: "neutral", probability: 0.5 },
  ]);
});

it("issues the same fine questions as the previous inline branch and never asks MBTI/style", async () => {
  let asked: Record<string, Question> = {};
  await generateFineMessageInsight(async (questions) => { asked = questions; return answersFor(); },
    "other", "请把文件发给我");
  assert.deepEqual(Object.keys(asked).sort(), ["emotion", "intent", "relationship"]);
  const intentCaption = asked.intent.criteria as string[];
  assert.ok(intentCaption.length >= 8, "general intent stays a bounded choice set with anchors");
  assert.ok(intentCaption.includes("一般交流"));
  assert.equal(ANALYSIS_QUESTIONS.emotion.type, "choice");
  assert.notEqual(asked.emotion, ANALYSIS_QUESTIONS.emotion,
    "fine display emotion must not reuse portrait emotion routing");
  assert.deepEqual(asked.emotion, FINE_DISPLAY_QUESTIONS.emotion);
  assert.equal(asked.relationship, ANALYSIS_QUESTIONS.relationship);
  for (const name of ["socialEnergy", "composure", "initiative"]) {
    assert.equal(name in asked, false, "fine path must not request MBTI/style questions");
  }
});

it("makes exactly one prediction call per target and leaves the injected predict in control", async () => {
  let count = 0;
  await generateFineMessageInsight(async () => { count++; return answersFor(); }, "self", "收到啦");
  assert.equal(count, 1);
});

it("returns an empty relationship distribution for SELF targets", async () => {
  const called: Array<{ intent?: unknown }> = [];
  const insight = await generateFineMessageInsight(async () => {
    called.push({});
    return answersFor({ relationship: undefined });
  }, "self", "好的");
  assert.deepEqual(insight.relationship, []);
  assert.equal(called.length, 1);
});

it("yields empty label lists when the model abstains, without a synthetic fallback", async () => {
  const insight = await generateFineMessageInsight(async () => ({}), "other", "嗯");
  assert.deepEqual(insight, { emotion: [], intent: [], intentBroad: [], relationship: [] });
});

it("keeps the fine module free of portrait, runtime, caching and DTO dependencies", async () => {
  const text = await import("node:fs").then(({ readFileSync }) =>
    readFileSync(new URL("../electron/local-message-insights.ts", import.meta.url), "utf8"));
  assert.doesNotMatch(text, /routeIntent|routeEmotion|styleEvidence|personalityEvidence/u);
  assert.doesNotMatch(text, /api-portrait|ensureModel|BoundedCache|CachedMessageAnalysis/u);
  assert.match(text, /from "\.\/laya\/options"/u);
  assert.match(text, /from "\.\/laya\/general-intent"/u);
});
