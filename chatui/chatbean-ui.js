/* QQ-only visual shell. All data operations are delegated to existing controllers. */
(function (global) {
  "use strict";
  if (global.ProductConfig?.key !== "qq") return;
  const doc = global.document;
  const byId = id => doc.getElementById(id);
  const paths = Object.freeze({
    chat: '<path d="M5 4h14a2 2 0 0 1 2 2v10a2 2 0 0 1-2 2H9l-5 3v-3a2 2 0 0 1-2-2V6a3 3 0 0 1 3-2Z"/>',
    persona: '<circle cx="12" cy="7.5" r="3.5"/><path d="M4.5 20v-1.2c0-3.2 3.3-5.3 7.5-5.3s7.5 2.1 7.5 5.3V20Z"/>',
    settings: '<path d="m9.3 3-.6 2.2-1.6.9-2.2-.6-2.2 3.8 1.6 1.6v2.2l-1.6 1.6 2.2 3.8 2.2-.6 1.6.9.6 2.2h4.4l.6-2.2 1.6-.9 2.2.6 2.2-3.8-1.6-1.6v-2.2l1.6-1.6-2.2-3.8-2.2.6-1.6-.9-.6-2.2Z"/><circle cx="11.5" cy="12" r="3"/>',
    add: '<path d="M12 5v14M5 12h14"/>',
    close: '<path d="m6 6 12 12M6 18 18 6"/>',
  });
  function icon(name) {
    const svg = doc.createElementNS("http://www.w3.org/2000/svg", "svg");
    svg.setAttribute("viewBox", "0 0 24 24");
    svg.setAttribute("class", "cb-icon");
    svg.setAttribute("aria-hidden", "true");
    svg.innerHTML = paths[name]; // fixed internal vector paths, never user content
    return svg;
  }
  doc.body.classList.add("chatbean");
  global.ChatBeanSplitter?.mount(doc, global);
  const displayName = global.ProductConfig.displayName || "句豆 · ChatBean";
  doc.title = displayName;
  const titlebar = doc.querySelector(".desktop-titlebar");
  if (titlebar) titlebar.textContent = displayName;
  const about = byId("panelAbout");
  const upstreamAuthor = about.querySelector('a[href="https://github.com/tswawa"]');
  if (upstreamAuthor) upstreamAuthor.closest(".about-row").querySelector(".about-label").textContent = "上游作者";
  const upstreamRepo = about.querySelector('a[href="https://github.com/tswawa/WechatVibe"]');
  if (upstreamRepo) {
    const row = upstreamRepo.closest(".about-row");
    row.querySelector(".about-label").textContent = "上游项目";
    const own = doc.createElement("div"); own.className = "about-row";
    const label = doc.createElement("span"); label.className = "about-label"; label.textContent = "项目与反馈";
    const link = doc.createElement("a"); link.className = "about-val about-link";
    link.href = "https://github.com/xzyj50609/QQVibe"; link.target = "_blank"; link.rel = "noopener noreferrer";
    link.textContent = "句豆 · ChatBean"; own.append(label,link); row.before(own);
  }
  for (const label of about.querySelectorAll(".about-label"))
    if (["B站", "抖音"].includes(label.textContent)) label.textContent = "上游 " + label.textContent;
  const brand = doc.createElement("img");
  brand.src = "/assets/qqvibe-icon.png";
  brand.alt = "句豆";
  brand.className = "cb-brand-mark";
  doc.querySelector(".nav-rail").prepend(brand);
  for (const [id, name, label] of [["navChat", "chat", "聊天"], ["navPersona", "persona", "人物画像"], ["btnSettings", "settings", "设置"]]) {
    const node = byId(id);
    node.replaceChildren(icon(name));
    node.setAttribute("role", "button");
    node.setAttribute("tabindex", "0");
    node.setAttribute("aria-label", label);
    node.addEventListener("keydown", event => {
      if (event.key === "Enter" || event.key === " ") { event.preventDefault(); node.click(); }
    });
  }
  const addButton = doc.createElement("button");
  addButton.type = "button";
  addButton.className = "cb-add-button";
  addButton.setAttribute("aria-label", "添加联系人或群聊");
  addButton.setAttribute("aria-haspopup", "dialog");
  addButton.append(icon("add"));
  doc.querySelector(".search-header").append(addButton);
  let controller = null;
  let busy = false;
  let opener = null;
  for (const header of [doc.querySelector(".chat-header"), doc.querySelector(".persona-header")]) {
    const tabs = doc.createElement("div");
    tabs.className = "cb-view-tabs";
    tabs.setAttribute("role", "tablist");
    tabs.setAttribute("aria-label", "聊天与画像");
    for (const [view, label] of [["chat", "聊天"], ["persona", "画像"]]) {
      const button = doc.createElement("button");
      button.type = "button";
      button.className = "cb-view-tab";
      button.dataset.cbView = view;
      button.setAttribute("role", "tab");
      button.setAttribute("aria-label", label);
      button.setAttribute("aria-controls", view === "chat" ? "chatView" : "personaView");
      const text = doc.createElement("span"); text.textContent = label;
      button.append(icon(view), text);
      button.addEventListener("click", () => controller?.switchView(view));
      button.addEventListener("keydown", event => {
        if (!["ArrowLeft", "ArrowRight", "Home", "End"].includes(event.key)) return;
        event.preventDefault();
        const target = event.key === "Home" ? "chat" : event.key === "End" ? "persona" : view === "chat" ? "persona" : "chat";
        controller?.switchView(target);
        const visible = doc.querySelector('.pane-view.active .cb-view-tab[data-cb-view="' + target + '"]');
        visible?.focus();
      });
      tabs.append(button);
    }
    header.insertBefore(tabs, header.lastElementChild);
  }
  const dialog = doc.createElement("dialog");
  dialog.className = "cb-add-dialog";
  dialog.setAttribute("aria-labelledby", "cbAddTitle");
  dialog.innerHTML = '<div class="cb-dialog-head"><h2 id="cbAddTitle">添加会话</h2><button type="button" class="cb-close" aria-label="关闭添加会话"></button></div>' +
    '<form><div class="cb-form-field"><label for="cbAddKind">会话类型</label><select id="cbAddKind"><option value="contact">联系人 · 单聊</option><option value="group">群聊</option></select></div>' +
    '<div class="cb-form-field"><label id="cbNumberLabel" for="cbAddNumber">QQ 号</label><input id="cbAddNumber" required pattern="[0-9]{5,20}" inputmode="numeric" maxlength="20" autocomplete="off" placeholder="输入联系人的 QQ 号" aria-describedby="cbAddStatus"></div>' +
    '<p class="cb-form-note" id="cbAddStatus" role="status">从已连接的 QQ 读取会话并加入分析列表。</p>' +
    '<div class="cb-dialog-actions"><button type="button" data-cb-action="setup">连接与导入</button><button type="button" data-cb-action="cancel">取消</button><button type="submit" class="cb-primary">添加到列表</button></div></form>';
  dialog.querySelector(".cb-close").append(icon("close"));
  doc.body.append(dialog);
  const form = dialog.querySelector("form");
  const kind = byId("cbAddKind"), number = byId("cbAddNumber"), status = byId("cbAddStatus");
  function setBusy(value) {
    busy = value;
    for (const node of dialog.querySelectorAll("button,input,select")) node.disabled = value;
    dialog.setAttribute("aria-busy", String(value));
  }
  function close() { if (!busy) dialog.close(); }
  dialog.querySelector(".cb-close").addEventListener("click", close);
  dialog.querySelector('[data-cb-action="cancel"]').addEventListener("click", close);
  dialog.addEventListener("cancel", event => { if (busy) event.preventDefault(); });
  dialog.addEventListener("close", () => opener?.focus());
  kind.addEventListener("change", () => {
    byId("cbNumberLabel").textContent = kind.value === "group" ? "群号" : "QQ 号";
    number.placeholder = kind.value === "group" ? "输入群号" : "输入联系人的 QQ 号";
  });
  addButton.addEventListener("click", () => {
    opener = addButton;
    status.textContent = "从已连接的 QQ 读取会话并加入分析列表。";
    dialog.showModal();
    number.focus();
  });
  dialog.querySelector('[data-cb-action="setup"]').addEventListener("click", () => {
    close(); byId("btnSettings").click();
    doc.querySelector('.settings-tab-btn[data-tab="general"]').click();
    byId("qqSyncPanel").open = true;
    byId("qqSyncPanel").scrollIntoView({ block: "start" });
  });
  form.addEventListener("submit", async event => {
    event.preventDefault();
    if (busy || !form.reportValidity()) return;
    if (!controller?.addConversation) { status.textContent = "连接服务尚未准备好，请稍后重试或打开连接与导入。"; return; }
    setBusy(true); status.textContent = "正在添加会话…";
    try {
      const result = await controller.addConversation(kind.value, number.value.trim());
      setBusy(false);
      if (result?.ok) { number.value = ""; dialog.close(); }
      else status.textContent = result?.error || "添加未完成，请检查 QQ 连接后重试。";
    } catch { setBusy(false); status.textContent = "添加未完成，请检查 QQ 连接后重试。"; }
  });
  function setView(view) {
    for (const tab of doc.querySelectorAll("[data-cb-view]")) {
      const active = tab.dataset.cbView === view;
      tab.classList.toggle("active", active);
      tab.setAttribute("aria-selected", String(active));
      tab.tabIndex = active ? 0 : -1;
    }
  }
  setView("chat");
  function messageHeading(message, session, self) {
    const heading = doc.createElement("div");
    heading.className = "msg-sender";
    const name = message.side === "self" ? self?.name || "我" :
      message.senderName || (session?.isGroup ? message.senderId || "未知成员" : session?.name || "对方");
    const stamp = Number.isFinite(message.time) && message.time > 0 ? new Date(message.time).toLocaleString("zh-CN", {
      month: "2-digit", day: "2-digit", hour: "2-digit", minute: "2-digit", second: "2-digit", hour12: false,
    }) : "";
    heading.textContent = stamp ? `${name}  ${stamp}` : name;
    heading.title = heading.textContent;
    return heading;
  }
  global.ChatBeanUI = Object.freeze({ mount(value) { controller = value; }, setView, messageHeading });
})(typeof window === "object" ? window : globalThis);
