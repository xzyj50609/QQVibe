"use strict";

const crypto = require("node:crypto");
const fs = require("node:fs");
const path = require("node:path");
const { execFile } = require("node:child_process");
const { promisify } = require("node:util");

const RELEASE_API = "https://api.github.com/repos/tswawa/WechatVibe/releases/latest";
const RELEASES_URL = "https://github.com/tswawa/WechatVibe/releases";
const MAX_RESPONSE_BYTES = 256 * 1024;
const MAX_MANIFEST_BYTES = 16 * 1024;
const MAX_SUMS_BYTES = 4 * 1024;
const MAX_ARCHIVE_BYTES = 2 * 1024 ** 3;
const ASSET_HOSTS = new Set(["github.com", "release-assets.githubusercontent.com",
  "objects.githubusercontent.com", "github-releases.githubusercontent.com"]);
const PUBLIC_KEY_PATH = path.join(__dirname, "update-signing.pub");
const execFileAsync = promisify(execFile);

class UpdateError extends Error {
  constructor(status, message = status) {
    super(message);
    this.status = status;
  }
}

function parseVersion(value) {
  if (typeof value !== "string" || value.length > 80) return null;
  const match = /^v?(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)(?:-([0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*))?(?:\+([0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*))?$/.exec(value);
  if (!match) return null;
  const prerelease = match[4] ? match[4].split(".") : [];
  if (prerelease.some(part => /^\d+$/.test(part) && part.length > 1 && part[0] === "0")) return null;
  return { numbers: match.slice(1, 4).map(BigInt), prerelease, normalized: value.replace(/^v/, "") };
}

function compareVersions(left, right) {
  for (let index = 0; index < 3; index++) {
    if (left.numbers[index] > right.numbers[index]) return 1;
    if (left.numbers[index] < right.numbers[index]) return -1;
  }
  if (!left.prerelease.length && right.prerelease.length) return 1;
  if (left.prerelease.length && !right.prerelease.length) return -1;
  for (let index = 0; index < Math.max(left.prerelease.length, right.prerelease.length); index++) {
    const a = left.prerelease[index];
    const b = right.prerelease[index];
    if (a === undefined) return -1;
    if (b === undefined) return 1;
    if (a === b) continue;
    const aNumber = /^\d+$/.test(a);
    const bNumber = /^\d+$/.test(b);
    if (aNumber && bNumber) return BigInt(a) > BigInt(b) ? 1 : -1;
    if (aNumber !== bNumber) return aNumber ? -1 : 1;
    return a > b ? 1 : -1;
  }
  return 0;
}

function validAsset(asset, tag, name, maxBytes) {
  if (!asset || asset.name !== name || asset.state !== "uploaded" ||
      !Number.isSafeInteger(asset.size) || asset.size <= 0 || asset.size > maxBytes ||
      typeof asset.digest !== "string" || !/^sha256:[a-f0-9]{64}$/.test(asset.digest)) return false;
  const expected = `${RELEASES_URL}/download/${encodeURIComponent(tag)}/${encodeURIComponent(name)}`;
  return asset.browser_download_url === expected;
}

function assessRelease(currentVersion, release) {
  const current = parseVersion(currentVersion);
  if (!current) return { status: "invalid-current" };
  const latest = parseVersion(release?.tag_name);
  if (!latest || latest.prerelease.length || release?.draft !== false || release?.prerelease !== false ||
      release?.html_url !== `${RELEASES_URL}/tag/${encodeURIComponent(release.tag_name)}`) {
    return { status: "invalid-release" };
  }
  const latestVersion = latest.normalized;
  if (compareVersions(latest, current) <= 0) {
    return { status: current.prerelease.length ? "preview-current" : "current", latestVersion };
  }
  const assets = release.assets;
  const packageName = `WechatVibe-${latestVersion}-windows-x64.zip`;
  const required = [["update-manifest.json", MAX_MANIFEST_BYTES],
    ["update-manifest.sig", 64], [packageName, MAX_ARCHIVE_BYTES],
    ["SHA256SUMS.txt", MAX_SUMS_BYTES]];
  if (!Array.isArray(assets) || required.some(([name, maxBytes]) =>
    assets.filter(asset => asset?.name === name).length !== 1 ||
    !validAsset(assets.find(asset => asset?.name === name), release.tag_name, name, maxBytes))) {
    return { status: "incomplete-release", latestVersion, releaseUrl: RELEASES_URL };
  }
  return { status: "available", latestVersion, releaseUrl: RELEASES_URL };
}

function sha256(bytes) {
  return crypto.createHash("sha256").update(bytes).digest("hex");
}

function asset(release, name) {
  return release.assets.find(item => item.name === name);
}

function checkedUrl(raw) {
  let url;
  try { url = new URL(raw); } catch (_) { throw new UpdateError("invalid-release", "invalid asset URL"); }
  if (url.protocol !== "https:" || !ASSET_HOSTS.has(url.hostname) || url.port ||
      url.username || url.password || url.hash) {
    throw new UpdateError("invalid-release", "unsafe asset URL");
  }
  return url;
}

function abortAfter(timeoutMs, outerSignal) {
  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), timeoutMs);
  const forward = () => controller.abort();
  if (outerSignal) {
    if (outerSignal.aborted) controller.abort();
    else outerSignal.addEventListener("abort", forward, { once: true });
  }
  return { signal: controller.signal, finish() {
    clearTimeout(timer);
    outerSignal?.removeEventListener("abort", forward);
  } };
}

