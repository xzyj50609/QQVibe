// Vendored from laya-mlx (Apache-2.0).
// Source: https://github.com/mizchi/laya-mlx @ dc3aa6b150cb861d0788fbd421cfd1303de4ed57
// Path: web/packages/laya-web/src/prompt.ts
// Modified: relative imports made extensionless ("./x.ts" -> "./x"). Logic unchanged.

import { pyJson } from "./pyjson";
import { renderOptions } from "./questions";
import type { LayaTokenizer } from "./tokenizer";
import type { Batch, InternalQuestion, PreparedItem, State } from "./types";
import { QTYPES } from "./types";

/** Python `serialize_state`: strings verbatim, otherwise `json.dumps(state, ensure_ascii=False)`. */
export function serializeState(state: State): string {
  return typeof state === "string" ? state : pyJson(state, { ensureAscii: false });
}

/**
 * Python `build_prefix`: [CLS] <type> question: instructions [SEP] [MASK] opt0 [MASK] opt1 ... [SEP].
 *
 * Budget rules (`headMaxLen` bounds the head + options together, not the whole sequence):
 * - Each option is encoded and capped at 48 tokens, *after* which its `[MASK]` id is
 *   prepended (so the mask id itself doesn't count against the 48-token cap).
 * - `optBudget` is what's left of `headMaxLen` once every (capped) option is accounted for.
 *   If that leaves fewer than 16 tokens for the head, every option is shrunk to
 *   `max(4, floor((headMaxLen - 16) / nOptions))` tokens and `optBudget` is recomputed from
 *   the shrunk options — this is a second, tighter pass, not an additional cap on top of 48.
 * - The head (`<type> question: <instructions>`) is then truncated to `optBudget` tokens, but
 *   never below 8, even if that leaves the total over `headMaxLen` in a many-option case.
 */
export function buildPrefix(
  tokenizer: LayaTokenizer,
  q: InternalQuestion,
  headMaxLen: number,
): { ids: number[]; markers: number[] } {
  const mask = tokenizer.maskToken;
  const options = renderOptions(q);
  const ins = q.ins.replaceAll(mask, " ");
  let headIds = tokenizer.encode(`${q.t} question: ${ins}`);
  let optIds = options.map((option) => [
    tokenizer.maskTokenId,
    ...tokenizer.encode(" " + option.replaceAll(mask, " ")).slice(0, 48),
  ]);
  const total = (rows: number[][]) => rows.reduce((n, row) => n + row.length, 0);
  let optBudget = headMaxLen - total(optIds);
  if (optBudget < 16) {
    const per = Math.max(4, Math.floor((headMaxLen - 16) / Math.max(1, optIds.length)));
    optIds = optIds.map((row) => row.slice(0, per));
    optBudget = headMaxLen - total(optIds);
  }
  headIds = headIds.slice(0, Math.max(8, optBudget));
  const ids = [tokenizer.clsTokenId, ...headIds, tokenizer.sepTokenId];
  const markers: number[] = [];
  for (const row of optIds) {
    markers.push(ids.length);
    ids.push(...row);
  }
  ids.push(tokenizer.sepTokenId);
  return { ids, markers };
}

/** Python `build_sequence`: prefix + state tokens (right-truncated) + [SEP], capped at maxLen. */
export function buildSequence(
  tokenizer: LayaTokenizer,
  state: State,
  q: InternalQuestion,
  maxLen: number,
  headMaxLen: number,
): PreparedItem {
  const prefix = buildPrefix(tokenizer, q, headMaxLen);
  const room = Math.max(0, maxLen - prefix.ids.length - 1);
  const stateIds = tokenizer
    .encode(serializeState(state).replaceAll(tokenizer.maskToken, " "))
    .slice(0, room);
  const ids = [...prefix.ids, ...stateIds, tokenizer.sepTokenId].slice(0, maxLen);
  return { ids, markers: prefix.markers.filter((m) => m < maxLen), qtype: QTYPES[q.t] };
}

/**
 * Python `collate_items`: right-padded int64 tensors with at least two marker slots.
 *
 * Python's `pad_to_multiple`/`max_length` options are intentionally omitted here: the web
 * fixtures were dumped without them, and callers are expected to chunk to `batchSize` first.
 */
export function collate(items: PreparedItem[], padId: number): Batch {
  if (items.length === 0) throw new Error("Cannot collate an empty batch");
  const rows = items.length;
  // reduce, not `Math.max(...spread)`: a spread argument list has no formal size limit in the
  // spec, but V8 still throws "Maximum call stack size exceeded" past roughly 60k-120k
  // arguments, which a large batch could plausibly hit.
  const length = items.reduce((max, item) => Math.max(max, item.ids.length), 0);
  const markers = items.reduce((max, item) => Math.max(max, item.markers.length), 2);
  const batch: Batch = {
    rows,
    length,
    markers,
    inputIds: new BigInt64Array(rows * length).fill(BigInt(padId)),
    attentionMask: new BigInt64Array(rows * length),
    markerPos: new BigInt64Array(rows * markers),
    markerMask: new Uint8Array(rows * markers),
    qtype: BigInt64Array.from(items, (item) => BigInt(item.qtype)),
  };
  items.forEach((item, row) => {
    item.ids.forEach((id, i) => {
      batch.inputIds[row * length + i] = BigInt(id);
      batch.attentionMask[row * length + i] = 1n;
    });
    item.markers.forEach((position, i) => {
      batch.markerPos[row * markers + i] = BigInt(position);
      batch.markerMask[row * markers + i] = 1;
    });
  });
  return batch;
}
