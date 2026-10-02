(function (global) {
  "use strict";
  const ERRORS = {
    "connector-awaiting-validation": "真实 QQ 接入尚待验证，连接开关暂未开放。",
    "configuration-invalid": "请填写裸的本机回环地址、有效本人 QQ 号和接入 token。",
    "configuration-unavailable": "连接配置不可用，请重新填写并保存。",
    "standard-installation-missing": "未找到标准 QCE 安装。请打开 QQ Chat Exporter 并登录 QQ；自定义安装请使用高级连接设置。",
    "standard-configuration-invalid": "标准 QCE 配置不可用。请在 QCE 中重新生成接入凭证，或使用高级连接设置。",
    "credential-storage-unavailable": "本机加密保存或读取失败，未改为明文保存。",
    "auth-required": "接入 token 不可用，请重新保存；同步已停止。",
    "account-changed": "当前 QQ 账号与配置或本地账号不一致，同步已暂停。",
    "qq-offline": "QQ 接入当前离线，本地历史仍可查看。",
    "connection-unavailable": "暂时无法读取 QCE，本地历史仍可查看。",
    "unsupported-version": "QCE 版本尚未验证，请使用当前兼容版本。",
    "unverified-version": "检测到未验证的 QCE 版本。身份结构已检查；请查看版本信息，再选择小范围试用或保留离线阅读。",
    "conversation-read-paused":"此会话已停止读取；已有消息和分析仍可浏览。",
    "protocol-invalid": "接入响应结构异常，同步已暂停。",
    "source-rejected": "QCE 拒绝读取，同步已暂停。",
    "rate-limited": "QCE 限流，正在有界退避。",
    "request-budget-exhausted": "本轮请求额度已用完，保留扫描边界。",
    "budget-exhausted": "窗口尚未读完，将在额度内重读。",
    "window-split": "这个区间较密，已缩小窗口继续补读。",
    "normalization-rejected": "有消息无法安全归入当前单聊，已保留未完成范围。",
    "pagination-missing": "缺少分页信息，尚不能确认窗口读完。",
    "page-repeated": "分页没有新进展，已停止本轮读取。",
    "scope-changed": "账号或会话范围已变化，旧任务已取消。",
    "sync-stopping": "同步正在停止，请稍后重试。",
  };
  function message(code) { return ERRORS[code] || "连接操作未完成，请核对配置与当前账号。"; }
  function mount({ document, api, onChange, onStatus, getAccount = () => null }) {
    const get = id => document.getElementById(id);
    const make = (tag, value) => { const item = document.createElement(tag); item.textContent = value; return item; };
    let snapshot = null, busy = false, serial = 0, contactPage = 1;
    get("qqSyncPanel").hidden = false;
    function render(data) {
      snapshot = data;
      onStatus?.(data);
      const ready = data?.liveValidated === true;
      const compatibility=data?.compatibility;
      if(get('qqCompatibilityStatus'))get('qqCompatibilityStatus').textContent=compatibility ?
        `QCE ${compatibility.qceVersion} · QQ ${compatibility.qqVersion==='unknown'?'未知':compatibility.qqVersion} · NapCat ${compatibility.napcatVersion==='unknown'?'未知':compatibility.napcatVersion} · ${compatibility.state==='verified-qce-api'?'已验证 QCE 接口；QQ组合仍按实际字段检查':'未验证版本，持续校验消息字段与分页'}` :
        'QQ / QCE 版本与能力尚未取得；未知不代表未安装。';
      if(get('btnTrialQQVersion')){
        get('btnTrialQQVersion').hidden=!compatibility?.requiresConsent || data?.enabled===true;
        get('btnTrialQQVersion').disabled=busy||!ready;
      }
      get("btnConnectStandardQQ").disabled = busy || !ready;
      get("qqSyncGate").textContent = ready ? "只读取已选单聊，不发送 QQ 消息。首次先读取近期窗口，未读完的范围会明确显示。" :
        "真实接入的缓存刷新与重启验证尚未完成。可保存本机配置；验证通过前，连接开关保持关闭。";
      for (const id of ["btnSaveQQConnection", "btnRefreshQQConnection", "qqSyncBase", "qqSyncOwner", "qqSyncToken"]) get(id).disabled = busy;
      get("btnConnectQQ").disabled = busy || !ready || !data?.tokenConfigured;
      get("btnDisconnectQQ").disabled = busy || !data?.enabled;
      get("btnClearQQToken").disabled = busy || !data?.tokenConfigured;
      for (const id of ["btnReadQQContacts", "btnNextQQContacts", "btnAddQQPeer", "btnAddQQGroup"]) get(id).disabled = busy || !ready || !data?.enabled;
      get("qqSyncToken").placeholder = data?.tokenConfigured ? "token 已保存；留空保留" : "输入后仅在本机加密保存";
      const names = { online: "已连接", fetching: "正在同步", partial: "存在未读完范围", offline: "当前离线", paused: "已暂停", disabled: "同步已停止", unavailable: "真实接入待验证" };
      get("qqSyncStatus").textContent = `${names[data?.state] || "正在读取连接状态"}${data?.reason ? " · " + message(data.reason) : ""}`;
      const lastSuccess = data?.lastSuccessAtMs;
      const successDate = Number.isSafeInteger(lastSuccess) && lastSuccess > 0 ? new Date(lastSuccess) : null;
      get("qqSyncLastSuccess").textContent = successDate && Number.isFinite(successDate.getTime()) ?
        `本次运行最近成功同步：${successDate.toLocaleString()}` : "本次运行尚无成功同步记录。";
      const windows = get("qqSyncWindows");
      windows.replaceChildren();
      const entries = Object.entries(data?.conversations || {});
      windows.hidden = !entries.length;
      for (const [user, item] of entries) {
        const range = [item.windowStartMs, item.windowEndMs].map(value => Number.isFinite(value) ? new Date(value).toLocaleString() : "待读取").join(" 至 ");
        windows.appendChild(make("p", `${item.name || "已选单聊"}：${range}；${item.state === "complete" || item.state === "complete-empty" ? "本次接口窗口已读完" : "窗口尚未读完"}${item.reason ? " · " + message(item.reason) : ""}。`));
        if (item.retryAvailable) {
          const retry = make("button", "重试这个窗口");
          retry.type = "button";
          retry.className = "settings-action-btn";
          retry.disabled = busy || !ready || !data?.enabled || getAccount() !== data.account;
          retry.addEventListener("click", () => { void send("retry", { account: data.account, user }); });
          windows.appendChild(retry);
        }
        for (const kind of ["history", "reconcile"]) {
          const work = item[kind];
          if (!work) continue;
          const title = kind === "history" ? "旧历史补读" : "近期迟到消息核对";
          const range = work.window?.map(value => new Date(value).toLocaleString()).join(" 至 ");
          const coverage = kind === "history" && Number.isFinite(work.coverageStartMs) && work.coverageStartMs > 0
            ? `；已补读至 ${new Date(work.coverageStartMs).toLocaleString()}` : "";
          const lastRange = work.lastWindow?.map(value => new Date(value).toLocaleString()).join(" 至 ");
          windows.appendChild(make("p", `${title}：${work.state === "complete" ? "本轮接口区间扫描完成" : "尚有未读完区间"}${coverage}${range ? "；当前窗口 " + range : lastRange ? "；本轮范围 " + lastRange : ""}${work.reason ? " · " + message(work.reason) : ""}。`));
          if (work.retryAvailable) {
            const retry = make("button", `重试${title}`);
            retry.type = "button";
            retry.className = "settings-action-btn";
            retry.disabled = busy || !ready || !data?.enabled || getAccount() !== data.account;
            retry.addEventListener("click", () => { void send("retry", { account: data.account, user, kind }); });
            windows.appendChild(retry);
          }
        }
      }
    }
    async function load() {
      if (busy) return;
      const request = ++serial;
      try {
        const data = await api("/api/qq/connection");
        if (request !== serial) return;
        if (!get("qqSyncBase").value) get("qqSyncBase").value = data.baseUrl || "";
        if (!get("qqSyncOwner").value) get("qqSyncOwner").value = data.ownerUin || "";
        render(data);
      } catch (error) {
        if (request !== serial) return;
        render(null);
        get("qqSyncStatus").textContent = message(error.code);
      }
    }
    async function send(action, body = {}) {
      if (busy) return;
      busy = true;
      const request = ++serial;
      render(snapshot);
      try {
        let data = await api(`/api/qq/connection/${action}`, { method: "POST", body: JSON.stringify(body) });
        if (action === "add-contact" || action === 'add-group') data = await api("/api/qq/connection");
        if (request !== serial) return;
        busy = false;
        if (action === "connect-standard") {
          get("qqSyncBase").value = data.baseUrl || "";
          get("qqSyncOwner").value = data.ownerUin || "";
          get("qqSyncToken").value = "";
        }
        if (action === "configure" || action === "clear-token") get("qqSyncToken").value = "";
        render(data);
        await onChange?.(data);
      } catch (error) {
        if (request === serial) {
          busy = false;
          render(snapshot);
          get("qqSyncStatus").textContent = message(error.code);
          if(error.code==='unverified-version')await load();
        }
      }
    }
    get("btnSaveQQConnection").addEventListener("click", () => {
      const body = { baseUrl: get("qqSyncBase").value.trim(), ownerUin: get("qqSyncOwner").value.trim() };
      if (get("qqSyncToken").value) body.token = get("qqSyncToken").value;
      void send("configure", body);
    });
    get("btnConnectQQ").addEventListener("click", () => { void send("connect"); });
    get('btnTrialQQVersion')?.addEventListener('click',()=>{void send('connect',{allowUnverified:true});});
    get("btnConnectStandardQQ").addEventListener("click", () => { void send("connect-standard"); });
    get("btnDisconnectQQ").addEventListener("click", () => { void send("disconnect"); });
    get("btnClearQQToken").addEventListener("click", () => { void send("clear-token"); });
    get("btnRefreshQQConnection").addEventListener("click", () => { void load(); });
    async function contacts(page) {
      if (busy) return;
      busy = true;
      const request = ++serial;
      render(snapshot);
      try {
        const data = await api(`/api/qq/contacts?page=${page}`);
        if (request !== serial) return;
        contactPage = page;
        const list = get("qqSyncContacts");
        list.replaceChildren();
        list.hidden = false;
        for (const contact of data.contacts || []) {
          const row = make("div", "");
          row.className = "account-row";
          row.appendChild(make("span", `${contact.name} · ${contact.uin}`));
          const add = make("button", "添加");
          add.className = "settings-action-btn";
          add.type = "button";
          add.addEventListener("click", () => { void send("add-contact", { peerUin: contact.uin, name: contact.name }); });
          row.appendChild(add);
          list.appendChild(row);
        }
        if (!data.contacts?.length) list.appendChild(make("p", "此页没有好友"));
        get("btnNextQQContacts").hidden = !data.mayHaveMore;
        busy = false;
        render(snapshot);
      } catch (error) {
        if (request === serial) {
          busy = false;
          render(snapshot);
          get("qqSyncStatus").textContent = message(error.code);
        }
      }
    }
    get("btnReadQQContacts").addEventListener("click", () => { void contacts(1); });
    get("btnNextQQContacts").addEventListener("click", () => { void contacts(contactPage + 1); });
    get("btnAddQQPeer").addEventListener("click", () => {
      if (get("qqSyncPeerUin").value.trim()) void send("add-contact", { peerUin: get("qqSyncPeerUin").value.trim(), name: "" });
    });
    get('btnAddQQGroup').addEventListener('click',()=>{
      if(get('qqSyncGroupCode').value.trim()) void send('add-group',{groupCode:get('qqSyncGroupCode').value.trim()});
    });
    get("qqSyncPanel").addEventListener("toggle", () => { if (get("qqSyncPanel").open) void load(); });
    render(null);
    void load();
    return { load, dispose() { serial++; } };
  }
  global.QQSyncUI = { mount, message };
})(typeof window === "object" ? window : globalThis);
