"use strict";

// Runs from workDir/helper with the bundled Node, after the old desktop has
// requested exit. It never examines source WeChat files or legacy processes.
const { execFileSync, spawn } = require("node:child_process");
const crypto = require("node:crypto");
const fs = require("node:fs");
const http = require("node:http");
const net = require("node:net");
const path = require("node:path");

const REPARSE_POINT = 0x400;
const MAX_OPERATION_BYTES = 16 * 1024;
const MAX_JOURNAL_BYTES = 32 * 1024;
const PARENT_EXIT_MS = 120 * 1000;
const PORT_VACANT_MS = 30 * 1000;
const HEALTH_MS = 180 * 1000;
const VALIDATION_MS = 45 * 1000;
const FINAL_READY_MS = 45 * 1000;
const REGISTRY_KEY = "HKCU\\Software\\Microsoft\\Windows\\CurrentVersion\\RunOnce";
const REG_EXE = path.join(process.env.SystemRoot || "C:\\Windows", "System32", "reg.exe");
const sleep = ms => new Promise(resolve => setTimeout(resolve, ms));

function fail(message) { throw new Error(message); }
function samePath(a, b) { return path.resolve(a).toLowerCase() === path.resolve(b).toLowerCase(); }
function present(file) { try { fs.lstatSync(file); return true; } catch (error) {
  if (error.code === "ENOENT") return false;
  throw error;
} }
function regular(file) {
  const stat = fs.lstatSync(file);
  if (!stat.isFile() || stat.isSymbolicLink() || !stat.size) fail("missing regular file: " + file);
  return stat;
}
function directory(file) {
  const stat = fs.lstatSync(file);
  if (!stat.isDirectory() || stat.isSymbolicLink()) fail("unsafe directory: " + file);
}
function noReparse(file) {
  const stat = fs.lstatSync(file);
  if (stat.isSymbolicLink() || (stat.reparseTag && stat.reparseTag !== 0) ||
      (stat.fileAttributes && (stat.fileAttributes & REPARSE_POINT))) {
    fail("reparse point rejected: " + file);
  }
  // realpath also rejects junctions and mount points on Node builds that do
  // not expose the Windows reparse attribute through fs.Stats.
  if (!samePath(fs.realpathSync.native(file), file)) fail("redirected path rejected: " + file);
  return stat;
}
function checkAncestors(file) {
  let cursor = path.resolve(file);
  for (;;) {
    noReparse(cursor);
    const parent = path.dirname(cursor);
    if (parent === cursor) break;
    cursor = parent;
  }
}
function checkTree(root, fresh = false) {
  const stat = noReparse(root);
  if (!stat.isDirectory()) fail("unsafe candidate tree");
  for (const entry of fs.readdirSync(root, { withFileTypes: true })) {
    // Neither product's local user data may ship inside an update candidate.
    if (fresh && LOCAL_DATA_DENY_NAMES.includes(entry.name.toLowerCase())) {
      fail(`update candidate contains ${entry.name}`);
    }
    const child = path.join(root, entry.name);
    const childStat = noReparse(child);
    if (childStat.isDirectory()) checkTree(child, fresh);
    else if (!childStat.isFile()) fail("special file rejected: " + child);
  }
}
function readJson(file, limit) {
  const stat = regular(file);
  if (stat.size > limit) fail("oversized JSON file: " + file);
  return JSON.parse(fs.readFileSync(file, "utf8"));
}
// Both products keep their local user data in a named directory that must survive a
// swap and must never arrive inside a download; the staged client carries the manifest
// that names it, and the deny list is deliberately wider than the active product.
const LOCAL_DATA_DENY_NAMES = [".local", "qqvibedata"];

