// Written for this project (not vendored): aggregation entry point for the laya runtime.
// The vendored modules are re-exported unchanged; `runner.ts`, `options.ts`, `scoring.ts` and
// `context.ts` are project-specific additions (see README.md).

export { LayaAgent, type LayaAgentOptions, type Runner } from "./agent";
export {
  argmax,
  confidenceFromProbs,
  formatAnswers,
  round4,
  softmax,
  tempBucket,
} from "./calibration";
export type { FormatInput } from "./calibration";
export { buildPrefix, buildSequence, collate, serializeState } from "./prompt";
export { pyJson } from "./pyjson";
export { renderCriterion, renderOptions, toInternal } from "./questions";
export { LayaTokenizer } from "./tokenizer";
export type { TokenizerConfig, TokenizerJson } from "./tokenizer";
export * from "./types";
export { NodeOnnxRunner, type NodeRunnerOptions } from "./runner";
export {
  ANALYSIS_QUESTIONS,
  DISPLAY_QUESTIONS,
  FINE_DISPLAY_QUESTIONS,
  FINE_EMOTION_OPTIONS,
  FINE_EMOTION_QUESTION,
  EMOTION_OPTIONS,
  INTENT_OPTIONS,
  RELATIONSHIP_OPTIONS,
  RELATIONSHIP_WEIGHT,
  SELF_QUALITY_OPTIONS,
  SELF_QUALITY_QUESTIONS,
  SELF_QUALITY_WEIGHT,
  type Emotion,
  type Intent,
  type Relationship,
  type SelfQuality,
} from "./options";
export {
  affinityFromScores,
  argmaxLabel,
  gradeFor,
  meanDistribution,
  messageScore,
  nextStepFor,
  NO_SELF_GRADE,
  recencyWeights,
  selfQualityGrade,
  selfQualityScore,
  type AffinityResult,
  type MessageSignal,
  type RelationshipLabel,
} from "./scoring";
export {
  buildTargetState,
  DEFAULT_CONTEXT_WINDOW,
  sanitizeContextText,
  type ContextMessage,
} from "./context";
