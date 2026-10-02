"use strict";

// Synthetic Release responses only. This test never opens the client or downloads an asset.
const assert = require("node:assert/strict");
const { RELEASES_URL, parseVersion, compareVersions, assessRelease, checkForUpdates } = require("./real-client-update.cjs");

function release(version = "1.0.2") {
  const tag = `v${version}`;
  const asset = (name, size) => ({
    name, size, state: "uploaded", digest: `sha256:${"a".repeat(64)}`,
    browser_download_url: `${RELEASES_URL}/download/${tag}/${name}`,
  });
  return {
    tag_name: tag, html_url: `${RELEASES_URL}/tag/${tag}`, draft: false, prerelease: false,
    assets: [asset("update-manifest.json", 200), asset("update-manifest.sig", 64),
      asset(`WechatVibe-${version}-windows-x64.zip`, 900_000_000), asset("SHA256SUMS.txt", 200)],
  };
}

function fetched(body, { status = 200, headers = {} } = {}) {
  return async () => new Response(body, { status, headers });
}

async function main() {
  assert.equal(compareVersions(parseVersion("1.10.0"), parseVersion("1.9.9")), 1);
  assert.equal(compareVersions(parseVersion("1.0.0"), parseVersion("1.0.0-rc.9")), 1);
  assert.equal(compareVersions(parseVersion("1.0.0-rc.10"), parseVersion("1.0.0-rc.9")), 1);
  assert.equal(compareVersions(parseVersion("v1.0.0+build"), parseVersion("1.0.0")), 0);
  assert.equal(parseVersion("01.0.0"), null);
  assert.equal(parseVersion("1.0.0-rc.01"), null);
  assert.equal(parseVersion("1.0"), null);

  assert.deepEqual(assessRelease("1.0.1", release()), {
    status: "available", latestVersion: "1.0.2", releaseUrl: RELEASES_URL,
  });
  assert.deepEqual(assessRelease("1.0.2", release()), { status: "current", latestVersion: "1.0.2" });
  assert.deepEqual(assessRelease("1.0.3", release()), { status: "current", latestVersion: "1.0.2" });
  assert.deepEqual(assessRelease("1.0.2-preview.1", release("1.0.1")), {
    status: "preview-current", latestVersion: "1.0.1",
  });
  assert.deepEqual(assessRelease("1.0.2-preview.1", release()), {
    status: "available", latestVersion: "1.0.2", releaseUrl: RELEASES_URL,
  });
  assert.equal(assessRelease("1.0.1", { ...release(), prerelease: true }).status, "invalid-release");
  assert.equal(assessRelease("1.0.1", { ...release(), html_url: "https://other.example/release" }).status, "invalid-release");
  assert.equal(assessRelease("1.0.1", { ...release(), tag_name: "v1.0.2-rc.1" }).status, "invalid-release");
  const missingArchive = release();
  missingArchive.assets.splice(2, 1);
  assert.equal(assessRelease("1.0.1", missingArchive).status, "incomplete-release");
  const duplicateArchive = release();
  duplicateArchive.assets.push({ ...duplicateArchive.assets[2] });
  assert.equal(assessRelease("1.0.1", duplicateArchive).status, "incomplete-release");
  const badDigest = release();
  badDigest.assets[2].digest = null;
  assert.equal(assessRelease("1.0.1", badDigest).status, "incomplete-release");
  const noChecksum = release();
  noChecksum.assets.pop();
  assert.equal(assessRelease("1.0.1", noChecksum).status, "incomplete-release");
  const emptyArchive = release();
  emptyArchive.assets[2].size = 0;
  assert.equal(assessRelease("1.0.1", emptyArchive).status, "incomplete-release");
  const redirectedAsset = release();
  redirectedAsset.assets[2].browser_download_url = "https://other.example/file.zip";
  assert.equal(assessRelease("1.0.1", redirectedAsset).status, "incomplete-release");

  const requests = [];
  const successfulFetch = async (url, options) => {
    requests.push({ url, options });
    return new Response(JSON.stringify(release()));
  };
  // GitHub metadata alone must never make a Release trustworthy.
  assert.equal((await checkForUpdates("1.0.1", { fetchImpl: successfulFetch })).status, "invalid-release");
  assert.equal(requests[0].url, "https://api.github.com/repos/tswawa/WechatVibe/releases/latest");
  assert.equal(requests[0].options.redirect, "error");
  assert.equal((await checkForUpdates("1.0.1", { fetchImpl: fetched("{}", { status: 404 }) })).status, "no-release");
  assert.equal((await checkForUpdates("1.0.1", { fetchImpl: fetched("", { status: 403, headers: { "x-ratelimit-remaining": "0" } }) })).status, "rate-limited");
  assert.equal((await checkForUpdates("1.0.1", { fetchImpl: fetched("bad json") })).status, "invalid-release");
  assert.equal((await checkForUpdates("1.0.1", { fetchImpl: fetched("x".repeat(256 * 1024 + 1)) })).status, "invalid-release");
  assert.equal((await checkForUpdates("1.0.1", { fetchImpl: async () => {
    throw new TypeError("fetch failed", { cause: Object.assign(new Error("dns failed"), { code: "ENOTFOUND" }) });
  } })).status, "offline");
  assert.equal((await checkForUpdates("1.0.1", { fetchImpl: async () => {
    throw new TypeError("unexpected implementation error");
  } })).status, "server-error");
  const waitForAbort = async (_url, { signal }) => new Promise((_resolve, reject) => {
    signal.addEventListener("abort", () => reject(new DOMException("aborted", "AbortError")), { once: true });
  });
  assert.equal((await checkForUpdates("1.0.1", { fetchImpl: waitForAbort, timeoutMs: 5 })).status, "timeout");
  const stalledBody = async (_url, { signal }) => new Response(new ReadableStream({
    start(controller) {
      signal.addEventListener("abort", () => controller.error(new DOMException("aborted", "AbortError")), { once: true });
    },
  }));
  assert.equal((await checkForUpdates("1.0.1", { fetchImpl: stalledBody, timeoutMs: 5 })).status, "timeout");
  assert.equal((await checkForUpdates("bad version", { fetchImpl: successfulFetch })).status, "invalid-current");
  process.stdout.write("real-client update checks passed\n");
}

main().catch(error => { console.error(error); process.exitCode = 1; });
