"use strict";
const fs = require("node:fs");
const path = require("node:path");

// Legacy callers remain on their own feed; the QQ shell explicitly passes its profile.
function updateConfig(profile) {
  const qq = profile?.productName === "QQVibe";
  if (profile && !qq && profile.productName !== "WechatVibe") throw new Error("unknown update product");
  const productName = qq ? "QQVibe" : "WechatVibe";
  const repository = qq ? "xzyj50609/QQVibe" : "tswawa/WechatVibe";
  if (profile && profile.updateRepository !== repository) throw new Error("update repository does not match product");
  return { productName, repository, releasesUrl: `https://github.com/${repository}/releases`,
    publicKeyPath: path.join(__dirname, qq ? "qq-update-signing.pub" : "update-signing.pub") };
}

class UpdatePreferences {
  constructor(root, profile) {
    this.file = path.join(root, profile.dataDir, "update-preferences.json");
    this.defaults = { autoCheck: true, autoDownload: false, channel: "stable", lastCheckedAt: 0 };
  }
  get() {
    let saved = {};
    try {
      const stat = fs.lstatSync(this.file);
      if (!stat.isFile() || stat.isSymbolicLink() || stat.size > 4096) throw new Error("unsafe preferences");
      saved = JSON.parse(fs.readFileSync(this.file, "utf8"));
    } catch (_) { /* Missing or damaged preferences use defaults. */ }
    return { autoCheck: typeof saved.autoCheck === "boolean" ? saved.autoCheck : this.defaults.autoCheck,
      autoDownload: typeof saved.autoDownload === "boolean" ? saved.autoDownload : this.defaults.autoDownload,
      channel: ["stable", "preview"].includes(saved.channel) ? saved.channel : this.defaults.channel,
      lastCheckedAt: Number.isSafeInteger(saved.lastCheckedAt) && saved.lastCheckedAt >= 0 ? saved.lastCheckedAt : 0 };
  }
  set(patch) {
    if (!patch || typeof patch !== "object" || Array.isArray(patch)) throw new Error("invalid update preferences");
    for (const [key, value] of Object.entries(patch)) {
      if (!["autoCheck", "autoDownload", "channel", "lastCheckedAt"].includes(key) ||
          (["autoCheck", "autoDownload"].includes(key) && typeof value !== "boolean") ||
          (key === "channel" && !["stable", "preview"].includes(value)) ||
          (key === "lastCheckedAt" && (!Number.isSafeInteger(value) || value < 0))) throw new Error("invalid update preferences");
    }
    const next = { ...this.get(), ...patch };
    const parent = path.dirname(this.file);
    fs.mkdirSync(parent, { recursive: true });
    if (fs.lstatSync(parent).isSymbolicLink() || (fs.existsSync(this.file) && fs.lstatSync(this.file).isSymbolicLink())) throw new Error("unsafe preferences path");
    const temporary = this.file + ".tmp-" + require("node:crypto").randomUUID();
    fs.writeFileSync(temporary, JSON.stringify(next) + "\n", { flag: "wx", mode: 0o600 });
    fs.renameSync(temporary, this.file);
    return next;
  }
  due(now = Date.now()) {
    const prefs = this.get();
    return prefs.autoCheck && (now - prefs.lastCheckedAt >= 24 * 60 * 60 * 1000 || prefs.lastCheckedAt > now);
  }
}
module.exports = { updateConfig, UpdatePreferences };
