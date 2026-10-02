// Written for this project (not vendored).
// Target-message-first context construction. No model dependency, so it can be unit tested
// without inference. `ContextMessage` is structurally identical to the frozen contract's
// `Message`, so callers can pass `Message[]` directly without this module importing it.

import { pyJson } from "./pyjson";

export interface ContextMessage {
  id: string;
  side: "self" | "other";
  text: string;
  time: number;
}

/** How many preceding messages are included as context for a target message. */
export const DEFAULT_CONTEXT_WINDOW = 6;

function roleLabel(side: ContextMessage["side"]): string {
  return side === "self" ? "me" : "them";
}

/** Normalize line endings, collapse runs of blank lines, trim. Keeps the original wording. */
export function sanitizeContextText(text: string): string {
  return text.replace(/\r\n?/g, "\n").replace(/\n{3,}/g, "\n\n").trim();
}

/** Match Laya's object-state serialization for a message without conversation context. */
export function buildIsolatedTargetState(text: string): string {
  return pyJson({ message: sanitizeContextText(text) }, { ensureAscii: false });
}

const CONTEXT_MARKER = "\n\nRecent context before the target (oldest first):\n";
const PORTRAIT_MARKER = "\n\nSaved portrait (weak prior; target text wins; MBTI is not intent): ";
const LEGACY_TARGET_PREFIX = /^TARGET message to judge:\n\[(?:me|them)\]: /;

/**
 * Fit a target-first legacy state without silently truncating its target. Drop the oldest
 * preceding messages first. If the complete target needs the shorter isolated wrapper used by
 * the native segmenter, discard the context and use that wrapper instead.
 */
export function fitTargetStateToBudget(
  state: string,
  maxStateTokens: number,
  tokenCount: (value: string) => number,
  hasPortrait = false,
): string {
  if (tokenCount(state) <= maxStateTokens) return state;
  // The saved portrait is only a weak prior. Preserve the target and recent conversation first.
  const portraitAt = hasPortrait ? state.lastIndexOf(PORTRAIT_MARKER) : -1;
  if (portraitAt >= 0) {
    state = state.slice(0, portraitAt);
    if (tokenCount(state) <= maxStateTokens) return state;
  }
  if (!state.startsWith("TARGET message to judge:\n")) return state;
  const markerAt = state.indexOf(CONTEXT_MARKER);
  if (markerAt < 0) return state;
  const target = state.slice(0, markerAt);
  const history = state.slice(markerAt + CONTEXT_MARKER.length);
  const entries = history.split(/\n(?=\[(?:me|them)\]: )/);
  while (entries.length > 0 && tokenCount(`${target}${CONTEXT_MARKER}${entries.join("\n")}`) > maxStateTokens) {
    entries.shift();
  }
  const fitted = entries.length > 0 ? `${target}${CONTEXT_MARKER}${entries.join("\n")}` : target;
  if (tokenCount(fitted) <= maxStateTokens) return fitted;
  const prefix = LEGACY_TARGET_PREFIX.exec(target)?.[0];
  return prefix ? buildIsolatedTargetState(target.slice(prefix.length)) : fitted;
}

/**
 * Build the model state for one target message. An isolated message uses the structure that
 * performed better on the fixed development examples. Conversation states keep the existing
 * target-first format: a separate synthetic holdout did not support changing that format.
 * Up to `contextWindow` preceding messages are appended after the target, oldest first.
 */
export function buildTargetState(
  messages: readonly ContextMessage[],
  index: number,
  contextWindow = DEFAULT_CONTEXT_WINDOW,
  portraitContext?: string,
): string {
  const target = messages[index];
  if (!target) throw new Error(`No message at index ${index}`);
  const start = Math.max(0, index - Math.max(0, contextWindow));
  const context = messages.slice(start, index);
  if (context.length === 0) {
    const state = buildIsolatedTargetState(target.text);
    return portraitContext?.trim() ? state + PORTRAIT_MARKER + JSON.stringify(sanitizeContextText(portraitContext)) : state;
  }
  const lines = [
    "TARGET message to judge:",
    `[${roleLabel(target.side)}]: ${sanitizeContextText(target.text)}`,
  ];
  if (context.length > 0) {
    lines.push("", "Recent context before the target (oldest first):");
    for (const message of context) {
      lines.push(`[${roleLabel(message.side)}]: ${sanitizeContextText(message.text)}`);
    }
  }
  const state = lines.join("\n");
  return portraitContext?.trim() ? state + PORTRAIT_MARKER + JSON.stringify(sanitizeContextText(portraitContext)) : state;
}
