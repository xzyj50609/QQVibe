import catalogJson from "../../chatui/data/analysis-catalog.json";
import type { LabelScore } from "../../shared/contracts";
import type { Answer, Question } from "./types";

export interface ExpressionFamily {
  id: string;
  label: string;
  modelLabel: string;
  groups: string[];
}

export interface ExpressionGroup {
  id: string;
  family: string;
  label: string;
  modelLabel: string;
}

export interface ExpressionEntry {
  id: string;
  group: string;
  label: string;
  modelLabel: string;
  definition: string;
  emotion: string;
  kaomoji: string[];
}

export const EXPRESSION_FAMILIES: readonly ExpressionFamily[] = catalogJson.expressionFamilies;
export const EXPRESSION_GROUPS: readonly ExpressionGroup[] = catalogJson.expressionGroups;
export const EXPRESSIONS: readonly ExpressionEntry[] = catalogJson.expressions;

const groupsById = new Map(EXPRESSION_GROUPS.map((group) => [group.id, group]));
const expressionsByGroup = new Map(EXPRESSION_GROUPS.map((group) => [
  group.id,
  EXPRESSIONS.filter((expression) => expression.group === group.id),
]));

/** Expression is display-only. It does not affect emotion, intent, affinity, or MBTI. */
export const EXPRESSION_FAMILY_QUESTION: Question = {
  type: "choice",
  instructions: "Which broad expressive manner does the TARGET sender convey? Judge the sender's own tone, not the recipient's feelings or the action requested. Use context to distinguish sincere tone, playful denial and quotation; use matter of fact for unmarked text.",
  criteria: EXPRESSION_FAMILIES.map((family) => family.modelLabel),
};

export function expressionGroupQuestion(familyId: string): Question {
  const family = EXPRESSION_FAMILIES.find((item) => item.id === familyId);
  if (!family) throw new Error(`Unknown expression family: ${familyId}`);
  return {
    type: "choice",
    instructions: "Which manner best fits the TARGET sender within this family? Distinguish genuine distress from playful coaxing, and joking from anger.",
    criteria: family.groups.map((id) => {
      const group = groupsById.get(id);
      if (!group) throw new Error(`Unknown expression group: ${id}`);
      return group.modelLabel;
    }),
  };
}

export function expressionDetailQuestion(groupId: string): Question {
  const leaves = expressionsByGroup.get(groupId);
  if (!leaves?.length) throw new Error(`Unknown or empty expression group: ${groupId}`);
  return {
    type: "choice",
    instructions: "Which specific manner does the TARGET sender express in context? Distinguish their own tone from quotations, and weigh whether a denial is sincere or playful.",
    criteria: leaves.map((expression) => expression.modelLabel),
  };
}

function usableProbability(value: number | undefined): value is number {
  return Number.isFinite(value) && value !== undefined && value >= 0 && value <= 1;
}

/**
 * Route through at most two model-scored families and two model-scored groups.
 * Every leaf score is the unrenormalized family × group × leaf probability.
 * No keyword or synthetic neutral fallback is applied when the model gives no answer.
 */
export async function routeExpression(
  answer: Answer | undefined,
  predict: (questions: Record<string, Question>) => Promise<Record<string, Answer>>,
  batchIndependent = false,
): Promise<LabelScore[]> {
  if (!answer || answer.type !== "choice") return [];

  const families = EXPRESSION_FAMILIES.map((family) => ({
    family,
    probability: answer.probabilities[family.modelLabel],
  })).filter((item) => usableProbability(item.probability) && item.probability > 0)
    .sort((left, right) => right.probability - left.probability)
    .slice(0, 2);

  const groupCandidates: Array<{ group: ExpressionGroup; probability: number }> = [];
  const groupAnswers: Array<Answer | undefined> = [];
  if (batchIndependent && families.length === 2) {
    const result = await predict({
      expression_group: expressionGroupQuestion(families[0]!.family.id),
      expression_group_2: expressionGroupQuestion(families[1]!.family.id),
    });
    groupAnswers.push(result.expression_group, result.expression_group_2);
  } else {
    for (const { family } of families) {
      groupAnswers.push((await predict({ expression_group: expressionGroupQuestion(family.id) })).expression_group);
    }
  }
  for (const [index, { family, probability: familyProbability }] of families.entries()) {
    const groupAnswer = groupAnswers[index];
    if (!groupAnswer || groupAnswer.type !== "choice") continue;
    for (const groupId of family.groups) {
      const group = groupsById.get(groupId)!;
      const conditional = groupAnswer.probabilities[group.modelLabel];
      if (usableProbability(conditional) && conditional > 0) {
        groupCandidates.push({ group, probability: familyProbability * conditional });
      }
    }
  }

  const selectedGroups = groupCandidates.sort((left, right) => right.probability - left.probability).slice(0, 2);
  const scores: LabelScore[] = [];
  const detailAnswers: Array<Answer | undefined> = [];
  if (batchIndependent && selectedGroups.length === 2) {
    const result = await predict({
      expression_detail: expressionDetailQuestion(selectedGroups[0]!.group.id),
      expression_detail_2: expressionDetailQuestion(selectedGroups[1]!.group.id),
    });
    detailAnswers.push(result.expression_detail, result.expression_detail_2);
  } else {
    for (const { group } of selectedGroups) {
      detailAnswers.push((await predict({ expression_detail: expressionDetailQuestion(group.id) })).expression_detail);
    }
  }
  for (const [index, { group, probability: parentProbability }] of selectedGroups.entries()) {
    const detail = detailAnswers[index];
    if (!detail || detail.type !== "choice") continue;
    for (const expression of expressionsByGroup.get(group.id)!) {
      const conditional = detail.probabilities[expression.modelLabel];
      if (usableProbability(conditional) && conditional >= 0) {
        scores.push({ label: expression.id, probability: parentProbability * conditional });
      }
    }
  }
  return scores.sort((left, right) => right.probability - left.probability);
}
