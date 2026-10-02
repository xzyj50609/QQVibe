(function (global) {
  "use strict";
  const kinds = { forward: "前向同步", history: "旧历史补读", reconcile: "近期核对", "file-import": "文件导入" };
  const formats = { "qce-api": "QCE 接口", "qce-single-json": "JSON 导出", "qce-chunked-jsonl": "分块 JSONL 导出" };
  const states = { complete: "窗口读取完成", "complete-empty": "窗口读取完成（无消息）", partial: "部分读取", error: "读取失败" };
  const dispositions = { inserted: "首次入库", unchanged: "重复观察", recalled: "撤回证据", revised: "修订证据", conflicts: "冲突证据" };
  const amount = value => Number.isSafeInteger(value) && value >= 0 ? String(value) : "未知";
  const date = value => {
    const parsed = Number.isSafeInteger(value) && value >= 0 ? new Date(value) : null;
    return parsed && Number.isFinite(parsed.getTime()) ? parsed.toLocaleString() : "未记录";
  };
  const range = value => Array.isArray(value) && value.length === 2 ? `${date(value[0])} 至 ${date(value[1])}` : "未记录";
  function same(left, right) {
    return left.account === right.account && left.user === right.user && left.generation === right.generation;
  }
  function create({ document, api, getScope, cursor = null, filter = false }) {
    const scope = getScope();
    const node = (tag, className, text) => {
      const item = document.createElement(tag); item.className = className;
      if (text !== undefined) item.textContent = text;
      return item;
    };
    const details = node("details", cursor ? "qq-message-source" : "qq-ingest-history");
    details.appendChild(node("summary", "", cursor ? "消息来源" : "接入和补导记录"));
    let select = null;
    if (filter) {
      select = node("select", "qq-ingest-kind"); select.setAttribute("aria-label", "记录类型");
      for (const [value, title] of [["", "全部记录"], ...Object.entries(kinds)]) {
        const option = node("option", "", title); option.value = value; select.appendChild(option);
      }
      select.value = ""; details.appendChild(select);
    }
    const list = node("div", "qq-ingest-records"); details.appendChild(list);
    const status = node("p", "qq-ingest-status"); status.setAttribute("role", "status"); details.appendChild(status);
    const more = node("button", "qq-ingest-more", "查看更早记录"); more.type = "button"; more.hidden = true; details.appendChild(more);
    const refresh = node("button", "qq-ingest-refresh", "刷新来源记录"); refresh.type = "button"; details.appendChild(refresh);
    let serial = 0, loading = false, before = null, activeController = null;
    function render(entry) {
      const block = node("div", "qq-ingest-record");
      const paragraph = text => block.appendChild(node("p", "", text));
      paragraph(`${kinds[entry.kind] || "未记录来源"} · ${formats[entry.format] || "格式未记录"} · 批次 ${amount(entry.runId)}`);
      paragraph(`记录时间 ${date(entry.completedAtMs)}；${entry.kind === "file-import" ?
        entry.state === "complete" ? "预览内记录已提交" : "按确认范围部分导入" : states[entry.state] || "状态未记录"}。`);
      if (entry.interfaceWindow) paragraph(`接口请求区间：${range(entry.interfaceWindow)}。`);
      if (entry.recordRange) paragraph(`本批消息时间：${range(entry.recordRange)}。`);
      paragraph(`本批接收 ${amount(entry.acceptedRows)} 条，拒绝 ${amount(entry.rejectedRows)} 条；新增 ${amount(entry.counts?.inserted)} 条，重复 ${amount(entry.counts?.unchanged)} 条。`);
      if (entry.sourceVersion) paragraph(`${entry.kind === "file-import" ? "导出声明版本" : "接口版本"} ${entry.sourceVersion}。`);
      if (entry.sourceSnapshot) paragraph(`解析快照指纹 ${entry.sourceSnapshot}（不是原文件字节校验）。`);
      for (const observation of entry.observations || [])
        paragraph(`${dispositions[observation.disposition] || "来源观察"}；规范化 ${observation.normalizeVersion || "未记录"}；内容版本指纹 ${observation.fingerprint || "未记录"}。`);
      if (entry.observationCount > (entry.observations || []).length)
        paragraph(`本批共 ${amount(entry.observationCount)} 个内容版本，仅展示前 5 个指纹；未把竞争内容覆盖成一条正文。`);
      if (entry.reason) paragraph(global.QQSyncUI?.message(entry.reason) || "本次读取尚未完成。");
      return block;
    }
    async function load(append = false) {
      if (loading || !same(scope, getScope()) || !scope.account || !scope.user) return;
      const request = ++serial;
      const controller = new global.AbortController(); activeController = controller;
      let timedOut = false;
      const timer = global.setTimeout(() => { timedOut = true; controller.abort(); }, 15000);
      loading = true; more.disabled = true; refresh.disabled = true;
      if (!append) { list.replaceChildren(); before = null; more.hidden = true; }
      status.textContent = "正在读取来源记录…";
      const query = new URLSearchParams({ account: scope.account, user: scope.user, limit: "10" });
      if (cursor) query.set("cursor", cursor);
      if (append && before) query.set("before", String(before));
      if (select?.value) query.set("kind", select.value);
      try {
        const data = await api(`/api/qq/${cursor ? "message-provenance" : "ingest-history"}?${query}`, {}, controller.signal);
        if (request !== serial || !same(scope, getScope())) return;
        if (data.account !== scope.account || data.user !== scope.user || !Array.isArray(data.entries)) throw new Error("scope changed");
        for (const entry of data.entries) list.appendChild(render(entry));
        before = Number.isSafeInteger(data.nextBefore) && data.nextBefore > 0 ? data.nextBefore : null;
        more.hidden = !before;
        status.textContent = list.children.length ? "来源记录不证明 QQ 全历史完整。" :
          cursor ? "此消息没有保存具体来源；旧记录不会倒推补造。" : "当前筛选暂无来源记录；旧记录不会倒推补造。";
      } catch (_) {
        if (request === serial && same(scope, getScope())) status.textContent = timedOut ? "读取来源记录超时，请重试。" : "来源记录暂不可用；数据可能已更新，请刷新消息或重试。";
      } finally {
        global.clearTimeout(timer);
        if (activeController === controller) activeController = null;
        if (request === serial) { loading = false; more.disabled = false; refresh.disabled = false; }
      }
    }
    function cancel() {
      serial++; activeController?.abort(); activeController = null; loading = false;
      more.disabled = false; refresh.disabled = false;
    }
    details.addEventListener("toggle", () => { if (details.open) void load(); else cancel(); });
    more.addEventListener("click", () => { void load(true); });
    refresh.addEventListener("click", () => { void load(); });
    select?.addEventListener("change", () => { cancel(); void load(); });
    return details;
  }
  global.QQProvenanceUI = { create, kinds };
})(typeof window === "object" ? window : globalThis);
