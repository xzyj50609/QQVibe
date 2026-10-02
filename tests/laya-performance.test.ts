// Offline batching regression for the Laya execution path.
//
// This uses a fake Runner and never loads the ONNX model or tokenizer. It pins the
// measured `BATCH_SIZE` behaviour: how many prepared questions are collated per
// `runner.run`, that the runner calls stay serial, and that answer merging is
// unchanged when the same question set is chunked differently. It deliberately does
// not assert wall-clock milliseconds.
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import path from "node:path";
import { it } from "node:test";
import { fileURLToPath } from "node:url";

import { LayaAgent, type Runner } from "../electron/laya/agent";
import { ANALYSIS_QUESTIONS } from "../electron/laya/options";
import { PERSONALITY_QUESTIONS } from "../electron/laya/personality";
import { STYLE_QUESTIONS } from "../electron/laya/style";
import { generateFineMessageInsight } from "../electron/local-message-insights";
import type { AgentConfig, Batch, Question, RunnerOutput, State } from "../electron/laya/types";
import type { LayaTokenizer } from "../electron/laya/tokenizer";

const ROOT = path.dirname(path.dirname(fileURLToPath(import.meta.url)));
const BATCHED_QUESTIONS = 8;

const config: AgentConfig = {
  encoder: "synthetic-encoder", head_layers: 1, max_len: 1024, head_max_len: 256,
  temperature: [1, 1, 1], temperature_by_options: {},
};

// Minimal structural tokenizer: deterministic tokens, no vocabulary file.
const tokenizer = {
  maskToken: "[MASK]", maskTokenId: 103, clsTokenId: 101, sepTokenId: 102, padTokenId: 0,
  encode: (text: string) => [11 + (text.length % 7), 12, 13],
} as unknown as LayaTokenizer;

const state: State = "Earlier context. TARGET message: 我再纠结纠结，改天再说吧。";

const portraitQuestions: Record<string, Question> = {
  ...ANALYSIS_QUESTIONS, ...PERSONALITY_QUESTIONS, ...STYLE_QUESTIONS,
};

interface RecordingRunner {
  runner: Runner;
  rows: number[];
  concurrency: () => number;
}

function recordingRunner(): RecordingRunner {
  const rows: number[] = [];
  let active = 0;
  let peak = 0;
  const runner: Runner = {
    async run(batch: Batch): Promise<RunnerOutput> {
      active += 1;
      peak = Math.max(peak, active);
      // Yield so an accidental overlap between awaited chunks would be observed.
      await new Promise((resolve) => setTimeout(resolve, 1));
      rows.push(batch.rows);
      active -= 1;
      return {
        rows: batch.rows,
        markers: batch.markers,
        actions: 1,
        logits: new Float32Array(batch.rows * batch.markers),
        actLogits: new Float32Array(batch.rows),
      };
    },
  };
  return { runner, rows, concurrency: () => peak };
}

it("collates the 14-question portrait set as 8+6 at the release batch size", async () => {
  const recorder = recordingRunner();
  const agent = new LayaAgent({ config, tokenizer, runner: recorder.runner,
    batchSize: BATCHED_QUESTIONS });
  assert.equal(Object.keys(portraitQuestions).length, 14);
  const result = await agent.predict(state, portraitQuestions);
  assert.deepEqual(recorder.rows, [8, 6]);
  assert.deepEqual(Object.keys(result.answers).sort(), Object.keys(portraitQuestions).sort());
  assert.equal(result.usage.input_tokens > 0, true);
});

it("runs the fine three-question set in a single batch", async () => {
  const recorder = recordingRunner();
  const agent = new LayaAgent({ config, tokenizer, runner: recorder.runner,
    batchSize: BATCHED_QUESTIONS });
  const fine = await generateFineMessageInsight(
    (questions) => agent.predict(state, questions).then((prediction) => prediction.answers),
    "other", "周六见吗？");
  assert.deepEqual(recorder.rows, [3]);
  assert.equal(fine.emotion.length > 0, true);
  assert.equal(fine.intent.length > 0, true);
  assert.equal(fine.relationship.length > 0, true);
});

it("keeps runner calls serial and answer merging unchanged across batch sizes", async () => {
  const chunked = recordingRunner();
  const single = recordingRunner();
  const chunkedAgent = new LayaAgent({ config, tokenizer, runner: chunked.runner, batchSize: 8 });
  const singleAgent = new LayaAgent({ config, tokenizer, runner: single.runner, batchSize: 14 });
  const [chunkedResult, singleResult] = await Promise.all([
    chunkedAgent.predict(state, portraitQuestions),
    singleAgent.predict(state, portraitQuestions),
  ]);
  assert.deepEqual(chunked.rows, [8, 6]);
  assert.deepEqual(single.rows, [14]);
  assert.equal(chunked.concurrency(), 1);
  assert.equal(single.concurrency(), 1);
  assert.deepEqual(chunkedResult, singleResult);
});

it("pins the production batch size to the benchmarked value", () => {
  const source = readFileSync(path.join(ROOT, "electron/analysis.ts"), "utf8");
  assert.match(source, /const BATCH_SIZE = 8;/u);
  assert.match(source, /batchSize: BATCH_SIZE/u);
});
