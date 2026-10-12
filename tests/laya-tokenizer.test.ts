// Offline tests adapted from nizitao's WechatVibe PR #26:
// https://github.com/tswawa/WechatVibe/pull/26 @ bca5c5ab4258e16340b5a4ecea94d070011853a9
// Synthetic Metaspace BPE only: no production tokenizer, model download or inference.
import assert from "node:assert/strict";
import { it } from "node:test";

import { LayaAgent, type Runner } from "../electron/laya/agent";
import { buildSequence, serializeState } from "../electron/laya/prompt";
import { toInternal } from "../electron/laya/questions";
import {
  isUnicodeWhitespace,
  LayaTokenizer,
  type TokenizerConfig,
  type TokenizerJson,
} from "../electron/laya/tokenizer";
import type { AgentConfig, Question, RunnerOutput, State } from "../electron/laya/types";

const PAD = 0;
const EOS = 1;
const BOS = 2;
const MASK = 3;
const NEWLINE = 8;
const RSTRIP = 9;
const REPLACEMENT = "\u2581";
const CHARS = "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789";

function syntheticTokenizerJson(): TokenizerJson {
  const vocab: Record<string, number> = {
    "<pad>": PAD,
    "<eos>": EOS,
    "<bos>": BOS,
    "<mask>": MASK,
    "<unk>": 4,
    [REPLACEMENT]: 5,
    "\n": NEWLINE,
    "<r>": RSTRIP,
  };
  let next = 10;
  for (const ch of CHARS) vocab[ch] = next++;
  return {
    added_tokens: [
      { id: PAD, content: "<pad>", lstrip: false, rstrip: false, normalized: false, single_word: false },
      { id: EOS, content: "<eos>", lstrip: false, rstrip: false, normalized: false, single_word: false },
      { id: BOS, content: "<bos>", lstrip: false, rstrip: false, normalized: false, single_word: false },
      { id: MASK, content: "<mask>", lstrip: true, rstrip: false, normalized: false, single_word: false },
      { id: NEWLINE, content: "\n", lstrip: false, rstrip: false, normalized: false, single_word: false },
      { id: RSTRIP, content: "<r>", lstrip: false, rstrip: true, normalized: false, single_word: false },
    ],
    normalizer: { type: "Replace", pattern: { String: " " }, content: REPLACEMENT },
    pre_tokenizer: { type: "Metaspace", replacement: REPLACEMENT, prepend_scheme: "always", split: true },
    decoder: null,
    post_processor: null,
    model: { type: "BPE", unk_token: "<unk>", fuse_unk: true, byte_fallback: false, vocab, merges: [] },
  } as unknown as TokenizerJson;
}

function newTokenizer(): LayaTokenizer {
  const config: TokenizerConfig = {
    cls_token: { content: "<bos>" }, sep_token: { content: "<eos>" },
    pad_token: { content: "<pad>" }, mask_token: { content: "<mask>" },
  };
  return new LayaTokenizer(syntheticTokenizerJson(), config);
}

const config: AgentConfig = {
  encoder: "synthetic-encoder", head_layers: 1, max_len: 64, head_max_len: 32,
  temperature: [1, 1, 1], temperature_by_options: {},
};
const runner: Runner = {
  async run(): Promise<RunnerOutput> {
    throw new Error("prepare must not run inference");
  },
};

it("matches Unicode White_Space for every code point and lone surrogate", () => {
  const whitespace = /\p{White_Space}/u;
  for (let cp = 0; cp <= 0x10ffff; cp++) {
    const ch = String.fromCodePoint(cp);
    assert.equal(isUnicodeWhitespace(ch), whitespace.test(ch), `U+${cp.toString(16)}`);
  }
  assert.equal(isUnicodeWhitespace(""), false);
});

it("keeps known added-token, lstrip, rstrip and Metaspace ids unchanged", () => {
  const tokenizer = newTokenizer();
  const cases: [string, number[]][] = [
    ["", []], ["a", [5, 10]], ["a b", [5, 10, 5, 11]],
    ["a<mask>b", [5, 10, MASK, 5, 11]], ["\n<mask>", [NEWLINE, MASK]],
    ["a\u0085\u3000<mask>b", [5, 10, MASK, 5, 11]],
    ["<r>\u0085\u3000b", [RSTRIP, 5, 11]],
    ["<r> \n<mask>b", [RSTRIP, NEWLINE, MASK, 5, 11]],
  ];
  for (const [text, ids] of cases) {
    assert.deepEqual(tokenizer.encode(text), ids, text);
    assert.deepEqual(tokenizer.encode(text), ids, text);
    tokenizer.clearEncodeCache();
    assert.deepEqual(tokenizer.encode(text), ids, text);
  }
});

