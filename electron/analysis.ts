// Written for this project (not vendored).
//
// Model-owned analysis entry point. This is the implementation behind the frozen
// `window.desktop.analyze()` contract. It runs the local Laya multilingual ONNX model on each
// target message (emotion + intent + relationship) and on the latest SELF reply (self-quality),
// then derives the entertainment affinity / grades / fixed next step. No keyword fallback, no
// random scores, no network.
//
// Signal usage (per review):
//   emotion      -> display only
//   intent       -> display only (includes reject / distance)
//   relationship -> the ONLY affinity signal, and only for OTHER-side messages
//   self_quality -> the "我的发挥" grade (SSS/SS/.../D)
//
// `AnalysisResult.grade` is the SELF-quality band, never the affinity band. The affinity band is
// exposed separately as `affinityGrade`.

import { createHash } from "node:crypto";
import { existsSync, readFileSync } from "node:fs";
import path from "node:path";

import type {
  AnalysisRequest,
  AnalysisResult,
  LabelScore,
  Message,
  MessageResult,
  ModelStatus,
  StyleEvidence,
} from "../shared/contracts";
import { LayaAgent } from "./laya/agent";
import { classifyPackedBatch, MESSAGE_BATCH_VERSION,
  type BatchConsumed, type MessageBatchRequest } from "./laya/message-batch";
import type { NodeOnnxRunner } from "./laya/runner";
import { ANALYSIS_QUESTIONS, DISPLAY_QUESTIONS, SELF_QUALITY_QUESTIONS } from "./laya/options";
import { buildIsolatedTargetState, buildTargetState, DEFAULT_CONTEXT_WINDOW, fitTargetStateToBudget, sanitizeContextText } from "./laya/context";
import { buildPrefix, serializeState } from "./laya/prompt";
import { checkedGroupContext, groupTargetState, fitGroupState } from '../shared/group-context';
import { toInternal } from "./laya/questions";
import { renderOptions } from "./laya/questions";
import { CATALOG_VERSION, routeEmotion, routeIntent } from "./laya/catalog";
import { GENERAL_LABEL_SCHEMA, generalIntentQuestion, generalIntentScores } from "./laya/general-intent";
import { generateFineMessageInsight } from "./local-message-insights";
import { groundedIntentWithContext, type GroundedIntent } from "./laya/grounded-intent";
import { classifyNextReply, REPLY_FORECAST_QUESTION, type ReplyForecastCandidate, type ForecastBudget } from "./laya/forecast";
import { MBTI_QUESTION_VERSION, PERSONALITY_QUESTIONS, personalityEvidenceFromAnswers, type PersonalityEvidence } from "./laya/personality";
import { STYLE_QUESTIONS, styleEvidenceFromAnswers } from "./laya/style";
import {
  affinityFromScores,
  argmaxLabel,
  gradeFor,
  meanDistribution,
  messageScore,
  nextStepFor,
  NO_SELF_GRADE,
  selfQualityGrade,
  selfQualityScore,
  type MessageSignal,
} from "./laya/scoring";
import type { AgentConfig, Answer, PredictResult, Question, State } from "./laya/types";

/** Pinned Laya multilingual bundle (see scripts/setup-models.ts for the same pins). */
export const MODEL_REPO = "mizchi/laya-multilingual-onnx";
export const MODEL_REVISION = "d9d003d543e63d6d3375c21d44624136bd1e0bad";
export const MODEL_LABEL = `laya-multilingual-onnx@${MODEL_REVISION.slice(0, 7)}`;

export const MODEL_FILES = {
  model: "model.onnx",
  agentConfig: "rl_agent_config.json",
  onnxConfig: "onnx_config.json",
  tokenizerJson: path.join("tokenizer", "tokenizer.json"),
  tokenizerConfig: path.join("tokenizer", "tokenizer_config.json"),
} as const;

/**
 * One local model instance; questions per forward pass.
 *
 * Raised 2 -> 8 from the isolated release-model benchmark
 * (`docs/plans/2026-09-28-laya-performance.md`): WebGPU reaches ~91 decisions/s at batch 8
 * and ~99 at 16/32, while the 14-question portrait set peaks near batch 8 (~106/s) and
 * regresses at 16. Batch 8 keeps the peak without the larger activation/VRAM footprint of
 * 16/32, and CPU throughput is flat at ~11.6-11.9 either way. This only changes how many
 * prepared questions are collated per `runner.run`; questions, ordering and cache keys are
 * unchanged.
 */
const BATCH_SIZE = 8;
export const MAX_PORTRAIT_CONTEXT_CODEPOINTS = 120;
const MAX_PORTRAIT_CONTEXT_TOKENS = 40;
/** Bounded analysis cache (per cached message / self-quality entry). */
const MAX_CACHE_ENTRIES = 5000;
// This value keys saved portrait progress. Keep it stable for the generic-v4 label rollout;
// per-message labelSchema selects which fine results need an individual refresh.
export const ANALYSIS_VERSION = `${CATALOG_VERSION}+routing-v4-style-v1+${MBTI_QUESTION_VERSION}+message-state-v2+expression-route-v2+social-intent-route-v2`;

const clamp = (value: number, min: number, max: number): number =>
  value < min ? min : value > max ? max : value;

export class ModelMissingError extends Error {
  readonly modelDir: string;
  readonly missing: string[];

  constructor(modelDir: string, missing: string[]) {
    super(
      `缺少本地模型文件: ${missing.join(", ")}。请先运行 npm run setup:models 下载模型（目录: ${modelDir}）`,
    );
    this.name = "ModelMissingError";
    this.modelDir = modelDir;
    this.missing = missing;
  }
}

/** Internal: a load finished after `configureModelDir` / `dispose` changed the generation. */
class SupersededModelLoadError extends Error {
  constructor() {
    super("模型加载已被配置变更取代");
    this.name = "SupersededModelLoadError";
  }
}

