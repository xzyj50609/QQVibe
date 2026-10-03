"use strict";

const { execFile, spawn } = require("node:child_process");
const { randomUUID } = require("node:crypto");
const fs = require("node:fs");
const path = require("node:path");
const { checkForUpdates, downloadAndStageUpdate } = require("./real-client-update.cjs");
const { productProfile, stateDir } = require("./product-identity.cjs");

const BUSY = new Set(["downloading", "verifying", "extracting", "installing", "restarting"]);
const WORK_PREFIX = ".wechatvibe-update-";

function samePath(a, b) {
  return path.resolve(a).toLowerCase() === path.resolve(b).toLowerCase();
}

function directChild(candidate, parent) {
  return samePath(path.dirname(candidate), parent);
}

function readRollback(parent, installRoot, currentVersion, productName = "WechatVibe") {
  let entries;
  try { entries = fs.readdirSync(parent, { withFileTypes: true }); }
  catch (_) { return null; }
  const candidates = [];
  for (const entry of entries) {
    if (!entry.isDirectory() || !entry.name.startsWith(WORK_PREFIX)) continue;
    const workDir = path.join(parent, entry.name);
    try {
      if (fs.lstatSync(workDir).isSymbolicLink()) continue;
      const journal = JSON.parse(fs.readFileSync(path.join(workDir, "journal.json"), "utf8"));
      const backup = path.join(workDir, "backup");
      if (journal.schema !== 1 || journal.phase !== "succeeded" ||
          !samePath(journal.installRoot, installRoot) || journal.expectedVersion !== currentVersion ||
          typeof journal.previousVersion !== "string" || !fs.statSync(backup).isDirectory() ||
          !fs.statSync(path.join(backup, productName + ".exe")).isFile()) continue;
      candidates.push({ workDir, backup, version: journal.previousVersion,
        mtime: fs.statSync(path.join(workDir, "journal.json")).mtimeMs });
    } catch (_) { /* A damaged operation must not become a rollback option. */ }
  }
  candidates.sort((a, b) => b.mtime - a.mtime);
  return candidates[0] || null;
}

function runLauncher(python, root, port, args) {
  return new Promise((resolve, reject) => {
    execFile(python, [path.join(root, "scripts", "start-real-client.py"), ...args], {
      cwd: root, windowsHide: true, timeout: 230000, maxBuffer: 65536,
      env: { ...process.env, CHATUI_PORT: String(port), WECHATVIBE_CLIENT_ROOT: root,
        WECHATVIBE_PYTHON: python },
    }, (error, stdout) => {
      if (error) return reject(new Error("本地分析服务未能安全退出"));
      try { resolve(JSON.parse(stdout)); }
      catch (_) { reject(new Error("本地分析服务状态异常")); }
    });
  });
}

function copyHelper(root, workDir) {
  const sourceNode = path.join(root, "runtime", "node", "node.exe");
  const sourceScript = path.join(root, "scripts", "real-client-update-helper.cjs");
  if (!fs.statSync(sourceNode).isFile() || !fs.statSync(sourceScript).isFile()) {
    throw new Error("更新组件不完整");
  }
  const helperDir = path.join(workDir, "helper");
  fs.mkdirSync(helperDir, { recursive: true });
  const node = path.join(helperDir, "node.exe");
  const script = path.join(helperDir, "real-client-update-helper.cjs");
  fs.copyFileSync(sourceNode, node);
  fs.copyFileSync(sourceScript, script);
  return { node, script };
}

function spawnHelper(helper, operationFile, workDir) {
  return new Promise((resolve, reject) => {
    const child = spawn(helper.node, [helper.script, operationFile], {
      cwd: workDir, detached: true, windowsHide: true, stdio: "ignore",
      env: { ...process.env, WECHATVIBE_CLIENT_ROOT: "", CHATUI_PORT: "" },
    });
    child.once("error", reject);
    child.once("spawn", () => { child.unref(); resolve(child.pid); });
  });
}

