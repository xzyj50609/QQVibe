"use strict";

const assert = require("node:assert/strict");
const {
  createUpdateProxyFetch, parseProxyDirective, parseSavedLoopbackProxy,
  readSavedProxyFromRegistry,
} = require("./real-client-update-proxy.cjs");

class FakeProxyAgent {
  constructor(url) { this.url = url; FakeProxyAgent.created.push(this); }
  async close() { this.closed = true; }
}
FakeProxyAgent.created = [];

async function main() {
  const originalEnvironment = { ...process.env };
  assert.deepEqual(parseProxyDirective("PROXY 127.0.0.1:7897; DIRECT"),
    { kind: "proxy", url: "http://127.0.0.1:7897" });
  assert.deepEqual(parseProxyDirective("HTTPS [::1]:8443; DIRECT"),
    { kind: "proxy", url: "https://[::1]:8443" });
  assert.deepEqual(parseProxyDirective("PROXY proxy.invalid:0; DIRECT"), { kind: "direct" });
  assert.deepEqual(parseProxyDirective("PROXY proxy.invalid:65536"), { kind: "none" });
  assert.equal(parseSavedLoopbackProxy("127.0.0.1:7897"), "http://127.0.0.1:7897");
  assert.equal(parseSavedLoopbackProxy("http=remote.invalid:8080;https=[::1]:7897"),
    "http://[::1]:7897");

  const api = "https://api.github.com/repos/tswawa/WechatVibe/releases/latest";
  const asset = "https://github.com/tswawa/WechatVibe/releases/download/v1.0.4/file.zip";
  const cdn = "https://release-assets.githubusercontent.com/file";
  const resolved = [];
  const fetched = [];
  const signal = new AbortController().signal;
  const headers = { Accept: "application/octet-stream" };
  const options = { redirect: "manual", signal, headers };
  const routed = createUpdateProxyFetch({
    resolveProxy: async url => {
      resolved.push(url);
      if (url === api) return "PROXY 127.0.0.1:7897; DIRECT";
      if (url === asset) return "HTTPS proxy.example:8443; DIRECT";
      return "DIRECT";
    },
    fetchImpl: async (url, request) => { fetched.push({ url, request }); return { ok: true }; },
    ProxyAgent: FakeProxyAgent, env: {},
  });
  await routed.fetchImpl(api, options);
  await routed.fetchImpl(asset, options);
  await routed.fetchImpl(cdn, options);
  await routed.fetchImpl(api, options);
  assert.deepEqual(resolved, [api, asset, cdn, api]);
  assert.equal(fetched[0].request.dispatcher.url, "http://127.0.0.1:7897");
  assert.equal(fetched[1].request.dispatcher.url, "https://proxy.example:8443");
  assert.equal(fetched[2].request, options);
  assert.equal(fetched[3].request.dispatcher, fetched[0].request.dispatcher);
  assert.equal(FakeProxyAgent.created.length, 2);
  for (const item of fetched) {
    assert.equal(item.request.redirect, "manual");
    assert.equal(item.request.signal, signal);
    assert.equal(item.request.headers, headers);
  }
  await routed.close();
  assert.ok(FakeProxyAgent.created.every(agent => agent.closed));

  const fallbackRequests = [];
  let registryReads = 0;
  let listenerChecks = 0;
  let fallbackResolves = 0;
  const fallback = createUpdateProxyFetch({
    resolveProxy: async () => { fallbackResolves++; return "DIRECT"; },
    fetchImpl: async (_url, request) => { fallbackRequests.push(request); return { ok: true }; },
    ProxyAgent: FakeProxyAgent, env: {},
    readSavedProxy: async () => { registryReads++; return "http=remote.invalid:8080;https=127.0.0.1:7897"; },
    isLoopbackListening: async url => { listenerChecks++; assert.equal(url, "http://127.0.0.1:7897"); return true; },
  });
  await fallback.fetchImpl(api, options);
  assert.equal(fallbackRequests[0], options);
  assert.deepEqual(await Promise.all([
    fallback.enableSavedLoopbackFallback(), fallback.enableSavedLoopbackFallback(),
  ]), [true, true]);
  await fallback.fetchImpl(api, options);
  assert.equal(fallbackRequests[1].dispatcher.url, "http://127.0.0.1:7897");
  assert.equal(fallbackResolves, 2);
  assert.equal(registryReads, 1);
  assert.equal(listenerChecks, 1);
  await fallback.close();

  let explicitReads = 0;
  const explicitRequests = [];
  const explicit = createUpdateProxyFetch({
    resolveProxy: async () => "PROXY 127.0.0.1:7897; DIRECT",
    fetchImpl: async (_url, request) => { explicitRequests.push(request); return { ok: true }; },
    ProxyAgent: FakeProxyAgent,
    env: { HTTPS_PROXY: "http://127.0.0.1:9000", https_proxy: "http://127.0.0.1:9001" },
    readSavedProxy: async () => { explicitReads++; return "127.0.0.1:7897"; },
  });
  assert.equal(await explicit.enableSavedLoopbackFallback(), false);
  await explicit.fetchImpl(api, options);
  assert.equal(explicitRequests[0].dispatcher.url, "http://127.0.0.1:9000/");
  assert.equal(explicitReads, 0);
  await explicit.close();

  for (const invalid of ["remote.example:7897", "192.168.1.1:7897",
    "https=remote.example:7897", "https://127.0.0.1:7897", "127.0.0.1:0",
    "socks=127.0.0.1:7897", "http=127.0.0.1:7897", "127.0.0.1.evil:7897"]) {
    assert.equal(parseSavedLoopbackProxy(invalid), null, invalid);
  }
  let rejectedListenerChecks = 0;
  const rejected = createUpdateProxyFetch({
    resolveProxy: async () => "DIRECT", fetchImpl: async () => ({ ok: true }),
    env: {}, readSavedProxy: async () => "remote.example:7897",
    isLoopbackListening: async () => { rejectedListenerChecks++; return true; },
  });
  assert.equal(await rejected.enableSavedLoopbackFallback(), false);
  assert.equal(rejectedListenerChecks, 0);
  let available = false;
  let laterReads = 0;
  const laterRequests = [];
  const notListening = createUpdateProxyFetch({
    resolveProxy: async () => "DIRECT",
    fetchImpl: async (_url, request) => { laterRequests.push(request); return { ok: true }; },
    env: {}, readSavedProxy: async () => { laterReads++; return "127.0.0.1:7897"; },
    isLoopbackListening: async () => available,
    ProxyAgent: FakeProxyAgent,
  });
  assert.equal(await notListening.enableSavedLoopbackFallback(), false);
  available = true;
  assert.equal(await notListening.enableSavedLoopbackFallback(), true);
  assert.equal(await notListening.enableSavedLoopbackFallback(), true);
  assert.equal(laterReads, 2);
  await notListening.fetchImpl(api, options);
  assert.equal(laterRequests[0].dispatcher.url, "http://127.0.0.1:7897");
  await notListening.close();

  if (process.platform === "win32") {
    const registry = await readSavedProxyFromRegistry((file, args, opts, callback) => {
      assert.equal(file, "reg.exe");
      assert.deepEqual(args, ["query", "HKCU\\Software\\Microsoft\\Windows\\CurrentVersion\\Internet Settings",
        "/v", "ProxyServer"]);
      assert.equal(opts.windowsHide, true);
      callback(null, "    ProxyServer    REG_SZ    127.0.0.1:7897\r\n");
    });
    assert.equal(registry, "127.0.0.1:7897");
  }
  assert.deepEqual({ ...process.env }, originalEnvironment);
  process.stdout.write("real-client update proxy checks passed\n");
}

main().catch(error => { console.error(error); process.exitCode = 1; });