/** Per-message result, extended with the relationship distribution (a backward-compatible add). */
export interface AnalyzedMessageResult extends MessageResult {
  /** Empty for SELF messages: relationship is only meaningful for OTHER-side messages. */
  relationship: LabelScore[];
  /** Explicit speech act in this fine-label target text; null means abstain. Never has a score. */
  groundedIntent?: GroundedIntent | null;
}

/** Separate "我的发挥" classification of the latest SELF reply. */
export interface SelfQualityResult {
  /** Id of the SELF message that was graded, or null when there is no SELF reply. */
  messageId: string | null;
  /** Raw argmax self-quality class, or "—" when there is no SELF reply / no model answer. */
  quality: string;
  /** Deterministic score in [0, 1] from the raw class distribution. */
  score: number;
  /** Deterministic grade SSS/SS/S/A/B/C/D, or "—" when there is no SELF reply. */
  grade: string;
  confidence: number;
  /** Raw model class probabilities, preserved for the UI/debugging. */
  probabilities: LabelScore[];
}

/** `AnalysisResult` plus the extra fields the UI may use. Still assignable to `AnalysisResult`. */
export interface EntertainmentAnalysisResult extends AnalysisResult {
  messages: AnalyzedMessageResult[];
  /** Session-level mean relationship distribution over OTHER-side messages. */
  relationship: LabelScore[];
  /** Affinity band (S/A/B/C/D/E) — NOT the same as `grade`. */
  affinityGrade: string;
  selfQuality: SelfQualityResult;
}

/** Minimal engine surface, so tests can inject a fake runner without the ONNX model. */
export interface AnalysisEngine {
  predict(state: State, questions: Record<string, Question>): Promise<PredictResult>;
}

/** Click-triggered future-reply classification. Never writes the per-message analysis cache. */
export async function forecastNextReply(
  messages: readonly Message[],
  draft = "",
): Promise<ReplyForecastCandidate[]> {
  if (!Array.isArray(messages) || messages.length === 0 || messages.length > 16 ||
      messages.some((message) => !message || (message.side !== "self" && message.side !== "other") ||
        typeof message.text !== "string" || !message.text.trim() || message.text.length > 4000) ||
      typeof draft !== "string" || draft.length > 2000) {
    throw new Error("Invalid reply forecast input");
  }
  const engine = await resolveEngine();
  let budget: ForecastBudget | undefined;
  if (loaded && !engineForTest) {
    const { tokenizer, config } = loaded.agent;
    const prefix = buildPrefix(tokenizer, toInternal(REPLY_FORECAST_QUESTION), config.head_max_len);
    budget = {
      maxStateTokens: config.max_len - prefix.ids.length - 1,
      tokenCount: (state) => tokenizer.encode(serializeState(state).replaceAll(tokenizer.maskToken, " ")).length,
    };
  }
  return classifyNextReply({ predict: (state, questions) => predictChecked(engine, state, questions) }, messages, draft, budget);
}

interface LoadedModel {
  agent: LayaAgent;
  runner: NodeOnnxRunner;
}

interface CachedMessageAnalysis {
  emotion: LabelScore[];
  intent: LabelScore[];
  intentBroad: LabelScore[];
  expression: LabelScore[];
  playfulIntent: LabelScore[];
  styleEvidence: StyleEvidence | null;
  relationship: LabelScore[];
  personalityEvidence: PersonalityEvidence | null;
}

/** Simple insertion-ordered LRU with a hard entry cap. */
class BoundedCache<V> {
  private readonly map = new Map<string, V>();
  constructor(private readonly max: number) {}

  get(key: string): V | undefined {
    const value = this.map.get(key);
    if (value === undefined) return undefined;
    // Refresh recency.
    this.map.delete(key);
    this.map.set(key, value);
    return value;
  }

  set(key: string, value: V): void {
    if (this.map.has(key)) this.map.delete(key);
    this.map.set(key, value);
    while (this.map.size > this.max) {
      const oldest = this.map.keys().next().value;
      if (oldest === undefined) break;
      this.map.delete(oldest);
    }
  }

  clearPrefix(prefix: string): void {
    for (const key of this.map.keys()) {
      if (key.startsWith(prefix)) this.map.delete(key);
    }
  }

  clear(): void {
    this.map.clear();
  }
}

const messageCache = new BoundedCache<CachedMessageAnalysis>(MAX_CACHE_ENTRIES);
const selfQualityCache = new BoundedCache<SelfQualityResult>(MAX_CACHE_ENTRIES);
/** Bumped by every cache reset; stale async results are not written after a reset. */
let cacheEpoch = 0;

let modelDirOverride: string | null = null;
let loaded: LoadedModel | null = null;
let loadingPromise: Promise<LoadedModel> | null = null;
let loadProgress: number | undefined;
let lastError: string | null = null;
let requestedRuntimeProvider: "cpu" | "gpu" = "gpu";
/** Bumped whenever the model directory changes or the model is disposed. */
let generation = 0;
/** Injected engine used by tests; never set in production. */
let engineForTest: AnalysisEngine | null = null;

function defaultModelDir(): string {
  const env = process.env.LAYA_MODEL_DIR;
  if (env && env.trim().length > 0) return path.resolve(env.trim());
  // Electron packaged: model ships under <resources>/models/laya. `defaultApp` is true only for
  // `electron .` (development), and absent under plain Node (which has no resourcesPath).
  const proc = process as NodeJS.Process & { resourcesPath?: string; defaultApp?: boolean };
  if (proc.resourcesPath && !proc.defaultApp) {
    return path.join(proc.resourcesPath, "models", "laya");
  }
  return path.resolve(process.cwd(), ".models", "laya");
}

/**
 * Override the directory that holds the Laya bundle. Highest precedence, ahead of
 * `LAYA_MODEL_DIR`; packaged main calls this with its packaged path. Any previously loaded
 * session is released, any in-flight load is invalidated, and the analysis cache is cleared.
 */