async function requestAsset(item, fetchImpl, signal) {
  let url = item.browser_download_url;
  for (let hop = 0; hop <= 4; hop++) {
    checkedUrl(url);
    const response = await fetchImpl(url, {
      method: "GET", redirect: "manual", signal,
      headers: { Accept: "application/octet-stream", "User-Agent": "WechatVibe-updater" },
    });
    if ([301, 302, 303, 307, 308].includes(response.status)) {
      if (hop === 4) throw new UpdateError("invalid-release", "too many redirects");
      const location = response.headers?.get("location");
      if (!location) throw new UpdateError("invalid-release", "redirect missing location");
      let next;
      try { next = new URL(location, url).href; }
      catch (_) { throw new UpdateError("invalid-release", "invalid redirect URL"); }
      checkedUrl(next);
      await response.body?.cancel().catch(() => {});
      url = next;
      continue;
    }
    if (!response.ok || !response.body) throw new UpdateError("server-error", "asset unavailable");
    const length = response.headers?.get("content-length");
    if (length !== null && length !== undefined &&
        (!/^\d+$/.test(length) || Number(length) !== item.size)) {
      throw new UpdateError("invalid-release", "asset content length mismatch");
    }
    const encoding = response.headers?.get("content-encoding");
    if (encoding && encoding.toLowerCase() !== "identity") {
      throw new UpdateError("invalid-release", "encoded asset refused");
    }
    return response;
  }
  throw new UpdateError("invalid-release", "too many redirects");
}

async function readAsset(item, fetchImpl, timeoutMs, maxBytes, outerSignal) {
  const abort = abortAfter(timeoutMs, outerSignal);
  let response;
  try {
    response = await requestAsset(item, fetchImpl, abort.signal);
    const chunks = [];
    let length = 0;
    for await (const chunk of response.body) {
      length += chunk.byteLength;
      if (length > maxBytes || length > item.size) throw new UpdateError("invalid-release", "asset too large");
      chunks.push(Buffer.from(chunk));
    }
    if (length !== item.size) throw new UpdateError("invalid-release", "asset size mismatch");
    const bytes = Buffer.concat(chunks);
    if (sha256(bytes) !== item.digest.slice(7)) throw new UpdateError("invalid-release", "asset digest mismatch");
    return bytes;
  } catch (error) {
    await response?.body?.cancel().catch(() => {});
    throw error;
  } finally {
    abort.finish();
  }
}

function exactKeys(object, keys) {
  return object && typeof object === "object" && !Array.isArray(object) &&
    Object.keys(object).sort().join("|") === [...keys].sort().join("|");
}

function verifySignedManifest(raw, signature, version, archiveAsset, publicKey) {
  if (!Buffer.isBuffer(raw) || !Buffer.isBuffer(signature) || signature.length !== 64) {
    throw new UpdateError("invalid-release", "manifest signature invalid");
  }
  let valid = false;
  try { valid = crypto.verify(null, raw, publicKey || fs.readFileSync(PUBLIC_KEY_PATH), signature); }
  catch (_) { /* Fail closed on malformed key or signature. */ }
  if (!valid) throw new UpdateError("invalid-release", "manifest signature invalid");
  let manifest;
  try { manifest = JSON.parse(raw.toString("utf8")); }
  catch (_) { throw new UpdateError("invalid-release", "manifest JSON invalid"); }
  const expectedName = "WechatVibe-" + version + "-windows-x64.zip";
  if (!exactKeys(manifest, ["schema", "product", "version", "platform", "arch", "layout",
        "dataSchema", "archive"]) ||
      !exactKeys(manifest.archive, ["name", "size", "sha256"]) ||
      manifest.schema !== 1 || manifest.product !== "WechatVibe" ||
      manifest.version !== version || manifest.platform !== "win32" ||
      manifest.arch !== "x64" || manifest.layout !== "win-unpacked" ||
      manifest.dataSchema !== "real-client-v1" || manifest.archive.name !== expectedName ||
      manifest.archive.name !== archiveAsset.name || manifest.archive.size !== archiveAsset.size ||
      !/^[a-f0-9]{64}$/.test(manifest.archive.sha256) ||
      manifest.archive.sha256 !== archiveAsset.digest.slice(7)) {
    throw new UpdateError("invalid-release", "manifest does not match release");
  }
  return manifest;
}

