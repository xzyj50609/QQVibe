const { test } = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const path = require('node:path');
const root = path.resolve(__dirname, '..');
const source = fs.readFileSync(path.join(root, 'scripts/real-client-shell.cjs'), 'utf8');
async function harness() {
  const handlers = new Map(), launches = [];
  const frame = { url: 'http://127.0.0.1:34567/' };
  const contents = { mainFrame: frame, on() {}, once() {}, setWindowOpenHandler() {}, loadURL() {}, send() {} };
  const window = { webContents: contents, isDestroyed: () => false, isVisible: () => true, on() {} };
  const electron = {
    app: { isPackaged: true, setAppUserModelId() {}, setPath() {}, requestSingleInstanceLock: () => true,
      on() {}, whenReady: () => Promise.resolve(), getVersion: () => '0.1.1', quit() {}, exit() {} },
    BrowserWindow: class { constructor() { return window; } }, clipboard: {}, dialog: { showErrorBox() {} },
    ipcMain: { on() {}, handle(name, fn) { handlers.set(name, fn); } },
    session: { defaultSession: { setPermissionRequestHandler() {}, setPermissionCheckHandler() {}, on() {},
      resolveProxy: async () => 'DIRECT', webRequest: { onBeforeRequest() {} } } }, shell: {},
  };
  vm.runInNewContext(source, {
    require(name) {
      if (name === './real-client-update-config.cjs') return { UpdatePreferences: class { get() { return {}; } due() { return false; } } };
      if (name === 'electron') return electron;
      if (name === 'node:fs') return { mkdirSync() {}, existsSync: () => true, readFileSync: () => '{}' };
      if (name === 'node:path') return path;
      if (name === './product-identity.cjs') return { productProfile: () => ({ key: 'qq', appId: 'test', productName: 'QQVibe', iconIco: 'chatui/assets/qqvibe-icon.ico', dataDir: 'QQVibeData', updateChannelEnabled: false }), stateDir: (_p, n, base) => path.join(base, n) };
      if (name === 'node:child_process') return { execFile() {}, spawn(exe, args, options) {
        const events = {};
        const child = { exitCode: null, on(n, fn) { events[n] = fn; }, unref() {} };
        launches.push({ exe, args, options, events, child }); return child;
      } };
      if (name === './real-client-model.cjs') return { ModelDownload: class { getState() { return { phase: 'idle' }; } cancel() {} } };
      if (name === './real-client-recovery.cjs') return { monitorBridge: () => () => {} };
      if (name === './real-client-update.cjs') return { RELEASES_URL: 'https://example.invalid', checkForUpdates() {}, downloadAndStageUpdate() {}, errorStatus: () => 'error' };
      if (name === './real-client-update-proxy.cjs') return { createUpdateProxyFetch: () => ({}) };
      if (name === './real-client-update-controller.cjs') return { createUpdateController: () => ({ getState: () => ({ phase: 'idle' }) }) };
      if (name.endsWith(path.join('node_modules', 'undici'))) return {};
      throw Error(name);
    },
    __dirname: path.join(root, 'scripts'), URL, setImmediate, setTimeout: () => ({ unref() {} }), clearTimeout() {},
    process: { platform: 'win32', env: { WECHATVIBE_INSTANCE_ID: 'a'.repeat(64) },
      argv: ['electron', 'shell', '--client-url', frame.url], stderr: { write() {} } },
  });
  await new Promise(resolve => setImmediate(resolve));
  return { handle: handlers.get('real-client:qce-doctor'), trusted: { sender: contents, senderFrame: frame }, launches };
}
test('doctor host rejects other frames and never accepts arbitrary renderer arguments', async () => {
  const f = await harness();
  assert.equal(f.handle({ sender: {}, senderFrame: f.trusted.senderFrame }), false);
  assert.equal(f.handle({ sender: f.trusted.sender, senderFrame: { url: f.trusted.senderFrame.url } }), false);
  assert.equal(f.launches.length, 0);
  assert.equal(f.handle(f.trusted, 'untrusted-script.ps1', '-Repair'), true);
  assert.equal(f.launches.length, 1);
  assert.match(f.launches[0].args.at(-1), /scripts[\\/]qce-doctor\.ps1$/);
  assert.equal(f.launches[0].args.includes('-Repair'), false);
  assert.equal(f.launches[0].options.windowsHide, true);
});
test('an already open doctor is reused and can reopen after exit', async () => {
  const f = await harness();
  assert.equal(f.handle(f.trusted), true);
  assert.equal(f.handle(f.trusted), true);
  assert.equal(f.launches.length, 1);
  f.launches[0].events.exit();
  assert.equal(f.handle(f.trusted), true);
  assert.equal(f.launches.length, 2);
});
