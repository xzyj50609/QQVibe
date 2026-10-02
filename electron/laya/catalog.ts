import catalogJson from "../../chatui/data/analysis-catalog.json";
import type { LabelScore } from "../../shared/contracts";
import type { Answer, Question } from "./types";

export interface CatalogEntry {
  id: string;
  label: string;
  modelLabel: string;
}
export interface IntentEntry extends CatalogEntry {
  group: string;
  definition: string;
}

export const CATALOG_VERSION = catalogJson.version;
export const EMOTIONS: readonly CatalogEntry[] = catalogJson.emotions;
export const INTENT_GROUPS: readonly CatalogEntry[] = catalogJson.intentGroups;
export const INTENTS: readonly IntentEntry[] = catalogJson.intents;

const intentByGroup = new Map(INTENT_GROUPS.map((group) => [
  group.id,
  INTENTS.filter((intent) => intent.group === group.id),
]));
export const INTENT_FAMILIES = [
  { id: "small_talk", modelLabel: "small talk", groups: ["greeting", "conversation", "closure"] },
  { id: "share_news", modelLabel: "share news", groups: ["sharing", "disclosure"] },
  { id: "ask_question", modelLabel: "ask question", groups: ["inquiry", "feedback"] },
  { id: "seek_comfort", modelLabel: "seek comfort", groups: ["help_request"] },
  { id: "give_comfort", modelLabel: "give comfort", groups: ["help_offer", "support", "gratitude"] },
  { id: "make_plan", modelLabel: "make plan", groups: ["coordination", "planning", "invitation", "commitment", "transaction", "work"] },
  { id: "flirt", modelLabel: "flirt", groups: ["affection", "flirt"] },
  { id: "complain", modelLabel: "complain", groups: ["conflict", "complaint"] },
  { id: "apologize", modelLabel: "apologize", groups: ["apology", "repair"] },
  { id: "joke", modelLabel: "joke", groups: ["humor"] },
  { id: "reject", modelLabel: "reject", groups: ["rejection"] },
  { id: "distance", modelLabel: "distance", groups: ["boundary"] },
] as const;
const familyByLabel = new Map<string, (typeof INTENT_FAMILIES)[number]>(INTENT_FAMILIES.map((family) => [family.modelLabel, family]));

export const EMOTION_BUCKETS = {
  happy: ["happy", "excited", "content", "relieved", "grateful", "hopeful", "proud"],
  affectionate: ["affectionate", "tender"],
  neutral: ["neutral", "calm", "focused", "bored", "tired", "detached", "determined", "curious"],
  amused: ["amused", "surprised"],
  sad: ["sad", "lonely", "disappointed", "hurt", "guilty", "ashamed"],
  anxious: ["anxious", "fearful", "worried", "stressed", "overwhelmed", "suspicious", "confused", "uncertain", "embarrassed", "shy"],
  angry: ["angry", "frustrated", "irritated", "resentful", "jealous"],
} as const;
export const EMOTION_QUESTION: Question = {
  type: "choice",
  instructions: "Which ONE broad emotion does the sender of the TARGET message feel? Judge the sender's emotion, not the recipient's illness or their conversational purpose. Use neutral for matter-of-fact messages.",
  criteria: Object.keys(EMOTION_BUCKETS),
};

export function emotionDetailQuestion(bucket: keyof typeof EMOTION_BUCKETS): Question {
  return {
    type: "choice",
    instructions: "Within this emotional family, what is the sender of the TARGET message actually feeling?",
    criteria: EMOTION_BUCKETS[bucket].map((id) => EMOTIONS.find((emotion) => emotion.id === id)!.modelLabel),
  };
}

