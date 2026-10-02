// Written for this project (not vendored).
//
// The local fine per-message label path (`messageLabelsOnly`) extracted from
// `analysis.ts::classifyMessage`. It owns only the fine question/answer handling;
// the caller injects an already budget-checked predict callback and keeps the model,
// runtime, cache, portrait and DTO responsibilities. The questions, probabilities,
// candidate ordering and the single model call are identical to the previous inline
// branch. The fine path never depends on the portrait implementation.

import type { LabelScore } from "../shared/contracts";
import { generalIntentQuestion, generalIntentScores } from "./laya/general-intent";
import { ANALYSIS_QUESTIONS, FINE_DISPLAY_QUESTIONS } from "./laya/options";
import type { Answer, Question } from "./laya/types";

export interface FineMessageInsight {
  emotion: LabelScore[];
  intent: LabelScore[];
  intentBroad: LabelScore[];
  /**
   * Legacy adaptation: the existing OTHER-side protocol still derives and validates the
   * relationship `score` from this distribution. It is not part of the fine label display,
   * and SELF targets always return an empty list.
   */
  relationship: LabelScore[];
}

type PredictAnswers = (questions: Record<string, Question>) => Promise<Record<string, Answer>>;

/** Mirrors the analysis-local label ranking: raw choice probabilities, no renormalization. */
function rankedScores(answer: Answer | undefined): LabelScore[] {
  if (!answer || answer.type !== "choice") return [];
  return Object.entries(answer.probabilities)
    .map(([label, probability]) => ({ label, probability }))
    .sort((a, b) => b.probability - a.probability);
}

/**
 * One budget-checked prediction per target, exactly like the previous inline branch.
 *
 * `contextHint` is a bounded slice of preceding messages; it only influences which intent
 * candidates enter the question and never carries a probability. Still one predict call.
 */
export async function generateFineMessageInsight(
  predict: PredictAnswers,
  side: "self" | "other",
  targetText: string,
  contextHint = "",
  relationshipApplicable = true,
): Promise<FineMessageInsight> {
  const intentQuestion = generalIntentQuestion(targetText, contextHint);
  const questions = side === "other" && relationshipApplicable
    // Keep the portrait question object untouched, while using the same fine display
    // emotion question for both sides.  The relationship question remains available for
    // OTHER messages because it is a fine score input, not a portrait emotion route.
    ? { ...ANALYSIS_QUESTIONS, ...FINE_DISPLAY_QUESTIONS, intent: intentQuestion.question }
    : { ...FINE_DISPLAY_QUESTIONS, intent: intentQuestion.question };
  const answers = await predict(questions);
  const intent = generalIntentScores(answers.intent);
  return {
    emotion: rankedScores(answers.emotion),
    intent,
    intentBroad: intent,
    relationship: side === "other" && relationshipApplicable ? rankedScores(answers.relationship) : [],
  };
}
