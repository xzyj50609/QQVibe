import catalog from '../../chatui/data/analysis-catalog.json';
import type { LabelScore } from '../../shared/contracts';
import { EXPRESSIONS } from './expression';
import type { Answer, Question } from './types';
import { mentionedSocialNeeds } from './social-cues';

type Need = { id: string; group: string; label: string; modelLabel: string; definition: string };
type Group = { id: string; label: string; modelLabel: string };
const data = catalog as typeof catalog & { socialIntents: Need[]; socialIntentGroups: Group[] };
export const SOCIAL_NEEDS: readonly Need[] = data.socialIntents;
export const SOCIAL_GROUPS: readonly Group[] = data.socialIntentGroups;
export const NO_SOCIAL_NEED = 'no such interpersonal wish';
export const NO_COMBINATION = 'none of these combinations';
const expressionsById = new Map(EXPRESSIONS.map(item => [item.id, item]));
const needsByGroup = new Map(SOCIAL_GROUPS.map(group => [group.id, SOCIAL_NEEDS.filter(need => need.group === group.id)]));
type Predict = (questions: Record<string, Question>) => Promise<Record<string, Answer>>;

export const SOCIAL_NEED_QUESTION: Question = {
  type: 'choice',
  instructions: 'What interpersonal wish does the TARGET sender express? Use context to distinguish sincere requests, playful denial, quotations and factual statements. Choose no such wish when none is expressed.',
  criteria: [NO_SOCIAL_NEED, ...SOCIAL_GROUPS.map(group => group.modelLabel)],
};

export function socialNeedQuestion(groupId: string): Question {
  const needs = needsByGroup.get(groupId);
  if (!needs) throw new Error('Unknown social need group');
  return {
    type: 'choice',
    instructions: 'Which wish does the TARGET sender express in this context? Keep who gives and who receives distinct. Consider the full conversation, including sincere or playful tone.',
    criteria: [NO_SOCIAL_NEED, ...needs.map(need => need.modelLabel)],
  };
}

function ranked(answer: Answer | undefined, allowed: readonly string[]): Array<{ label: string; probability: number }> {
  if (!answer || answer.type !== 'choice') throw new Error('Missing social intent distribution');
  const scores = allowed.map(label => ({ label, probability: answer.probabilities[label] }));
  if (scores.some(score => !Number.isFinite(score.probability) || score.probability < 0 || score.probability > 1)
    || !scores.some(score => score.probability > 0)) throw new Error('Invalid social intent distribution');
  return scores.filter(score => score.probability > 0).sort((a, b) => b.probability - a.probability);
}

export function combinationQuestion(options: readonly string[]): Question {
  return {
    type: 'choice',
    instructions: 'Which combination best matches the TARGET sender: manner AND interpersonal wish? Consider only what this sender expresses, including who gives and receives. Select none if no option fits.',
    criteria: [NO_COMBINATION, ...options],
  };
}

/**
 * Separately route the wish, then re-score at most nine actual expression/wish combinations.
 * The final probabilities come from this fresh model question, conditional on its shortlist.
 * Expression and wish probabilities are never multiplied into a made-up joint probability.
 */
export async function routeSocialIntent(expression: readonly LabelScore[], predict: Predict, targetText = ''): Promise<LabelScore[]> {
  const topExpressions = expression.filter(score => expressionsById.has(score.label) && Number.isFinite(score.probability) && score.probability > 0)
    .sort((a, b) => b.probability - a.probability).slice(0, 3);
  if (!topExpressions.length) return [];
  const broad = await predict({ social_need: SOCIAL_NEED_QUESTION });
  const groups = ranked(broad.social_need, [NO_SOCIAL_NEED, ...SOCIAL_GROUPS.map(group => group.modelLabel)]);
  if (groups[0]?.label === NO_SOCIAL_NEED) return [];
  const mentioned = new Set(mentionedSocialNeeds(targetText));
  const mentionedGroups = new Set(SOCIAL_NEEDS.filter(need => mentioned.has(need.id)).map(need => need.group));
  const selected = groups.filter(score => score.label !== NO_SOCIAL_NEED).sort((a, b) => {
    const matchA = mentionedGroups.has(SOCIAL_GROUPS.find(group => group.modelLabel === a.label)!.id);
    const matchB = mentionedGroups.has(SOCIAL_GROUPS.find(group => group.modelLabel === b.label)!.id);
    return Number(matchB) - Number(matchA) || b.probability - a.probability;
  }).slice(0, 2);
  const needs: Array<{ need: Need; routingScore: number }> = [];
  for (const groupScore of selected) {
    const group = SOCIAL_GROUPS.find(item => item.modelLabel === groupScore.label)!;
    const options = needsByGroup.get(group.id)!;
    const detail = await predict({ social_need_detail: socialNeedQuestion(group.id) });
    const scores = ranked(detail.social_need_detail, [NO_SOCIAL_NEED, ...options.map(need => need.modelLabel)]);
    if (scores[0]?.label === NO_SOCIAL_NEED) continue;
    for (const score of scores) {
      const need = options.find(item => item.modelLabel === score.label);
      if (need) needs.push({ need, routingScore: groupScore.probability * score.probability });
    }
  }
  // Mention retrieval only affects recall of the shortlist. The model can still choose none.
  const shortlist = needs.sort((a, b) => Number(mentioned.has(b.need.id)) - Number(mentioned.has(a.need.id)) || b.routingScore - a.routingScore).slice(0, 3);
  if (!shortlist.length) return [];
  const combinations = topExpressions.flatMap(score => {
    const manner = expressionsById.get(score.label)!;
    return shortlist.map(({ need }) => ({
      id: `playful:${manner.id}:${need.id}`,
      option: `${manner.modelLabel}; ${need.modelLabel}`,
    }));
  });
  const question = combinationQuestion(combinations.map(item => item.option));
  const final = await predict({ social_combination: question });
  const scores = ranked(final.social_combination, [NO_COMBINATION, ...combinations.map(item => item.option)]);
  if (scores[0]?.label === NO_COMBINATION) return [];
  return scores.filter(score => score.label !== NO_COMBINATION).map(score => ({
    label: combinations.find(item => item.option === score.label)!.id,
    probability: score.probability,
  }));
}
