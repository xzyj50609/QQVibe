import assert from "node:assert/strict";
import { it } from "node:test";

import { __setAnalysisEngineForTest, analyzeMessageTargets, analyzeObservedText, type AnalysisEngine } from "../electron/analysis";
import type { Answer, PredictResult, Question, State } from "../electron/laya/types";
import { GROUNDED_INTENT_LABELS, intentLabel } from "../src/lib/labels";
import { GENERAL_LABEL_SCHEMA, INTENTS, generalIntentQuestion } from "../electron/laya/general-intent";

function choice(probabilities: Record<string, number>): Answer {
  return {
    type: "choice",
    confidence: 1,
    action: { act_probability: 1 },
    choice: Object.entries(probabilities).sort((a, b) => b[1] - a[1])[0]![0],
    probabilities,
  };
}

it("fine labels use direct broad emotion and generic intent model probabilities", async () => {
  const calls: string[][] = [];
  const engine: AnalysisEngine = {
    async predict(_state: State, questions: Record<string, Question>): Promise<PredictResult> {
      calls.push(Object.keys(questions));
      assert.deepEqual(Object.keys(questions), ["emotion", "intent"]);
      return {
        model: "synthetic",
        usage: { input_tokens: 0, output_tokens: 0 },
        answers: {
          emotion: choice({ "亲昵俏皮": 0, "犹豫纠结": 0, "坦诚自然": 0.21,
            "客套防守": 0, "无奈低能": 0, "敏感上头": 0.79 }),
          intent: choice({ "分享": 0.61, "提问": 0.39 }),
        },
      };
    },
  };
  __setAnalysisEngineForTest(engine);
  try {
    const result = await analyzeObservedText("文件已上传到共享盘。");
    assert.equal(calls.length, 1, "expression/social and leaf routing must not run");
    assert.deepEqual(result.emotion[0], { label: "敏感上头", probability: 0.79 });
    assert.deepEqual(result.intent[0], { label: "share_news", probability: 0.61 });
    assert.deepEqual(result.intentBroad, result.intent);
    assert.deepEqual(result.playfulIntent, []);
    assert.deepEqual(result.expression, []);
  } finally {
    __setAnalysisEngineForTest(null);
  }
});

it("an uncertain focused result keeps the model's generic answer without a forced specific label", async () => {
  let calls = 0;
  const engine: AnalysisEngine = {
    async predict(_state: State, questions: Record<string, Question>): Promise<PredictResult> {
      calls++;
      return {
        model: "synthetic",
        usage: { input_tokens: 0, output_tokens: 0 },
        answers: {
          emotion: choice({ "坦诚自然": 1 }),
          intent: choice({ "建议或指令": 0.2, "求助": 0.1, "计划": 0, "一般交流": 0.7 }),
        },
      };
    },
  };
  __setAnalysisEngineForTest(engine);
  try {
    const result = await analyzeObservedText("建议先备份配置文件。");
    assert.equal(calls, 1);
    assert.deepEqual(result.intent, [
      { label: "general_exchange", probability: 0.7 },
      { label: "suggest_action", probability: 0.2 },
      { label: "seek_help", probability: 0.1 },
      { label: "plan", probability: 0 },
    ]);
  } finally {
    __setAnalysisEngineForTest(null);
  }
});

it("fine targets carry evidence only where target text supports it, without changing model scores", async () => {
  const engine: AnalysisEngine = {
    async predict(_state: State, questions: Record<string, Question>): Promise<PredictResult> {
      assert.deepEqual(Object.keys(questions), ["emotion", "intent", "relationship"]);
      return {
        model: "synthetic",
        usage: { input_tokens: 0, output_tokens: 0 },
        answers: {
          emotion: choice({ "坦诚自然": 0.8, "敏感上头": 0.2 }),
          intent: choice({ "状态报告": 0.64, "一般交流": 0.36 }),
          relationship: choice({ neutral: 1 }),
        },
      };
    },
  };
  __setAnalysisEngineForTest(engine);
  try {
    const result = await analyzeMessageTargets({
      sessionId: "grounded-fine-test",
      messages: [
        { id: "1", side: "other", text: "文件已上传到共享盘。", time: 1 },
        { id: "2", side: "other", text: "喊老板", time: 2 },
        { id: "3", side: "other", text: "好了", time: 3 },
        { id: "4", side: "other", text: "我看看", time: 4 },
        { id: "5", side: "other", text: "我这算是一步一步引导CODX帮我破解了", time: 5 },
      ],
      targetIds: ["1", "2", "3", "4", "5"], messageLabelsOnly: true,
    });
    assert.deepEqual(result.messages.map(message => message.groundedIntent), [
      { label: "status_report", evidenceKind: "progress_statement" }, null,
      { label: "confirm", evidenceKind: "short_acknowledgement" },
      { label: "inspect", evidenceKind: "first_person_inspection" },
      { label: "explain", evidenceKind: "process_explanation" },
    ]);
    assert.deepEqual(result.messages[0]!.intent[0], { label: "status_report", probability: 0.64 });
    assert.deepEqual(result.messages[0]!.emotion[0], { label: "坦诚自然", probability: 0.8 });
  } finally {
    __setAnalysisEngineForTest(null);
  }
});

it("every grounded label has a generic Chinese display label", () => {
  for (const [label, display] of Object.entries(GROUNDED_INTENT_LABELS)) {
    assert.equal(intentLabel(label), display);
  }
  assert.equal(Object.prototype.hasOwnProperty.call(GROUNDED_INTENT_LABELS, "喊老板"), false);
});

