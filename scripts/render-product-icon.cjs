"use strict";
// Render the local code-native SVG with the project's existing Chromium runtime.
// This helper opens no visible window and does not access the network or user data.
const { app, BrowserWindow, session } = require('electron');
const fs = require('node:fs');
const path = require('node:path');
const root = path.resolve(__dirname, '..');
const source = path.join(root, 'docs/design/brand/chatbean-icon.svg');
const output = path.join(root, 'chatui/assets/qqvibe-icon.png');
app.commandLine.appendSwitch('force-device-scale-factor', '1');
app.setPath('userData', path.join(root, '.zwork', 'icon-renderer'));
const timeout = setTimeout(() => { process.stderr.write('icon render timed out\n'); app.exit(1); }, 20000);
app.whenReady().then(async () => {
  session.defaultSession.webRequest.onBeforeRequest((details, callback) =>
    callback({ cancel: !details.url.startsWith('data:') }));
  const win = new BrowserWindow({ width:1024, height:1024, show:false, transparent:true,
    frame:false, webPreferences:{ offscreen:true, sandbox:true, contextIsolation:true, nodeIntegration:false } });
  await win.loadURL('data:image/svg+xml;charset=utf-8,' + encodeURIComponent(fs.readFileSync(source,'utf8')));
  await win.webContents.executeJavaScript('new Promise(resolve => requestAnimationFrame(() => requestAnimationFrame(resolve)))');
  const rendered = await win.webContents.capturePage({ x:0,y:0,width:1024,height:1024 });
  fs.writeFileSync(output, rendered.toPNG());
  fs.copyFileSync(source, path.join(root, 'chatui/assets/qqvibe-icon.svg'));
  clearTimeout(timeout); win.destroy(); app.quit();
}).catch(error => { process.stderr.write(error.message+'\n'); app.exit(1); });
