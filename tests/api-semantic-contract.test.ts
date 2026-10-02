// Simple API semantic contract: one short emotion + one short intent, successful
// terminal states, new revision isolation, and the shared UI adapter.
//
// Offline and synthetic only: the Python check normalizes plain dicts, the UI check runs
// the classic scripts in a VM. No model, no chat, no network.
import assert from "node:assert/strict";
import { execFileSync } from "node:child_process";
import { readFileSync } from "node:fs";
import path from "node:path";
import { it } from "node:test";
import { fileURLToPath } from "node:url";
import vm from "node:vm";

const ROOT = path.dirname(path.dirname(fileURLToPath(import.meta.url)));

it("moves Python to the independent free-label-v5-simple scope and validates the simple shape", () => {
  const fixture = `import json, sys
sys.path.insert(0, 'bridge')
from message_contracts import API_INSIGHT_REVISION, api_insight_scope, normalize_api_insight

def rejected(value):
    try:
        normalize_api_insight(value)
        return False
    except ValueError:
        return True

print(json.dumps({
    "revision": API_INSIGHT_REVISION,
    "scope": api_insight_scope("api:x"),
    "ok": normalize_api_insight({"id": "b", "status": "ok",
        "affect": {"feeling": "犹豫"},
        "intents": ["婉拒"]}),
    "routine": normalize_api_insight({"id": "r", "status": "routine"}),
    "uncertain": normalize_api_insight({"id": "u", "status": "uncertain"}),
    "legacy": normalize_api_insight({"id": "l", "status": "ok", "emotion": "犹豫", "intent": "拖延决定"}),
    "rejects": {
        "duplicate_intent": rejected({"id": "b", "status": "ok", "intents": ["拒绝", "拒绝"]}),
        "duplicate_affect": rejected({"id": "b", "status": "ok", "affect": {"tone": "犹豫", "feeling": "犹豫"}}),
        "cross_dimension_duplicate": rejected({"id": "b", "status": "ok", "affect": {"feeling": "犹豫"}, "intents": ["犹豫"]}),
        "too_many_intents": rejected({"id": "b", "status": "ok", "intents": ["一", "二", "三", "四"]}),
        "non_han": rejected({"id": "b", "status": "ok", "intents": ["ask"]}),
        "long_label": rejected({"id": "b", "status": "ok", "intents": ["非常非常非常长的标签"]}),
        "routine_with_content": rejected({"id": "r", "status": "routine", "intents": ["婉拒"]}),
    },
}))`;
  const output = JSON.parse(execFileSync(process.env.WECHATVIBE_PYTHON || "python",
    ["-B", "-c", fixture], { cwd: ROOT, encoding: "utf8" })) as Record<string, unknown>;
  assert.equal(output.revision, "free-label-v5-simple");
  assert.equal(output.scope, "api:x:free-label-v5-simple");
  assert.notEqual(output.scope, "api:x:free-label-v3", "old v3 rows stay in their own scope");
  assert.deepEqual(output.ok, { id: "b", status: "ok",
    affect: { feeling: "犹豫" }, intents: ["婉拒"] });
  assert.deepEqual(output.routine, { id: "r", status: "routine" });
  assert.deepEqual(output.uncertain, { id: "u", status: "uncertain" });
  assert.deepEqual(output.legacy, { id: "l", status: "ok",
    affect: { feeling: "犹豫" }, intents: ["拖延决定"] });
  assert.deepEqual(output.rejects, {
    duplicate_intent: true, duplicate_affect: true, too_many_intents: true,
    cross_dimension_duplicate: true, non_han: true, long_label: true, routine_with_content: true,
  });
});

function element(tag: string, className = "", textContent = "") {
  const node: Record<string, unknown> = {
    tag, className, textContent, title: "", children: [] as unknown[],
    appendChild(child: unknown) { (child as { parent?: unknown }).parent = node; (node.children as unknown[]).push(child); return child; },
  };
  return node;
}
function harness(pick: (item: { label?: string }, id: string) => string = () => "") {
  const context = vm.createContext({ element, percent: (value: number) => `${value * 100}%`,
    pickKaomoji: pick, window: {} });
  vm.runInContext(readFileSync(path.join(ROOT, "chatui/message-insight-adapters.js"), "utf8"), context);
  vm.runInContext(readFileSync(path.join(ROOT, "chatui/message-labels.js"), "utf8"), context);
  return context.window as {
    MessageInsightAdapters: { apiView(result: unknown): { emotions: Array<{ label: string }>; intents: Array<{ label: string }> } };
    MessageLabels: { create(deps: unknown): { render(view: unknown, id: string): { children: unknown[] } } };
  };
}
function texts(node: { textContent?: string; children?: unknown[] }): string {
  return [node.textContent || "", ...(node.children || []).map((child) => texts(child as { textContent?: string; children?: unknown[] }))].join(" ");
}
function countClass(node: { className?: string; children?: unknown[] }, name: string): number {
  return Number((node.className || "").split(" ").includes(name)) +
    (node.children || []).reduce((sum: number, child) => sum + countClass(child as { className?: string; children?: unknown[] }, name), 0);
}

