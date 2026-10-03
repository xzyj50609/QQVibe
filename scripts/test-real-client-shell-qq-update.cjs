"use strict";

// The QQ product must not reach the upstream WeChat update feed. Synthetic Electron
// objects only; no window, bridge or network is used.
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");

async function main() {
  process.env.QQVIBE_PRODUCT = "qq";
  const handles = new Map();
  const frames = { mainFrame: { url: "http://127.0.0.1:34567/" } };
  const contents = { mainFrame: frames.mainFrame, on() {}, once() {}, setWindowOpenHandler() {}, loadURL() {}, send() {} };
  const window = { webContents: contents, isDestroyed: () => false, isVisible: () => true, on() {} };
  const calls = { update: 0, stage: 0, proxyCreated: 0 };
  const app = {
    isPackaged: true,
    setAppUserModelId() {}, setPath() {}, requestSingleInstanceLock: () => true,
    on() {}, whenReady: () => Promise.resolve(), getVersion: () => "0.1.0",
    quit() {}, exit() {},
  };
  const electron = {
    app, BrowserWindow: class { constructor() { return window; } }, clipboard: {},
    dialog: { showErrorBox() {} },
    ipcMain: { on() {}, handle: (name, callback) => handles.set(name, callback) },
    session: { defaultSession: {
      setPermissionRequestHandler() {}, setPermissionCheckHandler() {}, on() {},
      resolveProxy: async () => "DIRECT",
      webRequest: { onBeforeRequest() {} },
    } }, shell: {},
  };
  vm.runInNewContext(fs.readFileSync(path.join(__dirname, "real-client-shell.cjs"), "utf8"), {
    require(name) {
      if (name === "electron") return electron;
      if (name === "node:fs") return { mkdirSync() {}, existsSync: () => true, readFileSync: () => "{}" };
      if (name === "node:path") return path;
      if (name === "./product-identity.cjs") return require("./product-identity.cjs");
      if (name === "./real-client-update-config.cjs") return { UpdatePreferences: class { get() { return {channel: "preview"}; } } };
      if (name === "node:child_process") return { execFile() { throw new Error("self-test must not stop a bridge"); } };
      if (name === "./real-client-model.cjs") return { ModelDownload: class { constructor() { calls.modelDownload = (calls.modelDownload || 0) + 1; } getState() { return { phase: "idle" }; } cancel() {} } };
      if (name === "./real-client-recovery.cjs") return { monitorBridge() { return () => {}; } };
      if (name === "./real-client-update.cjs") return {
        RELEASES_URL: "https://github.com/tswawa/WechatVibe/releases",
        checkForUpdates: async () => { calls.update++; return { status: "current" }; },
        downloadAndStageUpdate: async () => { calls.stage++; return { status: "ready" }; },
        errorStatus: () => "server-error",
      };
      if (name === "./real-client-update-proxy.cjs") return {
        createUpdateProxyFetch: () => { calls.proxyCreated++; return { fetchImpl: async () => ({}), enableSavedLoopbackFallback: async () => false }; },
      };
      if (name === "./real-client-update-controller.cjs") return {
        createUpdateController: () => ({ getState: () => ({ phase: "idle" }), check: async () => ({ phase: "idle" }), begin: async () => ({ phase: "ready" }), rollback: async () => ({ phase: "idle" }) }),
      };
      if (name.endsWith(path.join("node_modules", "undici"))) return { ProxyAgent: class {} };
      throw new Error(`Unexpected require: ${name}`);
    },
    __dirname, URL, setImmediate, setTimeout: () => ({ unref() {} }), clearTimeout() {},
    process: {
      platform: "win32",
      env: { QQVIBE_PRODUCT: "qq", WECHATVIBE_INSTANCE_ID: "a".repeat(64) },
      argv: ["electron", "shell", "--client-url", frames.mainFrame.url, "--self-test"],
      stderr: { write() {} },
    },
  });
  await new Promise(resolve => setImmediate(resolve));
  const trusted = { sender: contents, senderFrame: frames.mainFrame };

  assert.equal((await handles.get("real-client:check-updates")(trusted)).status, "current");
  assert.equal(calls.update, 1, "the QQ product uses its own explicitly supplied profile");
  assert.equal(handles.get("real-client:update-state")(trusted).phase, "idle");
  assert.equal((await handles.get("real-client:begin-update")(trusted)).phase, "blocked");
  assert.equal((await handles.get("real-client:rollback-update")(trusted)).phase, "blocked");
  console.log("QQ_UPDATE_CHANNEL_ISOLATION_TESTS_PASSED");
}

main().catch(error => { console.error(error); process.exit(1); });
