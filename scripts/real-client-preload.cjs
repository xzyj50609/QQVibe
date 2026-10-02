const { contextBridge, ipcRenderer } = require("electron");
const updateValidationMode = process.env.WECHATVIBE_UPDATE_VALIDATE === "1";
const updateFinalReadyMode = !updateValidationMode &&
  typeof process.env.WECHATVIBE_UPDATE_FINAL_READY_FILE === "string" &&
  typeof process.env.WECHATVIBE_UPDATE_FINAL_READY_NONCE === "string";

ipcRenderer.on("real-client:bridge-restored", () => {
  window.dispatchEvent(new Event("wechatvibe-service-restored"));
});
ipcRenderer.on("real-client:update-state", (_event, state) => {
  if (state && typeof state === "object")
    window.dispatchEvent(new CustomEvent("wechatvibe-update-state", { detail: state }));
});
ipcRenderer.on("real-client:model-download-state", (_event, state) => {
  if (state && typeof state === "object")
    window.dispatchEvent(new CustomEvent("wechatvibe-model-download-state", { detail: state }));
});

const DOC_URLS = new Set([
  "https://www.myersbriggs.org/my-mbti-personality-type/the-mbti-preferences/",
  "https://www.themyersbriggs.com/en-US/Products-and-Services/Myers-Briggs",
  "https://github.com/tswawa",
  "https://github.com/tswawa/WechatVibe",
  "https://github.com/tswawa/WechatVibe/releases",
]);

document.addEventListener("click", (event) => {
  if (!event.isTrusted || event.button !== 0 || !navigator.userActivation.isActive || window.top !== window) return;
  const anchor = event.target instanceof Element ? event.target.closest("a[href]") : null;
  if (!anchor) return;
  let target;
  try {
    target = new URL(anchor.href);
  } catch (_) {
    return;
  }
  if (!DOC_URLS.has(target.href) || target.username || target.password) return;
  event.preventDefault();
  event.stopImmediatePropagation();
  ipcRenderer.send("real-client:open-doc", target.href, true, true);
}, true);

contextBridge.exposeInMainWorld("desktopHost", Object.freeze({
  platform: "win32",
  updateValidationMode,
  updateFinalReadyMode,
  reportUiReady() {
    if ((!updateValidationMode && !updateFinalReadyMode) || window.top !== window)
      return Promise.resolve(false);
    return ipcRenderer.invoke("real-client:update-ui-ready");
  },
  setTheme(theme) {
    if (theme !== "dark" && theme !== "light") return false;
    ipcRenderer.send("real-client:set-theme", theme);
    return true;
  },
  copyDraft(value) {
    if (typeof value !== "string" || !value.trim() || value.length > 1_000_000 ||
        !navigator.userActivation.isActive || window.top !== window) return Promise.resolve(false);
    return ipcRenderer.invoke("real-client:copy-draft", value);
  },
  exitApp() {
    if (window.top !== window) return Promise.resolve(false);
    return ipcRenderer.invoke("real-client:exit-app");
  },
  getAppVersion() {
    if (window.top !== window) return Promise.resolve(null);
    return ipcRenderer.invoke("real-client:app-version");
  },
  getModelDownloadState() {
    if (window.top !== window) return Promise.resolve({ phase: "blocked" });
    return ipcRenderer.invoke("real-client:model-download-state");
  },
  downloadLayaModel() {
    if (window.top !== window || !navigator.userActivation.isActive)
      return Promise.resolve({ phase: "blocked" });
    return ipcRenderer.invoke("real-client:model-download");
  },
  chooseModelDirectory() {
    if (window.top !== window || !navigator.userActivation.isActive) return Promise.resolve(null);
    return ipcRenderer.invoke("real-client:model-choose-directory");
  },
  chooseQQExport() {
    if (window.top !== window || !navigator.userActivation.isActive) return Promise.resolve(null);
    return ipcRenderer.invoke("real-client:qq-choose-export");
  },
  backupQQData(preferences) {
    if (window.top !== window || !navigator.userActivation.isActive) return Promise.resolve({state:'blocked'});
    return ipcRenderer.invoke('real-client:qq-backup-data',preferences);
  },
  restoreQQData() {
    if (window.top !== window || !navigator.userActivation.isActive) return Promise.resolve({state:'blocked'});
    return ipcRenderer.invoke('real-client:qq-restore-data');
  },
  checkForUpdates() {
    if (window.top !== window) return Promise.resolve({ status: "blocked" });
    return ipcRenderer.invoke("real-client:check-updates");
  },
  getUpdateState() {
    if (window.top !== window) return Promise.resolve({ phase: "blocked" });
    return ipcRenderer.invoke("real-client:update-state");
  },
  beginUpdate() {
    if (window.top !== window || !navigator.userActivation.isActive)
      return Promise.resolve({ phase: "blocked" });
    return ipcRenderer.invoke("real-client:begin-update");
  },
  rollbackUpdate() {
    if (window.top !== window || !navigator.userActivation.isActive)
      return Promise.resolve({ phase: "blocked" });
    return ipcRenderer.invoke("real-client:rollback-update");
  },
}));
