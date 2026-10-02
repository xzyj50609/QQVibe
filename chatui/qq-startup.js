"use strict";
// Local account choice only. No QCE endpoint, credentials or network connector.
(() => {
  if (window.ProductConfig?.key !== "qq") return;
  let serial = 0;
  let busy = false;
  let opened = false;
  let lastSource = null;
  const list = document.getElementById("qqStartupAccounts");
  const summary = document.getElementById("qqConnectionStatus");
  const node = (tag, text, className = "") => {
    const result = document.createElement(tag);
    result.textContent = text;
    result.className = className;
    return result;
  };
  function setStatus(source) {
    lastSource = source;
    summary.hidden = false;
    if (source?.sync?.enabled && ["online", "fetching"].includes(source.sync.state)) {
      summary.textContent = "QQ 已连接 · 仅同步已选会话，本地历史可查看";
    } else if (source?.sync?.enabled && source.sync.state === "partial") {
      summary.textContent = "QQ 同步存在未读完范围 · 本地历史可查看；详见连接设置";
    } else if (source?.sync?.state === "paused") {
      summary.textContent = "QQ 同步已暂停 · 本地历史仍可查看；详见连接设置";
    } else summary.textContent = source?.localReady ?
      "QQ 本地记录可用 · 当前离线，自动同步尚未启用" :
      "当前离线 · 选择已保存账号可查看本地记录；自动同步尚未启用";
  }
  async function show(api) {
    if (busy || opened) return;
    const request = ++serial;
    list.hidden = false;
    setStatus(null);
    try {
      const data = await api("/api/accounts");
      if (request !== serial || busy || opened) return;
      if (data.platform !== "qq" || !Array.isArray(data.accounts) ||
          !data.accounts.every(item => item?.platform === "qq" &&
            typeof item.accountId === "string" && typeof item.displayId === "string"))
        throw new Error("账号列表无效");
      list.replaceChildren();
      list.appendChild(node("p", data.accounts.length ? "打开已保存的本地记录" : "还没有已保存的 QQ 账号"));
      for (const account of data.accounts) {
        const button = node("button", `${account.nickname || account.displayId}${account.deletionPending ? " · 待完成清理" : " · 查看记录"}`, "settings-action-btn");
        button.type = "button";
        button.disabled = account.deletionPending === true;
        button.addEventListener("click", async () => {
          if (busy || opened || button.disabled) return;
          busy = true;
          serial++;
          for (const item of list.querySelectorAll("button")) item.disabled = true;
          const message = node("p", "正在打开本地记录…");
          list.appendChild(message);
          try {
            const result = await api("/api/accounts/activate", { method: "POST",
              headers: { "Content-Type": "application/json" },
              body: JSON.stringify({ accountId: account.accountId }) });
            if (result.accountId !== account.accountId || result.messagesReady !== true)
              throw new Error("账号切换结果不匹配");
            opened = true;
            window.location.reload();
          } catch {
            message.textContent = "打开失败，请刷新账号列表后重试。";
            const retry = node("button", "刷新账号列表", "settings-action-btn");
            retry.type = "button";
            retry.addEventListener("click", () => { void show(api); });
            list.appendChild(retry);
          } finally { busy = false; }
        });
        list.appendChild(button);
      }
      const setup = node("button", "首次设置：连接 QQ、选择模型与会话", "settings-action-btn");
      setup.type = "button";
      setup.addEventListener("click", () => {
        document.getElementById("startupContinue").click();
        document.getElementById("btnSettings").click();
        document.querySelector('.settings-tab-btn[data-tab="general"]')?.click();
        document.getElementById("qqSyncPanel").open = true;
        document.getElementById("btnConnectStandardQQ").scrollIntoView({ block: "center" });
        document.getElementById("btnConnectStandardQQ").focus();
      });
      list.appendChild(setup);
      list.appendChild(node("p", "开始使用：连接本机 QQ / QCE，并选择下载/定位 Laya 或 API 接入；没有 Laya 也可先配置 API。再添加一个单聊或群聊，群画像需要选择具体成员。", "qq-startup-hint"));
    } catch {
      if (request !== serial) return;
      list.replaceChildren(node("p", "本地账号列表读取失败，请重试。"));
    }
  }
  window.QQStartup = Object.freeze({ show, setStatus,
    ready() { serial++; list.hidden = true; setStatus({ ...lastSource, localReady: true }); } });
  setStatus(null);
})();