function createUpdateController({ app, root, port, instanceId, onState, pauseRecovery,
  resumeRecovery, quit, profile, splitDownload = false, checkImpl = checkForUpdates, stageImpl = downloadAndStageUpdate }) {
  const productName = profile?.productName || "WechatVibe";
  const requestedInstallRoot = path.resolve(root, "..", "..");
  // Staging uses real paths. Resolve ancestor aliases before comparing or
  // handing paths to the helper; keep the install directory itself subject
  // to staging's existing symlink rejection.
  const parent = fs.realpathSync.native(path.dirname(requestedInstallRoot));
  const installRoot = path.join(parent, path.basename(requestedInstallRoot));
  const currentVersion = app.getVersion();
  let state = { phase: "idle", currentVersion, rollbackVersion: null };
  let busy = false;
  let staged = null;

  const notify = (phase, detail = {}) => {
    state = { ...state, ...detail, phase, currentVersion };
    onState?.({ ...state });
    return { ...state };
  };

  function getState() {
    const rollback = readRollback(parent, installRoot, currentVersion, productName);
    return { ...state, rollbackVersion: rollback?.version || null };
  }

  async function check() {
    if (busy || BUSY.has(state.phase) || state.phase === "ready") return getState();
    busy = true;
    try {
      const result = await checkImpl(currentVersion, { profile, channel: profile?.key === "qq" ? "preview" : "stable" });
      staged = null;
      return notify(result.status, { notes: "", totalBytes: 0, error: null, latestVersion: null,
        ...result, rollbackVersion: getState().rollbackVersion });
    } catch (_) {
      return notify("server-error", { error: null });
    } finally { busy = false; }
  }

  async function handoff(action, candidatePath, workDir, expectedVersion) {
    if (!app.isPackaged || process.platform !== "win32" || !directChild(workDir, parent) ||
        !samePath(path.dirname(candidatePath), workDir) ||
        !fs.existsSync(path.join(candidatePath, productName + ".exe"))) {
      throw new Error("更新包或安装位置不可用");
    }
    const python = path.join(root, "runtime", "python", "python.exe");
    const helper = copyHelper(root, workDir);
    const operationFile = path.join(workDir, `${action}-${randomUUID()}.json`);
    const operation = { schema: 1, action, installRoot, candidatePath, workDir,
      expectedVersion, previousVersion: currentVersion, parentPid: process.pid,
      port, instanceId, productName };
    fs.writeFileSync(operationFile, JSON.stringify(operation), { flag: "wx" });
    let stopped = false;
    try {
      await pauseRecovery?.();
      const result = await runLauncher(python, root, port, ["--stop-owned-bridge", "--json"]);
      if (result?.stopped !== true && result?.alreadyStopped !== true) {
        throw new Error("本地分析服务未能安全退出");
      }
      stopped = true;
      if (fs.existsSync(path.join(stateDir(productProfile(), "real-client-runtime", root), "no-auto-recovery.json"))) {
        const error = new Error("当前账号已清除，更新已取消");
        error.accountCleared = true;
        throw error;
      }
      notify("installing", { latestVersion: expectedVersion });
      await spawnHelper(helper, operationFile, workDir);
      notify("restarting", { latestVersion: expectedVersion });
      setImmediate(() => quit());
      return getState();
    } catch (error) {
      if (stopped && !error.accountCleared) {
        try { await runLauncher(python, root, port, ["--no-open", "--json"]); }
        catch (_) { /* The monitor or normal exit will handle an unavailable bridge. */ }
      }
      resumeRecovery?.();
      throw error;
    }
  }

  async function download() {
    if (busy || state.phase !== "available") return getState();
    busy = true;
    try {
      staged = await stageImpl(currentVersion, installRoot, value => notify(value.phase, value),
        { profile, channel: profile?.key === "qq" ? "preview" : "stable" });
      return notify("ready", { latestVersion: staged.expectedVersion });
    } catch (error) {
      return notify("failed", { error: error?.message || "下载失败，请重试" });
    } finally { busy = false; }
  }

  async function begin() {
    if (splitDownload && state.phase === "available") return download();
    if (busy) return getState();
    if (state.phase !== "available" && state.phase !== "ready") return getState();
    busy = true;
    try {
      if (!staged) {
        staged = await stageImpl(currentVersion, installRoot, progress => {
          if (!progress || typeof progress.phase !== "string") return;
          notify(progress.phase, {
            latestVersion: state.latestVersion,
            downloadedBytes: progress.downloadedBytes,
            totalBytes: progress.totalBytes,
          });
        }, { profile, channel: profile?.key === "qq" ? "preview" : "stable" });
      }
      notify("ready", { latestVersion: staged.expectedVersion });
      return await handoff("install", staged.candidatePath, staged.workDir, staged.expectedVersion);
    } catch (error) {
      return notify("failed", { error: error?.message || "更新失败，请重试" });
    } finally { busy = false; }
  }

  async function rollback() {
    if (busy) return getState();
    const candidate = readRollback(parent, installRoot, currentVersion, productName);
    if (!candidate) return notify("failed", { error: "没有可用的回退版本" });
    busy = true;
    try {
      return await handoff("rollback", candidate.backup, candidate.workDir, candidate.version);
    } catch (error) {
      return notify("failed", { error: error?.message || "回退失败，请重试" });
    } finally { busy = false; }
  }

  return { getState, check, download, begin, rollback };
}

module.exports = { createUpdateController, readRollback };