export function configureModelDir(dir: string): void {
  if (modelDirOverride === dir) return;
  modelDirOverride = dir;
  const previous = loaded;
  loaded = null;
  loadingPromise = null;
  loadProgress = undefined;
  lastError = null;
  generation++;
  clearAnalysisCache();
  if (previous) void previous.runner.dispose().catch(() => {});
}

/** Resolution order: explicit override -> LAYA_MODEL_DIR -> packaged resources -> cwd/.models. */
export function getModelDir(): string {
  return modelDirOverride ?? defaultModelDir();
}

interface FilesInspection {
  complete: boolean;
  missing: string[];
}

function inspectModelFiles(dir: string): FilesInspection {
  const missing: string[] = [];
  for (const relative of Object.values(MODEL_FILES)) {
    if (!existsSync(path.join(dir, relative))) missing.push(relative);
  }
  return { complete: missing.length === 0, missing };
}

function readyStatus(): ModelStatus {
  const runtime = loaded?.runner.provider === "webgpu" ? "WebGPU" : "CPU，4 线程";
  return {
    state: "ready",
    progress: 1,
    provider: loaded?.runner.provider ?? "cpu",
    message: `${MODEL_LABEL} 已载入（本地 ${runtime}）`,
  };
}

function statusOf(): ModelStatus {
  if (loaded) return readyStatus();
  if (loadingPromise) {
    const status: ModelStatus = { state: "loading", message: "正在载入本地模型…" };
    if (loadProgress !== undefined) status.progress = loadProgress;
    return status;
  }
  if (lastError) return { state: "error", message: lastError };
  const inspection = inspectModelFiles(getModelDir());
  if (!inspection.complete) {
    return {
      state: "missing",
      message: `未找到本地模型，请先运行 npm run setup:models（缺少: ${inspection.missing.join(", ")}）`,
    };
  }
  return { state: "loading", progress: 0, message: "本地模型已下载，等待载入" };
}

/**
 * Load the local bundle exactly once. Concurrent callers share one promise. Missing files raise
 * `ModelMissingError` without touching the network. Race-safe against `configureModelDir` /
 * `disposeAnalysisModel`.
 */
async function ensureModel(): Promise<LoadedModel> {
  if (loaded) return loaded;
  if (loadingPromise) return loadingPromise;
  const dir = getModelDir();
  const inspection = inspectModelFiles(dir);
  if (!inspection.complete) {
    lastError = null;
    throw new ModelMissingError(dir, inspection.missing);
  }
  const myGeneration = generation;
  loadProgress = 0.02;
  lastError = null;
  const holder: { promise: Promise<LoadedModel> | null } = { promise: null };
  holder.promise = (async (): Promise<LoadedModel> => {
    try {
      const agentConfig = JSON.parse(
        readFileSync(path.join(dir, MODEL_FILES.agentConfig), "utf8"),
      ) as AgentConfig;
      const tokenizerJson = JSON.parse(
        readFileSync(path.join(dir, MODEL_FILES.tokenizerJson), "utf8"),
      );
      const tokenizerConfig = JSON.parse(
        readFileSync(path.join(dir, MODEL_FILES.tokenizerConfig), "utf8"),
      );
      // Lazy-load the native runner and the tokenizer only when a real model is actually needed
      // (keeps the injected test engine free of native dependencies).
      const [{ NodeOnnxRunner }, { LayaTokenizer }] = await Promise.all([
        import("./laya/runner"),
        import("./laya/tokenizer"),
      ]);
      loadProgress = 0.1;
      const runner = await NodeOnnxRunner.create(path.join(dir, MODEL_FILES.model), {
        executionProvider: requestedRuntimeProvider === "cpu" ? "cpu" : "auto",
      });
      if (myGeneration !== generation) {
        await runner.dispose().catch(() => {});
        throw new SupersededModelLoadError();
      }
      loadProgress = 0.9;
      const tokenizer = new LayaTokenizer(tokenizerJson, tokenizerConfig);
      const agent = new LayaAgent({
        config: agentConfig,
        tokenizer,
        runner,
        batchSize: BATCH_SIZE,
      });
      loadProgress = 1;
      loaded = { agent, runner };
      return loaded;
    } catch (error) {
      if (!(error instanceof SupersededModelLoadError)) {
        lastError = error instanceof Error ? error.message : String(error);
      }
      throw error;
    } finally {
      if (loadingPromise === holder.promise) loadingPromise = null;
    }
  })();
  loadingPromise = holder.promise;
  return holder.promise;
}

/**
 * Current model lifecycle. Synchronous (main.ts wraps it for the async IPC bridge). When the
 * files are present but the model is not loaded yet, this starts the shared one-time load in
 * the background so that a reported `loading` state is always truthful.
 */
export function getModelStatus(): ModelStatus {
  if (!loaded && !loadingPromise && !engineForTest) {
    const inspection = inspectModelFiles(getModelDir());
    if (inspection.complete && !lastError) {
      void ensureModel().catch(() => {});
    }
  }
  return statusOf();
}

/** Load the local model only. Missing files return `missing` with setup instructions. */
export async function prepareModel(): Promise<ModelStatus> {
  try {
    await ensureModel();
    return readyStatus();
  } catch (error) {
    if (error instanceof ModelMissingError) {
      return { state: "missing", message: error.message };
    }
    if (error instanceof SupersededModelLoadError) return statusOf();
    return {
      state: "error",
      message: error instanceof Error ? error.message : String(error),
    };
  }
}

/** The JSONL server calls this between requests, after the prior target has returned. */
export async function configureAnalysisRuntime(provider: "cpu" | "gpu"): Promise<ModelStatus> {
  if (provider === requestedRuntimeProvider && loaded &&
      (provider === "cpu" || loaded.runner.provider === "webgpu")) return readyStatus();
  requestedRuntimeProvider = provider;
  generation++;
  const previous = loaded;
  const pending = loadingPromise;
  loaded = null;
  loadingPromise = null;
  loadProgress = undefined;
  if (pending) await pending.catch(() => {});
  if (previous) {
    try {
      await previous.runner.dispose();
    } catch (error) {
      lastError = error instanceof Error ? error.message : String(error);
      return { state: "error", message: lastError };
    }
  }
  // Preserve saved and in-memory per-message results across execution providers.
  lastError = null;
  return prepareModel();
}

