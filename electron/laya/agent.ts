// Vendored from laya-mlx (Apache-2.0).
// Source: https://github.com/mizchi/laya-mlx @ dc3aa6b150cb861d0788fbd421cfd1303de4ed57
// Path: web/packages/laya-web/src/agent.ts
// Modified: relative imports made extensionless ("./x.ts" -> "./x"). Logic unchanged.

import { formatAnswers } from "./calibration";
import { buildSequence, collate } from "./prompt";
import { renderOptions, toInternal } from "./questions";
import type { LayaTokenizer } from "./tokenizer";
import type {
  AgentConfig,
  Answer,
  Batch,
  InternalQuestion,
  PredictResult,
  PreparedItem,
  Question,
  RunnerOutput,
  State,
} from "./types";

/** Executes one collated batch. `OnnxRunner` in session.ts is the production implementation. */
export interface Runner {
  run(batch: Batch): Promise<RunnerOutput>;
}

export interface LayaAgentOptions {
  config: AgentConfig;
  tokenizer: LayaTokenizer;
  runner: Runner;
  /** Questions per forward pass; Python defaults to 16. */
  batchSize?: number;
}

/** Port of Python `Agent` without model loading; see `loadAgent` in session.ts. */
export class LayaAgent {
  readonly config: AgentConfig;
  readonly tokenizer: LayaTokenizer;
  readonly runner: Runner;
  readonly batchSize: number;

  constructor(options: LayaAgentOptions) {
    const batchSize = options.batchSize ?? 16;
    if (!Number.isInteger(batchSize) || batchSize < 1)
      throw new Error("batchSize must be a positive integer");
    validateConfig(options.config);
    // Store a normalized config with `temperature`/`temperature_by_options` defaults applied, so
    // `calibration.ts` (formatAnswers) can index them directly without repeating the `?? [1, 1,
    // 1]` / `?? {}` fallback validateConfig already computed to check for.
    this.config = {
      ...options.config,
      temperature: options.config.temperature ?? [1, 1, 1],
      temperature_by_options: options.config.temperature_by_options ?? {},
    };
    this.tokenizer = options.tokenizer;
    this.runner = options.runner;
    this.batchSize = batchSize;
  }

  /** Python `Agent.prepare`: upstream-compatible token sequences for every question. */
  prepare(
    state: State,
    questions: Record<string, Question>,
  ): { items: PreparedItem[]; internal: InternalQuestion[] } {
    if (typeof questions !== "object" || questions === null || Array.isArray(questions)) {
      throw new Error("questions must be a dictionary keyed by question id");
    }
    const items: PreparedItem[] = [];
    const internal: InternalQuestion[] = [];
    for (const [qid, definition] of Object.entries(questions)) {
      const q = toInternal(definition);
      const item = buildSequence(
        this.tokenizer,
        state,
        q,
        this.config.max_len,
        this.config.head_max_len,
      );
      if (item.markers.length !== renderOptions(q).length) {
        throw new Error(
          `Question ${JSON.stringify(qid)} has too many options for the token budget`,
        );
      }
      items.push(item);
      internal.push(q);
    }
    return { items, internal };
  }

  /** Python `Agent.predict` / `system_one`. */
  async predict(state: State, questions: Record<string, Question>): Promise<PredictResult> {
    const { items, internal } = this.prepare(state, questions);
    const questionIds = Object.keys(questions);
    const answers: Record<string, Answer> = {};
    let inputTokens = 0;
    for (let start = 0; start < items.length; start += this.batchSize) {
      const chunk = items.slice(start, start + this.batchSize);
      const chunkIds = questionIds.slice(start, start + chunk.length);
      let output: RunnerOutput;
      try {
        output = await this.runner.run(collate(chunk, this.tokenizer.padTokenId));
      } catch (error) {
        throw new Error(`Laya inference failed for questions ${chunkIds.join(", ")}`, {
          cause: error,
        });
      }
      const result = formatAnswers({
        config: this.config,
        questionIds: chunkIds,
        internal: internal.slice(start, start + chunk.length),
        items: chunk,
        logits: output.logits,
        actLogits: output.actLogits,
        markers: output.markers,
        actions: output.actions,
      });
      Object.assign(answers, result.answers);
      inputTokens += result.usage.input_tokens;
    }
    return {
      model: "laya-rl-agent",
      answers,
      usage: { input_tokens: inputTokens, output_tokens: 0 },
    };
  }
}

/**
 * Python `Agent.__init__` checks on the agent configuration. Python also checks
 * `max_len <= max_position_embeddings` using the encoder config; the web bundle does not load
 * the encoder config, so that check is skipped here.
 */
function validateConfig(config: AgentConfig): void {
  if (!("encoder" in config) || !("head_layers" in config)) {
    throw new Error("Laya config must specify encoder and head_layers");
  }
  const { max_len: maxLen, head_max_len: headMaxLen } = config;
  if (!(4 < headMaxLen && headMaxLen < maxLen)) {
    throw new Error("Expected 4 < head_max_len < max_len");
  }
  const temperature = config.temperature ?? [1, 1, 1];
  const temperatureByOptions = config.temperature_by_options ?? {};
  const temperatures = [...temperature, ...Object.values(temperatureByOptions)];
  if (temperature.length !== 3 || temperatures.some((t) => !Number.isFinite(t) || t <= 0)) {
    throw new Error("Calibration temperatures must be finite and positive");
  }
}