it("generic-v9 offers bounded, readable candidate menus instead of the same four defaults", () => {
  assert.equal(GENERAL_LABEL_SCHEMA, "generic-v9");
  assert.ok(INTENTS.length >= 35 && INTENTS.length <= 50);
  const cases: Array<[string, string[]]> = [
    ["截图", ["展示内容"]],
    ["我今天有点难过，想跟你说说", ["倾诉"]],
    ["能陪我聊一会儿吗", ["求陪伴", "求安慰"]],
    ["你是不是喜欢我？", ["探询心意"]],
    ["辛苦了，早点休息", ["关心"]],
    ["我们好好说，别再吵了", ["缓和关系", "设定边界"]],
    ["我来帮你处理", ["提供帮助"]],
    ["别太难过，我会陪你一起慢慢处理。", ["安慰"]],
    ["你到家了吗？今天看着挺累的，早点休息。", ["关心"]],
    ["周六要不要一起去看展？", ["邀约", "提问"]],
    ["这家店几点关门？", ["提问"]],
    ["老板喊妈妈", []],
    ["她说“我喜欢你”", []],
  ];
  for (const [text, expected] of cases) {
    const criteria = generalIntentQuestion(text).question.criteria;
    if (!Array.isArray(criteria)) throw new Error(`Expected choice candidates: ${text}`);
    assert.ok(criteria.length >= 8 && criteria.length <= 10, text);
    assert.equal(new Set(criteria).size, criteria.length, text);
    assert.equal(criteria.at(-1), "一般交流", text);
    for (const label of expected) assert.ok(criteria.includes(label), `${text}: ${label}`);
    assert.ok(!criteria.some(label => /喊老板|喊妈妈/.test(String(label))), text);
  }
  const quoted = generalIntentQuestion("她说“我喜欢你”").question.criteria;
  if (!Array.isArray(quoted)) throw new Error("Expected quoted choice candidates");
  assert.ok(!quoted.includes("表达好感"));
  const shared = generalIntentQuestion("我今天拿到了新工作，想第一时间告诉你。").question.criteria;
  if (!Array.isArray(shared)) throw new Error("Expected statement candidates");
  assert.ok(!shared.includes("提问"));
  const comfort = generalIntentQuestion("别太难过，我会陪你一起慢慢处理。").question.criteria;
  if (!Array.isArray(comfort)) throw new Error("Expected comfort candidates");
  assert.ok(!comfort.includes("倾诉") && !comfort.includes("邀约") && !comfort.includes("关心"));
  const care = generalIntentQuestion("你到家了吗？今天看着挺累的，早点休息。").question.criteria;
  if (!Array.isArray(care)) throw new Error("Expected care candidates");
  assert.ok(care.includes("关心") && !care.includes("提问"));
  const confiding = generalIntentQuestion("我最近工作压力很大，心里有点难受，想跟你说说。").question.criteria;
  if (!Array.isArray(confiding)) throw new Error("Expected confiding candidates");
  assert.ok(confiding.includes("倾诉") && !confiding.includes("表达感受"));
  const offering = generalIntentQuestion("我来帮你处理这个问题。").question.criteria;
  if (!Array.isArray(offering)) throw new Error("Expected offer candidates");
  assert.ok(offering.includes("提供帮助") && !offering.includes("求助"));
});

it("all curated generic labels have a wire display mapping", () => {
  for (const intent of INTENTS) assert.equal(intentLabel(intent.id), intent.zh);
});

it("common colloquial acts enter the candidate menu without forcing a plan or boundary", () => {
  const labels = (text: string): string[] => {
    const criteria = generalIntentQuestion(text).question.criteria;
    if (!Array.isArray(criteria)) throw new Error("Expected choice candidates");
    return criteria.map(String);
  };
  assert.ok(labels("好的，谢谢你了").includes("感谢"));
  assert.ok(labels("谢谢大家的帮助").includes("感谢"));
  assert.ok(labels("辛苦大家了").includes("感谢"));
  assert.ok(labels("他家几点开门啊").includes("提问"));
  assert.ok(labels("明天天气咋样").includes("提问"));
  assert.ok(labels("别生气，慢慢说").includes("安慰"));
  assert.ok(labels("你太厉害了").includes("称赞"));
  assert.ok(labels("下次见").includes("告别"));
  assert.ok(labels("这几家都不错，可以任选一家").includes("建议或指令"));
  assert.ok(!labels("我们开始吧").includes("计划"));
  assert.ok(labels("我后天去医院").includes("计划"));
  assert.ok(!labels("我后天去医院").includes("邀约"));
  assert.ok(labels("我周末去看电影").includes("计划"));
  assert.ok(labels("我明早去上班").includes("计划"));
  assert.ok(!labels("你的旅程到此为止了").includes("设定边界"));
  assert.ok(!labels("这个颜色不合适，我换一件").includes("保持距离"));
  assert.ok(labels("你们几个人").includes("提问"));
  assert.ok(labels("他家几楼").includes("提问"));
  assert.ok(!labels("没什么").includes("提问"));
  const rhetorical = labels("我这不把文件发过来了吗？");
  assert.ok(rhetorical.includes("纠正或异议"));
  assert.ok(rhetorical.includes("澄清"));
  assert.ok(!rhetorical.includes("提问"));
  assert.ok(labels("我的文件发过来了吗？").includes("提问"));
  assert.ok(labels("周六电影，还是算了。").includes("拒绝"));
  assert.ok(!labels("周六电影，还是照常去吧。").includes("拒绝"));
  assert.ok(!labels("他说还是算了。").includes("拒绝"));
});
