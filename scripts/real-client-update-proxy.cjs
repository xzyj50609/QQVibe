"use strict";

const { execFile } = require("node:child_process");
const net = require("node:net");

const INTERNET_SETTINGS = "HKCU\\Software\\Microsoft\\Windows\\CurrentVersion\\Internet Settings";

function hostAndPort(value) {
  const match = /^(\[[0-9a-fA-F:]+\]|[A-Za-z0-9.-]+):(\d{1,5})$/.exec(value);
  if (!match) return null;
  const port = Number(match[2]);
  if (port < 1 || port > 65535) return null;
  try {
    const url = new URL(`http://${match[1]}:${port}`);
    if (!url.hostname || url.username || url.password || url.pathname !== "/") return null;
    return { host: url.hostname, port, authority: `${url.hostname}:${port}` };
  } catch (_) { return null; }
}

function parseProxyDirective(value) {
  if (typeof value !== "string") return { kind: "none" };
  for (const part of value.split(";")) {
    const directive = part.trim();
    if (/^DIRECT$/i.test(directive)) return { kind: "direct" };
    const match = /^(PROXY|HTTPS)\s+(\S+)$/i.exec(directive);
    if (!match) continue;
    const target = hostAndPort(match[2]);
    if (target) return { kind: "proxy", url: `${match[1].toUpperCase() === "HTTPS" ? "https" : "http"}://${target.authority}` };
  }
  return { kind: "none" };
}

function explicitHttpsProxy(env) {
  for (const value of [env?.HTTPS_PROXY, env?.https_proxy]) {
    if (typeof value !== "string" || !value.trim()) continue;
    try {
      const url = new URL(value.trim());
      if ((url.protocol === "http:" || url.protocol === "https:") && url.hostname &&
          url.pathname === "/" && !url.search && !url.hash) return url.href;
    } catch (_) { /* Ignore an invalid explicit proxy. */ }
  }
  return null;
}

function parseSavedLoopbackProxy(value) {
  if (typeof value !== "string") return null;
  const entries = value.split(";").map(part => part.trim()).filter(Boolean);
  let candidate;
  if (entries.length === 1 && !entries[0].includes("=")) candidate = entries[0];
  else candidate = entries.find(part => /^https\s*=/i.test(part))?.replace(/^https\s*=\s*/i, "");
  if (!candidate) return null;
  if (/^http:\/\//i.test(candidate)) candidate = candidate.slice(7);
  else if (candidate.includes("://")) return null;
  const target = hostAndPort(candidate);
  if (!target) return null;
  const host = target.host.replace(/^\[|\]$/g, "");
  if (!(net.isIP(host) === 4 && host.startsWith("127.")) && host !== "::1") return null;
  return `http://${target.authority}`;
}

function readSavedProxyFromRegistry(execFileImpl = execFile) {
  if (process.platform !== "win32") return Promise.resolve(null);
  return new Promise(resolve => {
    execFileImpl("reg.exe", ["query", INTERNET_SETTINGS, "/v", "ProxyServer"],
      { windowsHide: true, timeout: 2000, maxBuffer: 16384 }, (error, stdout) => {
        if (error || typeof stdout !== "string") return resolve(null);
        const line = stdout.split(/\r?\n/).find(item => /^\s*ProxyServer\s+REG_(?:SZ|EXPAND_SZ)\s+/i.test(item));
        resolve(line?.replace(/^\s*ProxyServer\s+REG_(?:SZ|EXPAND_SZ)\s+/i, "").trim() || null);
      });
  });
}

function isLoopbackListening(proxyUrl, timeoutMs = 500) {
  const url = new URL(proxyUrl);
  return new Promise(resolve => {
    const socket = net.connect({ host: url.hostname.replace(/^\[|\]$/g, ""), port: Number(url.port) });
    let finished = false;
    const finish = result => {
      if (finished) return;
      finished = true;
      socket.destroy();
      resolve(result);
    };
    socket.setTimeout(timeoutMs);
    socket.once("connect", () => finish(true));
    socket.once("error", () => finish(false));
    socket.once("timeout", () => finish(false));
  });
}

function createUpdateProxyFetch(options = {}) {
  const resolveProxy = options.resolveProxy || (options.session &&
    (url => options.session.resolveProxy(url)));
  if (typeof resolveProxy !== "function") throw new TypeError("Electron session.resolveProxy is required");
  const baseFetch = options.fetchImpl || globalThis.fetch;
  if (typeof baseFetch !== "function") throw new TypeError("fetchImpl is required");
  const env = options.env || process.env;
  const readSavedProxy = options.readSavedProxy || readSavedProxyFromRegistry;
  const checkListening = options.isLoopbackListening || isLoopbackListening;
  const agents = new Map();
  let savedFallback = null;
  let fallbackAttempt = null;

  function agentFor(proxyUrl) {
    if (!agents.has(proxyUrl)) {
      const Agent = options.ProxyAgent || require("undici").ProxyAgent;
      agents.set(proxyUrl, new Agent(proxyUrl));
    }
    return agents.get(proxyUrl);
  }

  async function fetchImpl(url, requestOptions = {}) {
    const destination = url instanceof URL ? url.href : String(url);
    let route;
    try { route = parseProxyDirective(await resolveProxy(destination)); }
    catch (_) { route = { kind: "none" }; }
    const proxyUrl = explicitHttpsProxy(env) || (route.kind === "proxy" ? route.url :
      route.kind === "direct" ? savedFallback : null);
    return proxyUrl ? baseFetch(url, { ...requestOptions, dispatcher: agentFor(proxyUrl) }) :
      baseFetch(url, requestOptions);
  }

  async function enableSavedLoopbackFallback() {
    if (explicitHttpsProxy(env)) return false;
    if (!fallbackAttempt) {
      fallbackAttempt = (async () => {
        let proxyUrl;
        try { proxyUrl = parseSavedLoopbackProxy(await readSavedProxy()); }
        catch (_) { return false; }
        if (!proxyUrl) return false;
        try {
          if (!await checkListening(proxyUrl)) return false;
        } catch (_) { return false; }
        savedFallback = proxyUrl;
        return true;
      })();
    }
    const attempt = fallbackAttempt;
    const enabled = await attempt;
    if (!enabled && fallbackAttempt === attempt) fallbackAttempt = null;
    return enabled;
  }

  async function close() {
    const current = [...agents.values()];
    agents.clear();
    await Promise.all(current.map(agent => agent.close()));
  }

  return { fetchImpl, enableSavedLoopbackFallback, close };
}

module.exports = { createUpdateProxyFetch, parseProxyDirective, parseSavedLoopbackProxy,
  readSavedProxyFromRegistry, isLoopbackListening };
