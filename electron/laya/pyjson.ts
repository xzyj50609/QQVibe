// Vendored from laya-mlx (Apache-2.0).
// Source: https://github.com/mizchi/laya-mlx @ dc3aa6b150cb861d0788fbd421cfd1303de4ed57
// Path: web/packages/laya-web/src/pyjson.ts
// Modified: relative imports made extensionless ("./x.ts" -> "./x"). Logic unchanged.

import type { Json } from "./types";

export interface PyJsonOptions {
  /** Python's json.dumps default. false matches `ensure_ascii=False`. */
  ensureAscii?: boolean;
}

/**
 * Serialize like Python's `json.dumps(value, separators=(", ", ": "))`.
 *
 * Differences that cannot be reproduced from JavaScript values are documented rather than
 * hidden:
 * - JavaScript has one numeric type, so an integer-valued number always prints without a
 *   decimal point (`1`), where Python's `float` would print `1.0`.
 * - `-0` prints as `0` (Python's `-0.0` keeps its sign).
 * - Integers beyond 2**53 are not exactly representable as `number` and, being
 *   indistinguishable from an integer-valued float once stored, always print without a
 *   decimal point or exponent — even where Python's `repr(float)` would use exponent notation.
 * - Integer-like object keys ("0", "1", ...) are always enumerated by JavaScript before any
 *   other own key, in ascending numeric order, regardless of insertion order, so a source
 *   object mixing them with non-numeric keys cannot round-trip Python's (always
 *   insertion-ordered) dict order.
 */
export function pyJson(value: Json, options: PyJsonOptions = {}): string {
  return serialize(value, options.ensureAscii ?? true);
}

function serialize(value: Json, ensureAscii: boolean): string {
  if (value === null) return "null";
  if (typeof value === "boolean") return value ? "true" : "false";
  if (typeof value === "number") {
    if (!Number.isFinite(value)) throw new RangeError("Non-finite numbers are not JSON");
    return formatPyNumber(value);
  }
  if (typeof value === "string") return serializeString(value, ensureAscii);
  if (Array.isArray(value))
    return "[" + value.map((v) => serialize(v, ensureAscii)).join(", ") + "]";
  return (
    "{" +
    Object.entries(value)
      .map(([k, v]) => serializeString(k, ensureAscii) + ": " + serialize(v, ensureAscii))
      .join(", ") +
    "}"
  );
}

/**
 * Format a number the way Python's `repr(float)` would (the algorithm `json.dumps` uses for
 * non-integral values: fixed notation when the decimal exponent is in `[-4, 16)`, exponent
 * notation with at least two exponent digits otherwise). See the `pyJson` docblock for what
 * cannot be reproduced (integer-valued numbers, `-0`, integers beyond 2**53).
 */
export function formatPyNumber(value: number): string {
  if (Number.isInteger(value)) return String(value);
  const sign = value < 0 ? "-" : "";
  const match = /^(\d)(?:\.(\d+))?e([+-]\d+)$/.exec(Math.abs(value).toExponential());
  if (!match) throw new RangeError(`Unexpected exponential form for ${value}`);
  const [, leadDigit, fracDigits, expText] = match;
  const digits = (leadDigit ?? "") + (fracDigits ?? "");
  const exp = Number(expText ?? "0");
  if (exp >= -4 && exp < 16) {
    if (exp >= 0) {
      const intLen = exp + 1;
      return (
        sign +
        (digits.length <= intLen
          ? digits.padEnd(intLen, "0")
          : digits.slice(0, intLen) + "." + digits.slice(intLen))
      );
    }
    return sign + "0." + "0".repeat(-exp - 1) + digits;
  }
  const mantissa = digits.length > 1 ? digits.slice(0, 1) + "." + digits.slice(1) : digits;
  const expSign = exp < 0 ? "-" : "+";
  return sign + mantissa + "e" + expSign + String(Math.abs(exp)).padStart(2, "0");
}

function serializeString(value: string, ensureAscii: boolean): string {
  const escaped = JSON.stringify(value);
  if (!ensureAscii) return escaped;
  let out = "";
  for (let i = 0; i < escaped.length; i++) {
    const code = escaped.charCodeAt(i);
    // Python's ensure_ascii escapes everything outside printable ASCII \x20-\x7e, so DEL
    // (0x7f) must be escaped too, not just code points >= 0x80.
    out += code >= 0x7f ? "\\u" + code.toString(16).padStart(4, "0") : escaped[i];
  }
  return out;
}
