import assert from "node:assert/strict";
import { it } from "node:test";
import { execFileSync } from "node:child_process";

import { analyzeApiInsights, type ApiInsightInput } from "../electron/api-message-insights";
import type { GenerationRequest, ModelConfig } from "../electron/model-connectors";
import {
  buildUnifiedInput, normalizeInputMeta, normalizeQuote, projectLegacyWire,
  type MessageInputMeta,
} from "../shared/message-input";

const config: ModelConfig = {
  protocol: "responses", baseUrl: "https://example.invalid/v1",
  model: "synthetic-model", apiKey: "synthetic-key",
};

function meta(overrides: Partial<MessageInputMeta> = {}): MessageInputMeta {
  return {
    accountId: "acct", conversationId: "friend",
    senderId: "member-a", senderName: "阿甲", sentAtMs: 1700000000000,
    source: { kind: "wechat" }, quote: null,
    ...overrides,
  };
}

it("keeps SELF identity and missing senders null without guessing", () => {
  const self = buildUnifiedInput({
    id: "s1", side: "self", text: "收到",
    inputMeta: meta({ senderId: "me", senderName: "我自己" }),
  });
  assert.equal(self.senderId, "me");
  assert.equal(self.senderName, "我自己");
  const unknown = buildUnifiedInput({ id: "o1", side: "other", text: "嗯" });
  assert.equal(unknown.senderId, null);
  assert.equal(unknown.senderName, null);
  assert.equal(unknown.source.kind, "unknown");
  assert.equal(unknown.quote, null);
});

it("keeps two OTHER group members distinct through a JSON round trip", () => {
  // Exercise the actual Python normalizer and NodeAnalysis projection before JSONL/TS.
  const fixture = `import sys,json
sys.path.insert(0,'bridge')
from message_input import prepare_messages
from node_analysis import NodeAnalysis
rows=[{'id':'a','side':'other','text':'甲说','senderId':'member-a','senderName':'阿甲','time':1.5},
      {'id':'b','side':'other','text':'乙说','senderId':'member-b','senderName':'阿乙','time':2}]
prepared=prepare_messages(rows,account_id='acct',conversation_id='group@chatroom',source_kind='wechat')
print(json.dumps(NodeAnalysis._wire_messages(prepared),ensure_ascii=True))`;
  const wire = JSON.parse(execFileSync(process.env.WECHATVIBE_PYTHON || "python", ["-B", "-c", fixture],
    { cwd: new URL("../", import.meta.url), encoding: "utf8" })) as Record<string, unknown>[];
  const restored = wire.map((entry) => buildUnifiedInput(entry,
    { accountId: "acct", conversationId: "group@chatroom" }));
  assert.deepEqual(restored.map((entry) => entry.senderId), ["member-a", "member-b"]);
  assert.deepEqual(restored.map((entry) => entry.senderName), ["阿甲", "阿乙"]);
  assert.deepEqual(restored.map((entry) => entry.id), ["a", "b"]);
  assert.deepEqual(restored.map((entry) => entry.sentAtMs), [1.5, 2]);
});

it("keeps quote newlines/tabs/emoji and accepts draft aliases", () => {
  const text = "第一行\n\t第二行 😀🙂";
  const quote = normalizeQuote({ id: "q1", senderId: "member-b", senderName: "阿乙",
    text, sentAtMs: 0 });
  assert.equal(quote?.text, text);
  const draft = normalizeQuote({ id: "q1", author: "阿乙", text: "旧话", time: 5 });
  assert.equal(draft?.senderName, "阿乙");
  assert.equal(draft?.sentAtMs, 5);
  assert.equal(draft?.senderId, null);
  assert.equal(normalizeQuote({ text: "", id: null }), null);
  assert.equal(normalizeQuote({ unexpected: "x" }), null);
  assert.equal(normalizeQuote({ senderName: "阿甲\n小号" })?.senderName, "阿甲\n小号");
});

it("preserves numeric time without unit conversion and rejects invalid values", () => {
  const fractional = buildUnifiedInput({
    id: "m1", side: "other", text: "x", inputMeta: meta({ sentAtMs: 1.5 }),
  });
  assert.equal(fractional.sentAtMs, 1.5);
  assert.equal(buildUnifiedInput({ id: "m2", side: "other", text: "x", time: 0 }).sentAtMs, 0);
  for (const bad of [true, false, -1, -1.5, Number.NaN, Number.POSITIVE_INFINITY, "1700"]) {
    assert.throws(() => buildUnifiedInput({ id: "m3", side: "other", text: "x", time: bad as never }));
  }
  assert.throws(() => normalizeInputMeta({ sentAtMs: true }));
});

it("merges trusted scope and rejects a foreign envelope instead of overwriting", () => {
  const filled = buildUnifiedInput({ id: "m1", side: "other", text: "x",
    inputMeta: { quote: { text: "旧引用" } } },
  { accountId: "acct", conversationId: "friend" });
  assert.equal(filled.accountId, "acct");
  assert.equal(filled.conversationId, "friend");
  assert.equal(filled.quote?.text, "旧引用");
  assert.throws(() => buildUnifiedInput({ id: "m2", side: "other", text: "x",
    inputMeta: meta({ accountId: "other-acct" }) },
  { accountId: "acct", conversationId: "friend" }), /cross-accountId/u);
  assert.throws(() => buildUnifiedInput({ id: "m3", side: "other", text: "x",
    inputMeta: meta({ conversationId: "other-chat" }) },
  { accountId: "acct", conversationId: "friend" }), /cross-conversationId/u);
});

