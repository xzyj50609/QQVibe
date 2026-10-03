"use strict";

// Product identity is read from the shared manifest; synthetic env only, no Electron.
const assert = require("node:assert/strict");
const path = require("node:path");

const identity = require("./product-identity.cjs");

const ACCOUNT_KEY = `a:${"ab".repeat(16)}`;

function withoutProductEnv(run) {
  const previous = process.env[identity.PRODUCT_ENV];
  delete process.env[identity.PRODUCT_ENV];
  try {
    return run();
  } finally {
    if (previous !== undefined) process.env[identity.PRODUCT_ENV] = previous;
  }
}

const wechat = identity.productProfile("wechat");
const qq = identity.productProfile("qq");
assert.equal(qq.displayName, "句豆 · ChatBean");
assert.equal(qq.productName, "QQVibe", "branding must not change update matching");

for (const field of ["productName", "appId", "dataDir", "instanceSalt", "controlTokenEnv",
  "controlTokenHeader", "storageNamespace", "iconPng", "iconIco"]) {
  assert.notEqual(wechat[field], qq[field], `${field} must differ per product`);
}

withoutProductEnv(() => {
  assert.equal(identity.productProfile().key, "qq", "this repository is the QQ product");
  assert.equal(identity.productProfile().updateChannelEnabled, true,
    "QQ must not inherit the upstream update feed");
});
assert.equal(wechat.updateChannelEnabled, true);
assert.equal(qq.updateRepository, "xzyj50609/QQVibe");

assert.equal(identity.dataRoot(qq, "C:/app"), path.join("C:/app", "QQVibeData"));
assert.equal(identity.stateDir(qq, "real-client-data", "C:/app"),
  path.join("C:/app", "QQVibeData", "real-client-data"));
assert.equal(identity.stateDir(wechat, "real-client-data", "C:/app"),
  path.join("C:/app", ".local", "real-client-data"));

assert.equal(identity.accountDirectory(qq, ACCOUNT_KEY, "C:/app"),
  path.join("C:/app", "QQVibeData", "accounts", ACCOUNT_KEY.slice(2)));
for (const bad of [ACCOUNT_KEY.slice(2), "a:ZZZ", "", "u:peer", null]) {
  assert.throws(() => identity.accountDirectory(qq, bad, "C:/app"), /invalid account key/);
}

assert.throws(() => identity.productProfile("tim"), /unknown product/);
assert.equal(qq.iconIco, "chatui/assets/qqvibe-icon.ico");
assert.equal(wechat.iconIco, "chatui/assets/wechatvibe-icon.ico");
for (const bad of ["../private.ico", "C:/private.ico", "https://remote/icon.ico"]) {
  identity.manifest().products["bad-icon"] = { ...identity.manifest().products.qq, iconIco: bad };
  assert.throws(() => identity.productProfile("bad-icon"), /icon path/);
}

console.log("PRODUCT_IDENTITY_TESTS_PASSED");
