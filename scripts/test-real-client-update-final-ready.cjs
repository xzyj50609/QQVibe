"use strict";

const assert = require("node:assert/strict");
const fs = require("node:fs");
const os = require("node:os");
const path = require("node:path");
const vm = require("node:vm");

function removeFixture(root) {
  if (!fs.existsSync(root)) return;
  for (const entry of fs.readdirSync(root, { withFileTypes: true })) {
    const item = path.join(root, entry.name);
    if (entry.isDirectory() && !entry.isSymbolicLink()) removeFixture(item);
    else fs.unlinkSync(item);
  }
  fs.rmdirSync(root);
}

async function main() {
  const parent = fs.mkdtempSync(path.join(os.tmpdir(), "wechatvibe-final-ready-"));
  try {
    const installRoot = path.join(parent, "WechatVibe");
    const clientRoot = path.join(installRoot, "resources", "client");
    const workDir = path.join(parent, ".wechatvibe-update-fixture");
    fs.mkdirSync(clientRoot, { recursive: true });
    fs.mkdirSync(workDir);
    const marker = path.join(workDir, "ui-final-ready-11111111-1111-4111-8111-111111111111.json");
    const nonce = "a".repeat(64);
    const instanceId = "b".repeat(64);
    fs.writeFileSync(path.join(workDir, "journal.json"), JSON.stringify({ schema: 1,
      phase: "succeeded", guiStarted: true, expectedVersion: "1.0.2",
      installRoot }));

    const url = "http://127.0.0.1:34567/";
    const frame = { url };
    const contents = { mainFrame: frame, on() {}, once() {}, send() {},
      setWindowOpenHandler() {}, loadURL() {} };
    const window = { webContents: contents, isDestroyed: () => false,
      isVisible: () => true, isMinimized: () => false,
      on() {}, setTitleBarOverlay() {} };
    const handlers = new Map();
    const timers = [];
    const cleared = [];
    const app = { isPackaged: true, setAppUserModelId() {}, setPath() {},
      requestSingleInstanceLock: () => true, on() {}, whenReady: () => Promise.resolve(),
      getVersion: () => "1.0.2", quit() {} };
    const electron = { app, BrowserWindow: class { constructor() { return window; } },
      clipboard: {}, dialog: { showErrorBox() {} },
      ipcMain: { on() {}, handle: (name, handler) => handlers.set(name, handler) },
      session: { defaultSession: { setPermissionRequestHandler() {},
        setPermissionCheckHandler() {}, on() {}, webRequest: { onBeforeRequest() {} } } }, shell: {} };
    const source = fs.readFileSync(path.join(__dirname, "real-client-shell.cjs"), "utf8");
    vm.runInNewContext(source, { __dirname, URL, fs,
      process: { platform: "win32", env: {
        WECHATVIBE_CLIENT_ROOT: clientRoot, WECHATVIBE_INSTANCE_ID: instanceId,
        WECHATVIBE_UPDATE_FINAL_READY_FILE: marker,
        WECHATVIBE_UPDATE_FINAL_READY_NONCE: nonce,
      }, argv: ["electron", "shell", "--client-url", url], stderr: { write() {} } },
      setImmediate, setTimeout: callback => { const id = timers.length + 1; timers.push({ id, callback }); return id; },
      clearTimeout: id => cleared.push(id),
      require(name) {
        if (name === "electron") return electron;
        if (name === "node:fs") return fs;
        if (name === "node:path") return path;
        if (name === "./product-identity.cjs") return require("./product-identity.cjs");
      if (name === "./real-client-update-config.cjs") return { UpdatePreferences: class { get() { return {channel: "stable"}; } } };
        if (name === "node:child_process") return { execFile() { throw new Error("ready check must not stop a bridge"); } };
        if (name === "./real-client-model.cjs") return { ModelDownload: class { cancel() {} } };
        if (name === "./real-client-update-proxy.cjs") return {
          createUpdateProxyFetch: () => ({ fetchImpl: async () => { throw new Error("unexpected network"); } }),
        };
        if (name.endsWith(path.join("node_modules", "undici"))) return { ProxyAgent: class {} };
        if (name === "./real-client-recovery.cjs") return { monitorBridge: () => () => {} };
        if (name === "./real-client-update.cjs") return {
          RELEASES_URL: "https://github.com/tswawa/WechatVibe/releases",
          checkForUpdates: async () => ({ status: "current" }),
        };
        if (name === "./real-client-update-controller.cjs") return {
          createUpdateController: () => ({ getState: () => ({ phase: "idle" }),
            check: async () => ({ phase: "current" }), begin: async () => ({}), rollback: async () => ({}) }),
        };
        throw new Error(`Unexpected dependency: ${name}`);
      },
    });
    await new Promise(resolve => setImmediate(resolve));
    assert.equal(timers.length, 1, "Final handoff should have one bounded timer");
    const ready = handlers.get("real-client:update-ui-ready");
    assert.equal(typeof ready, "function");
    assert.equal(await ready({ sender: contents, senderFrame: frame }), true);
    assert.deepEqual(JSON.parse(fs.readFileSync(marker, "utf8")), {
      nonce, expectedVersion: "1.0.2", instanceId,
    });
    assert.deepEqual(cleared, [timers[0].id], "Successful final handoff must cancel the auto-quit timer");
    assert.equal(await handlers.get("real-client:update-ui-ready")({ sender: {}, senderFrame: frame }), false);
  } finally { removeFixture(parent); }
  process.stdout.write("real-client final UI ready checks passed\n");
}

main().catch(error => { console.error(error); process.exitCode = 1; });