it("keeps the legacy four-field projection byte-for-byte identical", () => {
  const withMeta = buildUnifiedInput({ id: "m1", side: "other", text: "你好",
    time: 1.5, inputMeta: meta({ sentAtMs: 1.5 }) });
  const withoutMeta = buildUnifiedInput({ id: "m1", side: "other", text: "你好", time: 1.5 });
  assert.deepEqual(projectLegacyWire(withMeta, 1.5), { id: "m1", side: "other",
    text: "你好", time: 1.5 });
  assert.deepEqual(projectLegacyWire(withoutMeta, undefined), { id: "m1", side: "other",
    text: "你好", time: 0 });
  const projected = projectLegacyWire(withMeta, 1.5) as Record<string, unknown>;
  assert.equal("inputMeta" in projected, false);
  assert.deepEqual(Object.keys(projected), ["id", "side", "text", "time"]);
});

it("bounds new metadata by codepoint but leaves legacy IDs unlimited", () => {
  assert.equal(normalizeInputMeta({ senderName: "😀".repeat(200) })?.senderName,
    "😀".repeat(200));
  assert.throws(() => normalizeInputMeta({ senderName: "😀".repeat(201) }));
  const longId = "i".repeat(500);
  assert.equal(buildUnifiedInput({ id: longId, side: "self", text: "x" }).id, longId);
  assert.throws(() => buildUnifiedInput({ id: "", side: "self", text: "x" }));
});

it("preserves supplied SELF identity and rejects conflicting known provenance", () => {
  const known = buildUnifiedInput({ id: "self", side: "self", text: "合成", senderId: "me", senderName: "本人" });
  assert.equal(known.senderId, "me");
  assert.equal(known.senderName, "本人");
  assert.throws(() => buildUnifiedInput({ id: "m", side: "other", text: "x", senderId: "another", inputMeta: meta() }), /conflicting senderId/u);
  assert.throws(() => buildUnifiedInput({ id: "m", side: "other", text: "x", inputMeta: meta({ source: { kind: "ocr" } }) }, { sourceKind: "wechat" }), /conflicting source/u);
  assert.throws(() => buildUnifiedInput({ id: "m", side: "other", text: "x", time: 7, inputMeta: meta() }), /conflicting sentAtMs/u);
  assert.equal(normalizeInputMeta({ source: { kind: null } })?.source.kind, "unknown");
});

it("never lets metadata change the provider system/prompt/budget", async () => {
  const base: ApiInsightInput = {
    messages: [
      { id: "a", sender: "SELF", text: "今天怎么样？" },
      { id: "b", sender: "OTHER", text: "我有点累。" },
    ],
    targetIds: ["b"],
  };
  const withMeta: ApiInsightInput = {
    messages: [
      { ...base.messages[0]!, inputMeta: meta({ senderId: "me", senderName: "我" }) },
      { ...base.messages[1]!, inputMeta: meta({ senderId: "member-b", senderName: "阿乙",
        quote: { id: null, senderId: null, senderName: null, text: "旧\n文", sentAtMs: 0 } }) },
    ],
    targetIds: ["b"],
  };
  const requestFor = async (input: ApiInsightInput): Promise<GenerationRequest> => {
    let request: GenerationRequest | undefined;
    await analyzeApiInsights(config, input, async (_config, value) => {
      request = value;
      return { text: JSON.stringify({ items: [{ id: "b", status: "ok",
        emotion: "疲惫", intent: "说明近况" }] }) };
    });
    return request!;
  };
  const plain = await requestFor(base);
  const enriched = await requestFor(withMeta);
  assert.equal(plain.system, enriched.system);
  assert.equal(plain.prompt, enriched.prompt);
  assert.equal(plain.maxOutputTokens, enriched.maxOutputTokens);
  assert.doesNotMatch(enriched.prompt, /member-b|阿乙|inputMeta/u);
});

it("accepts the qq source kind on both ends and keeps it through the wire", () => {
  const qq = buildUnifiedInput({ id: "q1", side: "other", text: "在吗",
    inputMeta: meta({ accountId: "a:" + "0".repeat(32), conversationId: "u:peer",
      senderId: "123456789", source: { kind: "qq" } }) }, { sourceKind: "qq" });
  assert.equal(qq.source.kind, "qq");
  assert.equal(qq.accountId, "a:" + "0".repeat(32));
  const fixture = `import sys,json
sys.path.insert(0,'bridge')
from message_input import prepare_messages, SOURCE_KINDS
from node_analysis import NodeAnalysis
rows=[{'id':'q1','side':'other','text':'在吗','senderId':'123456789','senderName':'阿乙','time':1700000000000}]
prepared=prepare_messages(rows,account_id='a:'+'0'*32,conversation_id='u:peer',source_kind='qq')
print(json.dumps({'kinds':sorted(SOURCE_KINDS),
                  'wire':NodeAnalysis._wire_messages(prepared)}))`;
  const result = JSON.parse(execFileSync(process.env.WECHATVIBE_PYTHON || "python", ["-B", "-c", fixture],
    { cwd: new URL("../", import.meta.url), encoding: "utf8" })) as
    { kinds: string[]; wire: Record<string, unknown>[] };
  assert.deepEqual(result.kinds, ["ocr", "qq", "unknown", "wechat"]);
  const restored = buildUnifiedInput(result.wire[0]!, { accountId: "a:" + "0".repeat(32),
    conversationId: "u:peer", sourceKind: "qq" });
  assert.equal(restored.source.kind, "qq");
});
