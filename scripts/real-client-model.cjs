"use strict";

// Download only the pinned Release model asset; the Python installer verifies
// every archive member before placing it in update-preserved local storage.
const crypto = require("node:crypto");
const { execFile } = require("node:child_process");
const fs = require("node:fs");
const path = require("node:path");
const { Readable, Transform } = require("node:stream");
const { pipeline } = require("node:stream/promises");
const { productProfile, stateDir } = require("./product-identity.cjs");

function modelAsset(root) {
  const file = path.join(root, "scripts", "model-asset.json");
  const asset = JSON.parse(fs.readFileSync(file, "utf8"));
  // App releases advance independently of this pinned model's file/hash revision.
  if (asset.schema !== 1 || asset.name !== "WechatVibe-Laya-model-v1.zip" ||
      !/^https:\/\/github\.com\/tswawa\/WechatVibe\/releases\/download\/v\d+\.\d+\.\d+\/WechatVibe-Laya-model-v1\.zip$/.test(asset.url) ||
      !Number.isSafeInteger(asset.bytes) || asset.bytes < 500_000_000 || asset.bytes > 800_000_000 ||
      !/^[a-f0-9]{64}$/.test(asset.sha256)) throw new Error("invalid pinned model asset");
  return asset;
}

function ownedDirectory(parent, name) {
  const target = path.join(parent, name);
  if (fs.existsSync(target)) {
    const info = fs.lstatSync(target);
    if (!info.isDirectory() || info.isSymbolicLink() ||
        path.resolve(fs.realpathSync.native(target)).toLowerCase() !== path.resolve(target).toLowerCase()) {
      throw new Error("model storage path is unsafe");
    }
  } else fs.mkdirSync(target);
  return target;
}

function installWithPython(python, script, archive, root) {
  return new Promise((resolve, reject) => {
    execFile(python, [script, archive, root],
      { cwd: root, windowsHide: true, timeout: 10 * 60 * 1000, maxBuffer: 8192 },
      (error, stdout) => {
        if (error) return reject(new Error("model archive validation or installation failed"));
        try {
          const result = JSON.parse(stdout);
          const expected = path.join(stateDir(productProfile(), "models", root), "laya");
          if (result.installed !== true || path.resolve(result.path).toLowerCase() !== expected.toLowerCase()) {
            throw new Error("invalid model installer result");
          }
          resolve(result);
        } catch (cause) { reject(cause); }
      });
  });
}

class ModelDownload {
  constructor({ root, python, fetchImpl, enableFallback, onState }) {
    this.root = path.resolve(root);
    this.python = python;
    this.fetchImpl = fetchImpl;
    this.enableFallback = enableFallback;
    this.onState = onState || (() => {});
    this.asset = modelAsset(this.root);
    this.state = { phase: "idle", received: 0, total: this.asset.bytes };
    this.inFlight = null;
    this.abort = null;
    this.cancelled = false;
  }

  publish(state) {
    this.state = state;
    this.onState({ ...state });
  }

  getState() { return { ...this.state }; }

  async downloadOnce(archive) {
    if (this.cancelled) throw new Error("model download cancelled");
    this.abort = new AbortController();
    const timer = setTimeout(() => this.abort.abort(), 30 * 60 * 1000);
    try {
      const response = await this.fetchImpl(this.asset.url, {
        headers: { Accept: "application/octet-stream", "User-Agent": "WechatVibe-model" },
        signal: this.abort.signal,
      });
      if (!response.ok || !response.body) throw new Error("model download unavailable");
      const reported = Number(response.headers.get("content-length"));
      if (reported && reported !== this.asset.bytes) throw new Error("model download size changed");
      const hash = crypto.createHash("sha256");
      let received = 0;
      let lastReport = 0;
      const progress = new Transform({ transform: (chunk, _encoding, done) => {
        received += chunk.length;
        if (received > this.asset.bytes) return done(new Error("model download exceeded pinned size"));
        hash.update(chunk);
        if (Date.now() - lastReport > 250) {
          lastReport = Date.now();
          this.publish({ phase: "downloading", received, total: this.asset.bytes });
        }
        done(null, chunk);
      } });
      await pipeline(Readable.fromWeb(response.body), progress,
        fs.createWriteStream(archive, { flags: "wx" }), { signal: this.abort.signal });
      if (received !== this.asset.bytes || hash.digest("hex") !== this.asset.sha256) {
        throw new Error("model download checksum mismatch");
      }
      this.publish({ phase: "installing", received, total: this.asset.bytes });
      await installWithPython(this.python, path.join(this.root, "bridge", "model_install.py"),
        archive, this.root);
      this.publish({ phase: "ready", received, total: this.asset.bytes });
    } finally {
      clearTimeout(timer);
      this.abort = null;
    }
  }

  async start() {
    if (this.inFlight) return this.inFlight;
    this.cancelled = false;
    this.inFlight = Promise.resolve().then(async () => {
      let archive;
      try {
        const local = ownedDirectory(this.root, productProfile().dataDir);
        const downloads = ownedDirectory(local, "model-downloads");
        archive = path.join(downloads, ".laya-" + crypto.randomUUID() + ".zip");
        this.publish({ phase: "downloading", received: 0, total: this.asset.bytes });
        try { await this.downloadOnce(archive); }
        catch (error) {
          if (fs.existsSync(archive)) fs.unlinkSync(archive);
          if (this.cancelled || this.state.phase !== "downloading" || !this.enableFallback ||
              !await this.enableFallback()) throw error;
          await this.downloadOnce(archive);
        }
        return this.getState();
      } catch (error) {
        const stage = this.state.phase;
        this.publish({ phase: "failed", received: 0, total: this.asset.bytes,
          stage,
          error: error instanceof Error ? error.message : "模型下载失败" });
        return this.getState();
      } finally {
        this.inFlight = null;
        if (archive && fs.existsSync(archive)) {
          try { fs.unlinkSync(archive); } catch (_) { /* Keep the failure state. */ }
        }
      }
    });
    return this.inFlight;
  }

  cancel() { this.cancelled = true; this.abort?.abort(); }
}

module.exports = { ModelDownload, modelAsset, ownedDirectory };
