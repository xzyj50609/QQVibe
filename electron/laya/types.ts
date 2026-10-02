// Vendored from laya-mlx (Apache-2.0).
// Source: https://github.com/mizchi/laya-mlx @ dc3aa6b150cb861d0788fbd421cfd1303de4ed57
// Path: web/packages/laya-web/src/types.ts
// Logic unchanged; see README.md for alignment notes.

export type QuestionType = "choice" | "score" | "noul";
export const QTYPES: Record<QuestionType, number> = { choice: 0, score: 1, noul: 2 };
export const QTYPE_NAMES: QuestionType[] = ["choice", "score", "noul"];

export type Json = string | number | boolean | null | Json[] | { [key: string]: Json };
export type Criterion = Json;
export type State = string | Json[] | { [key: string]: Json };

export interface ChoiceQuestion {
  type: "choice";
  instructions: Json;
  /**
   * `string[]` (list form) preserves option order unambiguously. The dict form,
   * `Record<string, Criterion>`, does too for arbitrary string keys, but JavaScript always
   * enumerates "array index" keys ("0", "1", "23", ...) first and in ascending numeric order,
   * regardless of insertion order — and by the time this object exists, `JSON.parse` has
   * already discarded the source order. A dict that mixes such keys with ordinary ones can no
   * longer round-trip Python's (always insertion-ordered) dict order, so `toInternal` rejects
   * it and asks for the list form instead; an all-numeric-key dict is fine, since ascending
   * order is the only order a JSON producer could have meant.
   */
  criteria: string[] | Record<string, Criterion>;
}
export interface ScoreQuestion {
  type: "score";
  instructions: Json;
  criteria: Criterion[];
}
export interface NoulQuestion {
  type: "noul";
  instructions: Json;
  criteria?: { false?: Criterion; true?: Criterion } | null;
}
export type Question = ChoiceQuestion | ScoreQuestion | NoulQuestion;

/** Mirrors Python `Agent._to_internal` output: {"t", "ins", "crit"}. */
export type InternalQuestion =
  | { t: "choice"; ins: string; crit: Record<string, Criterion> }
  | { t: "score"; ins: string; crit: Criterion[] }
  | { t: "noul"; ins: string; crit: { false?: Criterion; true?: Criterion } | null };

export interface AgentConfig {
  encoder: string;
  head_layers: number;
  max_len: number;
  head_max_len: number;
  max_prefixes?: number;
  act_costs?: Record<string, number>;
  temperature: number[];
  temperature_by_options: Record<string, number>;
  [extra: string]: Json | undefined;
}

export interface PreparedItem {
  ids: number[];
  markers: number[];
  qtype: number;
}

export interface Batch {
  rows: number;
  length: number;
  markers: number;
  inputIds: BigInt64Array;
  attentionMask: BigInt64Array;
  markerPos: BigInt64Array;
  markerMask: Uint8Array;
  qtype: BigInt64Array;
}

export interface RunnerOutput {
  logits: Float32Array;
  actLogits: Float32Array;
  rows: number;
  markers: number;
  actions: number;
}

export interface AnswerAction {
  act_probability: number;
}
export interface ChoiceAnswer {
  type: "choice";
  confidence: number;
  action: AnswerAction;
  choice: string;
  probabilities: Record<string, number>;
}
export interface ScoreAnswer {
  type: "score";
  confidence: number;
  action: AnswerAction;
  score: number;
  legend: Record<string, Criterion>;
  probabilities: Record<string, number>;
}
export interface NoulAnswer {
  type: "noul";
  confidence: number;
  action: AnswerAction;
  noul: number;
}
export type Answer = ChoiceAnswer | ScoreAnswer | NoulAnswer;

export interface PredictResult {
  model: "laya-rl-agent";
  answers: Record<string, Answer>;
  usage: { input_tokens: number; output_tokens: 0 };
}
