"use strict";

// Synthetic shell lifecycle only: no Electron window, Python process, or bridge is started.
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");

const SHELL_DIR = __dirname;
const ROOT = path.resolve(SHELL_DIR, "..");
const CLIENT_URL = "http://127.0.0.1:34567/";
const INSTANCE_ID = "a".repeat(64);

async function settle() {
  await new Promise(resolve => setImmediate(resolve));
  await new Promise(resolve => setImmediate(resolve));
}

async function startShell(extraEnv = {}, extraArgs = [], monitorDrain, options = {}) {
  const appEvents = new Map();
  const windowEvents = new Map();
  const ipcHandlers = new Map();
  const stopCalls = [];
  const dialogs = [];
  const monitorStops = [];
  let updateOptions;
  let quitAttempts = 0;
  let completedQuits = 0;
  let exitCode = null;

  const mainFrame = { url: CLIENT_URL };
  const contents = {
    mainFrame, on() {}, once() {}, setWindowOpenHandler() {}, loadURL() {}, send() {},
  };
  const window = {
    webContents: contents, isDestroyed: () => false, isVisible: () => true,
    isMinimized: () => false,
    on(name, callback) {
      const listeners = windowEvents.get(name) || [];
      listeners.push(callback);
      windowEvents.set(name, listeners);
    },
  };
  const app = {
    isPackaged: true,
    setAppUserModelId() {}, setPath() {}, requestSingleInstanceLock: () => true,
    getVersion: () => "1.0.2", whenReady: () => Promise.resolve(),
    on(name, callback) {
      const listeners = appEvents.get(name) || [];
      listeners.push(callback);
      appEvents.set(name, listeners);
    },
    quit() {
      quitAttempts++;
      let prevented = false;
      const event = { preventDefault() { prevented = true; } };
      for (const listener of appEvents.get("before-quit") || []) listener(event);
      if (!prevented) completedQuits++;
    },
    exit(code) { exitCode = code; },
  };
  const electron = {
    app, BrowserWindow: class { constructor() { return window; } },
    clipboard: { writeText() {} },
    dialog: { showErrorBox: (title, message) => dialogs.push({ title, message }) },
    ipcMain: { on() {}, handle: (name, callback) => ipcHandlers.set(name, callback) },
    session: { defaultSession: {
      setPermissionRequestHandler() {}, setPermissionCheckHandler() {}, on() {},
      webRequest: { onBeforeRequest() {} },
    } },
    shell: { openExternal() {} },
  };
  const source = fs.readFileSync(path.join(SHELL_DIR, "real-client-shell.cjs"), "utf8");
  vm.runInNewContext(source, {
    require(name) {
      if (name === "electron") return electron;
      if (name === "node:fs") return { mkdirSync() {}, existsSync: () => true };
      if (name === "node:path") return path;
      if (name === "./product-identity.cjs") return require("./product-identity.cjs");
      if (name === "./real-client-update-config.cjs") return { UpdatePreferences: class { get() { return {channel: "stable"}; } } };
      if (name === "node:child_process") return {
        execFile(file, args, options, callback) {
          stopCalls.push({ file, args: Array.from(args), options, callback });
          return { kill() {} };
        },
      };
      if (name === "./real-client-recovery.cjs") return {
        monitorBridge: () => () => { monitorStops.push(true); return monitorDrain; },
      };
      if (name === "./real-client-update.cjs") return {
        RELEASES_URL: "https://github.com/tswawa/WechatVibe/releases",
        checkForUpdates: async () => ({ status: "current" }),
      };
      if (name === "./real-client-update-proxy.cjs") return {
        createUpdateProxyFetch: () => {
          if (options.readyThrow) throw new Error("synthetic ready initialization failure");
          return { fetchImpl: async () => ({}),
            enableSavedLoopbackFallback: async () => false };
        }
      };
      if (name === "./real-client-model.cjs") return {
        ModelDownload: class { cancel() {} getState() { return { phase: "idle" }; } },
      };
      if (name === path.join(ROOT, "node_modules", "undici")) return { ProxyAgent: class {} };
      if (name === "./real-client-update-controller.cjs") return {
        createUpdateController(options) {
          updateOptions = options;
          return {
            getState: () => ({ phase: "idle" }),
            check: async () => ({ phase: "idle" }),
            begin: async () => ({ phase: "idle" }),
            rollback: async () => ({ phase: "idle" }),
          };
        },
      };
      throw new Error(`Unexpected require: ${name}`);
    },
    __dirname: SHELL_DIR,
    URL,
    setImmediate,
    setTimeout: () => ({ syntheticTimer: true }),
    clearTimeout() {},
    process: {
      platform: "win32", pid: 12345,
      env: { WECHATVIBE_CLIENT_ROOT: ROOT, WECHATVIBE_INSTANCE_ID: INSTANCE_ID, ...extraEnv },
      argv: ["electron", "shell", "--client-url", CLIENT_URL, ...extraArgs],
      stdout: { write() {} }, stderr: { write() {} },
    },
  }, { filename: path.join(SHELL_DIR, "real-client-shell.cjs") });
  await settle();
  assert.equal(exitCode, null, "synthetic shell must pass its startup validation");
  return {
    app, stopCalls, dialogs, monitorStops, get updateOptions() { return updateOptions; },
    get quitAttempts() { return quitAttempts; },
    get completedQuits() { return completedQuits; },
    exitApp: () => ipcHandlers.get("real-client:exit-app")({ sender: contents, senderFrame: mainFrame }),
    closeWindow() {
      let prevented = false;
      const event = { preventDefault() { prevented = true; } };
      for (const listener of windowEvents.get("close") || []) listener(event);
      return prevented;
    },
  };
}

