"use strict";

// Isolated filesystem fixtures only. No Electron, Python, registry, listener,
// installed client, or source WeChat database is used.
const assert = require("node:assert/strict");
const fs = require("node:fs");
const os = require("node:os");
const path = require("node:path");
const { EventEmitter } = require("node:events");
const { runOperation, recoverOperation, validateCandidate, atomicJournal, healthy,
  copyData, startBridge, launchClient, launchValidation, waitForValidation,
  launchFinalClient, waitForFinal, waitValidationStopped, runOnceAbsent } =
  require("./real-client-update-helper.cjs");

const OLD = "1.0.1";
const NEW = "1.0.2";
// The staged client decides its own local data directory from this manifest.
const DATA_DIR = require("./product-identity.cjs").productProfile().dataDir;
function put(file, content = "fixture") {
  fs.mkdirSync(path.dirname(file), { recursive: true });
  fs.writeFileSync(file, content);
}
function buildClient(root, version, withLocal = false) {
  put(path.join(root, "WechatVibe.exe"));
  put(path.join(root, "resources", "app.asar"));
  put(path.join(root, "resources", "client", "package.json"),
    JSON.stringify({ name: "wechatvibe-runtime", version }));
  put(path.join(root, "resources", "client", "runtime", "python", "python.exe"));
  put(path.join(root, "resources", "client", "scripts", "start-real-client.py"));
  put(path.join(root, "resources", "client", "scripts", "product-identity.json"),
    fs.readFileSync(path.join(__dirname, "product-identity.json"), "utf8"));
  if (withLocal) put(path.join(root, "resources", "client", DATA_DIR, "account", "saved.db"), "private bytes");
}
function fixture() {
  const parent = fs.mkdtempSync(path.join(os.tmpdir(), "wechatvibe-helper-test-"));
  const installRoot = path.join(parent, "WechatVibe");
  const workDir = path.join(parent, ".wechatvibe-update-fixture");
  const candidatePath = path.join(workDir, "win-unpacked");
  buildClient(installRoot, OLD, true);
  buildClient(candidatePath, NEW);
  put(path.join(workDir, "helper", "node.exe"));
  put(path.join(workDir, "helper", "real-client-update-helper.cjs"));
  put(path.join(workDir, `WechatVibe-${NEW}-windows-x64.zip`), "verified fixture archive");
  const op = { schema: 1, action: "install", installRoot, candidatePath, workDir,
    expectedVersion: NEW, previousVersion: OLD, parentPid: 99999,
    port: 45678, instanceId: "a".repeat(64) };
  const file = path.join(workDir, "install-fixture.json");
  put(file, JSON.stringify(op));
  return { parent, op, file };
}
function versionAt(root) {
  return JSON.parse(fs.readFileSync(path.join(root, "resources", "client", "package.json"), "utf8")).version;
}
function deps(extra = {}) {
  const calls = [];
  return { calls, implementations: {
    waitParent: async () => calls.push("waitParent"),
    portVacant: async () => { calls.push("portVacant"); return true; },
    healthy: async () => { calls.push("healthy"); return true; },
    startBridge: async () => { calls.push("startBridge"); return { created: true }; },
    launchValidation: async () => { calls.push("launchValidation"); return { pid: 77777 }; },
    waitForValidation: async () => calls.push("waitForValidation"),
    stopValidation: async () => calls.push("stopValidation"),
    launchFinalClient: async () => { calls.push("launchFinalClient");
      return { pid: 88888, unref: () => calls.push("finalUnref") }; },
    waitForFinal: async () => calls.push("waitForFinal"),
    stopFinal: async () => calls.push("stopFinal"),
    launchClient: async () => { calls.push("launchClient"); return { pid: 77777 }; },
    stopLaunched: async () => calls.push("stopLaunched"),
    stopOwnedBridge: () => calls.push("stopOwnedBridge"),
    setRunOnce: () => calls.push("setRunOnce"),
    clearRunOnce: () => calls.push("clearRunOnce"),
    runOnceAbsent: () => true,
    ...extra,
  } };
}
function rollbackOp(f) {
  const op = { ...f.op, action: "rollback", candidatePath: path.join(f.op.workDir, "backup"),
    expectedVersion: OLD, previousVersion: NEW };
  const file = path.join(op.workDir, "rollback-fixture.json");
  put(file, JSON.stringify(op));
  return { op, file };
}
function nextUpdate(f, suffix, expectedVersion) {
  const workDir = path.join(f.parent, `.wechatvibe-update-${suffix}`);
  const candidatePath = path.join(workDir, "win-unpacked");
  buildClient(candidatePath, expectedVersion);
  put(path.join(workDir, "helper", "node.exe"));
  put(path.join(workDir, "helper", "real-client-update-helper.cjs"));
  const op = { ...f.op, workDir, candidatePath, expectedVersion,
    previousVersion: versionAt(f.op.installRoot) };
  const file = path.join(workDir, `install-${suffix}.json`);
  put(file, JSON.stringify(op));
  return { op, file };
}
function historicalWork(f, suffix, { installRoot = f.op.installRoot,
  phase = "succeeded", guiStarted = true, restoreGuiStarted } = {}) {
  const workDir = path.join(f.parent, `.wechatvibe-update-${suffix}`);
  const candidatePath = path.join(workDir, "win-unpacked");
  put(path.join(workDir, "helper", "node.exe"));
  put(path.join(workDir, "helper", "real-client-update-helper.cjs"));
  const op = { ...f.op, installRoot, workDir, candidatePath,
    expectedVersion: "0.9.9", previousVersion: "0.9.8" };
  put(path.join(workDir, `install-${suffix}.json`), JSON.stringify(op));
  atomicJournal(path.join(workDir, "journal.json"), {
    schema: 1, action: "install", installRoot, workDir, candidatePath,
    expectedVersion: op.expectedVersion, previousVersion: op.previousVersion,
    phase, guiStarted, restoreGuiStarted, updatedAt: "2026-01-01T00:00:00.000Z",
  });
  return workDir;
}
function ageJournal(workDir, timestamp) {
  const file = path.join(workDir, "journal.json");
  const journal = JSON.parse(fs.readFileSync(file, "utf8"));
  atomicJournal(file, { ...journal, updatedAt: timestamp });
}
function cleanup(parent) {
  const absolute = path.resolve(parent);
  if (path.dirname(absolute) !== path.resolve(os.tmpdir()) ||
      !path.basename(absolute).startsWith("wechatvibe-helper-test-")) {
    throw new Error("unsafe fixture cleanup path");
  }
  fs.rmSync(absolute, { recursive: true, force: true });
}
async function main() {
  {
    const op = { workDir: path.join(os.tmpdir(), ".wechatvibe-update-fixture") };
    const name = "WechatVibeUpdate-.wechatvibe-update-fixture";
    const key = "HKEY_CURRENT_USER\\Software\\Microsoft\\Windows\\CurrentVersion\\RunOnce";
    assert.equal(runOnceAbsent(op, () => `${key}\n    ${name}    REG_SZ    recover\n`), false);
    assert.equal(runOnceAbsent(op, () => `${key}\n    AnotherValue    REG_SZ    command\n`), true);
    assert.equal(runOnceAbsent(op, () => ""), true);
    assert.throws(() => runOnceAbsent(op, () => "unexpected output"), /did not identify its key/);
    const missingKey = (_exe, args) => {
      if (args[1].endsWith("\\RunOnce")) throw new Error("missing or unreadable key");
      return "HKEY_CURRENT_USER\\Software\\Microsoft\\Windows\\CurrentVersion\n";
    };
    assert.equal(runOnceAbsent(op, missingKey), true);
    assert.throws(() => runOnceAbsent(op, (_exe, args) => {
      if (args[1].endsWith("\\RunOnce")) throw new Error("unreadable key");
      return `${key}\n`;
    }), /unreadable key/);
  }
  {
    const child = new EventEmitter();
    child.pid = 77776;
    let unrefed = false;
    child.unref = () => { unrefed = true; };
    const root = path.join(os.tmpdir(), "wechatvibe-visible-launch-fixture");
    const result = await launchClient(root, (program, args, options) => {
      assert.equal(program, path.join(root, "WechatVibe.exe"));
      assert.deepEqual(args, []);
      assert.equal(options.detached, true);
      assert.notEqual(options.windowsHide, true);
      assert.equal(options.env.WECHATVIBE_UPDATE_VALIDATE, undefined);
      assert.equal(options.env.WECHATVIBE_UPDATE_READY_FILE, undefined);
      process.nextTick(() => child.emit("spawn"));
      return child;
    });
    assert.equal(result, child);
    assert.equal(unrefed, true);
  }
  {
    const op = { port: 45678, instanceId: "a".repeat(64), expectedVersion: NEW };
    const response = (appVersion = NEW, pageType = "text/html") => async (_port, route) =>
      route === "/api/health" ? { status: 200, body: JSON.stringify({
        version: "real-ui-1", instanceId: op.instanceId, appVersion }), type: "application/json" } :
        { status: 200, body: "<!doctype html><title>client</title>", type: pageType };
    assert.equal(await healthy(op, NEW, response()), true);
    assert.equal(await healthy(op, NEW, response(OLD)), false);
    assert.equal(await healthy(op, NEW, response(NEW, "application/json")), false);
    assert.equal(await healthy(op, NEW, async (_port, route) => route === "/api/health" ?
      { status: 200, body: JSON.stringify({ version: "real-ui-1", instanceId: "b".repeat(64),
        appVersion: NEW }), type: "application/json" } :
      { status: 200, body: "<html></html>", type: "text/html" }), false);
  }
  {
    const f = fixture();
    try {
      const readyFile = path.join(f.op.workDir, "ui-ready-fixture.json");
      const nonce = "b".repeat(64);
      const child = new EventEmitter();
      child.pid = 77777;
      const spawned = await launchValidation(f.op.candidatePath, f.op, readyFile, nonce,
        (program, args, options) => {
          assert.equal(program, path.join(f.op.candidatePath, "WechatVibe.exe"));
          assert.deepEqual(args, []);
          assert.equal(options.env.WECHATVIBE_UPDATE_VALIDATE, "1");
          assert.notEqual(options.windowsHide, true);
          assert.equal(options.env.WECHATVIBE_UPDATE_READY_FILE, readyFile);
          assert.equal(options.env.WECHATVIBE_UPDATE_READY_NONCE, nonce);
          process.nextTick(() => child.emit("spawn"));
          return child;
        });
      assert.equal(spawned, child);
      put(readyFile, JSON.stringify({ nonce, expectedVersion: NEW, instanceId: f.op.instanceId }));
      await waitForValidation(f.op, child, readyFile, nonce, { alive: () => true,
        timeoutMs: 10, intervalMs: 1 });
      put(readyFile, JSON.stringify({ nonce: "c".repeat(64), expectedVersion: NEW,
        instanceId: f.op.instanceId }));
      await assert.rejects(waitForValidation(f.op, child, readyFile, nonce,
        { alive: () => true, timeoutMs: 10, intervalMs: 1 }), /marker mismatch/);
      fs.unlinkSync(readyFile);
      await assert.rejects(waitForValidation(f.op, child, readyFile, nonce,
        { alive: () => false, timeoutMs: 10, intervalMs: 1 }), /exited/);
      await assert.rejects(waitForValidation(f.op, child, readyFile, nonce,
        { alive: () => true, timeoutMs: 10, intervalMs: 1 }), /timed out/);
    } finally { cleanup(f.parent); }
  }
  {
    const f = fixture();
    try {
      const readyFile = path.join(f.op.workDir, "ui-final-ready-fixture.json");
      const nonce = "d".repeat(64);
      const child = new EventEmitter();
      child.pid = 88888;
      const spawned = await launchFinalClient(f.op.candidatePath, f.op, readyFile, nonce,
        (program, args, options) => {
          assert.equal(program, path.join(f.op.candidatePath, "WechatVibe.exe"));
          assert.deepEqual(args, []);
          assert.equal(options.windowsHide, undefined);
          assert.equal(options.env.WECHATVIBE_UPDATE_FINAL_READY_FILE, readyFile);
          assert.equal(options.env.WECHATVIBE_UPDATE_FINAL_READY_NONCE, nonce);
          assert.equal(options.env.WECHATVIBE_UPDATE_VALIDATE, undefined);
          process.nextTick(() => child.emit("spawn"));
          return child;
        });
      assert.equal(spawned, child);
      put(readyFile, JSON.stringify({ nonce, expectedVersion: NEW, instanceId: f.op.instanceId }));
      await waitForFinal(f.op, child, readyFile, nonce,
        { alive: () => true, timeoutMs: 10, intervalMs: 1 });
      fs.unlinkSync(readyFile);
      await assert.rejects(waitForFinal(f.op, child, readyFile, nonce,
        { alive: () => true, timeoutMs: 10, intervalMs: 1 }), /final UI readiness timed out/);
    } finally { cleanup(f.parent); }
  }
  {
    let probed = false;
    await waitValidationStopped({ validationPid: 77777,
      validationLaunchedAt: new Date(Date.now() - 80 * 1000).toISOString() }, {
      alive: () => { probed = true; return true; },
      pause: async () => { throw new Error("stale PID must not delay recovery"); },
    });
    assert.equal(probed, false);
  }
  {
    const f = fixture();
    try {
      const result = startBridge(f.op.candidatePath, f.op, (program, args, options) => {
        assert.equal(path.basename(program), "python.exe");
        assert.deepEqual(args.slice(1), ["--no-open", "--json"]);
        assert.equal(options.env.CHATUI_PORT, String(f.op.port));
        return JSON.stringify({ version: "real-ui-1", instanceId: f.op.instanceId,
          url: `http://127.0.0.1:${f.op.port}`, created: true });
      });
      assert.equal(result.created, true);
    } finally { cleanup(f.parent); }
  }
  {
    const f = fixture();
    try {
      const d = deps({ waitParent: async () => { throw new Error("parent still alive"); } });
      await assert.rejects(runOperation(f.op, f.file, d.implementations), /parent still alive/);
      assert.equal(versionAt(f.op.installRoot), OLD);
      assert.equal(d.calls.includes("launchClient"), false);
    } finally { cleanup(f.parent); }
  }
  {
    const f = fixture();
    try {
      put(path.join(f.op.candidatePath, "resources", "client", DATA_DIR, "unexpected.db"));
      const d = deps();
      await assert.rejects(runOperation(f.op, f.file, d.implementations),
        new RegExp("contains " + DATA_DIR));
      assert.equal(versionAt(f.op.installRoot), OLD);
      assert.deepEqual(d.calls.filter(call => call === "launchClient"), ["launchClient"]);
    } finally { cleanup(f.parent); }
  }
  {
    const f = fixture();
    try {
      put(path.join(f.op.installRoot, "resources", "client", DATA_DIR, "account", "second.db"),
        "second private bytes");
      let copied = 0;
      const d = deps({ copyData: (source, destination) => copyData(source, destination,
        (from, to, flags) => {
          if (++copied === 2) throw new Error("synthetic disk copy failure");
          fs.copyFileSync(from, to, flags);
        }) });
      await assert.rejects(runOperation(f.op, f.file, d.implementations), /disk copy failure/);
      assert.equal(fs.existsSync(path.join(f.op.candidatePath, "resources", "client", DATA_DIR)), false);
      assert.equal(fs.readFileSync(path.join(f.op.installRoot, "resources", "client", DATA_DIR,
        "account", "second.db"), "utf8"), "second private bytes");
      assert.deepEqual(d.calls.filter(call => call === "launchClient"), ["launchClient"]);
    } finally { cleanup(f.parent); }
  }
  {
    const f = fixture();
    try {
      const d = deps({ setRunOnce: () => { throw new Error("synthetic RunOnce failure"); } });
      await assert.rejects(runOperation(f.op, f.file, d.implementations), /RunOnce failure/);
      assert.equal(versionAt(f.op.installRoot), OLD);
      assert.deepEqual(d.calls.filter(call => call === "launchClient"), ["launchClient"]);
      assert.equal(JSON.parse(fs.readFileSync(path.join(f.op.workDir, "journal.json"))).phase,
        "rolled_back");
      assert.equal(fs.existsSync(path.join(f.op.candidatePath, "resources", "client", DATA_DIR)),
        false);
    } finally { cleanup(f.parent); }
  }
  {
    const f = fixture();
    try {
      put(path.join(f.op.candidatePath, "resources", "client", DATA_DIR, "injected.db"));
      assert.throws(() => validateCandidate(f.op.candidatePath, NEW, true),
        new RegExp("contains " + DATA_DIR));
      // A candidate that carries the *other* product's data directory is rejected too.
      fs.unlinkSync(path.join(f.op.candidatePath, "resources", "client", DATA_DIR, "injected.db"));
      fs.rmdirSync(path.join(f.op.candidatePath, "resources", "client", DATA_DIR));
      put(path.join(f.op.candidatePath, "resources", "client", ".local", "injected.db"));
      assert.throws(() => validateCandidate(f.op.candidatePath, NEW, true), /contains \.local/);
      fs.unlinkSync(path.join(f.op.candidatePath, "resources", "client", ".local", "injected.db"));
      fs.rmdirSync(path.join(f.op.candidatePath, "resources", "client", ".local"));
      const d = deps({ waitForFinal: async (operation, child, file, nonce) => {
        d.calls.push("waitForFinal");
        const journal = JSON.parse(fs.readFileSync(path.join(f.op.workDir, "journal.json")));
        assert.equal(journal.phase, "finalizing");
        assert.equal(journal.guiStarted, false);
        put(file, JSON.stringify({ nonce, expectedVersion: NEW,
          instanceId: operation.instanceId }));
        await waitForFinal(operation, child, file, nonce,
          { alive: () => true, timeoutMs: 10, intervalMs: 1 });
      } });
      assert.equal(await runOperation(f.op, f.file, d.implementations), "succeeded");
      assert.equal(versionAt(f.op.installRoot), NEW);
      assert.equal(versionAt(path.join(f.op.workDir, "backup")), OLD);
      assert.equal(fs.readFileSync(path.join(f.op.installRoot, "resources", "client", DATA_DIR,
        "account", "saved.db"), "utf8"), "private bytes");
      assert.equal(JSON.parse(fs.readFileSync(path.join(f.op.workDir, "journal.json"))).phase, "succeeded");
      assert.equal(fs.existsSync(path.join(f.op.workDir, `WechatVibe-${NEW}-windows-x64.zip`)), false);
      assert(d.calls.indexOf("setRunOnce") < d.calls.indexOf("launchFinalClient"));
      assert(d.calls.indexOf("healthy") < d.calls.indexOf("launchFinalClient"));
      assert(d.calls.indexOf("waitForValidation") < d.calls.indexOf("launchFinalClient"));
      assert(d.calls.indexOf("waitForFinal") < d.calls.indexOf("finalUnref"));
      assert(d.calls.includes("clearRunOnce"));
    } finally { cleanup(f.parent); }
  }
  {
    const f = fixture();
    try {
      const d = deps({ waitForFinal: async (operation, child, file, nonce) => {
        const journal = JSON.parse(fs.readFileSync(path.join(f.op.workDir, "journal.json")));
        assert.equal(journal.phase, "finalizing");
        assert.equal(journal.guiStarted, false);
        return waitForFinal(operation, child, file, nonce,
          { alive: () => true, timeoutMs: 10, intervalMs: 1 });
      } });
      await assert.rejects(runOperation(f.op, f.file, d.implementations), /final UI readiness timed out/);
      assert.equal(versionAt(f.op.installRoot), OLD);
      assert.equal(versionAt(path.join(f.op.workDir, "failed-candidate")), NEW);
      assert.equal(JSON.parse(fs.readFileSync(path.join(f.op.workDir, "journal.json"))).phase,
        "rolled_back");
      assert(d.calls.includes("stopFinal"));
    } finally { cleanup(f.parent); }
  }
  {
    const f = fixture();
    try {
      const d = deps({ waitForValidation: async () => {
        const journal = JSON.parse(fs.readFileSync(path.join(f.op.workDir, "journal.json")));
        assert.equal(journal.phase, "validating");
        assert.equal(journal.guiStarted, undefined);
        assert.equal(d.calls.includes("launchClient"), false);
        throw new Error("synthetic validation timeout");
      } });
      await assert.rejects(runOperation(f.op, f.file, d.implementations), /validation timeout/);
      assert.equal(versionAt(f.op.installRoot), OLD);
      assert.equal(JSON.parse(fs.readFileSync(path.join(f.op.workDir, "journal.json"))).phase,
        "rolled_back");
      assert(d.calls.includes("stopValidation"));
    } finally { cleanup(f.parent); }
  }
  {
    const f = fixture();
    try {
      const d = deps({ startBridge: async () => { throw new Error("synthetic bridge failure"); } });
      await assert.rejects(runOperation(f.op, f.file, d.implementations), /synthetic bridge failure/);
      assert.equal(versionAt(f.op.installRoot), OLD);
      assert.equal(versionAt(path.join(f.op.workDir, "failed-candidate")), NEW);
      assert.equal(JSON.parse(fs.readFileSync(path.join(f.op.workDir, "journal.json"))).phase, "rolled_back");
      assert.equal(fs.existsSync(path.join(f.op.workDir, `WechatVibe-${NEW}-windows-x64.zip`)), false);
    } finally { cleanup(f.parent); }
  }
  {
    const f = fixture();
    try {
      const d = deps({ startBridge: async () => { throw new Error("synthetic bridge failure"); },
        launchClient: async () => { throw new Error("synthetic old GUI failure"); } });
      await assert.rejects(runOperation(f.op, f.file, d.implementations), /bridge failure/);
      assert.equal(versionAt(f.op.installRoot), OLD);
      let journal = JSON.parse(fs.readFileSync(path.join(f.op.workDir, "journal.json")));
      assert.equal(journal.phase, "rolled_back");
      assert.equal(journal.restoreGuiStarted, false);
      assert.equal(await recoverOperation(f.op, f.file, deps().implementations), "rolled_back");
      journal = JSON.parse(fs.readFileSync(path.join(f.op.workDir, "journal.json")));
      assert.equal(journal.restoreGuiStarted, true);
    } finally { cleanup(f.parent); }
  }
  {
    const f = fixture();
    try {
      const journal = { schema: 1, action: "install", installRoot: f.op.installRoot,
        workDir: f.op.workDir, candidatePath: f.op.candidatePath,
        expectedVersion: NEW, previousVersion: OLD, phase: "validating",
        validationLaunchedAt: new Date().toISOString(), validationPid: 77777 };
      atomicJournal(path.join(f.op.workDir, "journal.json"), journal);
      fs.renameSync(f.op.installRoot, path.join(f.op.workDir, "backup"));
      fs.renameSync(f.op.candidatePath, f.op.installRoot);
      const d = deps({ waitValidationStopped: async () => {},
        healthy: async () => { throw new Error("recovery must not commit on backend health"); } });
      assert.equal(await recoverOperation(f.op, f.file, d.implementations), "rolled_back");
      assert.equal(versionAt(f.op.installRoot), OLD);
      assert.equal(versionAt(path.join(f.op.workDir, "failed-candidate")), NEW);
    } finally { cleanup(f.parent); }
  }
  {
    const f = fixture();
    try {
      const journal = { schema: 1, action: "install", installRoot: f.op.installRoot,
        workDir: f.op.workDir, candidatePath: f.op.candidatePath,
        expectedVersion: NEW, previousVersion: OLD, phase: "rolled_back",
        restoreGuiStarted: false };
      atomicJournal(path.join(f.op.workDir, "journal.json"), journal);
      const calls = [];
      const d = deps({ portVacant: async () => false,
        healthy: async (_op, requiredVersion) => {
          assert.equal(requiredVersion, OLD);
          calls.push("health");
          return true;
        },
        launchClient: async () => { calls.push("gui"); return { pid: 77777 }; } });
      assert.equal(await recoverOperation(f.op, f.file, d.implementations), "rolled_back");
      assert.deepEqual(calls, ["health", "gui"]);
    } finally { cleanup(f.parent); }
  }
  {
    const f = fixture();
    try {
      const journal = { schema: 1, action: "install", installRoot: f.op.installRoot,
        workDir: f.op.workDir, candidatePath: f.op.candidatePath,
        expectedVersion: NEW, previousVersion: OLD, phase: "prepared" };
      atomicJournal(path.join(f.op.workDir, "journal.json"), journal);
      fs.renameSync(f.op.installRoot, path.join(f.op.workDir, "backup"));
      const d = deps();
      assert.equal(await recoverOperation(f.op, f.file, d.implementations), "rolled_back");
      assert.equal(versionAt(f.op.installRoot), OLD);
      assert.equal(JSON.parse(fs.readFileSync(path.join(f.op.workDir, "journal.json"))).phase, "rolled_back");
    } finally { cleanup(f.parent); }
  }
  {
    const f = fixture();
    try {
      const journal = { schema: 1, action: "install", installRoot: f.op.installRoot,
        workDir: f.op.workDir, candidatePath: f.op.candidatePath,
        expectedVersion: NEW, previousVersion: OLD, phase: "starting" };
      atomicJournal(path.join(f.op.workDir, "journal.json"), journal);
      fs.renameSync(f.op.installRoot, path.join(f.op.workDir, "backup"));
      fs.renameSync(f.op.candidatePath, f.op.installRoot);
      let probes = 0;
      const d = deps({ healthy: async () => false,
        portVacant: async () => ++probes > 1 });
      assert.equal(await recoverOperation(f.op, f.file, d.implementations), "rolled_back");
      assert.equal(versionAt(f.op.installRoot), OLD);
      assert.equal(versionAt(path.join(f.op.workDir, "failed-candidate")), NEW);
      assert(d.calls.includes("stopOwnedBridge"));
    } finally { cleanup(f.parent); }
  }
  {
    const f = fixture();
    try {
      await runOperation(f.op, f.file, deps().implementations);
      put(path.join(f.op.installRoot, "resources", "client", DATA_DIR, "account", "recent.db"),
        "recent private bytes");
      const r = rollbackOp(f);
      const d = deps({ waitForFinal: async (operation, child, file, nonce) =>
        waitForFinal(operation, child, file, nonce,
          { alive: () => true, timeoutMs: 10, intervalMs: 1 }) });
      await assert.rejects(runOperation(r.op, r.file, d.implementations),
        /final UI readiness timed out/);
      assert.equal(versionAt(f.op.installRoot), NEW);
      assert.equal(versionAt(path.join(f.op.workDir, "backup")), OLD);
      assert.equal(fs.readFileSync(path.join(f.op.installRoot, "resources", "client", DATA_DIR,
        "account", "recent.db"), "utf8"), "recent private bytes");
      assert.equal(JSON.parse(fs.readFileSync(path.join(f.op.workDir, "journal.json"))).phase,
        "succeeded");
    } finally { cleanup(f.parent); }
  }
  {
    const f = fixture();
    try {
      await runOperation(f.op, f.file, deps().implementations);
      put(path.join(f.op.installRoot, "resources", "client", DATA_DIR, "account", "recent.db"),
        "recent private bytes");
      const r = rollbackOp(f);
      assert.equal(await runOperation(r.op, r.file, deps().implementations), "succeeded");
      assert.equal(versionAt(f.op.installRoot), OLD);
      assert.equal(versionAt(path.join(f.op.workDir, "backup")), NEW);
      assert.equal(fs.readFileSync(path.join(f.op.installRoot, "resources", "client", DATA_DIR,
        "account", "recent.db"), "utf8"), "recent private bytes");
      const journal = JSON.parse(fs.readFileSync(path.join(f.op.workDir, "journal.json")));
      assert.deepEqual([journal.phase, journal.expectedVersion, journal.previousVersion],
        ["succeeded", OLD, NEW]);
      const forward = { ...r.op, expectedVersion: NEW, previousVersion: OLD };
      const forwardFile = path.join(f.op.workDir, "rollback-fixture-2.json");
      put(forwardFile, JSON.stringify(forward));
      assert.equal(await runOperation(forward, forwardFile, deps().implementations), "succeeded");
      assert.equal(versionAt(f.op.installRoot), NEW);
      assert.equal(versionAt(path.join(f.op.workDir, "backup")), OLD);
      assert.equal(fs.readFileSync(path.join(f.op.installRoot, "resources", "client", DATA_DIR,
        "account", "recent.db"), "utf8"), "recent private bytes");
    } finally { cleanup(f.parent); }
  }
  {
    const f = fixture();
    try {
      await runOperation(f.op, f.file, deps().implementations);
      const r = rollbackOp(f);
      fs.renameSync(f.op.installRoot, path.join(f.op.workDir, "current-backup"));
      fs.renameSync(r.op.candidatePath, f.op.installRoot);
      atomicJournal(path.join(f.op.workDir, "journal.json"), { schema: 1,
        action: "rollback", installRoot: f.op.installRoot, workDir: f.op.workDir,
        candidatePath: r.op.candidatePath, expectedVersion: OLD, previousVersion: NEW,
        phase: "succeeded", backupCommitPending: true, guiStarted: false });
      assert.equal(await recoverOperation(r.op, r.file, deps().implementations), "succeeded");
      assert.equal(versionAt(f.op.installRoot), OLD);
      assert.equal(versionAt(path.join(f.op.workDir, "backup")), NEW);
      const journal = JSON.parse(fs.readFileSync(path.join(f.op.workDir, "journal.json")));
      assert.equal(journal.backupCommitPending, false);
      assert.equal(journal.guiStarted, true);
    } finally { cleanup(f.parent); }
  }
  {
    const f = fixture();
    try {
      await runOperation(f.op, f.file, deps().implementations);
      put(path.join(f.op.installRoot, "resources", "client", DATA_DIR, "account", "recent.db"),
        "recent private bytes");
      const r = rollbackOp(f);
      let copied = 0;
      const d = deps({ copyData: (source, destination) => copyData(source, destination,
        (from, to, flags) => {
          if (++copied === 2) throw new Error("synthetic rollback copy failure");
          fs.copyFileSync(from, to, flags);
        }) });
      await assert.rejects(runOperation(r.op, r.file, d.implementations), /rollback copy failure/);
      assert.equal(versionAt(f.op.installRoot), NEW);
      assert.equal(versionAt(path.join(f.op.workDir, "backup")), OLD);
      assert.equal(fs.existsSync(path.join(f.op.workDir, "rollback-local-copy-rollback-fixture")),
        false);
      assert.equal(fs.readFileSync(path.join(f.op.installRoot, "resources", "client", DATA_DIR,
        "account", "recent.db"), "utf8"), "recent private bytes");
      assert.deepEqual(d.calls.filter(call => call === "launchClient"), ["launchClient"]);
    } finally { cleanup(f.parent); }
  }
  {
    const f = fixture();
    try {
      await runOperation(f.op, f.file, deps().implementations);
      put(path.join(f.op.installRoot, "resources", "client", DATA_DIR, "account", "recent.db"),
        "recent private bytes");
      const r = rollbackOp(f);
      const d = deps({ startBridge: async () => { throw new Error("synthetic rollback bridge failure"); } });
      await assert.rejects(runOperation(r.op, r.file, d.implementations), /rollback bridge failure/);
      assert.equal(versionAt(f.op.installRoot), NEW);
      assert.equal(versionAt(path.join(f.op.workDir, "backup")), OLD);
      assert.equal(fs.readFileSync(path.join(f.op.installRoot, "resources", "client", DATA_DIR,
        "account", "recent.db"), "utf8"), "recent private bytes");
      assert.equal(fs.readFileSync(path.join(f.op.workDir, "backup", "resources", "client",
        DATA_DIR, "account", "saved.db"), "utf8"), "private bytes");
      assert.equal(fs.existsSync(path.join(f.op.workDir, "backup", "resources", "client",
        DATA_DIR, "account", "recent.db")), false);
      assert.equal(JSON.parse(fs.readFileSync(path.join(f.op.workDir, "journal.json"))).phase, "succeeded");
    } finally { cleanup(f.parent); }
  }
  {
    const f = fixture();
    try {
      // Three successful updates can coexist until RunOnce is demonstrably
      // absent. The third completion then keeps only its own rollback backup.
      const registered = deps({ runOnceAbsent: () => false }).implementations;
      assert.equal(await runOperation(f.op, f.file, registered), "succeeded");
      ageJournal(f.op.workDir, "2026-01-01T00:00:00.000Z");
      const second = nextUpdate(f, "second", "1.0.3");
      assert.equal(await runOperation(second.op, second.file, registered), "succeeded");
      ageJournal(second.op.workDir, "2026-01-02T00:00:00.000Z");
      const third = nextUpdate(f, "third", "1.0.4");
      const active = historicalWork(f, "active", { phase: "validating", guiStarted: false });
      const failedRecovery = historicalWork(f, "failed-recovery", {
        phase: "failed", guiStarted: false });
      const wrongRoot = historicalWork(f, "other-root", {
        installRoot: path.join(f.parent, "Unrelated") });
      const linked = historicalWork(f, "linked");
      const outside = path.join(f.parent, "outside");
      put(path.join(outside, "sentinel.txt"), "keep");
      fs.symlinkSync(outside, path.join(linked, "escape"),
        process.platform === "win32" ? "junction" : "dir");
      const stillRegistered = historicalWork(f, "registered");
      const absent = deps({ runOnceAbsent: operation =>
        operation.workDir !== stillRegistered }).implementations;
      assert.equal(await runOperation(third.op, third.file, absent), "succeeded");
      assert.equal(versionAt(f.op.installRoot), "1.0.4");
      assert.equal(versionAt(path.join(third.op.workDir, "backup")), "1.0.3");
      assert.equal(fs.existsSync(f.op.workDir), false);
      assert.equal(fs.existsSync(second.op.workDir), false);
      for (const kept of [third.op.workDir, active, failedRecovery, wrongRoot,
        linked, stillRegistered]) {
        assert.equal(fs.existsSync(kept), true, `must preserve ${path.basename(kept)}`);
      }
      assert.equal(fs.readFileSync(path.join(outside, "sentinel.txt"), "utf8"), "keep");

      const failedRemoval = historicalWork(f, "failed-removal");
      const failureDeps = deps({ removeOldWorkDir: () => {
        throw new Error("synthetic old directory cleanup failure");
      } }).implementations;
      assert.equal(await recoverOperation(third.op, third.file, failureDeps), "succeeded");
      assert.equal(fs.existsSync(failedRemoval), true);
      assert.equal(versionAt(f.op.installRoot), "1.0.4");
      assert.match(JSON.parse(fs.readFileSync(path.join(third.op.workDir, "journal.json")))
        .cleanupError, /synthetic old directory cleanup failure/);
    } finally { cleanup(f.parent); }
  }
  {
    const f = fixture();
    try {
      assert.equal(await runOperation(f.op, f.file, deps().implementations), "succeeded");
      const old = historicalWork(f, "before-rollback");
      const rollback = rollbackOp(f);
      assert.equal(await runOperation(rollback.op, rollback.file, deps().implementations),
        "succeeded");
      assert.equal(fs.existsSync(old), false);
      assert.equal(fs.existsSync(f.op.workDir), true);
      assert.equal(versionAt(f.op.installRoot), OLD);
      assert.equal(versionAt(path.join(f.op.workDir, "backup")), NEW);
    } finally { cleanup(f.parent); }
  }
  process.stdout.write("real-client update helper checks passed\n");
}

main().catch(error => { console.error(error); process.exitCode = 1; });
