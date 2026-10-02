// Written for this project. A future-reply classification, separate from message analysis.
// Laya ranks fixed reply-purpose options; it does not generate the other person's words.

import type { Message } from "../../shared/contracts";
import { intentLabel } from "../../src/lib/labels";
import { sanitizeContextText } from "./context";
import { INTENT_FAMILIES } from "./catalog";
import type { Answer, PredictResult, Question, State } from "./types";

export const MAX_FORECAST_CONTEXT_MESSAGES = 8;

export const REPLY_FORECAST_QUESTION: Question = {
  type: "choice",
  instructions:
    "Predict the OTHER person's NEXT reply after the conversation and optional SELF draft. " +
    "If a SELF draft is present, assume it will be sent. Judge the likely purpose of that future reply, " +
    "not the purpose or emotion of any message already shown. Choose one reply purpose.",
  criteria: INTENT_FAMILIES.map((family) => family.modelLabel),
};

const DESCRIPTIONS: Record<(typeof INTENT_FAMILIES)[number]["id"], string> = {
  small_talk: "可能继续简单寒暄",
  share_news: "可能补充近况或信息",
  ask_question: "可能继续追问细节",
  seek_comfort: "可能表达需要支持",
  give_comfort: "可能回应关心或安慰",
  make_plan: "可能讨论接下来的安排",
  flirt: "可能表达亲近或暧昧",
  complain: "可能表达不满",
  apologize: "可能表达歉意",
  joke: "可能用玩笑回应",
  reject: "可能明确拒绝",
  distance: "可能暂时拉开距离",
};

export interface ReplyForecastCandidate {
  id: string;
  label: string;
  probability: number;
  description: string;
}

export interface ForecastEngine {
  predict(state: State, questions: Record<string, Question>): Promise<PredictResult>;
}

export interface ForecastBudget {
  maxStateTokens: number;
  tokenCount: (state: State) => number;
}

export class ForecastInputTooLongError extends Error {
  constructor() {
    super("The latest message and draft exceed the local model token budget");
    this.name = "ForecastInputTooLongError";
  }
}

/** Preserve the draft and newest message; discard older context until the real tokenizer fits. */
export function buildReplyForecastState(
  messages: readonly Message[],
  draft = "",
  budget?: ForecastBudget,
): State {
  if (messages.length === 0) throw new Error("Reply forecast requires a recent conversation");
  const recent = messages.slice(-MAX_FORECAST_CONTEXT_MESSAGES).map((message) => ({
    speaker: message.side === "self" ? "me" : "them",
    message: sanitizeContextText(message.text),
  }));
  if (recent.some((message) => !message.message)) throw new Error("Reply forecast received an empty message");
  const cleanDraft = sanitizeContextText(draft);
  const state = (): State => ({
    next_speaker: "them",
    context_before: recent,
    ...(cleanDraft ? { self_draft: cleanDraft } : {}),
  });
  while (budget && budget.tokenCount(state()) > budget.maxStateTokens && recent.length > 1) {
    recent.shift();
  }
  const result = state();
  if (budget && budget.tokenCount(result) > budget.maxStateTokens) throw new ForecastInputTooLongError();
  return result;
}

/** Return exactly three raw model-ranked choices, with fixed descriptions of their class. */
export async function classifyNextReply(
  engine: ForecastEngine,
  messages: readonly Message[],
  draft = "",
  budget?: ForecastBudget,
): Promise<ReplyForecastCandidate[]> {
  const state = buildReplyForecastState(messages, draft, budget);
  const prediction = await engine.predict(state, { reply_intent: REPLY_FORECAST_QUESTION });
  const answer: Answer | undefined = prediction.answers.reply_intent;
  if (!answer || answer.type !== "choice") throw new Error("Reply forecast returned no choice distribution");
  const candidates = INTENT_FAMILIES.map((family, index) => {
    const probability = answer.probabilities[family.modelLabel];
    if (typeof probability !== "number" || !Number.isFinite(probability) || probability < 0 || probability > 1) {
      throw new Error(`Invalid reply forecast probability for ${family.id}`);
    }
    return {
      id: family.id,
      label: intentLabel(family.id),
      probability,
      description: DESCRIPTIONS[family.id],
      index,
    };
  });
  candidates.sort((left, right) => right.probability - left.probability || left.index - right.index);
  return candidates.slice(0, 3).map(({ index: _index, ...candidate }) => candidate);
}
