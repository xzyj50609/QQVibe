// Keep the local bridge available while this project's desktop client is open.
const { execFile } = require('node:child_process');
const fs = require('node:fs');
const http = require('node:http');
const path = require('node:path');
const { productProfile, stateDir } = require('./product-identity.cjs');

function launcherCreatedBridge(stdout) {
  if (typeof stdout !== 'string') return false;
  for (const line of stdout.split(/\r?\n/)) {
    const trimmed = line.trim();
    if (!trimmed.startsWith('{')) continue;
    try {
      const parsed = JSON.parse(trimmed);
      if (parsed && typeof parsed === 'object') return parsed.created === true;
    } catch (_) { }
  }
  return false;
}

function monitorBridge({ root, url, instanceId, isOpen, onRecovered }) {
  const port = Number(new URL(url).port);
  const bundledPython = path.join(root, 'runtime', 'python', 'python.exe');
  const python = process.env.WECHATVIBE_PYTHON || (fs.existsSync(bundledPython) ? bundledPython : 'python');
  const launcher = path.join(root, 'scripts', 'start-real-client.py');
  const noAutoRecovery = path.join(stateDir(productProfile(), 'real-client-runtime', root), 'no-auto-recovery.json');
  let checking = false;
  let recovering = false;
  let recoveryDone = Promise.resolve();
  let stopped = false;
  let retryAfter = 0;

  async function check() {
    if (stopped || !isOpen() || checking || recovering || fs.existsSync(noAutoRecovery) || Date.now() < retryAfter) return;
    checking = true;
    const healthy = await new Promise(resolve => {
      let done = false;
      const finish = value => {
        if (done) return;
        done = true;
        request.destroy();
        resolve(value);
      };
      const request = http.get({ hostname: '127.0.0.1', port, path: '/api/health', timeout: 1500 }, response => {
        if (response.statusCode !== 200) return finish(false);
        let body = '';
        response.on('data', chunk => {
          body += chunk;
          if (body.length > 65536) finish(false);
        });
        response.once('end', () => {
          try {
            const health = JSON.parse(body);
            finish(health.version === 'real-ui-1' && health.instanceId === instanceId);
          } catch (_) {
            finish(false);
          }
        });
        response.once('error', () => finish(false));
      });
      request.once('error', () => finish(false));
      request.once('timeout', () => finish(false));
    });
    checking = false;
    if (healthy || stopped || !isOpen() || fs.existsSync(noAutoRecovery) ||
        (path.isAbsolute(python) && !fs.existsSync(python))) return;
    recovering = true;
    // The existing launcher verifies process identity and service ownership, and
    // never stops another process or creates a duplicate bridge.
    recoveryDone = new Promise(resolve => {
      try {
        execFile(python, [launcher, '--no-open', '--recovery', '--json'],
          { cwd: root, windowsHide: true, timeout: 60000,
            env: { ...process.env, CHATUI_PORT: String(port) } }, (error, stdout) => {
            recovering = false;
            retryAfter = Date.now() + 10000;
            try {
              // A reused bridge is not a recovery: only a real (re)start may tell the
              // renderer to reset, so a busy-but-healthy bridge never flickers the UI.
              if (!error && !stopped && isOpen() && !fs.existsSync(noAutoRecovery) &&
                  launcherCreatedBridge(stdout)) onRecovered();
            } finally { resolve(); }
          });
      } catch (_) {
        recovering = false;
        resolve();
      }
    });
  }

  const timer = setInterval(() => { void check(); }, 3000);
  timer.unref();
  void check();
  return () => { stopped = true; clearInterval(timer); return recoveryDone; };
}

module.exports = { monitorBridge };