/** Release the ONNX session and invalidate any in-flight load (mainly tests / shutdown). */
export async function disposeAnalysisModel(): Promise<void> {
  generation++;
  const current = loaded;
  loaded = null;
  loadingPromise = null;
  loadProgress = undefined;
  clearAnalysisCache();
  if (current) await current.runner.dispose();
}

// ---------------------------------------------------------------------------
// Analysis cache
// ---------------------------------------------------------------------------

/**
 * Test-only seam: inject a fake engine (e.g. a scripted runner) so the cache and request logic
 * can be exercised without the 647 MB model. Passing null restores the real model. Always
 * clears the cache. There is no production code path that sets this.
 */
export function __setAnalysisEngineForTest(engine: AnalysisEngine | null): void {
  engineForTest = engine;
  clearAnalysisCache();
}

/** Clear the whole analysis cache (also called on model reconfiguration/disposal). */
export function clearAnalysisCache(): void {
  cacheEpoch++;
  messageCache.clear();
  selfQualityCache.clear();
}

/** Forget the cached analyses for one session (or all sessions when omitted). */
export function resetAnalysisSession(sessionId?: string): void {
  cacheEpoch++;
  if (sessionId === undefined) {
    messageCache.clear();
    selfQualityCache.clear();
    return;
  }
  const prefix = `${sessionId}\u0001`;
  messageCache.clearPrefix(prefix);
  selfQualityCache.clearPrefix(prefix);
}

/**
 * Cache key: session id + message id + side + text + the contents of up to the 6 preceding
 * messages. Any change to the message or its context changes the key and forces a re-analysis;
 * unrelated messages keep their cached results.
 */
function analysisKey(
  sessionId: string,
  messages: readonly Message[],
  index: number,
  portraitContext?: string,
  messageLabelsOnly = false,
): string {
  const start = Math.max(0, index - DEFAULT_CONTEXT_WINDOW);
  const parts = [ANALYSIS_VERSION, sessionId, messages[index]!.id, messages[index]!.side, messages[index]!.text];
  for (let j = start; j < index; j++) parts.push(messages[j]!.side, messages[j]!.text);
  if (portraitContext) parts.push("portrait", portraitContext);
  if (messageLabelsOnly) parts.push("message-labels-only", GENERAL_LABEL_SCHEMA);
  if(messages[index]?.groupContext) parts.push('qq-group-role-v1',JSON.stringify(messages.slice(start,index+1).map(item=>item.groupContext)));
  const digest = createHash("sha1").update(parts.join("\u0000")).digest("hex");
  return `${sessionId}\u0001${digest}`;
}

async function resolveEngine(): Promise<AnalysisEngine> {
  if (engineForTest) return engineForTest;
  const model = await ensureModel();
  return model.agent;
}

function toLabelScores(answer: Answer | undefined): LabelScore[] {
  if (!answer || answer.type !== "choice") return [];
  return Object.entries(answer.probabilities)
    .map(([label, probability]) => ({ label, probability }))
    .sort((a, b) => b.probability - a.probability);
}

async function predictChecked(
  engine: AnalysisEngine,
  state: State,
  questions: Record<string, Question>,
  hasPortrait = false,
): Promise<PredictResult> {
  let fittedState = state;
  if (loaded && !engineForTest) {
    const { tokenizer, config } = loaded.agent;
    let maxStateTokens = config.max_len;
    for (const [name, definition] of Object.entries(questions)) {
      const question = toInternal(definition);
      const headTokens = tokenizer.encode(`${question.t} question: ${question.ins.replaceAll(tokenizer.maskToken, " ")}`).length;
      const options = renderOptions(question);
      const optionSizes = options.map((option) => tokenizer.encode(" " + option.replaceAll(tokenizer.maskToken, " ")).length);
      if (optionSizes.some((size) => size > 48) || headTokens + optionSizes.reduce((sum, size) => sum + size + 1, 0) + 3 > config.head_max_len) {
        throw new Error(`Question ${name} exceeds Laya head token budget`);
      }
      const prefix = buildPrefix(tokenizer, question, config.head_max_len);
      maxStateTokens = Math.min(maxStateTokens, config.max_len - prefix.ids.length - 1);
    }
    const tokenCount = (value: State) => tokenizer.encode(serializeState(value).replaceAll(tokenizer.maskToken, " ")).length;
    if (tokenCount(fittedState) > maxStateTokens) {
      if (typeof state === "string") {
        fittedState = state.toString().startsWith('QQ_GROUP_TARGET_V1\n') ?
          fitGroupState(String(state),maxStateTokens,value=>tokenCount(value)) :
          fitTargetStateToBudget(state, maxStateTokens, tokenCount, hasPortrait);
      }
      if (tokenCount(fittedState) > maxStateTokens) {
        throw new ObservedTextTooLongError(tokenCount(fittedState) + config.max_len - maxStateTokens, config.max_len);
      }
    }
  }
  return engine.predict(fittedState, questions);
}

/** Bounded preceding-message slice for fine candidate retrieval; never scans history. */
const FINE_HINT_MESSAGES = 3;
const FINE_HINT_CODEPOINTS = 240;
function fineContextHint(messages: readonly Message[], index: number): string {
  const parts: string[] = [];
  for (let i = Math.max(0, index - FINE_HINT_MESSAGES); i < index; i++) {
    const text = messages[i]?.text;
    if (typeof text === "string" && text.trim()) parts.push(sanitizeContextText(text));
  }
  return Array.from(parts.join("\n")).slice(0, FINE_HINT_CODEPOINTS).join("");
}