export async function routeEmotion(
  answer: Answer | undefined,
  predict: (questions: Record<string, Question>) => Promise<Record<string, Answer>>,
): Promise<LabelScore[]> {
  if (!answer || answer.type !== "choice") return [];
  const buckets = Object.entries(answer.probabilities)
    .filter(([label, probability]) => label in EMOTION_BUCKETS && Number.isFinite(probability) && probability > 0)
    .sort((left, right) => right[1] - left[1])
    .slice(0, Math.max(...Object.values(answer.probabilities)) >= 0.65 ? 1 : 2);
  const scores: LabelScore[] = [];
  for (const [label, parentProbability] of buckets) {
    const bucket = label as keyof typeof EMOTION_BUCKETS;
    const detail = (await predict({ emotion_detail: emotionDetailQuestion(bucket) })).emotion_detail;
    if (!detail || detail.type !== "choice") continue;
    for (const id of EMOTION_BUCKETS[bucket]) {
      const leaf = EMOTIONS.find((emotion) => emotion.id === id)!;
      const conditional = detail.probabilities[leaf.modelLabel];
      if (Number.isFinite(conditional) && conditional >= 0) {
        scores.push({ label: id, probability: parentProbability * conditional });
      }
    }
  }
  return scores.sort((left, right) => right.probability - left.probability);
}
export const INTENT_GROUP_QUESTION: Question = {
  type: "choice",
  instructions: "What is the sender of the TARGET message mainly trying to do? flirt = romantic interest; reject = refuse an advance; distance = cool down or pull back; make plan = arrange to meet; give comfort = care for the other; seek comfort = ask for support.",
  criteria: INTENT_FAMILIES.map((family) => family.modelLabel),
};

export function groupQuestion(familyId: string): Question {
  const family = INTENT_FAMILIES.find((item) => item.id === familyId);
  if (!family) throw new Error(`Unknown intent family: ${familyId}`);
  return {
    type: "choice",
    instructions: "Which specific purpose best fits the TARGET message within this communication family?",
    criteria: family.groups.map((id) => INTENT_GROUPS.find((group) => group.id === id)!.modelLabel),
  };
}

export function leafQuestion(groupId: string): Question {
  const leaves = intentByGroup.get(groupId);
  if (!leaves?.length) throw new Error(`Unknown or empty intent group: ${groupId}`);
  return {
    type: "choice",
    instructions: "Within this category, what is the TARGET sender mainly trying to do? Choose one specific action.",
    criteria: leaves.map((leaf) => leaf.modelLabel),
  };
}

export interface RoutedIntent {
  scores: LabelScore[];
  selectedGroups: string[];
  parentProbabilities: Record<string, number>;
}

export async function routeIntent(
  answer: Answer | undefined,
  predict: (questions: Record<string, Question>) => Promise<Record<string, Answer>>,
): Promise<RoutedIntent> {
  if (!answer || answer.type !== "choice") return { scores: [], selectedGroups: [], parentProbabilities: {} };
  const ranked = Object.entries(answer.probabilities)
    .filter(([label, probability]) => familyByLabel.has(label) && Number.isFinite(probability) && probability > 0)
    .sort((left, right) => right[1] - left[1]);
  const families = ranked.slice(0, ranked[0]?.[1] >= 0.65 ? 1 : 2);
  const groupCandidates: Array<{ id: string; probability: number }> = [];
  for (const [label, familyProbability] of families) {
    const family = familyByLabel.get(label)!;
    const groupAnswer = (await predict({ intent_group: groupQuestion(family.id) })).intent_group;
    if (!groupAnswer || groupAnswer.type !== "choice") continue;
    for (const groupId of family.groups) {
      const group = INTENT_GROUPS.find((item) => item.id === groupId)!;
      const conditional = groupAnswer.probabilities[group.modelLabel];
      if (Number.isFinite(conditional) && conditional > 0) {
        groupCandidates.push({ id: groupId, probability: familyProbability * conditional });
      }
    }
  }
  const selected = groupCandidates.sort((left, right) => right.probability - left.probability).slice(0, 2);
  const scores: LabelScore[] = [];
  const parentProbabilities: Record<string, number> = {};
  for (const { id, probability } of selected) {
    parentProbabilities[id] = probability;
    const detail = (await predict({ intent_detail: leafQuestion(id) })).intent_detail;
    if (!detail || detail.type !== "choice") continue;
    const leaves = intentByGroup.get(id)!;
    for (const leaf of leaves) {
      const conditional = detail.probabilities[leaf.modelLabel];
      if (Number.isFinite(conditional) && conditional >= 0) {
        scores.push({ label: leaf.id, probability: probability * conditional });
      }
    }
  }
  return { scores: scores.sort((left, right) => right.probability - left.probability), selectedGroups: selected.map((item) => item.id), parentProbabilities };
}