function verifySums(raw, manifest, manifestBytes, signature) {
  const expected = [
    manifest.archive.sha256 + "  " + manifest.archive.name,
    sha256(manifestBytes) + "  update-manifest.json",
    sha256(signature) + "  update-manifest.sig",
  ].join("\n") + "\n";
  if (raw.toString("utf8") !== expected) throw new UpdateError("invalid-release", "checksums mismatch");
}

async function readBoundedJson(response) {
  const declared = response.headers?.get("content-length");
  if (declared !== null && declared !== undefined &&
      (!/^\d+$/.test(declared) || Number(declared) > MAX_RESPONSE_BYTES)) {
    throw new UpdateError("invalid-release", "release response too large");
  }
  if (!response.body) throw new UpdateError("invalid-release", "empty release response");
  const chunks = [];
  let length = 0;
  for await (const chunk of response.body) {
    length += chunk.byteLength;
    if (length > MAX_RESPONSE_BYTES) throw new UpdateError("invalid-release", "release response too large");
    chunks.push(Buffer.from(chunk));
  }
  try { return JSON.parse(Buffer.concat(chunks).toString("utf8")); }
  catch (_) { throw new UpdateError("invalid-release", "release JSON invalid"); }
}

async function discover(currentVersion, options = {}) {
  if (!parseVersion(currentVersion)) return { assessment: { status: "invalid-current" } };
  const fetchImpl = options.fetchImpl || globalThis.fetch;
  const abort = abortAfter(options.timeoutMs || 8000, options.signal);
  let release;
  try {
    const response = await fetchImpl(RELEASE_API, {
      method: "GET", redirect: "error", signal: abort.signal,
      headers: {
        Accept: "application/vnd.github+json", "User-Agent": "WechatVibe-update-check",
        "X-GitHub-Api-Version": "2022-11-28",
      },
    });
    if (response.status === 404) return { assessment: { status: "no-release" } };
    if (response.status === 429 || response.status === 403 &&
        response.headers?.get("x-ratelimit-remaining") === "0") {
      return { assessment: { status: "rate-limited" } };
    }
    if (!response.ok) return { assessment: { status: "server-error" } };
    release = await readBoundedJson(response);
  } finally {
    abort.finish();
  }
  const assessment = assessRelease(currentVersion, release);
  if (assessment.status !== "available") return { assessment };
  const version = assessment.latestVersion;
  const archiveAsset = asset(release, "WechatVibe-" + version + "-windows-x64.zip");
  const manifestAsset = asset(release, "update-manifest.json");
  const signatureAsset = asset(release, "update-manifest.sig");
  const sumsAsset = asset(release, "SHA256SUMS.txt");
  const timeout = options.assetTimeoutMs || 30000;
  const [manifestBytes, signature, sums] = await Promise.all([
    readAsset(manifestAsset, fetchImpl, timeout, MAX_MANIFEST_BYTES, options.signal),
    readAsset(signatureAsset, fetchImpl, timeout, 64, options.signal),
    readAsset(sumsAsset, fetchImpl, timeout, MAX_SUMS_BYTES, options.signal),
  ]);
  const manifest = verifySignedManifest(manifestBytes, signature, version, archiveAsset);
  verifySums(sums, manifest, manifestBytes, signature);
  return { assessment, archiveAsset, manifest };
}

function errorStatus(error, options = {}) {
  if (options.signal?.aborted || error?.name === "AbortError" ||
      error?.name === "TimeoutError") return "timeout";
  if (error?.status) return error.status;
  if (["ENOTFOUND", "EAI_AGAIN", "ENETUNREACH", "EHOSTUNREACH",
      "ECONNREFUSED", "ECONNRESET", "ETIMEDOUT", "UND_ERR_CONNECT_TIMEOUT",
      "UND_ERR_HEADERS_TIMEOUT"].includes(error?.cause?.code || error?.code)) return "offline";
  return "server-error";
}