it("shares immutable results without letting a caller poison future encodes", () => {
  const tokenizer = newTokenizer();
  const ids = tokenizer.encode("hello world");
  const expected = [...ids];
  assert.equal(tokenizer.encode("hello world"), ids);
  assert.equal(Object.isFrozen(ids), true);
  assert.throws(() => (ids as number[]).fill(999), TypeError);
  assert.throws(() => (ids as number[]).push(999), TypeError);
  assert.deepEqual(tokenizer.encode("hello world"), expected);
  const copy = [...ids];
  copy.fill(999);
  assert.deepEqual(tokenizer.encode("hello world"), expected);
});

it("uses bounded FIFO eviction without changing token ids", () => {
  const tokenizer = newTokenizer();
  const first = tokenizer.encode("first");
  for (let i = 0; i < 255; i++) tokenizer.encode(`filler ${i}`);
  assert.equal(tokenizer.encode("first"), first);
  tokenizer.encode("overflow");
  const afterEviction = tokenizer.encode("first");
  assert.notEqual(afterEviction, first);
  assert.deepEqual(afterEviction, first);
});

it("does not retain oversized token arrays or raw text with tiny encodings", () => {
  const tokenizer = newTokenizer();
  const atTokenLimit = "a".repeat(4095);
  assert.equal(tokenizer.encode(atTokenLimit).length, 4096);
  assert.equal(tokenizer.encode(atTokenLimit), tokenizer.encode(atTokenLimit));
  const overTokenLimit = "a".repeat(4096);
  const longIds = tokenizer.encode(overTokenLimit);
  assert.equal(longIds.length, 4097);
  assert.notEqual(tokenizer.encode(overTokenLimit), longIds);
  assert.deepEqual(tokenizer.encode(overTokenLimit), longIds);

  const atTextLimit = " ".repeat(16378) + "<mask>";
  const boundedIds = tokenizer.encode(atTextLimit);
  assert.deepEqual(boundedIds, [MASK]);
  assert.equal(tokenizer.encode(atTextLimit), boundedIds);
  const overTextLimit = " " + atTextLimit;
  const oversizedIds = tokenizer.encode(overTextLimit);
  assert.deepEqual(oversizedIds, [MASK]);
  assert.notEqual(tokenizer.encode(overTextLimit), oversizedIds);
  assert.equal(Object.isFrozen(oversizedIds), true);
});

it("clears retained encodings while preserving subsequent token ids", () => {
  const tokenizer = newTokenizer();
  const text = "synthetic account A text";
  const before = tokenizer.encode(text);
  tokenizer.clearEncodeCache();
  const after = tokenizer.encode(text);
  assert.notEqual(after, before);
  assert.deepEqual(after, before);
  tokenizer.clearEncodeCache();
  tokenizer.clearEncodeCache();
  assert.deepEqual(tokenizer.encode(text), before);
});

it("keeps precomputed state sequences identical across states and truncation budgets", () => {
  const tokenizer = newTokenizer();
  const states: State[] = ["", "a<mask>b", "message ".repeat(20),
    { who: "them", text: "hi<mask>there" }, ["hello", { text: "world" }]];
  const definitions: Question[] = [
    { type: "choice", instructions: "pick one", criteria: ["yes", "no"] },
    { type: "noul", instructions: "is it warm" },
  ];
  for (const state of states) {
    const stateIds = tokenizer.encode(serializeState(state).replaceAll(tokenizer.maskToken, " "));
    const snapshot = [...stateIds];
    for (const definition of definitions) {
      for (const maxLen of [1, 16, 64, 256]) {
        const q = toInternal(definition);
        assert.deepEqual(buildSequence(tokenizer, state, q, maxLen, 32, stateIds),
          buildSequence(tokenizer, state, q, maxLen, 32));
      }
    }
    assert.deepEqual(stateIds, snapshot);
  }
});

it("encodes shared state once per nonempty prepare and preserves the empty path", () => {
  const tokenizer = newTokenizer();
  const seen = new Map<string, number>();
  const counting = Object.create(tokenizer) as LayaTokenizer;
  counting.encode = (text: string): readonly number[] => {
    seen.set(text, (seen.get(text) ?? 0) + 1);
    return tokenizer.encode(text);
  };
  const agent = new LayaAgent({ config, tokenizer: counting, runner });
  const state: State = "TARGET message to judge:\n[them]: saturday";
  const questions: Record<string, Question> = {
    emotion: { type: "choice", instructions: "how does it feel", criteria: ["good", "bad"] },
    intent: { type: "choice", instructions: "what is the intent", criteria: ["plan", "small"] },
    relationship: { type: "noul", instructions: "is it warm" },
  };
  const first = agent.prepare(state, questions);
  const stateText = serializeState(state).replaceAll(tokenizer.maskToken, " ");
  assert.equal(seen.get(stateText), 1);
  const second = agent.prepare(state, questions);
  assert.deepEqual(first.items, second.items);
  assert.equal(seen.get(stateText), 2);
  seen.clear();
  assert.deepEqual(agent.prepare(state, {}), { items: [], internal: [] });
  assert.equal(seen.size, 0);
});
