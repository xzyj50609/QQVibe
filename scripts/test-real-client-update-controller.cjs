"use strict";

const assert = require("node:assert/strict");
const fs = require("node:fs");
const os = require("node:os");
const path = require("node:path");
const { EventEmitter } = require("node:events");
const vm = require("node:vm");
const { createUpdateController, readRollback } = require("./real-client-update-controller.cjs");

function removeOwnedTree(root) {
  if (!fs.existsSync(root)) return;
  for (const entry of fs.readdirSync(root, { withFileTypes: true })) {
    const item = path.join(root, entry.name);
    if (entry.isDirectory() && !entry.isSymbolicLink()) removeOwnedTree(item);
    else fs.unlinkSync(item);
  }
  fs.rmdirSync(root);
}

async function checkJunctionParent(parent) {
  const physicalParent = path.join(parent, "真实安装目录");
  const alias = path.join(parent, "入口别名");
  const installRoot = path.join(physicalParent, "win-unpacked");
  const root = path.join(alias, "win-unpacked", "resources", "client");
  const put = (file, value = "synthetic file") => {
    fs.mkdirSync(path.dirname(file), { recursive: true });
    fs.writeFileSync(file, value);
  };
  put(path.join(installRoot, "resources", "client", "runtime", "node", "node.exe"));
  put(path.join(installRoot, "resources", "client", "scripts", "real-client-update-helper.cjs"));
  fs.symlinkSync(physicalParent, alias, process.platform === "win32" ? "junction" : "dir");
  assert.notEqual(path.resolve(root), fs.realpathSync.native(root));

  const workDir = path.join(physicalParent, ".wechatvibe-update-staged");
  const candidatePath = path.join(workDir, "win-unpacked");
  put(path.join(candidatePath, "WechatVibe.exe"));
  const rollbackWork = path.join(physicalParent, ".wechatvibe-update-previous");
  put(path.join(rollbackWork, "backup", "WechatVibe.exe"));
  put(path.join(rollbackWork, "journal.json"), JSON.stringify({
    schema: 1, phase: "succeeded", installRoot,
    expectedVersion: "1.2.0", previousVersion: "1.0.4",
  }));

  // Mock every process boundary: these placeholder executables are never run.
  const calls = { launcher: [], helper: [], quit: 0 };
  const mockedChildProcess = {
    execFile(program, args, options, callback) {
      calls.launcher.push({ program, args, options });
      assert.ok(args.includes("--stop-owned-bridge"));
      callback(null, JSON.stringify({ stopped: true }));
    },
    spawn(program, args, options) {
      calls.helper.push({ program, args, options });
      const child = new EventEmitter();
      child.pid = 12345;
      child.unref = () => {};
      setImmediate(() => child.emit("spawn"));
      return child;
    },
  };
  const moduleStub = { exports: {} };
  vm.runInNewContext(fs.readFileSync(path.join(__dirname, "real-client-update-controller.cjs"), "utf8"), {
    module: moduleStub,
    require(name) {
      if (name === "node:child_process") return mockedChildProcess;
      if (name === "./real-client-update.cjs") return {};
      if (["node:crypto", "node:fs", "node:path"].includes(name)) return require(name);
      if (name === "./product-identity.cjs") return require("./product-identity.cjs");
      throw new Error(`Unexpected controller import: ${name}`);
    },
    process: { platform: "win32", pid: process.pid, env: {} }, setImmediate,
  });
  const options = {
    app: { getVersion: () => "1.2.0", isPackaged: true }, root,
    port: 34567, instanceId: "0".repeat(64),
    checkImpl: async () => ({ status: "available", latestVersion: "1.2.1" }),
    quit: () => { calls.quit++; },
  };
  let stageCalls = 0;
  const controller = moduleStub.exports.createUpdateController({
    ...options,
    stageImpl: async (currentVersion, receivedInstallRoot) => {
      stageCalls++;
      assert.equal(currentVersion, "1.2.0");
      assert.equal(receivedInstallRoot, installRoot, "staging must receive the canonical installation path");
      return { candidatePath, workDir, expectedVersion: "1.2.1" };
    },
  });
  assert.equal(controller.getState().rollbackVersion, "1.0.4", "an alias must find canonical rollback history");
  assert.equal((await controller.check()).phase, "available");
  assert.equal((await controller.begin()).phase, "restarting");
  await new Promise(resolve => setImmediate(resolve));
  assert.equal(stageCalls, 1);
  assert.equal(calls.launcher.length, 1);
  assert.equal(calls.helper.length, 1);
  assert.equal(calls.quit, 1);
  const operationFile = calls.helper[0].args[1];
  const operation = JSON.parse(fs.readFileSync(operationFile, "utf8"));
  for (const [key, expected] of Object.entries({ installRoot, workDir, candidatePath })) {
    assert.equal(operation[key], expected);
    assert.equal(operation[key], fs.realpathSync.native(operation[key]), `${key} must be canonical`);
  }
  assert.equal(calls.helper[0].options.cwd, workDir);
  assert.equal(controller.getState().rollbackVersion, "1.0.4");

  const outsideCandidate = path.join(parent, "越界候选目录");
  put(path.join(outsideCandidate, "WechatVibe.exe"));
  const rejected = moduleStub.exports.createUpdateController({
    ...options,
    stageImpl: async () => ({ candidatePath: outsideCandidate, workDir, expectedVersion: "1.2.1" }),
  });
  await rejected.check();
  const failed = await rejected.begin();
  assert.equal(failed.phase, "failed");
  assert.equal(failed.error, "更新包或安装位置不可用");
  assert.equal(calls.launcher.length, 1, "an out-of-bounds candidate must not stop the bridge");
  assert.equal(calls.helper.length, 1, "an out-of-bounds candidate must not launch the helper");
  assert.equal(calls.quit, 1);
}

