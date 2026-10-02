// Vendored from laya-mlx (Apache-2.0).
// Source: https://github.com/mizchi/laya-mlx @ dc3aa6b150cb861d0788fbd421cfd1303de4ed57
// Path: web/packages/laya-web/src/questions.ts
// Modified: relative imports made extensionless ("./x.ts" -> "./x"). Logic unchanged.

import { pyJson } from "./pyjson";
import type { Criterion, InternalQuestion, Json } from "./types";
import { QTYPES } from "./types";

const isPlainObject = (v: unknown): v is Record<string, Json> =>
  typeof v === "object" && v !== null && !Array.isArray(v);

/**
 * True for object keys JavaScript treats as "array index" keys ("0", "1", "23", but not "01"
 * or "-1"): these are always enumerated first, in ascending numeric order, ahead of any other
 * own key, regardless of insertion order. See the doc comment on `ChoiceQuestion.criteria`.
 */
const isArrayIndexKey = (key: string): boolean => /^(0|[1-9]\d*)$/.test(key);

/** Port of Python `Agent._to_internal`, including its error messages. */
export function toInternal(question: unknown): InternalQuestion {
  if (!isPlainObject(question)) throw new Error("Each question must be a dictionary");
  const kind = question.type;
  // `kind in QTYPES` would walk the prototype chain (e.g. accept `type: "toString"`), so use
  // Object.hasOwn instead of the `in` operator.
  if (typeof kind !== "string" || !Object.hasOwn(QTYPES, kind)) {
    const repr =
      kind === undefined ? "None" : typeof kind === "string" ? `'${kind}'` : String(kind);
    throw new Error(`Unknown question type ${repr}; expected choice, score, or noul`);
  }
  if (!Object.hasOwn(question, "instructions")) {
    throw new Error("Question is missing instructions");
  }
  // "instructions" is a present own key, so `question.instructions` is never the JS-only
  // `undefined` a Json value can't represent; `?? null` just spells that out for the type
  // checker without a cast.
  const raw: Json = question.instructions ?? null;
  // Mirrors Python: `instructions` is only turned into JSON text after criteria validation for
  // the question's kind, so a bad `instructions` value (e.g. NaN, which pyJson rejects) never
  // masks a criteria error.
  const computeIns = () => (typeof raw === "string" ? raw : pyJson(raw));

  let criteria = question.criteria;
  if (kind === "choice") {
    if (Array.isArray(criteria)) {
      if (!criteria.every((c): c is string => typeof c === "string")) {
        throw new Error("Choice labels must be strings");
      }
      if (new Set(criteria).size !== criteria.length) {
        throw new Error("Choice labels must be unique");
      }
      criteria = Object.fromEntries(criteria.map((c) => [c, null]));
    } else if (isPlainObject(criteria)) {
      // A dict mixing integer-like keys ("0", "1", ...) with other keys cannot preserve its
      // source order once parsed (see the doc comment on ChoiceQuestion.criteria); an
      // all-integer-like dict is fine, since ascending order is the only sensible reading.
      const keys = Object.keys(criteria);
      const hasArrayIndexKey = keys.some(isArrayIndexKey);
      const hasOtherKey = keys.some((k) => !isArrayIndexKey(k));
      if (hasArrayIndexKey && hasOtherKey) {
        throw new Error(
          "Choice criteria with numeric labels must use the list form to preserve option order",
        );
      }
    }
    if (!isPlainObject(criteria) || Object.keys(criteria).length === 0) {
      throw new Error("Choice criteria must be a nonempty dictionary or list");
    }
    return { t: "choice", ins: computeIns(), crit: criteria };
  }
  if (kind === "score") {
    if (!Array.isArray(criteria) || criteria.length === 0) {
      throw new Error("Score criteria must be a nonempty list");
    }
    return { t: "score", ins: computeIns(), crit: criteria };
  }
  if (criteria !== undefined && criteria !== null) {
    if (!isPlainObject(criteria)) {
      throw new Error("Noul criteria must be a dictionary with false/true descriptions");
    }
  }
  return { t: "noul", ins: computeIns(), crit: criteria ?? null };
}

/** Python `render_criterion`: strings pass through, structured values become compact JSON. */
export function renderCriterion(value: Criterion): string {
  return typeof value === "string" ? value : pyJson(value, { ensureAscii: false });
}

const isBlank = (v: Criterion | undefined): v is null | undefined | "" =>
  v === null || v === undefined || v === "";

/** Python `render_options`: option texts in label-index order; noul is always [false, true]. */
export function renderOptions(q: InternalQuestion): string[] {
  if (q.t === "choice") {
    return Object.entries(q.crit).map(([k, v]) => (isBlank(v) ? k : `${k}: ${renderCriterion(v)}`));
  }
  if (q.t === "score") return q.crit.map((c, i) => `level ${i}: ${renderCriterion(c)}`);
  const crit = q.crit ?? {};
  const falseCrit = crit.false;
  const trueCrit = crit.true;
  return [
    "false: " +
      (isBlank(falseCrit) ? "no, the statement does not hold" : renderCriterion(falseCrit)),
    "true: " + (isBlank(trueCrit) ? "yes, the statement holds" : renderCriterion(trueCrit)),
  ];
}
