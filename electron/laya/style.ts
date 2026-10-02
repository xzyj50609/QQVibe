import type { StyleEvidence } from "../../shared/contracts";
import type { Answer, Question } from "./types";

const DEFINITIONS = {
  socialEnergy: ["no lively social expression", "lively social expression", "Does the TARGET sender use an energetic, socially expressive tone?"],
  humor: ["no humor expressed", "humor expressed", "Does the TARGET sender deliberately joke or use playful humor?"],
  composure: ["no composed tone shown", "composed tone shown", "Does the TARGET sender show a calm, steady tone, including matter-of-fact speech?"],
  initiative: ["no conversation initiative", "conversation initiative", "Does the TARGET sender initiate or actively advance a topic, question, or plan?"],
  care: ["no care shown", "care shown", "Does the TARGET sender show concern, comfort, or practical support for another person?"],
  affection: ["no affection shown", "affection shown", "Does the TARGET sender express personal warmth, closeness, or affection toward someone?"],
} as const;

export const STYLE_QUESTIONS: Record<string, Question> = Object.fromEntries(
  Object.entries(DEFINITIONS).map(([key, [negative, positive, instruction]]) => [
    `style_${key}`,
    { type: "choice", instructions: `${instruction} Judge only displayed behaviour in the TARGET message.`, criteria: [negative, positive] },
  ]),
);

export function styleEvidenceFromAnswers(answers: Record<string, Answer>): StyleEvidence | null {
  const result: Record<string, number> = {};
  for (const [key, [negative, positive]] of Object.entries(DEFINITIONS)) {
    const answer = answers[`style_${key}`];
    if (!answer || answer.type !== "choice") return null;
    const no = answer.probabilities[negative];
    const yes = answer.probabilities[positive];
    if (![no, yes].every((value) => Number.isFinite(value) && value >= 0 && value <= 1)) return null;
    result[key] = yes;
  }
  return result as StyleEvidence;
}
