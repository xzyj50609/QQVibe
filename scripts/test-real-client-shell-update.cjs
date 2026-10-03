"use strict";

// Exercise shell IPC registration with synthetic Electron objects; no window or bridge is opened.
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");

async function main() {
  // The shell resolves its product from the ambient environment, like a real launch.
  process.env.QQVIBE_PRODUCT = "wechat";
  const handles = new Map();
  const frames = { mainFrame: { url: "http://127.0.0.1:34567/" } };
  const contents = {
    mainFrame: frames.mainFrame,
    on() {}, once() {}, setWindowOpenHandler() {}, loadURL() {},
  };
  const window = { webContents: contents, isDestroyed: () => false, on() {} };
  const calls = { update: 0, fallback: 0, proxyCreated: 0 };
  const environment = {};
  const proxyFetch = async () => ({ ok: true });
  const app = {
    setAppUserModelId() {}, setPath() {}, requestSingleInstanceLock: () => true,
    on() {}, whenReady: () => Promise.resolve(), getVersion: () => "1.0.1",
  };
  const electron = {
    app, BrowserWindow: class { constructor() { return window; } }, clipboard: {},
    ipcMain: { on() {}, handle: (name, callback) => handles.set(name, callback) },
    session: { defaultSession: {
      setPermissionRequestHandler() {}, setPermissionCheckHandler() {}, on() {},
      resolveProxy: async () => "DIRECT",
      webRequest: { onBeforeRequest() {} },
    } }, shell: {},
  };
  const source = fs.readFileSync(path.join(__dirname, "real-client-shell.cjs"), "utf8");
  vm.runInNewContext(source, {
    require(name) {
      if (name === "electron") return electron;
      if (name === "node:fs") return { mkdirSync() {} };
      if (name === "node:path") return path;
      if (name === "./product-identity.cjs") return require("./product-identity.cjs");
      if (name === "./real-client-update-config.cjs") return { UpdatePreferences: class { get() { return {channel: "stable"}; } } };
      if (name === "node:child_process") return { execFile() { throw new Error("self-test must not stop a bridge"); } };
      if (name === "./real-client-model.cjs") return { ModelDownload: class { getState() { return { phase: "idle" }; } cancel() {} } };
      if (name === "./real-client-recovery.cjs") return { monitorBridge() {} };
      if (name === "./real-client-update.cjs") return {
        RELEASES_URL: "https://github.com/tswawa/WechatVibe/releases",
        checkForUpdates: async (version, options) => {
          calls.update++;
          assert.equal(version, "1.0.1");
          assert.equal(options.fetchImpl, proxyFetch);
          return { status: calls.update === 1 ? "offline" : "current" };
        },
        errorStatus: () => "server-error",
      };
      if (name === "./real-client-update-proxy.cjs") return {
        createUpdateProxyFetch: ({ session, ProxyAgent }) => {
          assert.equal(session, electron.session.defaultSession);
          assert.equal(ProxyAgent.name, "FakeProxyAgent");
          calls.proxyCreated++;
          return { fetchImpl: proxyFetch, enableSavedLoopbackFallback: async () => {
            calls.fallback++;
            return true;
          } };
        },
      };
      if (name.endsWith(path.join("node_modules", "undici"))) return {
        ProxyAgent: class FakeProxyAgent {},
      };
      throw new Error(`Unexpected require: ${name}`);
    },
    __dirname, URL, process: {
      platform: "win32", env: environment, argv: ["electron", "shell", "--client-url", frames.mainFrame.url, "--self-test"],
      stderr: { write() {} },
    },
  });
  await new Promise(resolve => setImmediate(resolve));
  const trusted = { sender: contents, senderFrame: frames.mainFrame };
  const subframe = { sender: contents, senderFrame: { url: frames.mainFrame.url } };
  const otherWindow = { sender: {}, senderFrame: frames.mainFrame };
  assert.equal(handles.get("real-client:app-version")(trusted), "1.0.1");
  assert.equal(handles.get("real-client:app-version")(subframe), null);
  assert.equal(handles.get("real-client:app-version")(otherWindow), null);
  assert.equal((await handles.get("real-client:check-updates")(subframe)).status, "blocked");
  assert.equal(calls.update, 0);
  const [first, second] = await Promise.all([
    handles.get("real-client:check-updates")(trusted),
    handles.get("real-client:check-updates")(trusted),
  ]);
  assert.equal(first.status, "current");
  assert.equal(second.status, "current");
  assert.equal(calls.update, 2);
  assert.equal(calls.fallback, 1);
  assert.equal(calls.proxyCreated, 1);
  assert.deepEqual(environment, {});
  frames.mainFrame.url = "http://127.0.0.1:34567/other";
  assert.equal(handles.get("real-client:app-version")(trusted), null);
  assert.equal((await handles.get("real-client:check-updates")(trusted)).status, "blocked");
  process.stdout.write("real-client shell update IPC checks passed\n");
}

main().catch(error => { console.error(error); process.exitCode = 1; });
