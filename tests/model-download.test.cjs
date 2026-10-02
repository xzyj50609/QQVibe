const assert = require("node:assert/strict");
const fs = require("node:fs");
const os = require("node:os");
const path = require("node:path");
const { it } = require("node:test");
const { ModelDownload } = require("../scripts/real-client-model.cjs");
const { productProfile } = require("../scripts/product-identity.cjs");

function fixture(t, options) {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), "qq-model-download-"));
  t.after(() => fs.rmSync(root, { recursive: true, force: true }));
  fs.mkdirSync(path.join(root, "scripts"));
  fs.copyFileSync(path.join(__dirname, "../scripts/model-asset.json"), path.join(root, "scripts/model-asset.json"));
  const model = path.join(root, productProfile().dataDir, "models", "laya", "model.onnx");
  fs.mkdirSync(path.dirname(model), { recursive: true }); fs.writeFileSync(model, "old-model-fixture");
  const states = [];
  const downloader = new ModelDownload({ root, python: "unused", onState: value => states.push(value), ...options });
  const retained = () => {
    assert.equal(fs.readFileSync(model, "utf8"), "old-model-fixture");
    const downloads = path.join(root, productProfile().dataDir, "model-downloads");
    assert.deepEqual(fs.readdirSync(downloads), []);
  };
  return { root, downloader, retained, states };
}

it("download size/hash failure preserves the old model and permits another attempt", async t => {
  let calls = 0;
  const f = fixture(t, { fetchImpl: async () => { calls++; return new Response("synthetic corrupt model"); } });
  const first = await f.downloader.start();
  assert.equal(first.phase, "failed"); assert.equal(first.stage, "downloading"); f.retained();
  assert.ok(f.states.some(s => s.phase === "downloading" && s.received > 0));
  await f.downloader.start(); assert.equal(calls, 2); f.retained();
});

it("cancel interrupts the body, cleans partial archive and never invokes fallback", async t => {
  let fallbacks = 0, calls = 0, closed = false;
  const f = fixture(t, { fetchImpl: async () => { calls++; return new Response(new ReadableStream({
    start(controller) { controller.enqueue(new Uint8Array([1, 2, 3])); },
    cancel() { closed = true; },
  })); }, enableFallback: async () => { fallbacks++; return true; } });
  const pending = f.downloader.start();
  while (!f.states.some(s => s.received > 0)) await new Promise(resolve => setTimeout(resolve, 5));
  f.downloader.cancel();
  assert.equal((await pending).phase, "failed");
  assert.equal(closed, true); assert.equal(fallbacks, 0); assert.equal(calls, 1); f.retained();
});

it("a network failure retries at most once through the configured fallback", async t => {
  let calls = 0, fallbacks = 0;
  const f = fixture(t, { fetchImpl: async () => { calls++; throw new Error("synthetic network failure"); },
    enableFallback: async () => { fallbacks++; return true; } });
  assert.equal((await f.downloader.start()).phase, "failed");
  assert.equal(calls, 2); assert.equal(fallbacks, 1); f.retained();
});

it("concurrent download requests share one operation", async t => {
  let calls = 0, finish;
  const f = fixture(t, { fetchImpl: () => { calls++; return new Promise(resolve => { finish = resolve; }); } });
  const one = f.downloader.start(), two = f.downloader.start();
  await new Promise(resolve => setTimeout(resolve, 0));
  finish(new Response("synthetic incomplete model"));
  assert.deepEqual(await one, await two); assert.equal(calls, 1); f.retained();
});