async function classifyMessage(engine: AnalysisEngine, state: State, side: Message["side"], targetText: string,
  _batchIndependentExpression = false, hasPortrait = false, messageLabelsOnly = false,
  contextHint = "", portraitTarget = false, relationshipApplicable = true): Promise<CachedMessageAnalysis> {
  const predictForState = (questions: Record<string, Question>) =>
    predictChecked(engine, state, questions, hasPortrait);
  if (messageLabelsOnly) {
    // The fine label path is its own module. analysis.ts only adapts its result back to the
    // existing CachedMessageAnalysis DTO; model/runtime/cache ownership stays here.
    const fine = await generateFineMessageInsight(
      (questions) => predictForState(questions).then((prediction) => prediction.answers),
      side, targetText, contextHint, relationshipApplicable);
    return {
      emotion: fine.emotion,
      intent: fine.intent,
      intentBroad: fine.intentBroad,
      expression: [],
      playfulIntent: [],
      styleEvidence: null,
      relationship: fine.relationship,
      personalityEvidence: null,
    };
  }
  const questions = side === "other" && relationshipApplicable
    ? { ...ANALYSIS_QUESTIONS, ...PERSONALITY_QUESTIONS, ...STYLE_QUESTIONS }
    : { ...DISPLAY_QUESTIONS, ...(portraitTarget ? { ...PERSONALITY_QUESTIONS, ...STYLE_QUESTIONS } : {}) };
  const prediction = await predictForState(questions);
  // Portrait state is already persisted under this analysis version. Preserve its 40-emotion /
  // 547-intent dimensions so new portrait batches merge with old ones without mixing schemas.
  const routed = await routeIntent(prediction.answers.intent, (detailQuestions) =>
    predictForState(detailQuestions).then((detail) => detail.answers));
  const emotion = await routeEmotion(prediction.answers.emotion, (detailQuestions) =>
    predictForState(detailQuestions).then((detail) => detail.answers));
  return {
    emotion,
    intent: routed.scores,
    intentBroad: toLabelScores(prediction.answers.intent),
    expression: [],
    playfulIntent: [],
    styleEvidence: side === "other" || portraitTarget ? styleEvidenceFromAnswers(prediction.answers) : null,
    relationship: side === "other" && relationshipApplicable ? toLabelScores(prediction.answers.relationship) : [],
    personalityEvidence: side === "other" || portraitTarget ? personalityEvidenceFromAnswers(prediction.answers) : null,
  };
}

export interface AggregateMessageBatchResult {
  analysisVersion: string;
  batchVersion: string;
  consumed: BatchConsumed[];
  contextTrimmed: boolean;
  targetSide: "self" | "other" | "mixed" | null;
  result: (CachedMessageAnalysis & { score: number | null }) | null;
  durationMs: number;
}

/** Classify the packed TARGET span once; context records are never separately classified. */
export async function analyzeMessageBatch(request: MessageBatchRequest,
  batchIndependentExpression = true): Promise<AggregateMessageBatchResult> {
  const startedAt = Date.now();
  const { agent } = await ensureModel();
  const { tokenizer, config } = agent;
  // A routed question may use the full head budget plus framing tokens. Reserve that space so
  // LayaAgent.buildSequence cannot silently truncate any consumed message content.
  const maxStateTokens = config.max_len - config.head_max_len - 4;
  const countTokens = (state: string) =>
    tokenizer.encode(state.replaceAll(tokenizer.maskToken, " ")).length;
  const { packed, result } = await classifyPackedBatch(request, countTokens, maxStateTokens,
    async batch => {
      const side = batch.targetSide === "self" ? "self" : "other";
      const classified = await classifyMessage(agent, batch.state, side, batch.targetText,
        batchIndependentExpression, false, false, "", true,request.conversationKind!=='group');
      if (!classified.emotion.length || !classified.intent.length ||
          (side === "other" && request.conversationKind!=='group' && !classified.relationship.length)) {
        throw new Error("invalid aggregate model result");
      }
      const score = side === "other" && request.conversationKind!=='group' ? messageScore({ relationship:
        Object.fromEntries(classified.relationship.map(item => [item.label, item.probability])) }) : null;
      return { ...classified, score };
    });
  return { analysisVersion: ANALYSIS_VERSION, batchVersion: MESSAGE_BATCH_VERSION,
    consumed: packed.consumed, contextTrimmed: packed.contextTrimmed,
    targetSide: packed.targetSide, result, durationMs: Date.now() - startedAt };
}

function toProbabilityMap(scores: LabelScore[]): Record<string, number> {
  const out: Record<string, number> = {};
  for (const { label, probability } of scores) out[label] = probability;
  return out;
}

function toLabelScoresFromMean(mean: Record<string, number>): LabelScore[] {
  return Object.entries(mean)
    .map(([label, probability]) => ({ label, probability }))
    .sort((a, b) => b.probability - a.probability);
}

function validateRequest(request: AnalysisRequest): Message[] {
  if (!request || typeof request !== "object") throw new Error("Analysis request must be an object");
  if (typeof request.sessionId !== "string" || request.sessionId.length === 0) {
    throw new Error("Analysis request is missing a sessionId");
  }
  const messages = request.messages;
  if (!Array.isArray(messages)) throw new Error("Analysis request messages must be an array");
  messages.forEach((message, i) => {
    if (!message || typeof message !== "object") throw new Error(`Message ${i} is not an object`);
    if (typeof message.id !== "string") throw new Error(`Message ${i} is missing an id`);
    if (message.side !== "self" && message.side !== "other") {
      throw new Error(`Message ${i} has an invalid side`);
    }
    if (typeof message.text !== "string") throw new Error(`Message ${i} is missing text`);
  });
  return messages;
}

function lastSelfIndex(messages: readonly Message[]): number {
  for (let i = messages.length - 1; i >= 0; i--) {
    if (messages[i]!.side === "self") return i;
  }
  return -1;
}

function noSelfQuality(): SelfQualityResult {
  return {
    messageId: null,
    quality: NO_SELF_GRADE,
    score: 0,
    grade: NO_SELF_GRADE,
    confidence: 0,
    probabilities: [],
  };
}

