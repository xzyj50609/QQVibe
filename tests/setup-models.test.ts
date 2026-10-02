import assert from "node:assert/strict";
import test from "node:test";
import { createHash } from "node:crypto";
import { mkdtempSync, readFileSync, writeFileSync, existsSync, rmSync } from "node:fs";
import os from "node:os";
import path from "node:path";
import { downloadFile, downloadWithRetries, PINNED_FILES } from "../scripts/setup-models";

const data = "model-fixture-abcdefgh";
const spec = { path: "model.onnx", bytes: Buffer.byteLength(data),
  sha256: createHash("sha256").update(data).digest("hex") };
function fixture(t: any, force = false) {
  const dir = mkdtempSync(path.join(os.tmpdir(), "wechatvibe-模型-download-test-"));
  t.after(() => rmSync(dir, { recursive: true, force: true }));
  return { options: { dir, force }, dest: path.join(dir, spec.path), part: path.join(dir, spec.path + ".part") };
}
const fetcher = (fn: (...args: any[]) => any) => fn as typeof fetch;
test("complete pinned bundle includes the Python validator README", () => {
  const validator = JSON.parse(readFileSync(new URL("../scripts/model-files.json", import.meta.url), "utf8"));
  assert.equal(validator.schema, 1);
  assert.equal(PINNED_FILES.length, Object.keys(validator.files).length);
  for (const item of PINNED_FILES) {
    assert.deepEqual(validator.files[item.path], { bytes: item.bytes, sha256: item.sha256 });
  }
});
test("verified existing model skips network", async t => {
  const f = fixture(t); writeFileSync(f.dest, data);
  await downloadFile(spec, f.options, fetcher(() => { throw new Error("unexpected fetch"); }));
});
test("partial download resumes with a validated Content-Range", async t => {
  const f = fixture(t); writeFileSync(f.part, data.slice(0, 4));
  await downloadFile(spec, f.options, fetcher(async (_url, init) => {
    assert.equal(init.headers.Range, "bytes=4-");
    return new Response(data.slice(4), { status: 206, headers: { "content-range": `bytes 4-${spec.bytes - 1}/${spec.bytes}` } });
  }));
  assert.equal(readFileSync(f.dest, "utf8"), data); assert.ok(!existsSync(f.part));
});
test("ignored range replaces partial instead of appending full response", async t => {
  const f = fixture(t); writeFileSync(f.part, data.slice(0, 4));
  await downloadFile(spec, f.options, fetcher(async () => new Response(data)));
  assert.equal(readFileSync(f.dest, "utf8"), data);
});
test("invalid range leaves old model and partial intact", async t => {
  const f = fixture(t); writeFileSync(f.dest, "old-model"); writeFileSync(f.part, data.slice(0, 4));
  await assert.rejects(downloadFile(spec, f.options, fetcher(async () => new Response(data.slice(4), {
    status: 206, headers: { "content-range": `bytes 0-${spec.bytes - 5}/${spec.bytes}` },
  }))), /Content-Range/);
  assert.equal(readFileSync(f.dest, "utf8"), "old-model"); assert.equal(readFileSync(f.part, "utf8"), data.slice(0, 4));
});
test("force retry resumes new partial and retains old model until verified", async t => {
  const f = fixture(t, true); writeFileSync(f.dest, "old-model"); writeFileSync(f.part, "stale");
  let calls = 0;
  await downloadWithRetries(spec, f.options, fetcher(async (_url, init) => {
    assert.equal(readFileSync(f.dest, "utf8"), "old-model");
    if (++calls === 1) { assert.equal(init.headers, undefined); return new Response(data.slice(0, 4)); }
    assert.equal(init.headers.Range, "bytes=4-");
    return new Response(data.slice(4), { status: 206, headers: { "content-range": `bytes 4-${spec.bytes - 1}/${spec.bytes}` } });
  }), async () => {});
  assert.equal(calls, 2); assert.equal(readFileSync(f.dest, "utf8"), data);
});
test("three failed force attempts preserve installed model", async t => {
  const f = fixture(t, true); writeFileSync(f.dest, data); let calls = 0;
  await assert.rejects(downloadWithRetries(spec, f.options, fetcher(async () => {
    calls++; return new Response("offline", { status: 503 });
  }), async () => {}), /503/);
  assert.equal(calls, 3); assert.equal(readFileSync(f.dest, "utf8"), data);
});
test("hash mismatch removes corrupt partial but preserves old model", async t => {
  const f = fixture(t, true); writeFileSync(f.dest, "old-model");
  await assert.rejects(downloadFile(spec, f.options, fetcher(async () => new Response("x".repeat(spec.bytes)))), /SHA256/);
  assert.ok(!existsSync(f.part)); assert.equal(readFileSync(f.dest, "utf8"), "old-model");
});
test("oversized response is rejected without replacing old model", async t => {
  const f = fixture(t, true); writeFileSync(f.dest, "old-model");
  await assert.rejects(downloadFile(spec, f.options, fetcher(async () => new Response(data + "extra"))), /超过/);
  assert.ok(!existsSync(f.part)); assert.equal(readFileSync(f.dest, "utf8"), "old-model");
});
test("complete verified partial is promoted without downloading again", async t => {
  const f = fixture(t); writeFileSync(f.part, data); writeFileSync(f.dest, "old-model");
  await downloadFile(spec, f.options, fetcher(() => { throw new Error("unexpected fetch"); }));
  assert.equal(readFileSync(f.dest, "utf8"), data);
});
test("416 restarts the download cleanly", async t => {
  const f = fixture(t); writeFileSync(f.part, data.slice(0, 4)); let calls = 0;
  await downloadFile(spec, f.options, fetcher(async () => ++calls === 1 ?
    new Response(null, { status: 416 }) : new Response(data)));
  assert.equal(calls, 2); assert.equal(readFileSync(f.dest, "utf8"), data);
});
