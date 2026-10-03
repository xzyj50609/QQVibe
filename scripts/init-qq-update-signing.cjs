"use strict";
// Local release setup. Private material is never printed or included in a build.
const fs = require("node:fs");
const path = require("node:path");
const crypto = require("node:crypto");
const directory = path.join(__dirname, "..", ".local", "update-signing");
const privatePath = path.join(directory, "qq-update-private.pem");
const publicPath = path.join(__dirname, "qq-update-signing.pub");
if (fs.existsSync(privatePath) || fs.existsSync(publicPath)) {
  if (!fs.existsSync(privatePath) || !fs.existsSync(publicPath)) throw new Error("signing key incomplete; do not replace a pinned key");
  const derived = crypto.createPublicKey(crypto.createPrivateKey(fs.readFileSync(privatePath))).export({ type: "spki", format: "der" });
  const pinned = crypto.createPublicKey(fs.readFileSync(publicPath)).export({ type: "spki", format: "der" });
  if (!derived.equals(pinned)) throw new Error("signing keys differ; existing keys preserved");
  process.stdout.write("QQVibe signing key already configured\n");
} else {
  const keys = crypto.generateKeyPairSync("ed25519");
  fs.mkdirSync(directory, { recursive: true });
  fs.writeFileSync(privatePath, keys.privateKey.export({ type: "pkcs8", format: "pem" }), { flag: "wx", mode: 0o600 });
  fs.writeFileSync(publicPath, keys.publicKey.export({ type: "spki", format: "pem" }), { flag: "wx" });
  process.stdout.write("QQVibe local signing key configured; private key remains in ignored .local directory\n");
}