/**
 * Classify the SELF reply at `index` with its real preceding context. Shared by
 * `analyzeConversation` (latest SELF of the full history) and `analyzeMessageTargets` (latest
 * SELECTED SELF). Never called for a message that is only context.
 */
async function classifySelfQualityAt(
  engine: AnalysisEngine,
  sessionId: string,
  messages: readonly Message[],
  index: number,
  epoch: number,
): Promise<SelfQualityResult> {
  const key = analysisKey(sessionId, messages, index);
  const cached = selfQualityCache.get(key);
  if (cached) return cached;

  const state = buildTargetState(messages, index);
  const prediction = await predictChecked(engine, state, SELF_QUALITY_QUESTIONS);
  const answer = prediction.answers["self_quality"];
  const probabilities = toLabelScores(answer);
  const quality = probabilities[0]?.label ?? NO_SELF_GRADE;
  const score = selfQualityScore(toProbabilityMap(probabilities));
  const result: SelfQualityResult = {
    messageId: messages[index]!.id,
    quality,
    score,
    grade: probabilities.length === 0 ? NO_SELF_GRADE : selfQualityGrade(score),
    confidence: answer && answer.type === "choice" ? answer.confidence : 0,
    probabilities,
  };
  if (epoch === cacheEpoch) selfQualityCache.set(key, result);
  return result;
}

async function classifySelfQuality(
  engine: AnalysisEngine,
  sessionId: string,
  messages: readonly Message[],
  epoch: number,
): Promise<SelfQualityResult> {
  const index = lastSelfIndex(messages);
  if (index < 0) return noSelfQuality();
  return classifySelfQualityAt(engine, sessionId, messages, index, epoch);
}

function readPreviousAffinity(request: AnalysisRequest): number | undefined {
  return typeof request.previousAffinity === "number" && Number.isFinite(request.previousAffinity)
    ? request.previousAffinity
    : undefined;
}

/** One analyzed result paired with the side of the message it came from. */
interface OutcomeEntry {
  result: AnalyzedMessageResult;
  side: Message["side"];
}

/**
 * Shared outcome computation over a set of analyzed results (full history, or selected targets).
 * - Affinity aggregates ONLY OTHER-side relationship results; empty -> previous (clamped) or 50.
 * - `delta` = affinity - previousAffinity (0 when none), so repeated calls stay idempotent.
 * - `nextStep` follows the latest OTHER-side entry; `grade` follows the returned selfQuality.
 */
function buildOutcome(
  sessionId: string,
  startedAt: number,
  entries: readonly OutcomeEntry[],
  selfQuality: SelfQualityResult,
  previousAffinity: number | undefined,
): EntertainmentAnalysisResult {
  const otherSignals: MessageSignal[] = entries
    .filter((entry) => entry.side === "other")
    .map((entry) => ({ relationship: toProbabilityMap(entry.result.relationship) }));
  const scores = otherSignals.map((signal) => messageScore(signal));
  const affinity =
    scores.length === 0
      ? previousAffinity !== undefined
        ? Math.round(clamp(previousAffinity, 0, 100))
        : 50
      : affinityFromScores(scores).affinity;
  const delta =
    previousAffinity === undefined ? 0 : affinity - Math.round(clamp(previousAffinity, 0, 100));

  let dominantRelationship: string | undefined;
  let dominantIntent: string | undefined;
  for (let i = entries.length - 1; i >= 0; i--) {
    if (entries[i]!.side !== "other") continue;
    dominantRelationship = argmaxLabel(toProbabilityMap(entries[i]!.result.relationship));
    const leafIntent = argmaxLabel(toProbabilityMap(entries[i]!.result.intent));
    const group = leafIntent?.split("_")[0];
    dominantIntent = leafIntent === "invite" ? "make plan"
      : leafIntent === "show_affection" ? "flirt"
      : leafIntent === "ask_question" ? "ask question"
      : leafIntent === "seek_help" ? "seek comfort"
      : leafIntent === "give_comfort" ? "give comfort"
      : group === "rejection" ? "reject"
      : group === "boundary" ? "distance"
      : group === "flirt" ? "flirt"
      : group === "planning" || group === "invitation" ? "make plan"
      : leafIntent === "help_request_comfort" ? "seek comfort"
      : group === "support" ? "give comfort"
      : group === "inquiry" ? "ask question"
      : leafIntent;
    break;
  }

  return {
    sessionId,
    affinity,
    delta,
    grade: selfQuality.grade,
    nextStep: nextStepFor(affinity, dominantRelationship, dominantIntent, selfQuality.quality),
    messages: entries.map((entry) => entry.result),
    model: MODEL_LABEL,
    analysisVersion: ANALYSIS_VERSION,
    durationMs: Date.now() - startedAt,
    relationship: toLabelScoresFromMean(
      meanDistribution(otherSignals.map((signal) => signal.relationship)),
    ),
    affinityGrade: gradeFor(affinity),
    selfQuality,
  };
}

/**
 * Analyze the full history of one session with the real model.
 * - Every request message is returned (all ids), reusing cached per-message results.
 * - Affinity aggregates ONLY OTHER-side relationship results; with no OTHER message it keeps the
 *   provided previous affinity or the neutral 50.
 * - `delta` is `affinity - previousAffinity` (0 when none), so a repeated identical request is
 *   idempotent and never re-blends.
 * - `grade` is the separate self-quality band; the latest OTHER message drives `nextStep`.
 */
