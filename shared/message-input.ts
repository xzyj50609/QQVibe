// Unified message-input contract shared by the Python bridge and the Node side.
//
// This module is pure: no node/electron imports, no IO, no model access. It defines
// the metadata envelope that travels only over the local IPC, validates it, and
// projects back the exact legacy wire the model already consumed. Metadata never
// enters a provider prompt.
//
// Legacy compatibility:
// - `id` keeps the old "non-empty string" rule (no new length/control limits).
// - `time` keeps a finite non-negative number (ints and floats alike); 0 means
//   unknown and is preserved as-is, never multiplied by 1000 again.
// - New metadata is bounded by Unicode codepoint and type-checked. Names and quote
//   text are display text, so newlines/tabs/emoji stay verbatim; only IDs reject
//   control characters.

export type MessageSide = "self" | "other";
export type MessageSourceKind = "wechat" | "qq" | "ocr" | "unknown";

export interface MessageQuote {
  id: string | null;
  senderId: string | null;
  senderName: string | null;
  text: string | null;
  sentAtMs: number | null;
}

export interface MessageInputMeta {
  accountId: string | null;
  conversationId: string | null;
  senderId: string | null;
  senderName: string | null;
  sentAtMs: number | null;
  source: { kind: MessageSourceKind };
  quote: MessageQuote | null;
}

export interface UnifiedMessageInput {
  id: string;
  side: MessageSide;
  text: string;
  accountId: string | null;
  conversationId: string | null;
  senderId: string | null;
  senderName: string | null;
  sentAtMs: number | null;
  source: { kind: MessageSourceKind };
  quote: MessageQuote | null;
}

export interface LegacyWireMessage {
  id: string;
  side: MessageSide;
  text: string;
  time?: number;
  inputMeta?: unknown;
}

export interface TrustedScope {
  accountId?: string | null;
  conversationId?: string | null;
  sourceKind?: MessageSourceKind;
}

const SOURCE_KINDS = new Set<MessageSourceKind>(["wechat", "qq", "ocr", "unknown"]);

const MAX_ACCOUNT = 200;
const MAX_CONVERSATION = 256;
const MAX_SENDER_ID = 200;
const MAX_SENDER_NAME = 200;
const MAX_QUOTE_ID = 200;
const MAX_QUOTE_SENDER_ID = 200;
const MAX_QUOTE_SENDER_NAME = 200;
const MAX_QUOTE_TEXT = 4000;

function codePointLength(value: string): number {
  return [...value].length;
}

function hasControl(value: string): boolean {
  return /[\u0000-\u001f\u007f]/u.test(value);
}

function identifier(value: unknown, maximum: number, field: string): string {
  if (typeof value !== "string" || value === "" || codePointLength(value) > maximum ||
      hasControl(value)) {
    throw new Error("invalid " + field);
  }
  return value;
}

function displayText(value: unknown, maximum: number, field: string): string {
  if (typeof value !== "string" || codePointLength(value) > maximum) {
    throw new Error("invalid " + field);
  }
  return value;
}

function optionalIdentifier(value: unknown, maximum: number, field: string): string | null {
  if (value === undefined || value === null || value === "") return null;
  return identifier(value, maximum, field);
}

function optionalDisplay(value: unknown, maximum: number, field: string): string | null {
  if (value === undefined || value === null || value === "") return null;
  return displayText(value, maximum, field);
}

function sentAtMs(value: unknown, field: string): number | null {
  if (value === undefined || value === null) return null;
  if (typeof value !== "number" || !Number.isFinite(value) || value < 0) {
    throw new Error("invalid " + field);
  }
  return value;
}

/** Validate a quote without recovering missing content; accepts draft aliases. */
export function normalizeQuote(value: unknown): MessageQuote | null {
  if (value === undefined || value === null) return null;
  if (typeof value !== "object" || Array.isArray(value)) throw new Error("invalid quote");
  const draft = value as Record<string, unknown>;
  const senderName = draft.senderName !== undefined && draft.senderName !== null
    ? draft.senderName : draft.author;
  const sentAt = draft.sentAtMs !== undefined && draft.sentAtMs !== null
    ? draft.sentAtMs : draft.time;
  const quote: MessageQuote = {
    id: optionalIdentifier(draft.id, MAX_QUOTE_ID, "quote.id"),
    senderId: optionalIdentifier(draft.senderId, MAX_QUOTE_SENDER_ID, "quote.senderId"),
    senderName: optionalDisplay(senderName, MAX_QUOTE_SENDER_NAME, "quote.senderName"),
    text: optionalDisplay(draft.text, MAX_QUOTE_TEXT, "quote.text"),
    sentAtMs: sentAtMs(sentAt, "quote.sentAtMs"),
  };
  if (Object.values(quote).every((entry) => entry === null)) return null;
  return quote;
}