async function main() {
  const parent = fs.realpathSync.native(fs.mkdtempSync(path.join(os.tmpdir(), "wechatvibe-controller-")));
  try {
    const installRoot = path.join(parent, "WechatVibe");
    const clientRoot = path.join(installRoot, "resources", "client");
    fs.mkdirSync(clientRoot, { recursive: true });
    const work = path.join(parent, ".wechatvibe-update-valid");
    fs.mkdirSync(path.join(work, "backup"), { recursive: true });
    fs.writeFileSync(path.join(work, "backup", "WechatVibe.exe"), "previous binary");
    fs.writeFileSync(path.join(work, "journal.json"), JSON.stringify({
      schema: 1, phase: "succeeded", installRoot,
      expectedVersion: "1.0.2", previousVersion: "1.0.1",
    }));
    const damaged = path.join(parent, ".wechatvibe-update-damaged");
    fs.mkdirSync(path.join(damaged, "backup"), { recursive: true });
    fs.writeFileSync(path.join(damaged, "backup", "WechatVibe.exe"), "wrong binary");
    fs.writeFileSync(path.join(damaged, "journal.json"), JSON.stringify({
      schema: 1, phase: "succeeded", installRoot: path.join(parent, "Other"),
      expectedVersion: "1.0.2", previousVersion: "1.0.0",
    }));
    assert.equal(readRollback(parent, installRoot, "1.0.2")?.version, "1.0.1");
    assert.equal(readRollback(parent, installRoot, "1.0.3"), null);

    const controller = createUpdateController({
      app: { getVersion: () => "1.0.2", isPackaged: false }, root: clientRoot,
      port: 34567, instanceId: "0".repeat(64),
      checkImpl: async () => ({ status: "current", latestVersion: "1.0.2" }),
      stageImpl: async () => { throw new Error("No download expected"); },
    });
    assert.equal(controller.getState().rollbackVersion, "1.0.1");
    assert.equal((await controller.check()).phase, "current");
    assert.equal((await controller.begin()).phase, "current");
    await checkJunctionParent(parent);
  } finally {
    removeOwnedTree(parent);
  }
  process.stdout.write("real-client update controller checks passed\n");
}

main().catch(error => { console.error(error); process.exitCode = 1; });