export async function analyzeConversation(
  request: AnalysisRequest,
): Promise<EntertainmentAnalysisResult> {
  const startedAt = Date.now();
  const messages = validateRequest(request);
  const engine = await resolveEngine();
  const sessionId = request.sessionId;
  const epoch = cacheEpoch;

  const entries: OutcomeEntry[] = [];
  for (let i = 0; i < messages.length; i++) {
    const message = messages[i]!;
    const key = analysisKey(sessionId, messages, i);
    let cached = messageCache.get(key);
    if (!cached) {
      const state = buildTargetState(messages, i);
      cached = await classifyMessage(engine, state, message.side, message.text);
      if (epoch === cacheEpoch) messageCache.set(key, cached);
    }
    entries.push({
      result: {
        messageId: message.id,
        emotion: cached.emotion,
        intent: cached.intent,
        intentBroad: cached.intentBroad,
        expression: cached.expression,
        playfulIntent: cached.playfulIntent,
        styleEvidence: cached.styleEvidence,
        relationship: cached.relationship,
        personalityEvidence: cached.personalityEvidence,
      },
      side: message.side,
    });
  }

  const selfQuality = await classifySelfQuality(engine, sessionId, messages, epoch);
  return buildOutcome(sessionId, startedAt, entries, selfQuality, readPreviousAffinity(request));
}

/** Additive request: `messages` is context plus targets; only `targetIds` are analyzed. */
export type TargetAnalysisRequest = AnalysisRequest & {
  targetIds: string[];
  previousSelfQuality?: SelfQualityResult;
  /** Bounded saved portrait for the specified sender; a weak prior, never a history dump. */
  portraitContext?: string;
  portraitContextSide?: "self" | "other";
  conversationKind?: 'friend'|'group';
  /** Fine chat labels only; the separate batch portrait already owns style and MBTI evidence. */
  messageLabelsOnly?: boolean;
};

function assertTargetIds(request: TargetAnalysisRequest): Set<string> {
  const targetIds = request.targetIds;
  if (!Array.isArray(targetIds)) {
    throw new Error("TargetAnalysisRequest.targetIds must be an array");
  }
  const targetSet = new Set<string>();
  for (const id of targetIds) {
    if (typeof id !== "string" || id.length === 0) {
      throw new Error("targetIds must be non-empty strings");
    }
    if (targetSet.has(id)) throw new Error(`Duplicate target id: ${JSON.stringify(id)}`);
    targetSet.add(id);
  }
  return targetSet;
}

/**
 * Additive incremental analysis: analyze ONLY the messages named in `targetIds`, using the rest of
 * `request.messages` purely as preceding context. Context-only messages are never classified.
 *
 * - `result.messages` contains exactly the targets, in `request.messages` order.
 * - Duplicate target ids, unknown target ids and ambiguous duplicate message ids are rejected.
 * - Empty target list performs no inference: affinity preserves `previousAffinity` (clamped) or 50,
 *   and self-quality is `previousSelfQuality` or the NO_SELF shape.
 * - SELF_QUALITY is predicted only for the latest selected SELF (with its real context). With no
 *   selected SELF, `previousSelfQuality` is returned (or NO_SELF).
 * - Shares `analyzeConversation`'s engine, model, caches and questions; it never loads a SECOND
 *   model (the one shared singleton may be loaded by `resolveEngine` on first use).
 */
export async function analyzeMessageTargets(
  request: TargetAnalysisRequest,
): Promise<EntertainmentAnalysisResult> {
  const startedAt = Date.now();
  const messages = validateRequest(request);
  const targetSet = assertTargetIds(request);
  const previousAffinity = readPreviousAffinity(request);
  if (request.messageLabelsOnly !== undefined && typeof request.messageLabelsOnly !== "boolean") {
    throw new Error("Invalid messageLabelsOnly");
  }
  const messageLabelsOnly = request.messageLabelsOnly === true;

  const seenIds = new Set<string>();
  messages.forEach((message, index) => {
    if (seenIds.has(message.id)) {
      throw new Error(
        `Ambiguous duplicate message id at index ${index}: ${JSON.stringify(message.id)}`,
      );
    }
    seenIds.add(message.id);
  });
  for (const id of targetSet) {
    if (!seenIds.has(id)) throw new Error(`Unknown target id: ${JSON.stringify(id)}`);
  }

  const targetIndices: number[] = [];
  messages.forEach((message, index) => {
    if (targetSet.has(message.id)) targetIndices.push(index);
  });

  let portraitContext: string | undefined;
  const grouped=request.conversationKind==='group';
  if(grouped) {
    for(const message of messages) checkedGroupContext(message.groupContext);
    if(request.portraitContext) throw new Error('group labels cannot borrow a portrait prior');
  }
  if (request.portraitContext !== undefined) {
    if (typeof request.portraitContext !== "string") throw new Error("Invalid portraitContext");
    const clean = sanitizeContextText(request.portraitContext);
    if (clean) {
      if (targetIndices.length !== 1 || messages[targetIndices[0]!]!.side !== (request.portraitContextSide ?? "other")) {
        throw new Error("portraitContext requires one OTHER target");
      }
      if (Array.from(clean).length <= MAX_PORTRAIT_CONTEXT_CODEPOINTS) portraitContext = clean;
    }
  }

  if (targetIndices.length === 0) {
    // No inference at all: do not resolve (or load) the engine.
    const selfQuality = request.previousSelfQuality ?? noSelfQuality();
    return buildOutcome(request.sessionId, startedAt, [], selfQuality, previousAffinity);
  }

  const engine = await resolveEngine();
  if (portraitContext && loaded && !engineForTest &&
      loaded.agent.tokenizer.encode(portraitContext.replaceAll(loaded.agent.tokenizer.maskToken, " ")).length >
      MAX_PORTRAIT_CONTEXT_TOKENS) {
    portraitContext = undefined;
  }
  const sessionId = request.sessionId;
  const epoch = cacheEpoch;

  const entries: OutcomeEntry[] = [];
  for (const index of targetIndices) {
    const message = messages[index]!;
    const key = analysisKey(sessionId, messages, index, portraitContext, messageLabelsOnly);
    let cached = messageCache.get(key);
    if (!cached) {
      const state = grouped ? groupTargetState(messages,index) : buildTargetState(messages, index, DEFAULT_CONTEXT_WINDOW, portraitContext);
      const contextHint = messageLabelsOnly ? fineContextHint(messages, index) : "";
      cached = await classifyMessage(engine, state, message.side, message.text,
        false, !!portraitContext, messageLabelsOnly, contextHint, false, !grouped);
      if (epoch === cacheEpoch) messageCache.set(key, cached);
    }
    entries.push({
      result: {
        messageId: message.id,
        emotion: cached.emotion,
        intent: cached.intent,
        intentBroad: cached.intentBroad,
        ...(messageLabelsOnly ? { groundedIntent: groundedIntentWithContext(message.text, fineContextHint(messages, index)) } : {}),
        expression: cached.expression,
        playfulIntent: cached.playfulIntent,
        styleEvidence: cached.styleEvidence,
        relationship: cached.relationship,
        personalityEvidence: cached.personalityEvidence,
      },
      side: message.side,
    });
  }

  // SELF_QUALITY only when a selected target is SELF; use the latest selected SELF.
  let latestSelfTargetIndex = -1;
  for (const index of targetIndices) {
    if (messages[index]!.side === "self") latestSelfTargetIndex = index;
  }
  const selfQuality =
    latestSelfTargetIndex >= 0 && !messageLabelsOnly
      ? await classifySelfQualityAt(engine, sessionId, messages, latestSelfTargetIndex, epoch)
      : request.previousSelfQuality ?? noSelfQuality();

  return buildOutcome(sessionId, startedAt, entries, selfQuality, previousAffinity);
}

