(function (global) {
  "use strict";
  const ERRORS = {
    "owner-mismatch": "本人 QQ 号与文件声明不一致，请检查导出文件。",
    "not-single-chat": "文件包含群聊或多个对方，本软件只支持单聊。",
    "invalid-export-json": "文件内容不完整或 JSON 格式错误，请等待导出完成后重试。",
    "missing-export-file": "未找到文件或分块，请保留完整导出目录。",
    "invalid-chunk-path": "分块路径无效，请重新导出完整文件。",
    "chunk-outside-export-directory": "分块不在导出目录内，请重新导出。",
    "export-changed-during-read": "读取时文件发生变化，请等待导出完成后重试。",
    "chunk-count-mismatch": "分块消息数与清单不一致，可能未导出完整。",
    "export-count-mismatch": "文件消息数与声明不一致，可能未导出完整。",
    "byte-budget-exceeded": "导出超过 128 MiB，请按较小时间范围分次导出。",
    "row-budget-exceeded": "导出超过 20 万条，请按较小时间范围分次导出。",
    "export-item-too-large": "单条消息或文件元数据过大，请检查导出文件。",
    "unsupported-export-version": "导出版本不受支持，请使用 QQChatExporter V6 文件。",
    "unsupported-export-format": "导出格式不受支持，请选择单 JSON 或 manifest.json。",
    "peer-identity-mismatch": "文件中的对方身份不一致，请检查导出文件。",
    "invalid-export-encoding": "文件编码错误，请使用 UTF-8 导出。",
    "no-importable-messages": "没有可导入的消息。",
    "stale-import-preview": "预览已失效，请重新预览后确认。",
    "partial-import-confirmation-required": "请先确认是否跳过被拒绝的消息。",
  };
  function errorMessage(error) {
    return ERRORS[error?.code || error] || "导入未能完成，请检查文件和本地账号状态后重新预览。";
  }
  function mount({ document, api, chooseFile, onComplete, openAccount, timers = global }) {
    const get = id => document.getElementById(id);
    const node = (tag, value) => { const result = document.createElement(tag); result.textContent = value; return result; };
    let current = null, serial = 0, timer = null, requestBusy = false, pollFailures = 0;
    const path = get("qqImportPath"), status = get("qqImportStatus"), preview = get("qqImportPreview");
    get("qqImportPanel").hidden = false;
    get("btnChooseQQExport").hidden = typeof chooseFile !== "function";
    const busyStates = new Set(["reading", "normalizing", "committing"]);
    const ownerValue = () => get("qqImportOwner").value === "manual" ? get("qqImportOwnerManual").value.trim() : get("qqImportOwner").value;
    function render(job) {
      const state = job?.state || "idle", busy = busyStates.has(state);
      const ready = state === "ready", identity = state === "identity", done = state === "complete";
      path.disabled = busy || requestBusy;
      for (const id of ["btnChooseQQExport", "btnPreviewQQExport"]) get(id).disabled = busy || requestBusy;
      get("btnCancelQQImport").hidden = !job || ["cancelled", "failed", "complete"].includes(state);
      get("btnCancelQQImport").disabled = requestBusy;
      get("btnCommitQQImport").hidden = !ready;
      get("btnCommitQQImport").disabled = requestBusy || !job?.preview?.uniqueMessages ||
        (job?.preview?.counts?.rowsRejected > 0 && !get("qqImportAcceptPartial").checked);
      get("btnOpenQQImport").hidden = !done;
      get("qqImportIdentity").hidden = !identity;
      get("qqImportPeerUid").hidden = job?.reason !== "peer-uid-required";
      get("qqImportPeerLabel").hidden = job?.reason !== "peer-uid-required";
      get("btnMapQQImport").disabled = requestBusy || !ownerValue();
      get("qqImportPartialLabel").hidden = !ready || !(job.preview?.counts?.rowsRejected > 0);
      preview.hidden = !ready;
      if (state === "idle") status.textContent = "先预览文件，再确认本人身份和导入范围。";
      else if (state === "reading") status.textContent = `正在读取文件，已读 ${job.rowsRead || 0} 条；本地历史尚未更改。`;
      else if (state === "normalizing") status.textContent = "正在核对身份与消息…";
      else if (identity) status.textContent = job.reason === "peer-uid-required" ? "缺少对方 UID，确认后才能关联到同一会话。" : "文件未声明本人，请明确选择本人 QQ 号。";
      else if (ready) status.textContent = "预览已完成。确认后将写入所列账号的本地历史。";
      else if (state === "committing") status.textContent = "正在导入；取消或失败会撤销本次消息写入。";
      else if (done) status.textContent = `导入完成：新增 ${job.result.inserted} 条，已有 ${job.result.unchanged} 条，冲突 ${job.result.conflicts} 条，回补 ${job.result.backfilled} 条。`;
      else if (state === "cancelled") status.textContent = "已取消，本次没有写入消息。";
      else status.textContent = errorMessage(job.error);
      if (identity) {
        const selected = get("qqImportOwner").value;
        get("qqImportOwner").replaceChildren(node("option", "请选择本人 QQ 号"));
        get("qqImportOwner").firstChild.value = "";
        for (const item of job.participants || []) {
          const option = node("option", `${item.name ? item.name + " · " : ""}${item.uin}`);
          option.value = item.uin;
          get("qqImportOwner").appendChild(option);
        }
        if (job.ownerUin && !(job.participants || []).some(item => item.uin === job.ownerUin)) {
          const declared = node("option", String(job.ownerUin));
          declared.value = job.ownerUin;
          get("qqImportOwner").appendChild(declared);
        }
        const manual = node("option", "本人不在列表，手动填写");
        manual.value = "manual";
        get("qqImportOwner").appendChild(manual);
        get("qqImportOwner").value = selected || job.ownerUin || "";
        get("qqImportOwnerManual").hidden = get("qqImportOwner").value !== "manual";
        get("btnMapQQImport").disabled = requestBusy || !ownerValue();
      }
      if (ready) {
        const data = job.preview, counts = data.counts;
        const range = data.range?.map(value => new Date(value).toLocaleString()).join(" 至 ") || "无时间范围";
        preview.replaceChildren(node("p", `本人 QQ：${data.ownerUin}；对方：${data.name}（${data.peerUin || data.peerUid}）`),
          node("p", `${range}；共 ${counts.rowsTotal} 条，可接收 ${counts.rowsOk} 条，拒绝 ${counts.rowsRejected} 条，冲突 ${counts.rowsConflict} 条。`),
          node("p", `本人 ${data.directions.self} 条，对方 ${data.directions.peer} 条；文件内重复 ID ${data.duplicatesInFile} 条。`));
        if (counts.unknownTypes) preview.appendChild(node("p", `${counts.unknownTypes} 条未知类型保留为占位，不进入文本分析。`));
        for (const item of data.samples || []) preview.appendChild(node("blockquote", `${item.side === "self" ? "本人" : "对方"}：${item.text}`));
        if (data.rejected?.length) preview.appendChild(node("p", `被拒绝的行：${data.rejected.map(item => item.row).join("、")}${counts.rowsRejected > data.rejected.length ? " 等" : ""}。`));
      }
    }
    async function poll(expected) {
      if (expected !== serial || !current) return;
      try {
        const result = await api(`/api/qq/import?jobId=${encodeURIComponent(current.jobId)}`);
        if (expected !== serial || result.jobId !== current.jobId) return;
        current = result;
        pollFailures = 0;
        render(current);
        if (busyStates.has(result.state)) timer = timers.setTimeout(() => { void poll(expected); }, 500);
        else if (result.state === "complete") await onComplete?.(result.result);
      } catch (error) {
        if (expected === serial) {
          pollFailures++;
          status.textContent = pollFailures < 5 ? "读取导入进度失败，正在重试；可取消后重新预览。" :
            "暂时无法确认导入结果。恢复连接后，点取消核对结果，或重新预览同一文件；重复导入会按消息 ID 去重。";
          if (pollFailures < 5) timer = timers.setTimeout(() => { void poll(expected); }, 1500);
          else { get("btnPreviewQQExport").disabled = false; path.disabled = false; }
        }
      }
    }
    async function send(action, body) {
      if (requestBusy) return;
      requestBusy = true;
      const expected = ++serial;
      pollFailures = 0;
      timers.clearTimeout(timer);
      render(current);
      try {
        const result = await api(`/api/qq/import/${action}`, { method: "POST", body: JSON.stringify(body) });
        if (expected !== serial) return;
        current = result;
        get("qqImportAcceptPartial").checked = false;
        requestBusy = false;
        render(current);
        if (busyStates.has(current.state)) timer = timers.setTimeout(() => { void poll(expected); }, 200);
        else if (current.state === "complete") await onComplete?.(current.result);
      } catch (error) {
        if (expected === serial) {
          requestBusy = false;
          if (current && (action === "commit" || action === "cancel") && !error.status) {
            current = { ...current, state: "committing" };
            render(current);
            status.textContent = "请求结果尚未确认，正在核对进度；不会重复提交。";
            timer = timers.setTimeout(() => { void poll(expected); }, 500);
            return;
          }
          render(current);
          status.textContent = errorMessage(error);
        }
      }
    }
    get("btnChooseQQExport").addEventListener("click", async () => {
      const file = await chooseFile?.();
      if (file) path.value = file;
    });
    get("btnPreviewQQExport").addEventListener("click", () => {
      if (!path.value.trim()) { status.textContent = "请选择文件或填写本地完整路径。"; return; }
      void send("preview", { path: path.value.trim() });
    });
    get("btnMapQQImport").addEventListener("click", () => {
      if (!current || !ownerValue()) return;
      const body = { jobId: current.jobId, ownerUin: ownerValue() };
      if (current.reason === "peer-uid-required") body.peerUid = get("qqImportPeerUid").value.trim();
      void send("identity", body);
    });
    get("qqImportOwner").addEventListener("change", () => { render(current); });
    get("qqImportOwnerManual").addEventListener("input", () => { render(current); });
    get("qqImportAcceptPartial").addEventListener("change", () => { render(current); });
    get("btnCommitQQImport").addEventListener("click", () => {
      if (current?.state === "ready") void send("commit", { jobId: current.jobId,
        previewToken: current.previewToken, acceptPartial: get("qqImportAcceptPartial").checked });
    });
    get("btnCancelQQImport").addEventListener("click", () => {
      if (current) void send("cancel", { jobId: current.jobId });
    });
    get("btnOpenQQImport").addEventListener("click", () => {
      if (current?.state === "complete") void openAccount?.(current.result);
    });
    render(null);
    return { dispose() { serial++; timers.clearTimeout(timer); }, getCurrent: () => current };
  }
  global.QQImportUI = { mount, errorMessage };
})(typeof window === "object" ? window : globalThis);
