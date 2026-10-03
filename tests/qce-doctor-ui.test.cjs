const { test } = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const path = require('node:path');
const source = fs.readFileSync(path.join(__dirname, '../chatui/qq-support-ui.js'), 'utf8');
function setup(host) {
  const nodes = new Map();
  const get = id => {
    if (!nodes.has(id)) nodes.set(id, { hidden: true, events: {}, replaceChildren() {}, appendChild() {}, addEventListener(name, fn) { this.events[name] = fn; } });
    return nodes.get(id);
  };
  const window = { desktopHost: host };
  vm.runInNewContext(source, { window });
  window.QQSupportUI.mount({ document: { getElementById: get, createElement: () => ({}) }, api() { throw Error('must not read chat'); }, getScope: () => ({}) });
  return get;
}
test('doctor is shown only when desktop host supports it and requires a click', async () => {
  let calls = 0;
  const get = setup({ openQCEDoctor: async () => { calls++; return true; } });
  assert.equal(calls, 0);
  assert.equal(get('btnQCEDoctor').hidden, false);
  await get('btnQCEDoctor').events.click();
  assert.equal(calls, 1);
  assert.match(get('qceDoctorStatus').textContent, /修复和扫码完成后/);
  assert.equal(get('btnQCEDoctor').disabled, false);
});
test('browser hides doctor and host failure does not claim a repair', async () => {
  assert.equal(setup(undefined)('btnQCEDoctor').hidden, true);
  const get = setup({ openQCEDoctor: async () => { throw Error('private details'); } });
  await get('btnQCEDoctor').events.click();
  assert.match(get('qceDoctorStatus').textContent, /未能打开/);
  assert.doesNotMatch(get('qceDoctorStatus').textContent, /private details/);
});
