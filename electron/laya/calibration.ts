// Vendored from laya-mlx (Apache-2.0).
// Source: https://github.com/mizchi/laya-mlx @ dc3aa6b150cb861d0788fbd421cfd1303de4ed57
// Path: web/packages/laya-web/src/calibration.ts
// Modified: relative imports made extensionless ("./x.ts" -> "./x"). Logic unchanged.

import type {
  AgentConfig,
  Answer,
  InternalQuestion,
  PredictResult,
  PreparedItem,
} from "./types";
import { QTYPE_NAMES } from "./types";

/**
 * Index of the first maximum in `values` (ties keep the earliest index, matching Python's
 * `argmax`). Loop-based, not `Math.max(...values)` + `indexOf`: a spread call has no formal
 * argument-count limit but can blow V8's call stack on a large array, and `>` (not `>=`) keeps
 * the first occurrence on a tie.
 */
export function argmax(values: ArrayLike<number>): number {
  let best = 0;
  for (let i = 1; i < values.length; i++) {
    if (values[i]! > values[best]!) best = i;
  }
  return best;
}

export function softmax(values: ArrayLike<number>): number[] {
  const z = Array.from(values);
  let max = -Infinity;
  for (const v of z) if (v > max) max = v;
  const e = z.map((v) => Math.exp(v - max));
  let sum = 0;
  for (const v of e) sum += v;
  return e.map((v) => v / sum);
}

/** Python `temp_bucket`. */
export function tempBucket(qtype: number, k: number): string {
  const size = k <= 2 ? "2" : k <= 5 ? "3-5" : k <= 10 ? "6-10" : "11+";
  return `${QTYPE_NAMES[qtype]}:${size}`;
}

/** Python `confidence_from_probs`: 1 - H(p) / log(k), clipped to [0, 1]. */
export function confidenceFromProbs(p: number[], k: number): number {
  if (k < 2) return 1;
  let entropy = 0;
  for (const v of p.slice(0, k)) {
    const c = Math.min(Math.max(v, 1e-12), 1);
    entropy -= v * Math.log(c);
  }
  return Math.min(Math.max(1 - entropy / Math.log(k), 0), 1);
}

/**
 * Python `round(x, 4)` for the values this runtime produces. `toFixed` rounds the decimal
 * expansion half-up while Python `round` is half-even on the binary value; they differ only at
 * exact ties, which softmax outputs do not produce.
 */
export const round4 = (x: number): number => Number(x.toFixed(4));

export interface FormatInput {
  config: AgentConfig;
  questionIds: string[];
  internal: InternalQuestion[];
  items: PreparedItem[];
  logits: Float32Array;
  actLogits: Float32Array;
  markers: number;
  actions: number;
}

/** The second half of Python `Agent.system_one`: calibrated answers for one batch. */
export function formatAnswers(input: FormatInput): PredictResult {
  const { config, questionIds, internal, items, logits, actLogits, markers, actions } = input;
  if (logits.length !== items.length * markers) {
    throw new RangeError("formatAnswers: logits length mismatch");
  }
  if (actLogits.length !== items.length * actions) {
    throw new RangeError("formatAnswers: actLogits length mismatch");
  }
  if (questionIds.length !== items.length) {
    throw new RangeError("formatAnswers: questionIds length mismatch");
  }
  if (internal.length !== items.length) {
    throw new RangeError("formatAnswers: internal length mismatch");
  }
  for (let i = 0; i < logits.length; i++) {
    if (!Number.isFinite(logits[i])) throw new RangeError("Non-finite model outputs");
  }
  for (let i = 0; i < actLogits.length; i++) {
    if (!Number.isFinite(actLogits[i])) throw new RangeError("Non-finite model outputs");
  }
  for (const item of items) {
    if (item.markers.length > markers) {
      throw new RangeError("formatAnswers: item markers exceed the logits width");
    }
  }
  const answers: Record<string, Answer> = {};
  items.forEach((item, row) => {
    const qid = questionIds[row]!;
    const q = internal[row]!;
    const k = item.markers.length;
    const act = softmax(actLogits.subarray(row * actions, (row + 1) * actions));
    const scale =
      config.temperature_by_options[tempBucket(item.qtype, k)] ?? config.temperature[item.qtype]!;
    const z = Array.from(
      logits.subarray(row * markers, row * markers + k),
      (v) => v / Math.max(1e-3, scale),
    );
    const p = softmax(z);
    const base = {
      confidence: round4(confidenceFromProbs(p, k)),
      action: { act_probability: round4(act[0]!) },
    };
    if (q.t === "choice") {
      const labels = Object.keys(q.crit);
      // See `argmax`'s doc comment for why not `Math.max(...p)` + `indexOf`.
      const best = argmax(p);
      answers[qid] = {
        type: "choice",
        ...base,
        choice: labels[best]!,
        probabilities: Object.fromEntries(labels.map((label, i) => [label, round4(p[i]!)])),
      };
    } else if (q.t === "score") {
      answers[qid] = {
        type: "score",
        ...base,
        score: round4(p.reduce((sum, v, i) => sum + i * v, 0)),
        legend: Object.fromEntries(q.crit.map((value, i) => [String(i), value])),
        probabilities: Object.fromEntries(p.map((v, i) => [String(i), round4(v)])),
      };
    } else {
      const pTrue = p[1]!;
      answers[qid] = {
        type: "noul",
        ...base,
        confidence: round4(Math.max(pTrue, 1 - pTrue)),
        noul: round4(pTrue),
      };
    }
  });
  return {
    model: "laya-rl-agent",
    answers,
    usage: { input_tokens: items.reduce((n, item) => n + item.ids.length, 0), output_tokens: 0 },
  };
}