it("maps one simple emotion and one plain intent", () => {
  const { MessageInsightAdapters } = harness();
  const view = MessageInsightAdapters.apiView({ id: "m1", status: "ok",
    affect: { tone: "委婉", feeling: "犹豫", interaction: "保留" },
    intents: ["婉拒", "缓和", "暂缓", "第四项"] });
  // JSON round-trip normalizes the VM realm's object prototypes for deep comparison.
  assert.deepEqual(JSON.parse(JSON.stringify(view.emotions)), [
    { label: "犹豫" },
  ]);
  assert.deepEqual(JSON.parse(JSON.stringify(view.intents)), [{ label: "婉拒" }]);
});

it("treats routine/uncertain/insufficient as terminal and reads the legacy scalar shape", () => {
  const { MessageInsightAdapters } = harness();
  for (const status of ["routine", "uncertain", "insufficient"]) {
    assert.deepEqual(JSON.parse(JSON.stringify(MessageInsightAdapters.apiView({ id: "m1", status }))),
      { emotions: [], intents: [] });
  }
  assert.deepEqual(JSON.parse(JSON.stringify(MessageInsightAdapters.apiView(
    { id: "m1", status: "ok", emotion: "平静", intent: "请求帮助" }))),
      { emotions: [{ label: "平静" }], intents: [{ label: "请求帮助" }] });
});

it("renders at most two lines and three plain labels", () => {
  const seen: Array<[string, string]> = [];
  const { MessageInsightAdapters, MessageLabels } = harness((item, id) => {
    seen.push([item.label || "", id]); return "(face)";
  });
  const view = MessageInsightAdapters.apiView({ id: "m9", status: "ok",
    affect: { tone: "俏皮", feeling: "期待" }, intents: ["邀约", "继续安排"] });
  const renderer = MessageLabels.create({ element, percent: (v: number) => `${v * 100}%`,
    pickKaomoji: (item: { label?: string }, id: string) => { seen.push([item.label || "", id]); return "(face)"; } });
  const row = renderer.render(view, "m9");
  const again = renderer.render(view, "m9");
  assert.equal(row.children.length, 1);
  assert.equal(countClass(row, "intent-pct"), 0, "API labels never carry a fabricated score");
  assert.deepEqual(seen, []);
  assert.equal(texts(row), texts(again));
});

it("keeps affect/intents out of the portrait contract and the local adapter", () => {
  const portrait = readFileSync(path.join(ROOT, "electron/api-portrait.ts"), "utf8");
  assert.doesNotMatch(portrait, /\baffect\b|\bintents\b/u);
  const backend = readFileSync(path.join(ROOT, "bridge/backend_service.py"), "utf8");
  assert.match(backend, /api_insight_scope\(/u);
  assert.match(backend, /api_portrait_scope\(/u);
  const adapters = readFileSync(path.join(ROOT, "chatui/message-insight-adapters.js"), "utf8");
  const context = vm.createContext({ window: {} });
  vm.runInContext(adapters, context);
  const localView = (context.window as { MessageInsightAdapters: { localView(result: unknown, text: string, deps: unknown): { intents: unknown[] } } })
    .MessageInsightAdapters.localView;
  const dependencies = {
    rankedEmotionScores: (values: unknown) => [{ item: { label: "平静", probability: 0.8 } }].slice(0, Array.isArray(values) ? 3 : 0),
    displayedIntent: () => [{ label: "本地意图", probability: 0.5 }],
    hasIntentContent: () => true,
    isIncompleteFragment: () => false,
  };
  const view = localView({ emotion: [], intent: [], affect: { tone: "俏皮" }, intents: ["邀约"] },
    "合成文本", dependencies);
  assert.deepEqual((view.intents as Array<{ label: string }>).map((entry) => entry.label), ["本地意图"]);
});