function assertOwnedStop(call) {
  assert.equal(call.file, path.join(ROOT, "runtime", "python", "python.exe"));
  assert.deepEqual(call.args, [
    path.join(ROOT, "scripts", "start-real-client.py"), "--stop-owned-bridge", "--json",
  ]);
  assert.equal(call.options.cwd, ROOT);
  assert.equal(call.options.windowsHide, true);
  assert.equal(call.options.env.CHATUI_PORT, "34567");
  assert.equal(call.options.env.WECHATVIBE_CLIENT_ROOT, ROOT);
}

async function testNormalQuitWaitsForOwnedBridge() {
  const shell = await startShell();
  assert.equal(shell.exitApp(), true);
  await settle();
  assert.equal(shell.stopCalls.length, 1, "normal UI exit must request an owned-bridge stop");
  assertOwnedStop(shell.stopCalls[0]);
  assert.equal(shell.completedQuits, 0, "Electron must wait for the stop callback");

  shell.app.quit();
  await settle();
  assert.equal(shell.stopCalls.length, 1, "a second quit while stopping must reuse the first stop");
  assert.equal(shell.completedQuits, 0, "the second quit must also wait");

  shell.stopCalls[0].callback(null, JSON.stringify({ stopped: true }), "");
  await settle();
  assert.equal(shell.completedQuits, 1, "Electron must finish quitting after the stop callback");
  assert.equal(shell.stopCalls.length, 1);
  assert.equal(shell.dialogs.length, 0, "a successful stop must not show an error");
  shell.app.quit();
  await settle();
  assert.equal(shell.stopCalls.length, 1, "a later quit must never start another stop");
}

async function testFailedStopShowsErrorAndFinishesQuit() {
  const shell = await startShell();
  shell.app.quit();
  await settle();
  assert.equal(shell.stopCalls.length, 1);
  assert.equal(shell.completedQuits, 0);
  shell.stopCalls[0].callback(new Error("synthetic launcher failure"),
    JSON.stringify({ stopped: true }), "");
  await settle();
  assert.equal(shell.dialogs.length, 1, "a failed stop must alert the user");
  assert.match(shell.dialogs[0].title, /WechatVibe/);
  assert.match(shell.dialogs[0].message, /后台进程/);
  assert.equal(shell.completedQuits, 1, "the failed stop must still finish quitting");
  assert.equal(shell.stopCalls.length, 1, "failure must not retry an uncontrolled stop");
}

async function testReadyInitializationFailureStillStopsCreatedBridge() {
  const shell = await startShell({ WECHATVIBE_BRIDGE_CREATED: "1" }, [], undefined,
    { readyThrow: true });
  assert.equal(shell.stopCalls.length, 1,
    "a ready-time initialization failure must still stop the bridge created by this launch");
  assertOwnedStop(shell.stopCalls[0]);
}

