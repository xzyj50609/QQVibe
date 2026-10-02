// Written for this project (not vendored).
// Fixed, human-authored option sets for the entertainment analyzer.
//
// Prompt format was chosen by actual inference comparison on the five fixtures (see
// `.local/laya-prompt-experiment.log`): the model classifies best when the class definitions
// live in the concise natural-language instruction head and the options are short, readable
// labels (no vague underscores). Per-option description dictionaries were tested and regressed
// intent/relationship accuracy, so only the self-quality question keeps a criteria dictionary
// (which measured best there). The conversation content fed as state stays in its original
// language (typically Chinese).
//
// Division of labor (per review):
// - emotion      : DISPLAY only. Never used to compute affinity.
// - intent       : DISPLAY only. Includes rejection/distancing so they are not misread.
// - relationship : the ONLY signal behind relationship affinity, and only for OTHER messages.
// - self_quality : classifies the latest SELF reply; its distribution maps to the "我的发挥" grade.

import type { Question } from "./types";
import { EMOTION_QUESTION, INTENT_GROUP_QUESTION, EMOTIONS, INTENT_FAMILIES } from "./catalog";

/**
 * Fine per-message emotion/stance buckets.  These are deliberately separate from the
 * seven broad emotions used by the portrait route: a short message often communicates
 * a social stance (for example, polite guardedness or playful closeness) rather than a
 * clinical mood.  Keep this question on DISPLAY_QUESTIONS only; portrait analysis must
 * continue to use EMOTION_QUESTION + routeEmotion so its saved state is not mixed with
 * the fine-message cache.
 */
export const FINE_EMOTION_OPTIONS = [
  "俏皮",
  "娇嗔",
  "傲娇",
  "犹豫",
  "纠结",
  "试探",
  "客套",
  "尴尬",
  "戒备",
  "无奈",
  "敷衍",
  "疲惫",
  "委屈",
  "烦躁",
  "吃醋",
  "坦诚",
  "随和",
  "自然",
] as const;

export const FINE_EMOTION_QUESTION: Question = {
  type: "choice",
  instructions:
    "判断 TARGET 发送者在当前语境呈现的情绪或语气，从具体词中选最贴切的一项。" +
    "可用具体词：俏皮、娇嗔、傲娇、犹豫、纠结、试探、客套、尴尬、戒备、无奈、敷衍、疲惫、" +
    "委屈、烦躁、吃醋、坦诚、随和、自然；优先选择最贴切的一个，不要把多个近义词一起输出。" +
    "这些是情感表达，不是交流意图；不要把拒绝、拖延、试探、状态报告、分享、邀约等动作当成情绪。" +
    "结合前文和表情的语用作用判断：表情只能强化、缓和或反讽已有语气，不能单独证明开心。" +
    "普通事实或确认没有明显情绪时可选自然，但不要为了凑标签虚构负面情绪；前端会按显著性留白。",
  criteria: [...FINE_EMOTION_OPTIONS],
};

/** 7 emotion options, display only. */
export const EMOTION_OPTIONS = EMOTIONS.map((emotion) => emotion.modelLabel);

/** 12 intent options, display only. `reject` / `distance` cover rejection/distancing. */
export const INTENT_OPTIONS = INTENT_FAMILIES.map((family) => family.modelLabel);

/** 5 relationship classes. This is the affinity signal (OTHER messages only). */
export const RELATIONSHIP_OPTIONS = ["romantic", "warm", "neutral", "distant", "rejecting"] as const;

/** 5 self-reply quality classes. Their distribution maps to the "我的发挥" grade. */
export const SELF_QUALITY_OPTIONS = [
  "connected",
  "empathic",
  "off topic",
  "pressuring",
  "offensive",
] as const;

export type Emotion = (typeof EMOTION_OPTIONS)[number];
export type Intent = (typeof INTENT_OPTIONS)[number];
export type Relationship = (typeof RELATIONSHIP_OPTIONS)[number];
export type SelfQuality = (typeof SELF_QUALITY_OPTIONS)[number];

/**
 * Entertainment affinity weight of each relationship class in [-1, 1]. Positive = the other
 * person is signalling romantic/warm interest, negative = distancing/rejection. Hand-authored
 * heuristic for a game-like score, not a psychological measure.
 *
 * Deliberately NOT derived from emotion: sadness or anxiety in the emotion display must not by
 * itself lower affection.
 */
export const RELATIONSHIP_WEIGHT: Record<Relationship, number> = {
  romantic: 1.0,
  warm: 0.55,
  neutral: 0.0,
  distant: -0.45,
  rejecting: -1.0,
};

/**
 * Self-reply quality weight of each class in [0, 1] (1 = ideal reply, 0 = worst). The self-quality
 * score is Σ P(class) * weight(class); `scoring.ts` maps it to SSS/SS/S/A/B/C/D.
 */
export const SELF_QUALITY_WEIGHT: Record<SelfQuality, number> = {
  connected: 1.0,
  empathic: 0.9,
  "off topic": 0.3,
  pressuring: 0.15,
  offensive: 0.0,
};

const RELATIONSHIP_QUESTION: Question = {
  type: "choice",
  instructions:
    "From the sender of the TARGET message toward the recipient, what relationship signal does " +
    "the TARGET message carry? Choose romantic for clear romantic interest, warm for friendly " +
    "affection, neutral for ordinary or task talk, distant for cooling off or emotional " +
    "distance, and rejecting for an explicit refusal or push-away.",
  criteria: [...RELATIONSHIP_OPTIONS],
};

const SELF_QUALITY_QUESTION: Question = {
  type: "choice",
  instructions:
    "The other person sent the PREVIOUS message. The SELF reply is the TARGET. How good is the " +
    "SELF reply as a response to the other person?",
  criteria: {
    connected: "responds directly and keeps the conversation moving naturally",
    empathic: "acknowledges the other person's feelings before anything else",
    "off topic": "ignores or changes the subject away from what the other person said",
    pressuring: "pushes, demands or rushes the other person",
    offensive: "hostile, insulting, contemptuous or rude",
  },
};

/** Three questions run for every OTHER-side target message (affinity + display). */
export const ANALYSIS_QUESTIONS: Record<string, Question> = {
  emotion: EMOTION_QUESTION,
  intent: INTENT_GROUP_QUESTION,
  relationship: RELATIONSHIP_QUESTION,
};

/** Two questions run for SELF-side target messages (display only; relationship is not needed). */
export const DISPLAY_QUESTIONS: Record<string, Question> = {
  emotion: EMOTION_QUESTION,
  intent: INTENT_GROUP_QUESTION,
};

/** Fine message labels use concrete social-emotion words; portrait/self-quality paths keep DISPLAY_QUESTIONS. */
export const FINE_DISPLAY_QUESTIONS: Record<string, Question> = {
  emotion: FINE_EMOTION_QUESTION,
  intent: INTENT_GROUP_QUESTION,
};

/** Question run once for the latest SELF reply (if any). */
export const SELF_QUALITY_QUESTIONS: Record<string, Question> = {
  self_quality: SELF_QUALITY_QUESTION,
};