/** Validate an existing metadata envelope into the canonical shape. */
export function normalizeInputMeta(value: unknown): MessageInputMeta | null {
  if (value === undefined || value === null) return null;
  if (typeof value !== "object" || Array.isArray(value)) throw new Error("invalid inputMeta");
  const draft = value as Record<string, unknown>;
  let kind: MessageSourceKind = "unknown";
  const source = draft.source;
  if (source !== undefined && source !== null) {
    if (typeof source !== "object" || Array.isArray(source)) {
      throw new Error("invalid inputMeta.source");
    }
    const rawKind = (source as Record<string, unknown>).kind ?? "unknown";
    if (typeof rawKind !== "string" || !SOURCE_KINDS.has(rawKind as MessageSourceKind)) {
      throw new Error("invalid inputMeta.source.kind");
    }
    kind = rawKind as MessageSourceKind;
  }
  return {
    accountId: optionalIdentifier(draft.accountId, MAX_ACCOUNT, "inputMeta.accountId"),
    conversationId: optionalIdentifier(draft.conversationId, MAX_CONVERSATION,
      "inputMeta.conversationId"),
    senderId: optionalIdentifier(draft.senderId, MAX_SENDER_ID, "inputMeta.senderId"),
    senderName: optionalDisplay(draft.senderName, MAX_SENDER_NAME, "inputMeta.senderName"),
    sentAtMs: sentAtMs(draft.sentAtMs, "inputMeta.sentAtMs"),
    source: { kind },
    quote: normalizeQuote(draft.quote),
  };
}

function mergeScope(trusted: string | null | undefined, foreign: string | null,
  maximum: number, field: string): string | null {
  if (trusted === undefined || trusted === null) return foreign;
  const validated = identifier(trusted, maximum, field);
  if (foreign !== null && foreign !== validated) {
    throw new Error("cross-" + field + " envelope");
  }
  return validated;
}

function mergeKnown<T>(left: T | null, right: T | null, field: string): T | null {
  if (left === null) return right;
  if (right !== null && left !== right) throw new Error("conflicting " + field);
  return left;
}
function mergeTime(left: number | null, right: number | null): number | null {
  if (left === null || left === 0) return right ?? left;
  if (right === null || right === 0) return left;
  return mergeKnown(left, right, "sentAtMs");
}
function mergeQuote(left: MessageQuote | null, right: MessageQuote | null): MessageQuote | null {
  if (left === null) return right;
  if (right === null) return left;
  return { id: mergeKnown(left.id, right.id, "quote.id"),
    senderId: mergeKnown(left.senderId, right.senderId, "quote.senderId"),
    senderName: mergeKnown(left.senderName, right.senderName, "quote.senderName"),
    text: mergeKnown(left.text, right.text, "quote.text"),
    sentAtMs: mergeTime(left.sentAtMs, right.sentAtMs) };
}

/**
 * Build the unified record from a legacy wire entry. Any existing `inputMeta` is
 * validated and merged with the caller's trusted scope; a foreign scope is rejected
 * instead of silently overwritten.
 */
export function buildUnifiedInput(wire: Record<string, unknown>,
  trusted: TrustedScope = {}): UnifiedMessageInput {
  const id = wire.id;
  if (typeof id !== "string" || id === "") throw new Error("invalid id");
  const side = wire.side;
  if (side !== "self" && side !== "other") throw new Error("invalid side");
  const text = wire.text;
  if (typeof text !== "string") throw new Error("invalid text");
  let time: number | null = null;
  if (wire.time !== undefined) {
    const raw = wire.time;
    if (typeof raw !== "number" || !Number.isFinite(raw) || raw < 0) {
      throw new Error("invalid time");
    }
    time = raw;
  }
  const existing = normalizeInputMeta(wire.inputMeta);
  const accountId = mergeScope(trusted.accountId, existing?.accountId ?? null,
    MAX_ACCOUNT, "accountId");
  const conversationId = mergeScope(trusted.conversationId, existing?.conversationId ?? null,
    MAX_CONVERSATION, "conversationId");
  const senderId = mergeKnown(existing?.senderId ?? null,
    optionalIdentifier(wire.senderId, MAX_SENDER_ID, "senderId"), "senderId");
  const senderName = mergeKnown(existing?.senderName ?? null,
    optionalDisplay(wire.senderName, MAX_SENDER_NAME, "senderName"), "senderName");
  const existingKind = existing?.source.kind ?? "unknown";
  const trustedKind = trusted.sourceKind ?? "unknown";
  if (!SOURCE_KINDS.has(trustedKind)) throw new Error("invalid source.kind");
  if (existingKind !== "unknown" && trustedKind !== "unknown" && existingKind !== trustedKind) {
    throw new Error("conflicting source.kind");
  }
  const sourceKind = trustedKind === "unknown" ? existingKind : trustedKind;
  return {
    id,
    side,
    text,
    accountId,
    conversationId,
    senderId,
    senderName,
    sentAtMs: mergeTime(existing?.sentAtMs ?? null, time),
    source: { kind: sourceKind },
    quote: mergeQuote(existing?.quote ?? null, normalizeQuote(wire.quote)),
  };
}

/**
 * Explicit legacy projection for the model: exactly the old four fields. Metadata is
 * intentionally dropped here and never enters the model input.
 */
export function projectLegacyWire(record: UnifiedMessageInput,
  time?: number): { id: string; side: MessageSide; text: string; time: number } {
  return {
    id: record.id,
    side: record.side,
    text: record.text,
    time: typeof time === "number" ? time : 0,
  };
}
