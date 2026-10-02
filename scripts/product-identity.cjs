"use strict";
// Reads the same product manifest as bridge/product_profile.py so the desktop shell
// and the Python bridge can never disagree on data root, name or update channel.

const fs = require("node:fs");
const path = require("node:path");

const MANIFEST_PATH = path.join(__dirname, "product-identity.json");
const PRODUCT_ENV = "QQVIBE_PRODUCT";
const ACCOUNT_KEY_PATTERN = /^a:[0-9a-f]{32}$/;
const REQUIRED_FIELDS = ["productName", "appId", "dataDir", "instanceSalt", "controlTokenEnv",
  "controlTokenHeader", "storageNamespace", "updateRepository", "iconPng", "iconIco"];

let document = null;

function manifest() {
  if (document) return document;
  const parsed = JSON.parse(fs.readFileSync(MANIFEST_PATH, "utf8"));
  if (parsed.schema !== 1 || !parsed.products || !Object.keys(parsed.products).length) {
    throw new Error("product identity manifest is unreadable");
  }
  document = parsed;
  return document;
}

function productKey(explicit) {
  const loaded = manifest();
  return explicit || process.env[PRODUCT_ENV] || loaded.default;
}

function productProfile(explicit) {
  const loaded = manifest();
  const key = productKey(explicit);
  const record = loaded.products[key];
  if (!record) throw new Error(`unknown product: ${key}`);
  for (const field of REQUIRED_FIELDS) {
    if (!(field in record)) throw new Error(`product identity ${key} is missing ${field}`);
  }
  for (const [field, extension] of [["iconPng", "png"], ["iconIco", "ico"]]) {
    if (typeof record[field] !== "string" || !new RegExp(`^chatui/assets/[a-z0-9-]+\\.${extension}$`).test(record[field])) {
      throw new Error("invalid product icon path");
    }
  }
  return {
    key,
    productName: record.productName,
    appId: record.appId,
    dataDir: record.dataDir,
    instanceSalt: record.instanceSalt,
    controlTokenEnv: record.controlTokenEnv,
    controlTokenHeader: record.controlTokenHeader,
    storageNamespace: record.storageNamespace,
    updateRepository: record.updateRepository,
    iconPng: record.iconPng,
    iconIco: record.iconIco,
    updateChannelEnabled: Boolean(record.updateRepository),
  };
}

function dataRoot(profile, root) {
  return path.join(root, profile.dataDir);
}

function stateDir(profile, name, root) {
  return path.join(dataRoot(profile, root), name);
}

function accountDirectory(profile, accountKey, root) {
  if (!ACCOUNT_KEY_PATTERN.test(accountKey)) throw new Error("invalid account key");
  return path.join(dataRoot(profile, root), "accounts", accountKey.slice(2));
}

// Every product's user-data directory, so an update package can never carry one
// product's local data in place of the other's.
function userDataDirectoryNames() {
  const loaded = manifest();
  return Object.keys(loaded.products)
    .map(key => String(loaded.products[key].dataDir).toLowerCase())
    .filter(Boolean);
}

module.exports = { MANIFEST_PATH, PRODUCT_ENV, manifest, productKey, productProfile,
  dataRoot, stateDir, accountDirectory, userDataDirectoryNames };
