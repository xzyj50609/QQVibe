"use strict";
// Each legacy script has an independent main() and owns only synthetic fixtures.
const { readdirSync } = require("node:fs");
const path = require("node:path");
const { spawnSync } = require("node:child_process");
const root = path.resolve(__dirname, "..");
const files = readdirSync(__dirname).filter(name => /^test-.*\.cjs$/.test(name))
  .sort().map(name => path.join(__dirname, name));
files.push(path.join(root, "tests", "test-partial-startup.cjs"));
for (const file of files) {
  console.log(`===== ${path.relative(root, file)} =====`);
  const result = spawnSync(process.execPath, [file], {
    cwd: root, stdio: "inherit", timeout: 180_000, windowsHide: true,
  });
  if (result.error) console.error(result.error.message);
  if (result.error || result.status !== 0) process.exit(result.status || 1);
}
console.log(`ALL_NODE_SCRIPT_TESTS_PASSED (${files.length} scripts)`);