/**
 * Experiment-only multi-judgment entry: keep the current Laya engine/state builder,
 * but let a local reference rule library provide the emotion/intent choice criteria.
 * It does not write caches or relationship/portrait state.
 */
export type CustomLabelQuestions = Record<string, Question>;

export interface CustomMessageLabelResult {
  messageId: string;
  answers: Record<string, LabelScore[]>;
}

export async function analyzeMessageTargetsWithQuestions(
  request: TargetAnalysisRequest,
  questions: CustomLabelQuestions,
): Promise<{ analysisVersion: string; durationMs: number; messages: CustomMessageLabelResult[] }> {
  const startedAt = Date.now();
  const messages = validateRequest(request);
  const targetSet = assertTargetIds(request);
  const targetIndices = messages.flatMap((message, index) => targetSet.has(message.id) ? [index] : []);
  if (targetIndices.length === 0) {
    return { analysisVersion: ANALYSIS_VERSION, durationMs: Date.now() - startedAt, messages: [] };
  }
  const engine = await resolveEngine();
  const results: CustomMessageLabelResult[] = [];
  for (const index of targetIndices) {
    const state = buildTargetState(messages, index, DEFAULT_CONTEXT_WINDOW);
    const prediction = await predictChecked(engine, state, questions);
    const answers: Record<string, LabelScore[]> = {};
    for (const key of Object.keys(questions)) answers[key] = toLabelScores(prediction.answers[key]);
    results.push({ messageId: messages[index]!.id, answers });
  }
  return { analysisVersion: ANALYSIS_VERSION, durationMs: Date.now() - startedAt, messages: results };
}

/** Coarse character guard; the real limit is the model token budget below. */
export const MAX_OBSERVED_CHARS = 4000;

/** Typed error: the observed text does not fit the model's real token budget. */
export class ObservedTextTooLongError extends Error {
  constructor(readonly tokens: number, readonly maxLen: number) {
    super(`observed text needs ${tokens} tokens > max_len ${maxLen}`);
    this.name = "ObservedTextTooLongError";
  }
}

/** Display-only emotion/intent for one observed, UNATTRIBUTED message text. */
export interface ObservedTextResult {
  emotion: LabelScore[];
  intent: LabelScore[];
  intentBroad: LabelScore[];
  expression: LabelScore[];
  playfulIntent: LabelScore[];
  styleEvidence: StyleEvidence | null;
}

/**
 * Display-only analysis for the WeChat module's observed text.
 *
 * The sender is unknown, so the model receives only the message text (no inferred speaker).
 * It returns ONLY emotion/intent probabilities; it never produces relationship, affinity or
 * self-quality, and it never fabricates a score when the model gives nothing. Shares the singleton
 * engine, model and DISPLAY_QUESTIONS with the rest of the app. The SerialModelLane serialises
 * calls at the IPC layer. Text is sanitised and length-capped; over-long input throws rather than
 * being truncated silently.
 */
export async function analyzeObservedText(text: string): Promise<ObservedTextResult> {
  const clean = sanitizeContextText(typeof text === "string" ? text : "");
  if (clean.length === 0) return { emotion: [], intent: [], intentBroad: [], expression: [], playfulIntent: [], styleEvidence: null };
  if (clean.length > MAX_OBSERVED_CHARS) {
    throw new ObservedTextTooLongError(clean.length, MAX_OBSERVED_CHARS);
  }
  const engine = await resolveEngine();
  const state = buildIsolatedTargetState(clean);
  const intentQuestion = generalIntentQuestion(clean);
  // Reject on the REAL tokenizer/config (never a character count): the whole prompt for every
  // DISPLAY question must fit max_len, or the model would silently truncate it.
  if (loaded) {
    const tokenizer = loaded.agent.tokenizer;
    const config = loaded.agent.config;
    const stateTokens = tokenizer
      .encode(serializeState(state).replaceAll(tokenizer.maskToken, " "))
      .length;
    for (const definition of [DISPLAY_QUESTIONS.emotion, intentQuestion.question]) {
      const question = toInternal(definition);
      const prefix = buildPrefix(tokenizer, question, config.head_max_len);
      const needed = prefix.ids.length + stateTokens + 1;
      if (needed > config.max_len) {
        throw new ObservedTextTooLongError(needed, config.max_len);
      }
    }
  }
  const prediction = await predictChecked(engine, state, { ...DISPLAY_QUESTIONS, intent: intentQuestion.question });
  const generalIntent = generalIntentScores(prediction.answers.intent);
  const emotion = toLabelScores(prediction.answers.emotion);
  return {
    emotion,
    intent: generalIntent,
    intentBroad: generalIntent,
    expression: [],
    playfulIntent: [],
    styleEvidence: null,
  };
}
