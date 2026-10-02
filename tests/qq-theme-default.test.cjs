const { test } = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");

const app = fs.readFileSync(path.join(__dirname, "../chatui/app.js"), "utf8");
function section(start, end) {
  const first = app.indexOf(start);
  const last = app.indexOf(end, first);
  assert.ok(first >= 0 && last > first, `Missing production section: ${start}`);
  return app.slice(first, last);
}

const initialize = section("const defaults =", "chatState.sessions =");
const apply = section("function applySettings()", "settingsState.runtimeSnapshot =");
const change = section('for (const [id, key] of [["selectThemeMode", "theme"],',
  'byId("selectRuntimeProvider").addEventListener');

function harness(saved) {
  const values = new Map(saved === undefined ? [] : [["real-ui-settings-1", saved]]);
  const desktopThemes = [];
  const nodes = new Map();
  function node() {
    const classes = new Set();
    return { events: {}, classes,
      classList: { toggle(value, enabled) { enabled ? classes.add(value) : classes.delete(value); } },
      setAttribute(name, value) { this[name] = value; },
      addEventListener(name, callback) { this.events[name] = callback; },
      style: { setProperty() {} },
    };
  }
  const body = node(), documentElement = node();
  // Match the HTML's light first paint; the production applySettings must also
  // remove it when an existing user has selected dark.
  body.classes.add("theme-light");
  const context = vm.createContext({
    window: { ProductConfig: { key: "qq" }, desktopHost: { setTheme(theme) { desktopThemes.push(theme); } } },
    document: { body, documentElement },
    localStorage: {
      getItem(key) { return values.get(key) ?? null; },
      setItem(key, value) { values.set(key, value); },
    },
    byId(id) { if (!nodes.has(id)) nodes.set(id, node()); return nodes.get(id); },
    refreshLabels() {},
  });
  vm.runInContext(`const settingsState = {};\n${initialize}\n${apply}\n${change}\n` +
    "globalThis.ui = { applySettings, settings: settingsState.settings };", context);
  context.ui.applySettings();
  return { ui: context.ui, nodes, values, body, desktopThemes };
}

test("a clean profile starts in light mode and sends the light titlebar theme", () => {
  const view = harness();
  assert.equal(view.ui.settings.theme, "light");
  assert.equal(view.body.classes.has("theme-light"), true);
  assert.equal(view.nodes.get("selectThemeMode").value, "light");
  assert.deepEqual(view.desktopThemes, ["light"]);
  assert.equal(view.values.has("real-ui-settings-1"), false);
});

test("existing valid theme choices and other preferences survive initialization", () => {
  for (const theme of ["dark", "light"]) {
    const saved = JSON.stringify({ theme, zoom: "1.25", intent: false, labelDetails: true });
    const view = harness(saved);
    assert.equal(view.ui.settings.theme, theme);
    assert.equal(view.ui.settings.zoom, "1.25");
    assert.equal(view.ui.settings.intent, false);
    assert.equal(view.ui.settings.labelDetails, true);
    assert.equal(view.body.classes.has("theme-light"), theme === "light");
    assert.deepEqual(view.desktopThemes, [theme]);
    assert.equal(view.values.get("real-ui-settings-1"), saved);
  }
});

test("missing, invalid and corrupt saved themes fall back to light mode", () => {
  for (const saved of ['{"zoom":"1.1"}', '{"theme":"system"}', '{"theme":null}', "not-json"]) {
    const view = harness(saved);
    assert.equal(view.ui.settings.theme, "light");
    assert.equal(view.body.classes.has("theme-light"), true);
    assert.deepEqual(view.desktopThemes, ["light"]);
  }
});

test("choosing a theme persists and remains selected after closing and reopening", () => {
  const view = harness();
  for (const theme of ["dark", "light"]) {
    view.nodes.get("selectThemeMode").events.change({ target: { value: theme } });
    assert.equal(view.ui.settings.theme, theme);
    assert.equal(view.body.classes.has("theme-light"), theme === "light");
    assert.equal(view.desktopThemes.at(-1), theme);
    const saved = view.values.get("real-ui-settings-1");
    assert.equal(JSON.parse(saved).theme, theme);
    const reopened = harness(saved);
    assert.equal(reopened.ui.settings.theme, theme);
    assert.equal(reopened.body.classes.has("theme-light"), theme === "light");
  }
});