function localDataDirName(clientTree) {
  const manifest = readJson(path.join(clientTree, "scripts", "product-identity.json"),
    MAX_OPERATION_BYTES);
  const record = manifest.schema === 1 && manifest.products &&
    manifest.products[process.env.QQVIBE_PRODUCT || manifest.default];
  const name = record && record.dataDir;
  if (typeof name !== "string" || !name || name !== path.basename(name) || name === "." || name === "..") {
    fail("product identity for local data is unreadable");
  }
  return name;
}
function atomicJournal(file, value) {
  const temp = file + ".tmp-" + crypto.randomUUID();
  let fd;
  try {
    fd = fs.openSync(temp, "wx", 0o600);
    fs.writeFileSync(fd, JSON.stringify(value) + "\n");
    fs.fsyncSync(fd);
    fs.closeSync(fd);
    fd = undefined;
    fs.renameSync(temp, file);
    // Windows does not allow opening every directory for fsync. The file is
    // synced before the atomic replacement; directory sync is best effort.
    try {
      const dirFd = fs.openSync(path.dirname(file), "r");
      try { fs.fsyncSync(dirFd); } finally { fs.closeSync(dirFd); }
    } catch (_) { /* Windows may deny directory handles. */ }
  } finally {
    if (fd !== undefined) fs.closeSync(fd);
    if (present(temp)) fs.unlinkSync(temp);
  }
}
function version(value) {
  return typeof value === "string" &&
    /^(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)(?:-[0-9A-Za-z.-]+)?$/.test(value);
}
function validateOperation(op, operationFile, { recovery = false } = {}) {
  if (op?.productName !== undefined && !["WechatVibe", "QQVibe"].includes(op.productName)) fail("invalid update product");
  if (!op || typeof op !== "object" || Array.isArray(op) || op.schema !== 1 ||
      !["install", "rollback"].includes(op.action) ||
      !Number.isSafeInteger(op.parentPid) || op.parentPid <= 0 ||
      !Number.isSafeInteger(op.port) || op.port < 1 || op.port > 65535 ||
      !/^[a-f0-9]{64}$/.test(op.instanceId) ||
      !version(op.expectedVersion) || !version(op.previousVersion)) fail("invalid operation schema");
  for (const key of ["installRoot", "workDir", "candidatePath"]) {
    const value = op[key];
    if (typeof value !== "string" || !path.isAbsolute(value) ||
        value !== path.resolve(value) || value.endsWith(" ") || value.endsWith(".")) {
      fail("invalid absolute path: " + key);
    }
  }
  const installParent = path.dirname(op.installRoot);
  if (!samePath(path.dirname(op.workDir), installParent) ||
      !path.basename(op.workDir).startsWith(".wechatvibe-update-") ||
      samePath(op.installRoot, op.workDir) ||
      path.parse(op.installRoot).root.toLowerCase() !== path.parse(op.workDir).root.toLowerCase() ||
      !samePath(op.candidatePath, path.join(op.workDir,
        op.action === "install" ? "win-unpacked" : "backup")) ||
      !samePath(path.dirname(operationFile), op.workDir) ||
      path.extname(operationFile).toLowerCase() !== ".json") fail("unsafe operation topology");
  checkAncestors(installParent);
  checkAncestors(op.workDir);
  if (present(op.installRoot)) checkAncestors(op.installRoot);
  if (present(op.candidatePath)) checkAncestors(op.candidatePath);
  if (!recovery) {
    directory(op.installRoot);
    directory(op.candidatePath);
    const helper = path.join(op.workDir, "helper");
    directory(helper);
    regular(path.join(helper, "node.exe"));
    regular(path.join(helper, "real-client-update-helper.cjs"));
  }
  return op;
}
function validateCandidate(candidate, expectedVersion, fresh, productName = "WechatVibe") {
  if (!["WechatVibe", "QQVibe"].includes(productName)) fail("invalid candidate product");
  checkTree(candidate, fresh);
  regular(path.join(candidate, productName + ".exe"));
  regular(path.join(candidate, "resources", "app.asar"));
  regular(path.join(candidate, "resources", "client", "runtime", "python", "python.exe"));
  regular(path.join(candidate, "resources", "client", "scripts", "start-real-client.py"));
  const metadata = path.join(candidate, "resources", "client", "package.json");
  if (regular(metadata).size > 64 * 1024) fail("oversized candidate metadata");
  const pkg = readJson(metadata, 64 * 1024);
  if (pkg.name !== productName.toLowerCase() + "-runtime" || pkg.version !== expectedVersion) {
    fail("candidate version mismatch");
  }
  if (productName === "QQVibe") {
    const identity = readJson(path.join(candidate, "resources", "client", "scripts", "product-identity.json"), MAX_OPERATION_BYTES);
    if (identity.default !== "qq" || identity.products?.qq?.productName !== "QQVibe" ||
        identity.products.qq.dataDir !== "QQVibeData") fail("candidate product identity mismatch");
  }
}
function hash(file) {
  const h = crypto.createHash("sha256");
  const fd = fs.openSync(file, "r");
  const buffer = Buffer.allocUnsafe(1024 * 1024);
  try {
    for (;;) {
      const length = fs.readSync(fd, buffer, 0, buffer.length, null);
      if (!length) break;
      h.update(buffer.subarray(0, length));
    }
  } finally { fs.closeSync(fd); }
  return h.digest("hex");
}
function removeOwnedTree(root, identity) {
  const stat = noReparse(root);
  if (!stat.isDirectory() || stat.dev !== identity.dev || stat.ino !== identity.ino) {
    fail("owned directory changed before cleanup");
  }
  const walk = directoryPath => {
    const entries = fs.readdirSync(directoryPath, { withFileTypes: true });
    // Leave ownership evidence in place if a large backup fails partway
    // through deletion, so a later successful run can inspect and retry it.
    const rank = name => name === "journal.json" ? 3 :
      /^(?:install|rollback)-[a-zA-Z0-9-]{1,100}\.json$/.test(name) ? 2 :
        name === "helper" ? 1 : 0;
    entries.sort((a, b) => rank(a.name) - rank(b.name));
    for (const entry of entries) {
      const target = path.join(directoryPath, entry.name);
      const targetStat = noReparse(target);
      if (targetStat.isDirectory()) {
        walk(target);
        fs.rmdirSync(target);
      } else if (targetStat.isFile()) fs.unlinkSync(target);
      else fail("unsafe owned entry during cleanup");
    }
  };
  walk(root);
  fs.rmdirSync(root);
  if (present(root)) fail("owned directory cleanup did not finish");
}
function removeOwnedCopy(root, identity) { removeOwnedTree(root, identity); }
function copyData(source, destination, copyFile = fs.copyFileSync) {
  const sourceStat = noReparse(source);
  if (!sourceStat.isDirectory() || present(destination)) fail("unsafe user data copy target");
  fs.mkdirSync(destination);
  const created = fs.lstatSync(destination);
  const walk = (from, to) => {
    for (const entry of fs.readdirSync(from, { withFileTypes: true })) {
      const sourceFile = path.join(from, entry.name);
      const targetFile = path.join(to, entry.name);
      const stat = noReparse(sourceFile);
      if (stat.isDirectory()) {
        fs.mkdirSync(targetFile);
        walk(sourceFile, targetFile);
      } else if (stat.isFile()) {
        copyFile(sourceFile, targetFile, fs.constants.COPYFILE_EXCL);
        const copied = fs.lstatSync(targetFile);
        if (!copied.isFile() || copied.size !== stat.size || hash(sourceFile) !== hash(targetFile)) {
          fail("user data copy mismatch: " + sourceFile);
        }
        const fd = fs.openSync(targetFile, "r+");
        try { fs.fsyncSync(fd); } finally { fs.closeSync(fd); }
      } else fail("special user data file rejected: " + sourceFile);
    }
  };
  try { walk(source, destination); }
  catch (error) {
    try { removeOwnedCopy(destination, created); }
    catch (cleanupError) {
      throw new Error(`user data copy failed; owned copy cleanup failed: ${cleanupError.message}`,
        { cause: error });
    }
    throw error;
  }
}
function preserveBundledModel(oldRoot, candidateRoot) {
  const oldModel = path.join(oldRoot, "resources", "client", ".models", "laya");
  const newModel = path.join(candidateRoot, "resources", "client", ".models", "laya");
  if (present(path.join(newModel, "model.onnx")) || !present(path.join(oldModel, "model.onnx"))) return false;
  const manifest = readJson(path.join(candidateRoot, "resources", "client", "scripts", "model-files.json"), 16 * 1024);
  const expected = ["model.onnx", "onnx_config.json", "README.md", "rl_agent_config.json",
    "tokenizer/tokenizer_config.json", "tokenizer/tokenizer.json"];
  if (manifest.schema !== 1 || !manifest.files ||
      Object.keys(manifest.files).sort().join("\n") !== expected.sort().join("\n")) {
    fail("candidate model pin manifest is invalid");
  }
  const client = path.join(candidateRoot, "resources", "client");
  const local = path.join(client, localDataDirName(client));
  const models = path.join(local, "models");
  const target = path.join(models, "laya");
  const createdDirs = [];
  const createdFiles = [];
  const ensure = directoryPath => {
    if (present(directoryPath)) {
      if (!noReparse(directoryPath).isDirectory()) fail("model storage path is unsafe");
    } else {
      fs.mkdirSync(directoryPath);
      createdDirs.push(directoryPath);
    }
  };
  try {
    if (present(target)) {
      if (!noReparse(target).isDirectory()) fail("existing model directory is unsafe");
      for (const name of expected) {
        const file = path.join(target, ...name.split("/"));
        if (regular(file).size !== manifest.files[name].bytes || hash(file) !== manifest.files[name].sha256) {
          fail("existing downloaded model differs from pinned version");
        }
      }
      return false;
    }
    for (const name of expected) {
      const spec = manifest.files[name];
      if (!Number.isSafeInteger(spec.bytes) || spec.bytes < 1 ||
          !/^[a-f0-9]{64}$/.test(spec.sha256)) fail("candidate model pin is invalid");
      const source = path.join(oldModel, ...name.split("/"));
      checkAncestors(source);
      if (regular(source).size !== spec.bytes || hash(source) !== spec.sha256) {
        fail("old bundled model differs from pinned version");
      }
    }
    ensure(local);
    ensure(models);
    const temporary = path.join(models, ".laya-preserve-" + crypto.randomUUID());
    ensure(temporary);
    ensure(path.join(temporary, "tokenizer"));
    for (const name of expected) {
      const source = path.join(oldModel, ...name.split("/"));
      const destination = path.join(temporary, ...name.split("/"));
      try { fs.linkSync(source, destination); }
      catch (error) {
        if (!["EXDEV", "EPERM", "EACCES"].includes(error.code)) throw error;
        fs.copyFileSync(source, destination, fs.constants.COPYFILE_EXCL);
      }
      createdFiles.push(destination);
      if (regular(destination).size !== manifest.files[name].bytes || hash(destination) !== manifest.files[name].sha256) {
        fail("preserved model copy differs from original");
      }
    }
    fs.renameSync(temporary, target);
    createdDirs.splice(createdDirs.indexOf(path.join(temporary, "tokenizer")), 1);
    createdDirs.splice(createdDirs.indexOf(temporary), 1);
    return true;
  } catch (error) {
    for (const file of createdFiles.reverse()) {
      try { if (present(file)) fs.unlinkSync(file); } catch (_) { /* Preserve the original failure. */ }
    }
    for (const directoryPath of createdDirs.reverse()) {
      try { if (present(directoryPath)) fs.rmdirSync(directoryPath); }
      catch (_) { /* Preserve the original failure. */ }
    }
    throw error;
  }
}
function pidAlive(pid) {
  try { process.kill(pid, 0); return true; }
  catch (error) {
    if (error.code === "ESRCH") return false;
    return true; // Permission errors must fail closed.
  }
}
async function waitParent(pid, timeout = PARENT_EXIT_MS) {
  const end = Date.now() + timeout;
  while (pidAlive(pid)) {
    if (Date.now() >= end) fail("old client did not exit");
    await sleep(250);
  }
}
function portVacant(port) {
  return new Promise((resolve, reject) => {
    const socket = net.connect({ host: "127.0.0.1", port });
    socket.setTimeout(1500);
    socket.once("connect", () => { socket.destroy(); resolve(false); });
    socket.once("error", error => {
      socket.destroy();
      if (error.code === "ECONNREFUSED") resolve(true);
      else reject(error);
    });
    socket.once("timeout", () => { socket.destroy(); reject(new Error("port probe timed out")); });
  });
}
async function waitVacant(port, probe = portVacant, timeout = PORT_VACANT_MS) {
  const end = Date.now() + timeout;
  do {
    if (await probe(port)) return;
    await sleep(250);
  } while (Date.now() < end);
  fail("bridge port is occupied");
}
function getLoopback(port, route) {
  return new Promise((resolve, reject) => {
    const request = http.get({ hostname: "127.0.0.1", port, path: route,
      timeout: 1500, headers: { Host: `127.0.0.1:${port}` } }, response => {
      const chunks = [];
      let size = 0;
      response.on("data", chunk => {
        size += chunk.length;
        if (size > 128 * 1024) {
          request.destroy(new Error("oversized health response"));
        } else chunks.push(chunk);
      });
      response.on("end", () => resolve({ status: response.statusCode,
        type: response.headers["content-type"] || "", body: Buffer.concat(chunks).toString("utf8") }));
    });
    request.once("timeout", () => request.destroy(new Error("health timeout")));
    request.once("error", reject);
  });
}
async function healthy(op, requiredVersion = op.expectedVersion, request = getLoopback) {
  try {
    const health = await request(op.port, "/api/health");
    if (health.status !== 200) return false;
    const body = JSON.parse(health.body);
    if (body.version !== "real-ui-1" || body.instanceId !== op.instanceId ||
        (requiredVersion && body.appVersion !== requiredVersion)) return false;
    const page = await request(op.port, "/");
    return page.status === 200 && /^text\/html\b/i.test(page.type) && page.body.trim().length > 0;
  } catch (_) { return false; }
}
async function waitHealthy(op, check = healthy, timeout = HEALTH_MS, requiredVersion = op.expectedVersion) {
  const end = Date.now() + timeout;
  do {
    if (await check(op, requiredVersion)) return;
    await sleep(500);
  } while (Date.now() < end);
  fail("updated client failed health or UI check");
}
function launchClient(root, spawnImpl = spawn, productName = "WechatVibe") {
  const env = { ...process.env, QQVIBE_PRODUCT: productName === "QQVibe" ? "qq" : "wechat", CHATUI_PORT: "", WECHATVIBE_CLIENT_ROOT: "" };
  delete env.WECHATVIBE_UPDATE_VALIDATE;
  delete env.WECHATVIBE_UPDATE_READY_FILE;
  delete env.WECHATVIBE_UPDATE_READY_NONCE;
  delete env.WECHATVIBE_UPDATE_FINAL_READY_FILE;
  delete env.WECHATVIBE_UPDATE_FINAL_READY_NONCE;
  const child = spawnImpl(path.join(root, productName + ".exe"), [], { cwd: root,
    detached: true, stdio: "ignore", env });
  return new Promise((resolve, reject) => {
    child.once("error", reject);
    child.once("spawn", () => { child.unref(); resolve(child); });
  });
}
function launchValidation(root, op, readyFile, nonce, spawnImpl = spawn) {
  const child = spawnImpl(path.join(root, (op.productName || "WechatVibe") + ".exe"), [], { cwd: root,
    detached: true, stdio: "ignore",
    env: { ...process.env, QQVIBE_PRODUCT: op.productName === "QQVibe" ? "qq" : "wechat", CHATUI_PORT: "", WECHATVIBE_CLIENT_ROOT: "",
      WECHATVIBE_UPDATE_VALIDATE: "1", WECHATVIBE_UPDATE_READY_FILE: readyFile,
      WECHATVIBE_UPDATE_READY_NONCE: nonce } });
  return new Promise((resolve, reject) => {
    child.once("error", reject);
    child.once("spawn", () => resolve(child));
  });
}
function launchFinalClient(root, op, readyFile, nonce, spawnImpl = spawn) {
  const env = { ...process.env, QQVIBE_PRODUCT: op.productName === "QQVibe" ? "qq" : "wechat", CHATUI_PORT: "", WECHATVIBE_CLIENT_ROOT: "",
    WECHATVIBE_UPDATE_FINAL_READY_FILE: readyFile,
    WECHATVIBE_UPDATE_FINAL_READY_NONCE: nonce };
  delete env.WECHATVIBE_UPDATE_VALIDATE;
  delete env.WECHATVIBE_UPDATE_READY_FILE;
  delete env.WECHATVIBE_UPDATE_READY_NONCE;
  const child = spawnImpl(path.join(root, (op.productName || "WechatVibe") + ".exe"), [], { cwd: root,
    detached: true, stdio: "ignore", env });
  return new Promise((resolve, reject) => {
    child.once("error", reject);
    child.once("spawn", () => resolve(child));
  });
}
function validationAlive(child) {
  return child && Number.isSafeInteger(child.pid) && child.pid > 0 &&
    child.exitCode === null && child.signalCode === null && pidAlive(child.pid);
}
async function waitForReadyMarker(op, child, readyFile, nonce,
  { timeoutMs, intervalMs = 200, alive = validationAlive, label } = {}) {
  const end = Date.now() + timeoutMs;
  do {
    if (!alive(child)) fail(`${label} client exited before UI readiness`);
    if (present(readyFile)) {
      noReparse(readyFile);
      const marker = readJson(readyFile, 4096);
      if (marker.nonce !== nonce || marker.expectedVersion !== op.expectedVersion ||
          marker.instanceId !== op.instanceId) fail(`${label} UI marker mismatch`);
      if (!alive(child)) fail(`${label} client exited after UI readiness`);
      return;
    }
    await sleep(intervalMs);
  } while (Date.now() < end);
  fail(`${label} UI readiness timed out`);
}
function waitForValidation(op, child, readyFile, nonce, options = {}) {
  return waitForReadyMarker(op, child, readyFile, nonce,
    { timeoutMs: VALIDATION_MS, label: "validation", ...options });
}
function waitForFinal(op, child, readyFile, nonce, options = {}) {
  return waitForReadyMarker(op, child, readyFile, nonce,
    { timeoutMs: FINAL_READY_MS, label: "final", ...options });
}
async function stopValidation(child) {
  if (!child || !Number.isSafeInteger(child.pid) || child.pid <= 0) return;
  if (pidAlive(child.pid)) {
    try { child.kill(); } catch (_) { /* A process that already exited needs no signal. */ }
    const end = Date.now() + 15000;
    while (pidAlive(child.pid) && Date.now() < end) await sleep(100);
    if (pidAlive(child.pid)) fail("validation client did not stop");
  }
}
async function waitValidationStopped(journal,
  { alive = pidAlive, pause = sleep, now = Date.now } = {}) {
  const pid = journal.validationPid;
  const start = Date.parse(journal.validationLaunchedAt || journal.updatedAt || "");
  if (!Number.isFinite(start)) fail("validation start time missing during recovery");
  // Validation mode self-quits after 60 seconds. A reused PID cannot be
  // safely signalled here. After a 70-second grace, the PID may belong to an
  // unrelated process; the owned bridge and directory checks gate rollback.
  const end = Math.min(start + 70 * 1000, now() + 70 * 1000);
  while (now() < end && (!pid || alive(pid))) await pause(Math.min(250, end - now()));
}
function waitFinalStopped(journal, options = {}) {
  return waitValidationStopped({ validationPid: journal.finalPid,
    validationLaunchedAt: journal.finalLaunchedAt, updatedAt: journal.updatedAt }, options);
}
function startBridge(root, op, execute = execFileSync) {
  const clientRoot = path.join(root, "resources", "client");
  const python = path.join(clientRoot, "runtime", "python", "python.exe");
  const script = path.join(clientRoot, "scripts", "start-real-client.py");
  regular(python);
  regular(script);
  const stdout = execute(python, [script, "--no-open", "--json"], {
    cwd: clientRoot, windowsHide: true, timeout: 45000, maxBuffer: 65536, encoding: "utf8",
    env: { ...process.env, CHATUI_PORT: String(op.port),
      WECHATVIBE_CLIENT_ROOT: clientRoot, WECHATVIBE_PYTHON: python },
  });
  const result = JSON.parse(stdout);
  if (result.version !== "real-ui-1" || result.instanceId !== op.instanceId ||
      result.url !== `http://127.0.0.1:${op.port}`) fail("new bridge identity mismatch");
  return result;
}
function stopOwnedBridge(root, port) {
  const python = path.join(root, "resources", "client", "runtime", "python", "python.exe");
  const script = path.join(root, "resources", "client", "scripts", "start-real-client.py");
  regular(python);
  regular(script);
  const stdout = execFileSync(python, [script, "--stop-owned-bridge", "--json"], {
    cwd: path.join(root, "resources", "client"), windowsHide: true,
    timeout: 45000, maxBuffer: 65536, encoding: "utf8",
    env: { ...process.env, CHATUI_PORT: String(port),
      WECHATVIBE_CLIENT_ROOT: path.join(root, "resources", "client"), WECHATVIBE_PYTHON: python },
  });
  const result = JSON.parse(stdout);
  if (result.stopped !== true && result.alreadyStopped !== true) fail("owned bridge did not stop");
}
function runOnceName(op) { return "WechatVibeUpdate-" + path.basename(op.workDir).slice(0, 80); }
function setRunOnce(op, operationFile) {
  const node = path.join(op.workDir, "helper", "node.exe");
  const script = path.join(op.workDir, "helper", "real-client-update-helper.cjs");
  regular(node);
  regular(script);
  const command = `"${node}" "${script}" --recover "${operationFile}"`;
  execFileSync(REG_EXE, ["add", REGISTRY_KEY, "/v", runOnceName(op), "/t", "REG_SZ",
    "/d", command, "/f"], { windowsHide: true, timeout: 10000, stdio: "ignore" });
}
function clearRunOnce(op) {
  try { execFileSync(REG_EXE, ["delete", REGISTRY_KEY, "/v", runOnceName(op), "/f"],
    { windowsHide: true, timeout: 10000, stdio: "ignore" }); }
  catch (_) { /* RunOnce may already have consumed its own value. */ }
}
function registryKeyListed(output, key) {
  const fullName = "HKEY_CURRENT_USER" + key.slice("HKCU".length);
  return output.split(/\r?\n/).some(line => [key, fullName]
    .some(name => name.toUpperCase() === line.trim().toUpperCase()));
}
function runOnceAbsent(op, execute = execFileSync) {
  // Query the whole key: a failed query cannot distinguish an absent value
  // from a registry access error, so it must never authorize deletion.
  const options = {
    windowsHide: true, timeout: 10000, encoding: "utf8", maxBuffer: 1024 * 1024,
    stdio: ["ignore", "pipe", "ignore"],
  };
  let output;
  try { output = execute(REG_EXE, ["query", REGISTRY_KEY], options); }
  catch (error) {
    // RunOnce itself can disappear after its last value is consumed. Its
    // existing parent must be readable and show no RunOnce child in that case.
    const parent = REGISTRY_KEY.slice(0, REGISTRY_KEY.lastIndexOf("\\"));
    const listing = execute(REG_EXE, ["query", parent], options);
    if (!registryKeyListed(listing, parent) || registryKeyListed(listing, REGISTRY_KEY)) throw error;
    return true;
  }
  // On this Windows build, reg.exe exits successfully with only a blank line
  // for an existing key that has no values. A successful empty query proves
  // that no recovery value remains; nonempty unrecognized output is unsafe.
  if (!registryKeyListed(output, REGISTRY_KEY)) {
    if (!output.trim()) return true;
    fail("RunOnce query did not identify its key");
  }
  const name = runOnceName(op);
  return !output.split(/\r?\n/).some(line => {
    const row = line.trimStart();
    return row.startsWith(name) && /^\s+REG_/.test(row.slice(name.length));
  });
}
function journalPath(op) { return path.join(op.workDir, "journal.json"); }
function save(journal, phase, extra = {}) {
  Object.assign(journal, extra, { phase, updatedAt: new Date().toISOString() });
  atomicJournal(journalPath(journal), journal);
}
function rollbackNames(op, operationFile) {
  const name = path.basename(operationFile, ".json");
  if (!/^[a-zA-Z0-9-]{1,100}$/.test(name)) fail("unsafe rollback operation name");
  return { prior: path.join(op.workDir, `prior-journal-${name}.json`),
    staged: path.join(op.workDir, `rollback-local-copy-${name}`),
    oldLocal: path.join(op.workDir, `backup-local-before-rollback-${name}`),
    failedLocal: path.join(op.workDir, `failed-rollback-local-${name}`) };
}
function restorePriorJournal(op, operationFile) {
  const prior = rollbackNames(op, operationFile).prior;
  if (present(prior)) atomicJournal(journalPath(op), readJson(prior, MAX_JOURNAL_BYTES));
}
function restoreCandidateLocal(op, operationFile, journal) {
  if (op.action !== "rollback") return;
  const { oldLocal, failedLocal } = rollbackNames(op, operationFile);
  const candidateLocal = path.join(op.workDir, "backup", "resources", "client",
    localDataDirName(path.join(op.installRoot, "resources", "client")));
  if (!present(oldLocal) && journal.candidateHadLocal !== false) return;
  if (present(candidateLocal)) {
    if (present(failedLocal)) fail("failed rollback data target occupied");
    fs.renameSync(candidateLocal, failedLocal);
  }
  if (present(oldLocal)) fs.renameSync(oldLocal, candidateLocal);
}
function paths(op) {
  return { backup: path.join(op.workDir, op.action === "install" ? "backup" : "current-backup"),
    failed: path.join(op.workDir, "failed-candidate") };
}
function cleanupArchive(op) {
  const archiveVersion = op.action === "install" ? op.expectedVersion : op.previousVersion;
  if (!/^(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)$/.test(archiveVersion)) return;
  const archive = path.join(op.workDir, `${op.productName || "WechatVibe"}-${archiveVersion}-windows-x64.zip`);
  if (!present(archive)) return;
  noReparse(archive);
  if (!fs.lstatSync(archive).isFile()) fail("archive cleanup target is not a file");
  fs.unlinkSync(archive);
  if (present(archive)) fail("archive cleanup did not finish");
}
function terminalJournal(journal, workDir, installRoot, before = Infinity) {
  if (!journal || journal.schema !== 1 || !["install", "rollback"].includes(journal.action) ||
      journal.installRoot !== installRoot || journal.workDir !== workDir ||
      !version(journal.expectedVersion) || !version(journal.previousVersion) ||
      journal.candidatePath !== path.join(workDir,
        journal.action === "install" ? "win-unpacked" : "backup") ||
      !Number.isFinite(Date.parse(journal.updatedAt || "")) ||
      Date.parse(journal.updatedAt) >= before) return false;
  return (journal.phase === "succeeded" && journal.guiStarted === true &&
      journal.backupCommitPending !== true) ||
    (journal.phase === "rolled_back" && journal.restoreGuiStarted === true &&
      !journal.recoveryError && !journal.restoreLaunchError);
}
function matchingOperation(workDir, journal) {
  regular(path.join(workDir, "helper", "node.exe"));
  regular(path.join(workDir, "helper", "real-client-update-helper.cjs"));
  for (const entry of fs.readdirSync(workDir, { withFileTypes: true })) {
    if (!entry.isFile() || !/^(?:install|rollback)-[a-zA-Z0-9-]{1,100}\.json$/.test(entry.name)) continue;
    const file = path.join(workDir, entry.name);
    try {
      const operation = validateOperation(readJson(file, MAX_OPERATION_BYTES), file,
        { recovery: true });
      if (operation.action === journal.action && operation.installRoot === journal.installRoot &&
          operation.workDir === journal.workDir && operation.candidatePath === journal.candidatePath &&
          operation.expectedVersion === journal.expectedVersion &&
          operation.previousVersion === journal.previousVersion) return true;
    } catch (_) { /* A malformed operation cannot prove ownership. */ }
  }
  return false;
}
function pruneOldWorkDirs(op, { runOnceAbsent: isAbsent = runOnceAbsent,
  removeOldWorkDir = removeOwnedTree } = {}) {
  const current = readJson(journalPath(op), MAX_JOURNAL_BYTES);
  if (!terminalJournal(current, op.workDir, op.installRoot)) return [];
  validateCandidate(path.join(op.workDir, "backup"), current.previousVersion, false, op.productName || "WechatVibe");
  if (!isAbsent(op)) fail("current RunOnce recovery registration remains");
  const parent = path.dirname(op.installRoot);
  const currentTime = Date.parse(current.updatedAt);
  const errors = [];
  for (const entry of fs.readdirSync(parent, { withFileTypes: true })) {
    if (!entry.isDirectory() || !/^\.wechatvibe-update-[a-zA-Z0-9-]+$/.test(entry.name)) continue;
    const workDir = path.join(parent, entry.name);
    if (samePath(workDir, op.workDir)) continue;
    try {
      const stat = noReparse(workDir);
      if (!stat.isDirectory()) continue;
      const journal = readJson(path.join(workDir, "journal.json"), MAX_JOURNAL_BYTES);
      if (!terminalJournal(journal, workDir, op.installRoot, currentTime) ||
          !matchingOperation(workDir, journal)) continue;
      if (!isAbsent({ workDir })) continue;
      // Preflight the entire owned tree, so a reparse point leaves it untouched.
      checkTree(workDir);
      const latest = readJson(path.join(workDir, "journal.json"), MAX_JOURNAL_BYTES);
      if (!terminalJournal(latest, workDir, op.installRoot, currentTime) ||
          latest.updatedAt !== journal.updatedAt || !isAbsent({ workDir })) continue;
      removeOldWorkDir(workDir, stat);
      if (present(workDir)) fail("old work directory cleanup did not finish");
    } catch (error) {
      // No old-directory failure may reverse a committed update or rollback.
      errors.push(`${entry.name}: ${error.message}`);
    }
  }
  return errors;
}
function terminalCleanup(op, journal, deps = {}) {
  const errors = [];
  try { cleanupArchive(op); }
  catch (error) { errors.push(error.message); }
  if (journal.phase === "succeeded" && journal.guiStarted === true) {
    try { errors.push(...pruneOldWorkDirs(op, deps)); }
    catch (error) { errors.push(error.message); }
  }
  if (errors.length) {
    try {
      const current = readJson(journalPath(op), MAX_JOURNAL_BYTES);
      save(current, current.phase, { cleanupError: errors.join("; ").slice(0, 2000) });
    } catch (_) { /* A cleanup error must not reverse a completed swap. */ }
  }
}
async function openCommittedClient(op, journal, deps) {
  const commitBackup = () => {
    if (op.action !== "rollback" || journal.backupCommitPending !== true) return;
    const currentBackup = paths(op).backup;
    if (present(currentBackup) && !present(op.candidatePath)) {
      fs.renameSync(currentBackup, op.candidatePath);
    } else if (!(present(op.candidatePath) && !present(currentBackup))) {
      fail("rollback backup commit topology is unsafe");
    }
    validateCandidate(op.candidatePath, op.previousVersion, false, op.productName || "WechatVibe");
    save(journal, "succeeded", { backupCommitPending: false });
  };
  if (journal.phase === "succeeded" && journal.guiStarted === true) {
    commitBackup();
    deps.clearRunOnce(op);
    terminalCleanup(op, journal, deps);
    return "succeeded";
  }
  const readyFile = path.join(op.workDir, `ui-final-ready-${crypto.randomUUID()}.json`);
  const nonce = crypto.randomBytes(32).toString("hex");
  if (present(readyFile)) fail("final UI marker target already exists");
  save(journal, "finalizing", { guiStarted: false, finalReadyFile: readyFile,
    finalNonce: nonce, finalLaunchedAt: new Date().toISOString(), finalPid: null });
  let child;
  try {
    child = await deps.launchFinalClient(op.installRoot, op, readyFile, nonce);
    save(journal, "finalizing", { finalPid: child.pid || null });
    await deps.waitForFinal(op, child, readyFile, nonce);
    if (op.action === "rollback") {
      save(journal, "succeeded", { guiStarted: true, guiPid: child.pid || null,
        backupCommitPending: true, guiLaunchError: null });
    } else {
      save(journal, "succeeded", { guiStarted: true, guiPid: child.pid || null,
        guiLaunchError: null });
    }
    child.unref?.();
    commitBackup();
    deps.clearRunOnce(op);
    terminalCleanup(op, journal, deps);
    return "succeeded";
  } catch (error) {
    let durableSuccess = false;
    try {
      const persisted = readJson(journalPath(op), MAX_JOURNAL_BYTES);
      durableSuccess = persisted.phase === "succeeded" && persisted.guiStarted === true;
    } catch (_) { /* A failed journal read cannot authorize rollback by itself. */ }
    if (durableSuccess) child?.unref?.();
    else {
      try { await deps.stopFinal(child); }
      catch (stopError) {
        const unsafe = new Error(`final GUI could not be stopped: ${stopError.message}`,
          { cause: error });
        unsafe.finalStopFailed = true;
        throw unsafe;
      }
    }
    throw error;
  }
}
async function relaunchOldIfSafe(op, deps) {
  if (!present(op.installRoot)) return "old installation is missing";
  try {
    validateCandidate(op.installRoot, op.previousVersion, false, op.productName || "WechatVibe");
    if (!(await deps.portVacant(op.port))) return "bridge port is occupied";
    await deps.launchClient(op.installRoot, undefined, op.productName || "WechatVibe");
    return null;
  } catch (error) { return error.message; }
}
async function finishRestored(op, journal, deps, operationFile) {
  if (journal.restoreGuiStarted !== true) {
    validateCandidate(op.installRoot, op.previousVersion, false, op.productName || "WechatVibe");
    const vacant = await deps.portVacant(op.port);
    if (!vacant && !(await deps.healthy(op, op.previousVersion))) {
      fail("occupied port does not serve restored client version");
    }
    // A healthy bridge can be running without a GUI. Electron's single-instance
    // lock safely focuses an already open window when one exists.
    await deps.launchClient(op.installRoot, undefined, op.productName || "WechatVibe");
    save(journal, "rolled_back", { restoreGuiStarted: true, restoreLaunchError: null });
  }
  if (op.action === "rollback") restorePriorJournal(op, operationFile);
  deps.clearRunOnce(op);
  terminalCleanup(op, journal, deps);
  return "rolled_back";
}
async function rollbackFiles(op, journal, deps, operationFile) {
  const { backup, failed } = paths(op);
  const backupExists = present(backup);
  const installExists = present(op.installRoot);
  if (backupExists && installExists) {
    if (present(op.candidatePath) && op.action === "install") {
      fail("ambiguous install directory; manual recovery required");
    }
    if (op.action === "install") {
      if (present(failed)) fail("failed candidate target occupied");
      fs.renameSync(op.installRoot, failed);
    } else {
      if (present(op.candidatePath)) fail("rollback candidate target occupied");
      fs.renameSync(op.installRoot, op.candidatePath);
    }
  } else if (!backupExists && !installExists) fail("installation and backup are both missing");
  if (present(backup)) {
    if (present(op.installRoot)) fail("installation target occupied during restore");
    fs.renameSync(backup, op.installRoot);
  }
  restoreCandidateLocal(op, operationFile, journal);
  save(journal, "rolled_back", { restoreGuiStarted: false });
  try { await finishRestored(op, journal, deps, operationFile); }
  catch (error) {
    save(journal, "rolled_back", { restoreGuiStarted: false,
      restoreLaunchError: error.message });
    // The files are restored; RunOnce retries opening the old UI later.
  }
}
async function recoverOperation(op, operationFile, overrides = {}) {
  const deps = { portVacant, healthy, launchClient, launchFinalClient,
    waitForFinal, stopFinal: stopValidation, stopOwnedBridge,
    waitValidationStopped, waitFinalStopped, setRunOnce, clearRunOnce, runOnceAbsent,
    ...overrides };
  validateOperation(op, operationFile, { recovery: true });
  const file = journalPath(op);
  const journal = readJson(file, MAX_JOURNAL_BYTES);
  if (journal.schema !== 1 || journal.action !== op.action ||
      !samePath(journal.installRoot, op.installRoot) ||
      journal.expectedVersion !== op.expectedVersion || journal.previousVersion !== op.previousVersion) {
    fail("journal does not match operation");
  }
  if (journal.phase === "succeeded") {
    deps.setRunOnce(op, operationFile);
    return openCommittedClient(op, journal, deps);
  }
  if (journal.phase === "rolled_back") {
    deps.setRunOnce(op, operationFile);
    return finishRestored(op, journal, deps, operationFile);
  }
  deps.setRunOnce(op, operationFile);
  if (journal.phase === "validating" || journal.phase === "validated") {
    await deps.waitValidationStopped(journal);
  } else if (journal.phase === "finalizing") {
    await deps.waitFinalStopped(journal);
  }
  let vacant = await deps.portVacant(op.port);
  if (!vacant && ["starting", "validating", "validated", "finalizing"].includes(journal.phase) &&
      present(op.installRoot)) {
    // The recorded PID may have been reused after a reboot. The owned launcher
    // verifies its control record before stopping anything.
    deps.stopOwnedBridge(op.installRoot, op.port);
    vacant = await deps.portVacant(op.port);
  }
  if (!vacant) fail("occupied port prevents safe recovery");
  await rollbackFiles(op, journal, deps, operationFile);
  return "rolled_back";
}
async function runOperation(op, operationFile, overrides = {}) {
  const deps = { waitParent, portVacant, healthy, launchClient, startBridge, copyData,
    launchValidation, waitForValidation, stopValidation,
    launchFinalClient, waitForFinal, stopFinal: stopValidation,
    stopOwnedBridge, setRunOnce, clearRunOnce, runOnceAbsent, ...overrides };
  validateOperation(op, operationFile);
  const journalFile = journalPath(op);
  const names = rollbackNames(op, operationFile);
  const priorFile = names.prior;
  if (op.action === "install" && present(journalFile)) fail("operation journal already exists");
  if (op.action === "rollback") {
    const prior = readJson(journalFile, MAX_JOURNAL_BYTES);
    if (prior.schema !== 1 || prior.phase !== "succeeded" ||
        !samePath(prior.installRoot, op.installRoot) ||
        prior.expectedVersion !== op.previousVersion ||
        prior.previousVersion !== op.expectedVersion || present(priorFile)) {
      fail("rollback journal is not a matching successful install");
    }
  }
  await deps.waitParent(op.parentPid);
  let journal;
  let copiedLocal;
  let validationChild;
  try {
    await waitVacant(op.port, deps.portVacant);
    validateCandidate(op.candidatePath, op.expectedVersion, op.action === "install", op.productName || "WechatVibe");
    if (present(paths(op).backup) || present(paths(op).failed)) fail("swap target already exists");
    const dataDir = localDataDirName(path.join(op.installRoot, "resources", "client"));
    const local = path.join(op.installRoot, "resources", "client", dataDir);
    if (op.action === "install") {
      if (present(local)) {
        const target = path.join(op.candidatePath, "resources", "client", dataDir);
        deps.copyData(local, target);
        copiedLocal = { target, identity: fs.lstatSync(target) };
      }
      if (preserveBundledModel(op.installRoot, op.candidatePath) && !copiedLocal) {
        const target = path.join(op.candidatePath, "resources", "client", dataDir);
        copiedLocal = { target, identity: fs.lstatSync(target) };
      }
    } else {
      if (present(names.staged) || present(names.oldLocal)) fail("rollback data staging already exists");
      if (present(local)) {
        deps.copyData(local, names.staged);
        copiedLocal = { target: names.staged, identity: fs.lstatSync(names.staged) };
      }
      atomicJournal(priorFile, readJson(journalFile, MAX_JOURNAL_BYTES));
    }
    journal = { schema: 1, action: op.action, installRoot: op.installRoot,
      workDir: op.workDir, candidatePath: op.candidatePath,
      expectedVersion: op.expectedVersion, previousVersion: op.previousVersion,
      candidateHadLocal: op.action === "rollback" &&
        present(path.join(op.candidatePath, "resources", "client", dataDir)) };
    save(journal, "prepared");
    try { deps.setRunOnce(op, operationFile); }
    catch (error) {
      if (op.action === "rollback") restorePriorJournal(op, operationFile);
      else save(journal, "rolled_back", { error: error.message });
      throw error;
    }
  } catch (error) {
    if (copiedLocal) {
      try { removeOwnedCopy(copiedLocal.target, copiedLocal.identity); }
      catch (cleanupError) { error.message += `; copied data cleanup failed: ${cleanupError.message}`; }
    }
    const restartError = await relaunchOldIfSafe(op, deps);
    if (!restartError && op.action === "install" && journal?.phase === "rolled_back") {
      try { save(journal, "rolled_back", { restoreGuiStarted: true }); }
      catch (_) { /* The original installation was already reopened. */ }
    }
    if (restartError) error.message += `; old client was not reopened: ${restartError}`;
    throw error;
  }
  try {
    if (op.action === "rollback") {
      const candidateLocal = path.join(op.candidatePath, "resources", "client",
        localDataDirName(path.join(op.installRoot, "resources", "client")));
      const oldLocal = names.oldLocal;
      const staged = names.staged;
      if (present(candidateLocal)) fs.renameSync(candidateLocal, oldLocal);
      if (present(staged)) fs.renameSync(staged, candidateLocal);
    }
    // A second probe closes the gap between the initial wait and first rename.
    if (!(await deps.portVacant(op.port))) fail("bridge port became occupied");
    fs.renameSync(op.installRoot, paths(op).backup);
    save(journal, "old_moved");
    fs.renameSync(op.candidatePath, op.installRoot);
    save(journal, "new_moved");
    const bridge = await deps.startBridge(op.installRoot, op);
    save(journal, "starting", { bridgeCreated: bridge?.created === true });
    await waitHealthy(op, deps.healthy);
    const readyFile = path.join(op.workDir, `ui-ready-${crypto.randomUUID()}.json`);
    const nonce = crypto.randomBytes(32).toString("hex");
    if (present(readyFile)) fail("validation marker target already exists");
    save(journal, "validating", { validationReadyFile: readyFile,
      validationNonce: nonce, validationLaunchedAt: new Date().toISOString(),
      validationPid: null });
    validationChild = await deps.launchValidation(op.installRoot, op, readyFile, nonce);
    save(journal, "validating", { validationPid: validationChild.pid || null });
    await deps.waitForValidation(op, validationChild, readyFile, nonce);
    await deps.stopValidation(validationChild);
    validationChild = null;
    if (!(await deps.healthy(op, op.expectedVersion))) fail("bridge changed after validation UI");
    save(journal, "validated");
    if (op.action === "rollback") {
      save(journal, "validated", { backupCommitPending: true });
    }
    return await openCommittedClient(op, journal, deps);
  } catch (error) {
    if (error.finalStopFailed) throw error;
    let persisted;
    try { persisted = readJson(journalPath(op), MAX_JOURNAL_BYTES); }
    catch (readError) {
      // Without a readable journal, recovery must not discard the backup.
      throw new Error(`update failed; journal unreadable: ${readError.message}`, { cause: error });
    }
    if (persisted.phase === "succeeded" && persisted.guiStarted === true) throw error;
    save(journal, "failed", { error: error.message });
    try {
      await deps.stopValidation(validationChild);
      if (present(op.installRoot) && !present(op.candidatePath) &&
          !(await deps.portVacant(op.port))) {
        deps.stopOwnedBridge(op.installRoot, op.port);
      }
      if (!(await deps.portVacant(op.port))) fail("bridge port occupied during rollback");
      await rollbackFiles(op, journal, deps, operationFile);
    } catch (restoreError) {
      save(journal, "failed", { error: error.message,
        recoveryError: restoreError.message });
      // Keep RunOnce for a reboot attempt. The backup is never deleted here.
    }
    throw error;
  }
}
async function main(argv = process.argv.slice(2)) {
  const recovery = argv[0] === "--recover";
  const operationFile = argv[recovery ? 1 : 0];
  if (argv.length !== (recovery ? 2 : 1) || !operationFile || !path.isAbsolute(operationFile)) {
    fail("usage: helper.cjs [--recover] <absolute operation file>");
  }
  const op = validateOperation(readJson(operationFile, MAX_OPERATION_BYTES), operationFile,
    { recovery });
  if (process.platform !== "win32") fail("Windows update helper requires Windows");
  if (op.productName) process.env.QQVIBE_PRODUCT = op.productName === "QQVibe" ? "qq" : "wechat";
  if (!samePath(__filename, path.join(op.workDir, "helper", "real-client-update-helper.cjs"))) {
    fail("helper must run from its stable copied location");
  }
  return recovery ? recoverOperation(op, operationFile) : runOperation(op, operationFile);
}

if (require.main === module) {
  main().then(result => { process.stdout.write(result + "\n"); })
    .catch(error => { process.stderr.write("update helper: " + error.message + "\n"); process.exitCode = 1; });
}

module.exports = { validateOperation, validateCandidate, copyData, preserveBundledModel, atomicJournal,
  portVacant, healthy, startBridge, launchClient, launchValidation, waitForValidation,
  launchFinalClient, waitForFinal, waitValidationStopped, waitFinalStopped,
  runOnceAbsent, runOperation, recoverOperation, main };
