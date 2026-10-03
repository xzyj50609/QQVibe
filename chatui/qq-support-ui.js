(function (global) {
  "use strict";
  function mount({ document, api, getScope, saveDiagnostic, onConversationChange }) {
    const get = id => document.getElementById(id);
    const panel = get("qqDataScopePanel"), rows = get("qqDataScopeRows");
    const exportButton = get("btnExportQQDiagnostics"), exportStatus = get("qqDiagnosticsStatus");
    let serial = 0, exporting = false, reading = true, managing = false, clearScope = null;
    panel.hidden = false;
    get("qqDiagnosticsRow").hidden = false;
    exportStatus.hidden = false;
    const doctorButton = get("btnQCEDoctor"), doctorStatus = get("qceDoctorStatus");
    if (doctorButton && doctorStatus && typeof global.desktopHost?.openQCEDoctor === "function") {
      doctorButton.hidden = false;
      doctorButton.addEventListener("click", async () => {
        doctorButton.disabled = true;
        doctorStatus.hidden = false;
        try {
          const opened = await global.desktopHost.openQCEDoctor();
          doctorStatus.textContent = opened ? "检测工具已打开。修复和扫码完成后，再点“连接本机 QQ / QCE”。" : "检测工具未能打开，可双击程序目录中的 QCE 检测入口。";
        } catch (_) { doctorStatus.textContent = "检测工具未能打开，请使用程序目录中的 QCE 检测入口。"; }
        finally { doctorButton.disabled = false; }
      });
    }
    const paragraph = value => { const node = document.createElement("p"); node.textContent = value; rows.appendChild(node); };
    const amount = value => Number.isSafeInteger(value) && value >= 0 ? String(value) : "未知";
    const date = (value, scan = false) => {
      if (scan && value === 0) return "接口起点（0）";
      const parsed = Number.isSafeInteger(value) && value >= 0 ? new Date(value) : null;
      return parsed && Number.isFinite(parsed.getTime()) ? parsed.toLocaleString() : "未记录";
    };
    const interval = value => Array.isArray(value) && value.length === 2 ?
      `${date(value[0], true)} 至 ${date(value[1], true)}` : "未记录";
    function render(data) {
      rows.replaceChildren();
      paragraph(`来源：QQVibe 本机消息副本；数据版本 ${amount(data.dataRevision)}。`);
      paragraph(`本机记录 ${amount(data.counts?.messages)} 条；有效文本 ${amount(data.counts?.validTexts)} 条；其中对方有效文本 ${amount(data.counts?.peerValidTexts)} 条。`);
      paragraph(`本机记录时间：${date(data.range?.startMs)} 至 ${date(data.range?.endMs)}。`);
      paragraph(`规范化版本：${(data.normalizeVersions || []).map(item => `${item.version}（${amount(item.messages)}条）`).join("、") || "暂无记录"}${data.moreNormalizeVersions ? "；还有其他版本" : ""}。`);
      paragraph("本机记录和已扫描的接口区间不能证明 QQ 全历史完整。");
      const provenance = data.provenance;
      if (provenance) {
        paragraph(`有来源观察 ${amount(provenance.recordedMessages)} 条；暂无来源观察 ${amount(provenance.unrecordedMessages)} 条。旧记录初次来源不会倒推补造。`);
        paragraph(`前向窗口成功记录 ${amount(provenance.forwardSuccesses)} 次；最近成功 ${date(provenance.lastForwardSuccessAtMs)}。重启后保留，文件补导不会更新该时间。`);
        for (const source of provenance.sources || []) paragraph(`${global.QQProvenanceUI?.kinds[source.kind] || "其他来源"}观察到 ${amount(source.messages)} 条；来源条数可能重叠。`);
        if (global.QQProvenanceUI) rows.appendChild(global.QQProvenanceUI.create({ document, api, getScope, filter: true }));
      } else paragraph("具体来源尚无保存记录，旧记录不会倒推补造。");
      const forward = data.forward;
      paragraph(forward ? `前向接口窗口：${interval([forward.windowStartMs, forward.windowEndMs])}；${["complete", "complete-empty"].includes(forward.state) ? "本轮窗口已扫描" : "窗口尚未读完"}；核对至 ${date(forward.scannedThroughMs, true)}。` : "前向接口扫描：暂无保存记录。");
      for (const [kind, title] of [["history", "旧历史补读"], ["reconcile", "近期迟到消息核对"]]) {
        const work = data[kind];
        if (!work) { paragraph(`${title}：暂无保存记录。`); continue; }
        paragraph(`${title}：${work.state === "complete" ? "本轮接口区间已扫描" : "尚有未读完区间"}；本轮截至 ${date(work.anchorEndMs, true)}；最近成功扫描时间 ${work.lastCompletedAtMs > 0 ? date(work.lastCompletedAtMs) : "未记录"}。`);
        const pending = [...(work.stack || []), ...(work.window ? [work.window] : [])];
        for (const range of pending) paragraph(`${title}待扫描：${interval(range)}。`);
        if (work.reason) paragraph(`${title}状态：${global.QQSyncUI?.message(work.reason) || "扫描尚未完成，请查看连接状态。"}`);
      }
    }
    function same(before, after) {
      return before.account === after.account && before.user === after.user && before.generation === after.generation;
    }
    async function load() {
      const scope = getScope(), request = ++serial;
      rows.replaceChildren();
      if (!scope.account || !scope.user) { paragraph("请先选择一个本机会话，再查看数据范围。"); return; }
      paragraph("正在读取当前会话的数据范围…");
      try {
        const data = await api(`/api/qq/data-scope?${new URLSearchParams({ account: scope.account, user: scope.user })}`);
        if (request !== serial || !same(scope, getScope())) return;
        if (data.account !== scope.account || data.user !== scope.user) throw new Error("scope changed");
        render(data);
        reading=data.readEnabled!==false;
        if(get('btnToggleQQConversationRead'))get('btnToggleQQConversationRead').textContent=reading?'停止读取此会话':'继续读取此会话';
        paragraph(reading?'当前会话允许同步新消息。':'当前会话已停止读取；已有消息与分析仍可浏览。');
      } catch (_) {
        if (request !== serial || !same(scope, getScope())) return;
        rows.replaceChildren(); paragraph("数据范围暂不可用，请核对当前账号并刷新。");
      }
    }
    function scopeChanged() {
      serial++;
      rows.replaceChildren(); paragraph("当前会话已变化，展开或刷新查看数据范围。");
      clearScope=null;
      if(get('qqClearConversationConfirm'))get('qqClearConversationConfirm').hidden=true;
      if (panel.open) void load();
    }
    async function download(report) {
      const url = global.URL.createObjectURL(new global.Blob([JSON.stringify(report, null, 2)], { type: "application/json;charset=utf-8" }));
      const link = document.createElement("a");
      link.href = url; link.download = "QQVibe-diagnostics.json";
      try { document.body.appendChild(link); link.click(); }
      finally { link.remove(); global.setTimeout(() => global.URL.revokeObjectURL(url), 1000); }
    }
    async function exportDiagnostic() {
      if (exporting) return;
      exporting = true; exportButton.disabled = true; exportStatus.textContent = "正在整理诊断信息…";
      try {
        const report = await api("/api/qq/diagnostics");
        if (report?.schema !== "qq-diagnostics-v1") throw new Error("invalid report");
        await (saveDiagnostic || download)(report);
        exportStatus.textContent = "已发起 JSON 下载，请在下载位置查看。";
      } catch (_) { exportStatus.textContent = "诊断信息导出未完成，请稍后重试。"; }
      finally { exporting = false; exportButton.disabled = false; }
    }
    panel.addEventListener("toggle", () => { if (panel.open) void load(); });
    get("btnRefreshQQDataScope").addEventListener("click", () => { void load(); });
    get('btnToggleQQConversationRead')?.addEventListener('click',async()=>{
      const scope=getScope();if(managing||!scope.account||!scope.user)return;
      managing=true;
      try{
        const result=await api('/api/qq/conversation/read',{method:'POST',body:JSON.stringify({account:scope.account,user:scope.user,enabled:!reading})});
        if(!same(scope,getScope()))return;
        reading=result.readEnabled;await onConversationChange?.();await load();
      }catch(_){paragraph('会话读取状态未能修改，请刷新后重试。');}
      finally{managing=false;}
    });
    get('btnClearQQConversation')?.addEventListener('click',()=>{
      const scope=getScope();if(managing||!scope.account||!scope.user)return;
      clearScope={...scope};get('qqClearConversationConfirm').hidden=false;
    });
    get('btnCancelClearQQConversation')?.addEventListener('click',()=>{clearScope=null;get('qqClearConversationConfirm').hidden=true;});
    get('btnConfirmClearQQConversation')?.addEventListener('click',async()=>{
      if(managing||!clearScope)return;
      const scope=clearScope;
      if(!same(scope,getScope())){clearScope=null;get('qqClearConversationConfirm').hidden=true;return;}
      managing=true;
      try{
        await api('/api/qq/conversation/clear',{method:'POST',body:JSON.stringify({account:scope.account,user:scope.user,confirm:true})});
        clearScope=null;get('qqClearConversationConfirm').hidden=true;await onConversationChange?.();
        rows.replaceChildren();paragraph('此会话副本已清除，完整恢复点保存在本机备份目录。');
      }catch(_){paragraph('副本清除未完成；请刷新读取状态后重试，或从保留的备份恢复。');}
      finally{managing=false;}
    });
    exportButton.addEventListener("click", () => { void exportDiagnostic(); });
    scopeChanged();
    return { load, scopeChanged, exportDiagnostic };
  }
  global.QQSupportUI = { mount };
})(typeof window === "object" ? window : globalThis);
