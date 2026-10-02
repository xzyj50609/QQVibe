// Synthetic desktop startup checks: no Electron window or bridge is launched.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

const source = fs.readFileSync(path.join(__dirname, 'desktop-main.cjs'), 'utf8');
const tick = () => new Promise(resolve => setImmediate(resolve));

async function launch(isPackaged, stdout, options = {}) {
  const calls = { errors: [], launches: [], shellLoads: 0, profiles: [], quit: 0 };
  const env = { CHATUI_PORT: '8805', PATH: 'synthetic-path', ...options.env };
  const processStub = {
    env, argv: [], resourcesPath: path.join(__dirname, 'synthetic-resources'),
  };
  const app = {
    isPackaged,
    setPath: (name, value) => calls.profiles.push({ name, value }),
    whenReady: () => Promise.resolve(),
    quit: () => { calls.quit += 1; },
  };
  const fakeFs = { existsSync: () => true, mkdirSync: () => {} };
  const fakeChild = {
    execFile: (python, args, execOptions, done) => {
      calls.launches.push({ python, args, options: execOptions });
      queueMicrotask(() => done(options.execError || null, stdout));
    },
  };
  vm.runInNewContext(source, {
    require: name => {
      if (name === 'electron') return { app, dialog: { showErrorBox: (...args) => calls.errors.push(args) } };
      if (name === 'node:child_process') return fakeChild;
      if (name === 'node:fs') return fakeFs;
      if (name === 'node:path') return path;
      if (name === './product-identity.cjs') return require('./product-identity.cjs');
      if (name === './real-client-shell.cjs') {
        calls.shellLoads += 1;
        if (options.shellThrows) throw new Error('synthetic shell init failure');
        return {};
      }
      throw new Error(`Unexpected import: ${name}`);
    },
    process: processStub, __dirname,
  });
  await tick();
  await tick();
  return { calls, processStub };
}

function validResult(created) {
  return JSON.stringify({ version: 'real-ui-1', url: 'http://127.0.0.1:34567',
    instanceId: 'a'.repeat(64), created });
}

function cleanupLaunches(calls) {
  return calls.launches.filter(call => call.args.includes('--stop-owned-bridge'));
}

async function main() {
  const instanceId = 'a'.repeat(64);
  for (const isPackaged of [false, true]) {
    const { calls, processStub } = await launch(isPackaged, validResult(true));
    assert.equal(calls.launches.length, 1);
    assert.equal(calls.launches[0].options.env.CHATUI_PORT, undefined);
    assert.deepEqual(Array.from(calls.launches[0].args).slice(-2), ['--no-open', '--json']);
    assert.equal(processStub.env.CHATUI_PORT, '34567');
    assert.equal(processStub.env.WECHATVIBE_INSTANCE_ID, instanceId);
    assert.equal(processStub.env.WECHATVIBE_BRIDGE_CREATED, '1');
    assert.deepEqual(Array.from(processStub.argv).slice(-2), ['--client-url', 'http://127.0.0.1:34567/']);
    assert.equal(calls.shellLoads, 1);
    assert.equal(calls.errors.length, 0);
    assert.equal(calls.quit, 0);
    assert.equal(calls.profiles.length, 1);
    assert.equal(calls.profiles[0].name, 'userData');
  }

  // A reused ready bridge is never stopped on a startup failure.
  const reused = await launch(true, validResult(false));
  assert.equal(reused.calls.shellLoads, 1);
  assert.equal(reused.processStub.env.WECHATVIBE_BRIDGE_CREATED, '0');
  assert.equal(cleanupLaunches(reused.calls).length, 0);
  const isolated = await launch(true, validResult(false), { env: {
    WECHATVIBE_PYTHON: 'foreign-python', WECHATVIBE_NODE: 'foreign-node',
    PYTHONPATH: 'foreign-modules', NODE_PATH: 'foreign-packages', LAYA_MODEL_DIR: 'foreign-model',
  } });
  const packaged = isolated.calls.launches[0];
  assert.equal(packaged.python, path.join(__dirname, 'synthetic-resources', 'client', 'runtime', 'python', 'python.exe'));
  assert.equal(packaged.options.env.WECHATVIBE_NODE, path.join(__dirname, 'synthetic-resources', 'client', 'runtime', 'node', 'node.exe'));
  for (const name of ['PYTHONPATH', 'NODE_PATH', 'LAYA_MODEL_DIR']) assert.equal(packaged.options.env[name], undefined);
  assert.equal(packaged.options.env.QQVIBE_LIVE_CORE, '1');

  // An invalid launcher result only stops the bridge this launch created.
  const invalidOwned = await launch(true, JSON.stringify({ version: 'real-ui-1',
    url: 'http://localhost:34567', instanceId, created: true }));
  assert.equal(invalidOwned.calls.shellLoads, 0);
  assert.equal(invalidOwned.calls.errors.length, 1);
  assert.equal(invalidOwned.calls.quit, 1);
  const ownedCleanups = cleanupLaunches(invalidOwned.calls);
  assert.equal(ownedCleanups.length, 1);
  assert.deepEqual(Array.from(ownedCleanups[0].args).slice(-2), ['--stop-owned-bridge', '--json']);
  assert.equal(ownedCleanups[0].options.env.WECHATVIBE_CLIENT_ROOT,
    path.join(__dirname, 'synthetic-resources', 'client'));

  const invalidReused = await launch(true, JSON.stringify({ version: 'real-ui-1',
    url: 'http://localhost:34567', instanceId, created: false }));
  assert.equal(invalidReused.calls.errors.length, 1);
  assert.equal(cleanupLaunches(invalidReused.calls).length, 0);

  // Corrupt stdout that still says "created":true must not orphan the bridge.
  const corrupt = await launch(true, 'x "created": true y');
  assert.equal(corrupt.calls.errors.length, 1);
  assert.equal(corrupt.calls.quit, 1);
  assert.equal(cleanupLaunches(corrupt.calls).length, 1);
  const corruptReused = await launch(true, 'not json at all');
  assert.equal(cleanupLaunches(corruptReused.calls).length, 0);

  // The launcher cleans up a bridge it started before reporting an error, so the
  // desktop must not issue a second stop.
  const launcherFailed = await launch(true, '', { execError: new Error('launcher failed') });
  assert.equal(launcherFailed.calls.launches.length, 1);
  assert.equal(launcherFailed.calls.errors.length, 1);
  assert.equal(launcherFailed.calls.quit, 1);
  assert.equal(cleanupLaunches(launcherFailed.calls).length, 0);

  // A synchronous shell require failure stops only a bridge this launch created.
  const shellOwned = await launch(true, validResult(true), { shellThrows: true });
  assert.equal(shellOwned.calls.errors.length, 1);
  assert.equal(shellOwned.calls.quit, 1);
  assert.equal(cleanupLaunches(shellOwned.calls).length, 1);
  const shellReused = await launch(true, validResult(false), { shellThrows: true });
  assert.equal(shellReused.calls.errors.length, 1);
  assert.equal(cleanupLaunches(shellReused.calls).length, 0);
}

main().catch(error => { console.error(error); process.exitCode = 1; });
