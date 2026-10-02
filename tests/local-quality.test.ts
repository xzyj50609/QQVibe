// Offline S3 local-quality contract: bounded context hints only move candidates into the
// general-intent question, invitations + deferrals can surface a decline, concrete
// reschedules keep plan/invite, hesitation is not a decline, and quoted text is never
// attributed to the current speaker. No model is loaded.
import assert from "node:assert/strict";
import { it } from "node:test";

import { generalIntentQuestion } from "../electron/laya/general-intent";
import { generateFineMessageInsight } from "../electron/local-message-insights";
import type { Answer, Question } from "../electron/laya/types";

function labels(text: string, hint = ""): string[] {
  const criteria = generalIntentQuestion(text, hint).question.criteria;
  if (!Array.isArray(criteria)) throw new Error("Expected choice candidates");
  return criteria.map(String);
}
function choice(probabilities: Record<string, number>): Answer {
  return {
    type: "choice", confidence: 1, action: { act_probability: 1 },
    choice: Object.entries(probabilities).sort((a, b) => b[1] - a[1])[0]![0],
    probabilities,
  };
}

it("the same deferral changes its candidate menu with invitation context", () => {
  const without = labels("改天吧");
  const withInvite = labels("改天吧", "周六一起去看展吗？");
  assert.ok(!without.includes("拒绝"), "no context must keep the old candidates");
  assert.ok(withInvite.includes("拒绝"), "an invitation + deferral may surface a decline");
  assert.notDeepEqual(withInvite, without);
});

it("surfaces a decline without forcing it and keeps the question bounded", () => {
  for (const text of ["改天吧", "下次吧", "以后再说", "过两天再说"]) {
    const criteria = labels(text, "要不要一起吃饭？");
    assert.ok(criteria.includes("拒绝"), `${text}: expected 拒绝 candidate`);
    assert.ok(criteria.length >= 8 && criteria.length <= 10, `${text}: bounded menu`);
    assert.equal(new Set(criteria).size, criteria.length, `${text}: unique candidates`);
    assert.equal(criteria.at(-1), "一般交流", text);
  }
});

it("keeps plan and invite for a concrete reschedule and never adds a decline", () => {
  const criteria = labels("今天不行，周六我请你", "今天有空吗？");
  assert.ok(criteria.includes("计划"), "a concrete reschedule stays a plan");
  assert.ok(criteria.includes("邀约"), "a concrete reschedule keeps the invitation");
  assert.ok(!criteria.includes("拒绝"), "a specific later time is not a refusal");
});

it("does not turn hesitation into a decline", () => {
  const criteria = labels("我再纠结纠结", "周六一起去看展吗？");
  assert.ok(!criteria.includes("拒绝"), "hesitation is not a refusal");
  assert.ok(criteria.includes("一般交流"));
});

it("quoted deferrals and irrelevant hints never add a decline", () => {
  assert.ok(!labels("她说“改天吧”", "周六一起去看展吗？").includes("拒绝"),
    "quoted text must not be attributed to the current speaker");
  assert.ok(!labels("改天吧", "今天天气不错").includes("拒绝"),
    "an unrelated hint must not invent a decline");
  assert.ok(!labels("改天吧", "").includes("拒绝"), "an empty hint keeps the old candidates");
});

it("keeps playful denial out of the decline/question candidates", () => {
  const criteria = labels("木有", "你刚才不是说喜欢我吗？");
  assert.ok(!criteria.includes("拒绝"));
  assert.ok(!criteria.includes("提问"));
  assert.ok(criteria.includes("一般交流"));
});

it("bounds the context hint so it cannot grow the option set or leak", () => {
  const huge = "周六一起去看展吗？".repeat(200);
  const criteria = labels("改天吧", huge);
  assert.ok(criteria.includes("拒绝"));
  assert.ok(criteria.length >= 8 && criteria.length <= 10);
  assert.equal(criteria.at(-1), "一般交流");
});

it("passes the hint into the fine candidate menu with one predict call", async () => {
  const calls: Record<string, Question>[] = [];
  let criteria: string[] = [];
  const insight = await generateFineMessageInsight(async (questions) => {
    calls.push(questions);
    criteria = questions.intent!.criteria as string[];
    return {
      emotion: choice({ "坦诚自然": 1 }),
      intent: choice({ "拒绝": 0.62, "一般交流": 0.38 }),
      relationship: choice({ neutral: 1 }),
    };
  }, "other", "改天吧", "周六一起去看展吗？");
  assert.equal(calls.length, 1, "fine stays a single predict call");
  assert.ok(criteria.includes("拒绝"), "the hint reaches the question options");
  assert.deepEqual(insight.intent[0], { label: "reject", probability: 0.62 });
  assert.deepEqual(insight.intentBroad, insight.intent);
});

it("keeps the fine path unchanged when no bounded hint is available", async () => {
  let criteria: string[] = [];
  await generateFineMessageInsight(async (questions) => {
    criteria = questions.intent!.criteria as string[];
    return { emotion: choice({ "坦诚自然": 1 }), intent: choice({ "一般交流": 1 }),
      relationship: choice({ neutral: 1 }) };
  }, "other", "改天吧");
  assert.ok(!criteria.includes("拒绝"), "no hint means the old candidate menu");
});
