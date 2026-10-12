"use strict";
// Optional controls layered onto the accepted ChatBean UI and existing renderers.
window.AnalysisTools = (() => {
  let profiles = [], sourceSnapshot = null, profileBusy = false, activeDialog = null;
  const scope = () => ({ account: chatState.currentAccount, user: chatState.currentUser,
    generation: chatState.generation, sourceId: settingsState.modelSourceSnapshot?.sourceId });
  const current = captured => captured.account === chatState.currentAccount &&
    captured.user === chatState.currentUser && captured.generation === chatState.generation &&
    captured.sourceId === settingsState.modelSourceSnapshot?.sourceId;
  const post = (path, body) => api(path, { method: "POST", body: JSON.stringify(body) });
  const profileId = () => byId("selectApiProfile").value || null;
  const eligible = row => labelSideEligible(row) && row.kind === "text" &&
    typeof row.text === "string" && !!row.text.trim();

  function showSource(data) {
    sourceSnapshot = data;
    profiles = Array.isArray(data?.profiles) ? data.profiles : [];
    const select = byId("selectApiProfile"), selected = data?.profile || data?.api?.id || select.value;
    select.replaceChildren();
    const create = element("option", "", "新建档案"); create.value = ""; select.append(create);
    for (const profile of profiles) {
      const option = element("option", "", profile.name || profile.model);
      option.value = profile.id; select.append(option);
    }
    select.value = profiles.some(profile => profile.id === selected) ? selected : "";
    byId("inputApiProfileName").value = profiles.find(profile => profile.id === select.value)?.name || "";
  }
  function selectProfile() {
    const profile = profiles.find(value => value.id === profileId());
    invalidateModelDiscovery();
    byId("inputApiProfileName").value = profile?.name || "";
    byId("selectApiProtocol").value = profile?.protocol || "responses";
    byId("inputApiBaseUrl").value = profile?.baseUrl || "";
    byId("inputApiModelId").value = profile?.model || "";
    byId("inputApiContextTokens").value = profile?.contextTokens || "";
    byId("inputApiKey").value = "";
    byId("apiKeySaved").hidden = !profile?.hasKey;
    settingsState.modelSourceDraftDirty = true;
  }
  async function saveDraft() {
    const draft = apiModelDraft(true, true);
    const name = byId("inputApiProfileName").value.trim() || draft.model;
    return post("/api/model-source/profile/save", { ...draft, name,
      ...(profileId() ? { profileId: profileId() } : {}) });
  }
  async function profileAction(action) {
    if (profileBusy || settingsState.modelSourceBusy) return;
    profileBusy = true; settingsState.modelSourceBusy = true; updateModelSourceControls();
    for (const id of ["btnUseApiProfile", "btnSaveApiProfile", "btnDeleteApiProfile"]) byId(id).disabled = true;
    text("modelSourceStatus", "正在处理模型档案…");
    try {
      let data;
      if (action === "save") data = await saveDraft();
      else if (action === "activate") {
        const saved = await saveDraft();
        data = await post("/api/model-source/activate", { mode: "api", profileId: saved.profile });
      } else {
        if (!profileId()) throw new Error("请先选择已保存的档案");
        data = await post(action === "delete" ? "/api/model-source/profile/delete" : "/api/model-source/activate",
          action === "delete" ? { profileId: profileId() } : { mode: "api", profileId: profileId() });
      }
      if (!validModelSource(data)) throw new Error("模型档案状态读取失败");
      showModelSource(data);
      if (action === "save") {
        byId("selectModelSource").value = "api";
        selectProfile(); syncSavedApiKeyHint(); showModelSourceMode();
      }
      text("modelSourceStatus", action === "delete" ? "档案已删除，已有分析保留" :
        action === "save" ? "档案已保存" : "模型档案已启用");
    } catch (error) {
      text("modelSourceStatus", error.code ? modelSourceRequestError(error) : error.message || modelSourceRequestError(error));
    } finally {
      profileBusy = false; settingsState.modelSourceBusy = false; updateModelSourceControls();
      for (const id of ["btnUseApiProfile", "btnSaveApiProfile", "btnDeleteApiProfile"]) byId(id).disabled = false;
    }
  }
  async function loadWorkers() {
    try {
      const [local, remote] = await Promise.all([api("/api/analysis-workers"), api("/api/api-workers")]);
      byId("selectLocalWorkers").value = String(local.workers);
      byId("checkboxElasticWorkers").checked = local.elastic === true;
      byId("selectApiWorkers").value = String(remote.workers);
      text("analysisWorkersStatus", `本地运行 ${local.active}/${local.limit} · API 请求 ${remote.totalActive}/${remote.totalLimit}`);
    } catch { text("analysisWorkersStatus", "并发设置读取失败，请重新打开设置"); }
  }
  async function saveWorkers() {
    const button = byId("btnSaveAnalysisWorkers"); button.disabled = true;
    try {
      await post("/api/analysis-workers", { workers: Number(byId("selectLocalWorkers").value),
        elastic: byId("checkboxElasticWorkers").checked });
      await post("/api/api-workers", { workers: Number(byId("selectApiWorkers").value) });
      await loadWorkers();
      text("analysisWorkersStatus", "并发设置已保存");
    } catch (error) { text("analysisWorkersStatus", `保存失败：${error.message}`); }
    finally { button.disabled = false; }
  }
  function dialog(title) {
    activeDialog?.close(); activeDialog?.remove();
    const node = element("dialog", "analysis-tools-dialog");
    node.append(element("h2", "", title));
    const content = element("div", "analysis-tools-content"); node.append(content);
    const status = element("div", "model-source-status"); status.setAttribute("role", "status"); node.append(status);
    const close = element("button", "settings-action-btn", "关闭"); close.type = "button";
    close.addEventListener("click", () => node.close()); node.append(close);
    document.body.append(node); node.showModal(); activeDialog = node;
    return { node, content, status };
  }
  async function readLabels(captured, selected, around) {
    const ids = selected.map(row => String(row.id));
    if (settingsState.modelSourceSnapshot.mode === "api") {
      const params = new URLSearchParams({ user: captured.user, ids: JSON.stringify(ids) });
      if (around) params.set("around", around);
      const data = await api(`/api/model-insights?${params}`);
      if (data.account !== captured.account || data.sourceId !== captured.sourceId || !current(captured)) return;
      const key = apiInsightKey(captured.account, captured.user, captured.sourceId);
      const entry = labelState.apiInsightCache.get(key);
      if (entry) for (const id of ids) {
        if (data.results?.[id]) entry.results[id] = data.results[id];
        else delete entry.results[id];
      }
    } else {
      const params = new URLSearchParams({ account: captured.account, user: captured.user, ids: JSON.stringify(ids) });
      const data = await api(`/api/analysis-labels?${params}`);
      if (data.account !== captured.account || data.user !== captured.user || !current(captured)) return;
      for (const id of ids) {
        if (data.results?.[id]) labelState.results[id] = data.results[id];
        else delete labelState.results[id];
      }
    }
    refreshLabels();
    cacheCurrentSession({ messages: chatState.messages, results: labelState.results });
  }
  async function runTargets(selected, force, statusNode) {
    const captured = scope();
    if (!captured.account || !captured.user || !selected.length) throw new Error("请先选择要分析的文本");
    const ids = selected.map(row => String(row.id));
    const around = selected[Math.floor(selected.length / 2)]?.historyCursor;
    const remote = settingsState.modelSourceSnapshot.mode === "api";
    cancelApiInsightWork(); // An earlier automatic preview must not overwrite this exact request's UI.
    statusNode.textContent = `正在提交 ${ids.length} 条文本…`;
    const reply = await post(remote ? "/api/model-insights" : "/api/analyze", remote ?
      { account: captured.account, user: captured.user, limit: 500, targetIds: ids, force: true,
        ...(around ? { around } : {}) } :
      { account: captured.account, user: captured.user, mode: force ? "message" : "recent", limit: 500,
        ...(force ? { messageId: ids[0] } : { targetIds: ids }) });
    let job = reply.job;
    if (job && ["queued", "running"].includes(job.status)) {
      while (current(captured) && ["queued", "running"].includes(job.status)) {
        statusNode.textContent = `分析中 ${Number(job.processed) || 0}/${Number(job.total) || ids.length}`;
        await new Promise(resolve => setTimeout(resolve, 800));
        const data = await api(`/api/analysis-job?id=${encodeURIComponent(job.id)}`);
        if (!current(captured)) return;
        job = data.job;
      }
    }
    if (!current(captured)) { statusNode.textContent = "会话或模型已切换，结果保留在原范围"; return; }
    if (job?.status !== "done") throw new Error(job?.error || `任务${job?.status || "未提交"}`);
    await readLabels(captured, selected, around);
    statusNode.textContent = `已完成所选 ${ids.length} 条文本`;
  }
  function chooseTargets() {
    if (!chatState.currentUser) return;
    const captured = scope(), rows = chatState.messages.filter(eligible).slice(-500);
    const panel = dialog(`选句分析 · 当前载入 ${rows.length} 条文本`);
    const choices = [];
    const list = element("div", "analysis-target-list"); panel.content.append(list);
    for (const message of rows) {
      const row = element("label", "analysis-target-row"), check = document.createElement("input");
      check.type = "checkbox"; check.dataset.messageId = String(message.id);
      row.append(check, element("span", "", `${message.senderName || (message.side === "self" ? "我" : "对方")}：${message.text.slice(0, 180)}`));
      list.append(row); choices.push({ check, message });
    }
    const all = element("button", "settings-action-btn", "全选已载入文本"); all.type = "button";
    all.addEventListener("click", () => choices.forEach(({ check }) => { check.checked = true; }));
    const run = element("button", "settings-action-btn model-source-primary", "分析所选文本"); run.type = "button";
    run.addEventListener("click", async () => {
      if (!current(captured)) { panel.status.textContent = "会话已切换，请重新打开选句"; return; }
      run.disabled = true;
      try { await runTargets(choices.filter(({ check }) => check.checked).map(({ message }) => message), false, panel.status); }
      catch (error) { panel.status.textContent = `分析失败：${error.message}`; }
      finally { run.disabled = false; }
    }); panel.content.append(all, run);
  }
  function decorateMessage(message, wrap) {
    if (!eligible(message) || wrap.querySelector(".analysis-rerun")) return;
    const captured = scope(), button = element("button", "analysis-rerun", "重算这句"); button.type = "button";
    button.title = "使用当前模型重新分析这一句";
    button.addEventListener("click", async () => {
      if (captured.account!==chatState.currentAccount || captured.user!==chatState.currentUser ||
          captured.generation!==chatState.generation) return;
      button.disabled = true; button.textContent = "正在重算…";
      const result = element("span", "analysis-rerun-status"); wrap.append(result);
      try { await runTargets([message], true, result); }
      catch (error) { result.textContent = `重算失败：${error.message}`; }
      finally { button.disabled = false; button.textContent = "重算这句"; }
    }); wrap.append(button);
  }
  function manageScope() {
    if (!chatState.currentUser) return;
    const captured = scope(), panel = dialog("当前会话分析管理");
    const requested = document.createElement("input"); requested.type = "checkbox";
    requested.checked = chatState.requestedConversations?.has(captured.user) || false;
    const requestRow = element("label", "analysis-target-row"); requestRow.append(requested, element("span", "", "持续更新本单聊画像"));
    if (!chatState.sessions.get(captured.user)?.isGroup) panel.content.append(requestRow);
    requested.addEventListener("change", async () => {
      requested.disabled = true;
      try {
        if (!current(captured)) throw new Error("会话已切换");
        const saved = await post("/api/conversation-request", { expectedAccount: captured.account,
          session: captured.user, requested: requested.checked });
        chatState.requestedConversations = new Set(saved.requestedSessions || []);
        if (requested.checked) resumeLocalAnalysis();
        panel.status.textContent = requested.checked ? "本会话将继续更新画像" : "已停止后续自动画像更新";
      } catch (error) { requested.checked = !requested.checked; panel.status.textContent = error.message; }
      finally { requested.disabled = false; }
    });
    panel.content.append(element("p", "qq-startup-hint", "清除所选来源的分析结果后可重新计算，聊天记录和其他来源的结果继续保留。"));
    for (const [kind, title] of [["portrait", "清除当前目标画像"], ["conversation", "清除当前来源的会话分析"]]) {
      const button = element("button", "settings-danger-btn", title); button.type = "button";
      button.addEventListener("click", async () => {
        if (button.dataset.confirmed !== "yes") { button.dataset.confirmed = "yes"; button.textContent = `确认${title}`; return; }
        button.disabled = true;
        try {
          if (!current(captured)) throw new Error("会话或模型已切换");
          await post("/api/analysis-scope/clear", { account: captured.account, user: captured.user,
            sourceId: captured.sourceId, kind, ...(kind === "portrait" && portraitState.activeMember ? { member: portraitState.activeMember } : {}) });
          panel.status.textContent = "已清除所选范围，可重新分析";
          if (kind === "conversation") {
            if(settingsState.modelSourceSnapshot.mode==='api')
              labelState.apiInsightCache.delete(apiInsightKey(captured.account,captured.user,captured.sourceId));
            else labelState.results = {};
            cancelApiInsightWork(); refreshLabels();
          }
          for(const map of [portraitState.profileCache,portraitState.storedProfileSnapshots])
            for(const key of map.keys()) {
              const saved=JSON.parse(key);
              if(saved[0]!==captured.account || saved[1]!==captured.user) continue;
              const sameSource=settingsState.modelSourceSnapshot.mode==='api'?saved[3]===captured.sourceId:saved.length===3;
              if(sameSource && (kind==='conversation' || saved[2]===(portraitState.activeMember || ''))) map.delete(key);
            }
          saveStoredProfiles();
          ++portraitState.apiPortraitRequest;
          clearTimeout(portraitState.apiPortraitPollTimer);
          portraitState.apiPortraitSnapshot = null;
          clearProfileView("已清除当前范围，可重新分析");
        } catch (error) { panel.status.textContent = `清除失败：${error.message}`; }
        finally { button.disabled = false; button.dataset.confirmed = ""; button.textContent = title; }
      }); panel.content.append(button);
    }
  }
  byId("selectApiProfile").addEventListener("change", selectProfile);
  byId("btnSaveApiProfile").addEventListener("click", () => profileAction("save"));
  byId("btnUseApiProfile").addEventListener("click", () => profileAction("use"));
  byId("btnDeleteApiProfile").addEventListener("click", () => profileAction("delete"));
  byId("btnSaveAnalysisWorkers").addEventListener("click", saveWorkers);
  byId("btnSettings").addEventListener("click", loadWorkers);
  byId("btnChooseAnalysis").addEventListener("click", chooseTargets);
  byId("btnAnalysisScope").addEventListener("click", manageScope);
  if (settingsState.modelSourceResolved) showSource(settingsState.modelSourceSnapshot);
  return { showSource, decorateMessage, chooseTargets, manageScope, runTargets,
    draftProfile: () => profiles.find(value => value.id === profileId()),
    selectedProfileId: profileId, activateDraft: () => profileAction("activate") };
})();
