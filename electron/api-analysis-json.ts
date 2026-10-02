// Shared pure JSON/input helpers for API-mode model output.
// Chat text is untrusted data; callers keep their own structural checks.

import { ModelConnectorError } from "./model-connectors";

export function inputError(): never {
  throw new ModelConnectorError("invalid-request", "分析请求超出允许范围");
}

export function outputError(): never {
  // The raw model response may contain private chat or a reflected key. Never expose it.
  throw new ModelConnectorError("invalid-output", "模型返回的分析格式无效");
}

export function charCount(value: string): number {
  return Array.from(value).length;
}

export function validId(value: unknown): value is string {
  return typeof value === "string" && value.length > 0 && value.length <= 200 &&
    !/[\u0000-\u001f\u007f]/u.test(value);
}

// One displayed short label: 1-8 Han characters and nothing else. This is the S1 API
// semantic contract for affect/intent phrases; the application never invents one.
const HAN_LABEL = /^\p{Script=Han}{1,8}$/u;

export function validShortLabel(value: unknown): value is string {
  return typeof value === "string" && HAN_LABEL.test(value);
}

export function exactObject(value: unknown, keys: readonly string[]): Record<string, unknown> {
  if (!value || typeof value !== "object" || Array.isArray(value)) outputError();
  const record = value as Record<string, unknown>;
  const actual = Object.keys(record);
  if (actual.length !== keys.length || actual.some((key) => !keys.includes(key))) outputError();
  return record;
}

// Some Responses-compatible models wrap the whole JSON object in one Markdown
// fence. Accept exactly that, never prose-embedded JSON, and keep every later
// structural check in place.
const FENCED_JSON = /^```(?:json)?\s*([\s\S]*?)\s*```$/iu;

export function decodeJsonOutput(text: string): unknown {
  const trimmed = text.trim();
  const fenced = FENCED_JSON.exec(trimmed);
  try {
    return JSON.parse(fenced ? fenced[1]!.trim() : trimmed);
  } catch {
    outputError();
  }
}