async function testQuitWaitsForRecoveryDrain() {
  let finishRecovery;
  const pendingRecovery = new Promise(resolve => { finishRecovery = resolve; });
  const shell = await startShell({}, [], pendingRecovery);
  shell.app.quit();
  await settle();
  assert.equal(shell.monitorStops.length, 1, "quit must stop the recovery monitor");
  assert.equal(shell.stopCalls.length, 0, "owned stop must wait for in-flight recovery");
  assert.equal(shell.completedQuits, 0);

  shell.app.quit();
  await settle();
  assert.equal(shell.stopCalls.length, 0, "a repeated quit must not bypass the recovery drain");

  finishRecovery();
  await settle();
  assert.equal(shell.stopCalls.length, 1, "owned stop starts after recovery has drained");
  assert.equal(shell.completedQuits, 0, "quit still waits for the owned stop result");
  shell.stopCalls[0].callback(null, JSON.stringify({ stopped: true }), "");
  await settle();
  assert.equal(shell.completedQuits, 1);
  assert.equal(shell.stopCalls.length, 1);
}

async function testUpdateHandoffWaitsUntilReady() {
  const shell = await startShell();
  assert.equal(typeof shell.updateOptions?.pauseRecovery, "function");
  shell.updateOptions.pauseRecovery();
  shell.app.quit();
  await settle();
  assert.equal(shell.stopCalls.length, 0, "preparing handoff must not start a second stop");
  assert.equal(shell.completedQuits, 0, "preparing handoff must block premature exit");
  shell.updateOptions.quit();
  await settle();
  assert.equal(shell.stopCalls.length, 0, "ready handoff already stopped its bridge");
  assert.equal(shell.completedQuits, 1);
}

async function testAbortedHandoffResumesNormalExit() {
  const shell = await startShell();
  shell.updateOptions.pauseRecovery();
  assert.equal(shell.closeWindow(), true, "window close must be blocked during handoff preparation");
  await settle();
  assert.equal(shell.stopCalls.length, 0);
  assert.equal(shell.completedQuits, 0);
  shell.updateOptions.resumeRecovery();
  await settle();
  assert.equal(shell.stopCalls.length, 1, "aborted handoff must use normal owned stop");
  assert.equal(shell.completedQuits, 0, "normal exit waits for owned stop after resume");
  shell.stopCalls[0].callback(null, JSON.stringify({ alreadyStopped: true }), "");
  await settle();
  assert.equal(shell.completedQuits, 1);
  assert.equal(shell.stopCalls.length, 1);
}

async function testValidationSkipsStop() {
  const shell = await startShell({ WECHATVIBE_UPDATE_VALIDATE: "1" });
  shell.app.quit();
  await settle();
  assert.equal(shell.stopCalls.length, 0, "validation mode must not stop the live bridge");
  assert.equal(shell.completedQuits, 1);
}

async function testUncommittedFinalUpdateStopsOwnedBridge() {
  const shell = await startShell({
    WECHATVIBE_UPDATE_FINAL_READY_FILE: path.join(ROOT, "final-ready.json"),
    WECHATVIBE_UPDATE_FINAL_READY_NONCE: "b".repeat(32),
  });
  shell.app.quit();
  await settle();
  assert.equal(shell.stopCalls.length, 1, "final-update window must stop its owned bridge");
  assert.equal(shell.completedQuits, 0);
  shell.stopCalls[0].callback(null, JSON.stringify({ stopped: true }), "");
  await settle();
  assert.equal(shell.completedQuits, 1);
}

async function testSelfTestSkipsStop() {
  const shell = await startShell({}, ["--self-test"]);
  shell.app.quit();
  await settle();
  assert.equal(shell.stopCalls.length, 0, "shell self-test must not stop a real bridge");
  assert.equal(shell.completedQuits, 1);
}

async function main() {
  // These checks cover the shell lifecycle that owns an update handoff, which only a
  // product with a configured feed has. QQ refusal is asserted in its own file.
  process.env.QQVIBE_PRODUCT = "wechat";
  await testNormalQuitWaitsForOwnedBridge();
  await testFailedStopShowsErrorAndFinishesQuit();
  await testReadyInitializationFailureStillStopsCreatedBridge();
  await testQuitWaitsForRecoveryDrain();
  await testUpdateHandoffWaitsUntilReady();
  await testAbortedHandoffResumesNormalExit();
  await testValidationSkipsStop();
  await testUncommittedFinalUpdateStopsOwnedBridge();
  await testSelfTestSkipsStop();
  process.stdout.write("real-client shell exit lifecycle checks passed\n");
}

main().catch(error => { console.error(error); process.exitCode = 1; });
