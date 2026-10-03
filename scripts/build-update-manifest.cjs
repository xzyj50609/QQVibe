"use strict";

// Create the four public Release assets from an already reviewed portable ZIP.
// The private signing key is supplied only through an environment variable.
const crypto = require("node:crypto");
const fs = require("node:fs");
const path = require("node:path");

const MAX_ARCHIVE_BYTES = 2 * 1024 ** 3;
const { updateConfig } = require("./real-client-update-config.cjs");

function stableVersion(value) {
  return typeof value === "string" && value.length <= 80 &&
    /^(?:0|[1-9]\d*)\.(?:0|[1-9]\d*)\.(?:0|[1-9]\d*)$/.test(value);
}

function parseArguments(argv) {
  const args = {};
  for (let i = 0; i < argv.length; i += 2) {
    if (!["--version", "--archive", "--output-dir", "--product"].includes(argv[i]) ||
        typeof argv[i + 1] !== "string" || args[argv[i]]) {
      throw new Error("usage: node scripts/build-update-manifest.cjs --version X.Y.Z --archive ZIP --output-dir DIR");
    }
    args[argv[i]] = argv[i + 1];
  }
  if (![3, 4].includes(Object.keys(args).length) || (args["--product"] && !["QQVibe", "WechatVibe"].includes(args["--product"])) || !stableVersion(args["--version"])) {
    throw new Error("stable version, archive and output directory are required");
  }
  return args;
}

function sha256File(file) {
  return new Promise((resolve, reject) => {
    const hash = crypto.createHash("sha256");
    const stream = fs.createReadStream(file);
    stream.on("data", chunk => hash.update(chunk));
    stream.once("error", reject);
    stream.once("end", () => resolve(hash.digest("hex")));
  });
}

async function buildManifest(version, archivePath, productName = "WechatVibe") {
  if (!["WechatVibe", "QQVibe"].includes(productName)) throw new Error("unknown product");
  if (!stableVersion(version)) throw new Error("stable version required");
  const archiveName = productName + "-" + version + "-windows-x64.zip";
  if (path.basename(archivePath) !== archiveName) throw new Error("archive name does not match version");
  const stats = await fs.promises.lstat(archivePath);
  if (!stats.isFile() || stats.isSymbolicLink() || !Number.isSafeInteger(stats.size) ||
      stats.size <= 0 || stats.size > MAX_ARCHIVE_BYTES) throw new Error("archive size or type invalid");
  return {
    schema: 1, product: productName, version, platform: "win32", arch: "x64",
    layout: "win-unpacked", dataSchema: "real-client-v1",
    archive: { name: archiveName, size: stats.size, sha256: await sha256File(archivePath) },
  };
}

async function main() {
  const args = parseArguments(process.argv.slice(2));
  const productName = args["--product"] || "WechatVibe";
  const config = updateConfig(productName === "QQVibe" ? { productName, updateRepository: "xzyj50609/QQVibe" } : undefined);
  const keyPath = productName === "QQVibe" ? process.env.QQVIBE_UPDATE_SIGNING_KEY_FILE : process.env.WECHATVIBE_UPDATE_SIGNING_KEY_FILE;
  if (!keyPath) throw new Error(productName === "QQVibe" ? "QQVIBE_UPDATE_SIGNING_KEY_FILE is required" : "WECHATVIBE_UPDATE_SIGNING_KEY_FILE is required");
  const outputDir = path.resolve(args["--output-dir"]);
  const archivePath = path.resolve(args["--archive"]);
  if (path.dirname(archivePath) !== outputDir) {
    throw new Error("archive and update metadata must share one output directory");
  }
  const manifest = await buildManifest(args["--version"], archivePath, productName);
  // Confirm that the supplied key corresponds to the public key pinned in the app.
  const privateKey = crypto.createPrivateKey(await fs.promises.readFile(keyPath));
  if (privateKey.asymmetricKeyType !== "ed25519") throw new Error("Ed25519 signing key required");
  const publicKey = crypto.createPublicKey(await fs.promises.readFile(config.publicKeyPath));
  const derived = crypto.createPublicKey(privateKey).export({ type: "spki", format: "der" });
  const pinned = publicKey.export({ type: "spki", format: "der" });
  if (!crypto.timingSafeEqual(derived, pinned)) throw new Error("signing key does not match pinned public key");
  const manifestBytes = Buffer.from(JSON.stringify(manifest, null, 2) + "\n", "utf8");
  const signature = crypto.sign(null, manifestBytes, privateKey);
  if (signature.length !== 64) throw new Error("invalid Ed25519 signature length");
  const sums = [
    manifest.archive.sha256 + "  " + manifest.archive.name,
    crypto.createHash("sha256").update(manifestBytes).digest("hex") + "  update-manifest.json",
    crypto.createHash("sha256").update(signature).digest("hex") + "  update-manifest.sig",
  ].join("\n") + "\n";
  // Existing output is never overwritten; a partial failure can be removed deliberately.
  await fs.promises.writeFile(path.join(outputDir, "update-manifest.json"), manifestBytes, { flag: "wx" });
  await fs.promises.writeFile(path.join(outputDir, "update-manifest.sig"), signature, { flag: "wx" });
  await fs.promises.writeFile(path.join(outputDir, productName === "QQVibe" ? "UPDATE-SHA256SUMS.txt" : "SHA256SUMS.txt"), sums, { flag: "wx" });
  process.stdout.write(JSON.stringify({ version: manifest.version, archive: manifest.archive.name,
    archiveBytes: manifest.archive.size, assets: ["update-manifest.json", "update-manifest.sig",
      productName === "QQVibe" ? "UPDATE-SHA256SUMS.txt" : "SHA256SUMS.txt", manifest.archive.name] }) + "\n");
}

if (require.main === module) {
  main().catch(error => {
    process.stderr.write("update manifest build failed: " + error.message + "\n");
    process.exitCode = 1;
  });
}

module.exports = { stableVersion, parseArguments, buildManifest };
