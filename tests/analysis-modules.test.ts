import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import path from "node:path";
import { it } from "node:test";
import { fileURLToPath } from "node:url";

import * as compat from "../electron/api-insights";
import * as messages from "../electron/api-message-insights";
import * as portrait from "../electron/api-portrait";
import type { GenerationRequest, ModelConfig } from "../electron/model-connectors";

const ROOT = path.dirname(path.dirname(fileURLToPath(import.meta.url)));
const config: ModelConfig = {
  protocol: "responses", baseUrl: "https://example.invalid/v1",
  model: "synthetic-model", apiKey: "synthetic-key",
};

it("re-exports the exact business functions from the compatibility facade", () => {
  assert.equal(compat.analyzeApiInsights, messages.analyzeApiInsights);
  assert.equal(compat.updateApiPortrait, portrait.updateApiPortrait);
  assert.equal(compat.refreshApiPortraitAxes, portrait.refreshApiPortraitAxes);
});

it("sends an unchanged bounded per-message request through the facade and business module", async () => {
  const input = {
    messages: [
      { id: "m1", sender: "OTHER" as const, text: "周六见吗？" },
      { id: "m2", sender: "OTHER" as const, text: "再纠结纠结。" },
    ],
    targetIds: ["m1", "m2"],
  };
  const requests: GenerationRequest[] = [];
  const generate = async (_config: ModelConfig, request: GenerationRequest) => {
    requests.push(request);
    return { text: JSON.stringify({ items: [
      { id: "m1", status: "ok", affect: { feeling: "期待" }, intents: ["邀约"] },
      { id: "m2", status: "ok", affect: { feeling: "犹豫" }, intents: ["暂缓决定"] },
    ] }), usage: { inputTokens: 3, outputTokens: 4 } };
  };
  const viaFacade = await compat.analyzeApiInsights(config, input, generate);
  const viaBusiness = await messages.analyzeApiInsights(config, input, generate);
  assert.deepEqual(viaFacade, viaBusiness);
  assert.deepEqual(viaFacade.insights, [
    { id: "m1", status: "ok", affect: { feeling: "期待" }, intents: ["邀约"] },
    { id: "m2", status: "ok", affect: { feeling: "犹豫" }, intents: ["暂缓决定"] },
  ]);
  assert.deepEqual(viaFacade.usage, { inputTokens: 3, outputTokens: 4 });
  for (const request of requests) {
    assert.deepEqual(Object.keys(request).sort(), ["jsonMode", "prompt", "stream", "system"]);
    assert.equal(request.jsonMode, false);
    assert.equal(request.stream, true);
    assert.equal(request.maxOutputTokens, undefined);
    assert.match(request.prompt, /^CHAT_BATCH_JSON:\n/u);
  }
});

it("keeps the portrait request parameter set unchanged through the facade", async () => {
  const previous = {
    summary: "常讨论日程", communication: "表达简洁", emotionExpression: "",
    interactionPreferences: "", topics: [], patterns: [], boundaries: [], uncertain: [],
    affinity: null, mbtiAxes: { EI: null, SN: null, TF: null, JP: null },
    traits: { socialEnergy: null, humor: null, composure: null,
      initiative: null, care: null, affection: null },
  };
  const messagesInput = [{ id: "m1", sender: "OTHER" as const, target: true, text: "周六见。" }];
  const requests: GenerationRequest[] = [];
  const generate = async (_config: ModelConfig, request: GenerationRequest) => {
    requests.push(request);
    return { text: JSON.stringify({ ...previous, summary: "常讨论见面时间" }) };
  };
  const result = await compat.updateApiPortrait(config, previous, messagesInput, generate);
  assert.equal(result.portrait.summary, "常讨论见面时间");
  await portrait.updateApiPortrait(config, previous, messagesInput, generate);
  for (const request of requests) {
    assert.deepEqual(Object.keys(request).sort(),
      ["jsonMode", "maxOutputTokens", "prompt", "system", "timeoutMs"]);
    assert.equal(request.jsonMode, true);
    assert.equal(request.maxOutputTokens, 8192);
    assert.equal(request.timeoutMs, 30_000);
  }
});

it("keeps the business modules free of facade imports", () => {
  for (const name of ["api-message-insights.ts", "api-portrait.ts", "api-analysis-json.ts"]) {
    const text = readFileSync(path.join(ROOT, "electron", name), "utf8");
    assert.doesNotMatch(text, /from "\.\/api-insights"/u, `${name} must not import the facade`);
  }
  const facade = readFileSync(path.join(ROOT, "electron/api-insights.ts"), "utf8");
  assert.match(facade, /from "\.\/api-message-insights"/u);
  assert.match(facade, /from "\.\/api-portrait"/u);
});

it("lists the new TypeScript and UI modules in the public staging allowlist", () => {
  const stage = readFileSync(path.join(ROOT, "scripts/stage-real-client.py"), "utf8");
  for (const name of ["chatui/message-labels.js", "electron/api-message-insights.ts",
    "electron/api-portrait.ts", "electron/api-analysis-json.ts"]) {
    assert.ok(stage.includes(`"${name}"`), `staging allowlist is missing ${name}`);
  }
  assert.ok(stage.includes('"message_results.py"'), "staging allowlist is missing message_results.py");
});