async function checkForUpdates(currentVersion, options = {}) {
  try { return (await discover(currentVersion, options)).assessment; }
  catch (error) { return { status: errorStatus(error, options) }; }
}

function progress(callback, phase, downloadedBytes = 0, totalBytes = 0) {
  if (typeof callback === "function") {
    try { callback({ phase, downloadedBytes, totalBytes }); }
    catch (_) { /* Progress is advisory. */ }
  }
}

async function removeOwnedTree(directory) {
  for (const entry of await fs.promises.readdir(directory, { withFileTypes: true })) {
    const target = path.join(directory, entry.name);
    if (entry.isDirectory() && !entry.isSymbolicLink()) await removeOwnedTree(target);
    else await fs.promises.unlink(target);
  }
  await fs.promises.rmdir(directory);
}

async function downloadArchive(item, destination, fetchImpl, onProgress, options) {
  const abort = abortAfter(options.downloadTimeoutMs || 30 * 60 * 1000, options.signal);
  let response;
  let handle;
  try {
    response = await requestAsset(item, fetchImpl, abort.signal);
    handle = await fs.promises.open(destination, "wx", 0o600);
    const hash = crypto.createHash("sha256");
    let length = 0;
    progress(onProgress, "downloading", 0, item.size);
    for await (const raw of response.body) {
      const chunk = Buffer.from(raw);
      length += chunk.length;
      if (length > item.size || length > MAX_ARCHIVE_BYTES) {
        throw new UpdateError("invalid-release", "archive exceeds declared size");
      }
      hash.update(chunk);
      let offset = 0;
      while (offset < chunk.length) {
        const result = await handle.write(chunk, offset, chunk.length - offset);
        if (result.bytesWritten <= 0) throw new Error("archive write failed");
        offset += result.bytesWritten;
      }
      progress(onProgress, "downloading", length, item.size);
    }
    progress(onProgress, "verifying", length, item.size);
    if (length !== item.size || hash.digest("hex") !== item.digest.slice(7)) {
      throw new UpdateError("invalid-release", "archive digest or size mismatch");
    }
    await handle.sync();
  } finally {
    abort.finish();
    await handle?.close();
    await response?.body?.cancel().catch(() => {});
  }
}

async function downloadAndStageUpdate(currentVersion, installRoot, onProgress, options = {}) {
  const discovered = await discover(currentVersion, options);
  if (discovered.assessment.status !== "available") {
    throw new UpdateError(discovered.assessment.status, "update unavailable: " + discovered.assessment.status);
  }
  const rootStat = await fs.promises.lstat(installRoot);
  if (!rootStat.isDirectory() || rootStat.isSymbolicLink()) {
    throw new UpdateError("invalid-install", "installation root must be a directory");
  }
  const root = await fs.promises.realpath(installRoot);
  const parent = path.dirname(root);
  const workDir = await fs.promises.mkdtemp(path.join(parent, ".wechatvibe-update-"));
  const archivePath = path.join(workDir, discovered.archiveAsset.name);
  const candidatePath = path.join(workDir, "win-unpacked");
  try {
    await downloadArchive(discovered.archiveAsset, archivePath, options.fetchImpl || globalThis.fetch,
      onProgress, options);
    progress(onProgress, "extracting", discovered.archiveAsset.size, discovered.archiveAsset.size);
    const pythonExe = options.pythonExe ||
      path.join(root, "resources", "client", "runtime", "python", "python.exe");
    const extractorPath = options.extractorPath ||
      path.join(root, "resources", "client", "scripts", "real-client-update-extract.py");
    await execFileAsync(pythonExe, ["-I", extractorPath, archivePath, workDir,
      discovered.assessment.latestVersion], {
      cwd: workDir, windowsHide: true, timeout: options.extractTimeoutMs || 20 * 60 * 1000,
      maxBuffer: 16 * 1024,
    });
    const exe = await fs.promises.stat(path.join(candidatePath, "WechatVibe.exe"));
    if (!exe.isFile() || exe.size === 0) throw new UpdateError("invalid-release", "candidate executable missing");
    return { candidatePath, expectedVersion: discovered.assessment.latestVersion, workDir };
  } catch (error) {
    // mkdtemp created this exact sibling directory; never remove the installation.
    if (path.dirname(workDir) === parent && path.basename(workDir).startsWith(".wechatvibe-update-")) {
      await removeOwnedTree(workDir);
    }
    throw error;
  }
}

module.exports = { RELEASES_URL, parseVersion, compareVersions, assessRelease,
  checkForUpdates, downloadAndStageUpdate, verifySignedManifest, errorStatus };
