"use strict";
const assert = require("node:assert/strict");
const fs = require("node:fs");
const os = require("node:os");
const path = require("node:path");
const { assertPayloadCompatible } = require("./real-client-update-helper.cjs");
const root = fs.mkdtempSync(path.join(os.tmpdir(), "qq-analysis-format-"));
const runtime = path.join(root, "QQVibeData", "real-client-runtime");
const marker = path.join(runtime, "payload-format.json");
try {
  assertPayloadCompatible(root, "1.0.0", "QQVibe");
  fs.mkdirSync(runtime, { recursive: true });
  fs.writeFileSync(marker, JSON.stringify({ schema: 1, payloadFormat: "wv1:" }));
  assert.throws(() => assertPayloadCompatible(root, "1.0.0", "QQVibe"), /不能回退/);
  assertPayloadCompatible(root, "1.1.0", "QQVibe");
  assertPayloadCompatible(root, "2.0.0", "QQVibe");
  assertPayloadCompatible(root, "1.0.0", "WechatVibe");
  fs.writeFileSync(marker, '{}');
  assert.throws(() => assertPayloadCompatible(root, "1.2.0", "QQVibe"), /格式标记/);
  process.stdout.write("analysis format update compatibility checks passed\n");
} finally {
  fs.unlinkSync(marker); fs.rmdirSync(runtime); fs.rmdirSync(path.dirname(runtime)); fs.rmdirSync(root);
}
