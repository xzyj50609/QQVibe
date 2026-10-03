(function (global) {
  "use strict";
  const ERRORS = {
    "owner-mismatch": "本人 QQ 号与文件声明不一致，请检查导出文件。",
    "not-single-chat": "单聊文件出现多个对方，请核对会话类型；群聊请在字段设置中选择群聊并填写群号。",
    "invalid-export-json": "文件内容不完整或 JSON 格式错误，请等待导出完成后重试。",
    "missing-export-file": "未找到文件或分块，请保留完整导出目录。",
    "invalid-chunk-path": "分块路径无效，请重新导出完整文件。",
    "chunk-outside-export-directory": "分块不在导出目录内，请重新导出。",
    "export-changed-during-read": "读取时文件发生变化，请等待导出完成后重试。",
    "chunk-count-mismatch": "分块消息数与清单不一致，可能未导出完整。",
    "export-count-mismatch": "文件消息数与声明不一致，可能未导出完整。",
    "byte-budget-exceeded": "本次文件总量超过 2 GiB，请按时间范围分次导出；一个 manifest 的全部分块合计计算。",
    "row-budget-exceeded": "本次消息超过 200 万条，请按时间范围分次导出。",
    "export-item-too-large": "单条消息或文件元数据过大，请检查导出文件。",
    "unsupported-export-version": "导出版本不受支持，请使用 QQChatExporter V6 文件。",
    "unsupported-export-format": "导出格式不受支持，请选择单 JSON 或 manifest.json。",
    "peer-identity-mismatch": "文件中的对方身份不一致，请检查导出文件。",
    "invalid-export-encoding": "文件编码无法识别。支持 UTF-8，以及带编码标记的 UTF-16/UTF-32；请重新导出为 UTF-8。",
    "no-importable-messages": "没有可导入的消息。",
    "stale-import-preview": "预览已失效，请重新预览后确认。",
    "partial-import-confirmation-required": "请先确认是否跳过被拒绝的消息。",
    "invalid-export-metadata": "聊天信息字段不是预期结构，请核对文件，或指定消息列表与会话身份后重新预览。",
    "multiple-message-arrays": "发现多个消息列表，请在字段设置中指定本次导入的列表位置。",
    "message-array-required": "未识别出消息列表，请在字段设置中选择列表位置，再次预览。",
    "conversation-kind-required": "发现多位发言者，请明确选择单聊或群聊；群聊需要填写群号。",
    "multiple-conversations": "文件混有多个会话，请分别导出或选择单个会话的消息列表。",
    "conversation-kind-mismatch": "所选会话类型与文件声明不一致，请核对后重新预览。",
    "invalid-conversation-identity": "文件中的会话身份无效，请核对群号或对方 UID。",
    "sender-mapping-required": "发送者不是 QQ 号，请指定发送者字段，或填写昵称对应的 QQ 号。",
    "time-zone-required": "日期没有时区，请在字段设置中选择原记录的时区。",
    "time-unit-ambiguous": "数字时间的单位不明确，请选择秒或毫秒。",
    "invalid-time": "时间字段无法识别，请指定正确字段及时间单位。",
    "text-field-required": "没有识别出正文，请指定正文对应的字段。",
    "ambiguous-text-field": "多个正文字段的内容不同，请明确指定一个。",
    "ambiguous-time-field": "多个时间字段的值不同，请明确指定一个。",
    "ambiguous-sender-field": "多个发送者字段的值不同，请明确指定一个。",
    "non-qq-export": "文件声明来自其他聊天平台，不能归入 QQ 人物；请使用 QQ 记录。",
    "unsafe-export-path": "导出路径经过链接或云端占位目录，请将完整导出复制到普通本地文件夹再预览。",
    "export-permission-denied": "当前无法读取文件，请检查权限，或复制到可读取的本地文件夹后重试。",
    "export-io-error": "文件读取或临时存储失败，请检查文件可用性与磁盘剩余空间。",
    "invalid-json-mapping": "字段设置无效，请检查字段路径、会话身份和发送者 QQ 号。",
    "invalid-json-timezone": "时区设置无效，请重新选择。",
    "export-nesting-too-deep": "文件嵌套层级过深，请导出单个会话的消息列表。",
    "too-many-json-arrays": "文件包含过多列表，请导出单个会话后重试。",
    "invalid-json-line": "这行不是完整的 JSON 消息。",
    "invalid-body": "消息正文结构无法识别。",
    "invalid-native-id": "消息 ID 无效。",
    "invalid-sender": "发送者身份格式无效。",
  };
  function errorMessage(error) {
    return ERRORS[error?.code || error] || "导入未能完成，请检查文件和本地账号状态后重新预览。";
  }
  function mount({ document, api, chooseFile, onComplete, openAccount, timers = global }) {
    const get = id => document.getElementById(id);
    const node = (tag, value) => { const result = document.createElement(tag); result.textContent = value; return result; };
    let current = null, serial = 0, timer = null, requestBusy = false, pollFailures = 0;
    const senderInputs = new Map();
    let lastPreviewPath = null;
    const mappingFields = { recordPath: "qqImportRecordPath", text: "qqImportTextField",
      sender: "qqImportSenderField", time: "qqImportTimeField", id: "qqImportIdField",
      timeUnit: "qqImportTimeUnit", timeZone: "qqImportTimeZone", kind: "qqImportKind" };
    function mappingValue() {
      const mapping = {};
      for (const [key, id] of Object.entries(mappingFields)) {
        const value = get(id)?.value?.trim();
        if (value && value !== "auto") mapping[key] = value;
      }
      const target = get("qqImportTarget")?.value?.trim();
      if (target) mapping[mapping.kind === "group" || /^[0-9]+$/.test(target) ? "groupCode" : "peerUid"] = target;
      const senders = {};
      for (const [name, input] of senderInputs) if (input.value.trim()) senders[name] = input.value.trim();
      if (Object.keys(senders).length) mapping.senders = senders;
      return mapping;
    }
    const path = get("qqImportPath"), status = get("qqImportStatus"), preview = get("qqImportPreview");
    get("qqImportPanel").hidden = false;
    get("btnChooseQQExport").hidden = typeof chooseFile !== "function";
    const busyStates = new Set(["reading", "normalizing", "committing"]);
    const ownerValue = () => get("qqImportOwner").value === "manual" ? get("qqImportOwnerManual").value.trim() : get("qqImportOwner").value;
    function invalidatePreview() {
      if (requestBusy || busyStates.has(current?.state) || !current) return;
      serial++; timers.clearTimeout(timer); current = null;
      render(null); status.textContent = "文件或字段设置已更改，请重新预览后确认。";
    }
    function render(job) {
      const state = job?.state || "idle", busy = busyStates.has(state);
      const ready = state === "ready", identity = state === "identity", done = state === "complete";
      const needsPeer = ["peer-uid-required", "group-code-required"].includes(job?.reason);
      path.disabled = busy || requestBusy;
      for (const id of ["btnChooseQQExport", "btnPreviewQQExport"]) get(id).disabled = busy || requestBusy;
      get("btnCancelQQImport").hidden = !job || ["cancelled", "failed", "complete"].includes(state);
      get("btnCancelQQImport").disabled = requestBusy;
      get("btnCommitQQImport").hidden = !ready;
      get("btnCommitQQImport").disabled = requestBusy || !job?.preview?.uniqueMessages ||
        (job?.preview?.counts?.rowsRejected > 0 && !get("qqImportAcceptPartial").checked);
      get("btnOpenQQImport").hidden = !done;
      get("qqImportIdentity").hidden = !identity;
      get("qqImportPeerUid").hidden = !needsPeer;
      get("qqImportPeerLabel").hidden = !needsPeer;
      get("qqImportPeerLabel").textContent = job?.reason === "group-code-required" ? "文件缺少群号，请填写已核对的群号" : "文件缺少对方 UID，请填写已核对的对方 UID（u_ 开头）";
      get("btnMapQQImport").disabled = requestBusy || !ownerValue();
      get("qqImportPartialLabel").hidden = !ready || !(job.preview?.counts?.rowsRejected > 0);
      preview.hidden = !ready;
      if (state === "idle") status.textContent = "先预览文件，再确认本人身份和导入范围。";
      else if (state === "reading") status.textContent = `正在读取文件，已读 ${job.rowsRead || 0} 条；本地历史尚未更改。`;
      else if (state === "normalizing") status.textContent = "正在核对身份与消息…";
      else if (identity) status.textContent = needsPeer ? (job.reason === "group-code-required" ? "缺少群号，请确认后继续。" : "缺少对方 UID，确认后才能关联到同一会话。") : "文件未声明本人，请明确选择本人 QQ 号。";
      else if (state === "mapping") status.textContent = errorMessage(job.reason);
      else if (ready) status.textContent = "预览已完成。确认后将写入所列账号的本地历史。";
      else if (state === "committing") status.textContent = "正在导入；取消或失败会撤销本次消息写入。";
      else if (done) status.textContent = `导入完成：新增 ${job.result.inserted} 条，已有 ${job.result.unchanged} 条，冲突 ${job.result.conflicts} 条，回补 ${job.result.backfilled} 条。`;
      else if (state === "cancelled") status.textContent = "已取消，本次没有写入消息。";
      else status.textContent = errorMessage(job.error);
      const schema = job?.schema;
      get("qqImportSchema").hidden = !schema;
      if (schema) {
        get("qqImportFields").replaceChildren(...(schema.fields || []).map(value => { const option = node("option", value); option.value = value; return option; }));
        get("qqImportArrays").replaceChildren(...(schema.arrayPaths || []).map(value => { const option = node("option", value); option.value = value; return option; }));
        const lines = [];
        if (schema.arrayPaths?.length) lines.push(node("p", `消息列表位置：${schema.arrayPaths.join("、")}`));
        for (const sample of schema.samples || []) lines.push(node("blockquote", `${sample.sender} · ${sample.time}：${sample.text}`));
        for (const [reason, count] of Object.entries(schema.rejections || {})) lines.push(node("p", `${count} 条：${errorMessage(reason)}`));
        if (schema.generatedIds) lines.push(node("p", `${schema.generatedIds} 条没有原始消息 ID，按文件与行号保留。重导同一文件会去重；不同文件的重叠记录可能重复。`));
        get("qqImportSchema").replaceChildren(...lines);
        if (state === "mapping" || schema.senders?.length || Object.keys(schema.rejections || {}).length) get("qqImportMapping").open = true;
        for (const name of schema.senders || []) {
          if (senderInputs.has(name)) continue;
          const label = node("label", `${name} 对应的 QQ 号`);
          label.className = "qq-import-label";
          const input = node("input", ""); input.type = "text"; input.inputMode = "numeric"; input.autocomplete = "off";
          input.addEventListener("input", invalidatePreview);
          label.appendChild(input); senderInputs.set(name, input);
          get("qqImportSenderMappings").appendChild(label);
        }
      }
      for (const id of [...Object.values(mappingFields), "qqImportTarget"]) get(id).disabled = busy || requestBusy;
      for (const input of senderInputs.values()) input.disabled = busy || requestBusy;
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
        const target = data.kind === "group" ? `群聊：${data.name}（群号 ${data.groupCode}）` : `对方：${data.name}（${data.peerUin || data.peerUid}）`;
        preview.replaceChildren(node("p", `本人 QQ：${data.ownerUin}；${target}`),
          node("p", `${range}；共 ${counts.rowsTotal} 条，可接收 ${counts.rowsOk} 条，拒绝 ${counts.rowsRejected} 条，冲突 ${counts.rowsConflict} 条。`),
          node("p", `本人 ${data.directions.self} 条，${data.kind === "group" ? "其他成员" : "对方"} ${data.directions.peer} 条；文件内重复 ID ${data.duplicatesInFile} 条。`));
        if (counts.unknownTypes) preview.appendChild(node("p", `${counts.unknownTypes} 条未知类型保留为占位，不进入文本分析。`));
        for (const item of data.samples || []) preview.appendChild(node("blockquote", `${item.side === "self" ? "本人" : "对方"}：${item.text}`));
        if (data.rejected?.length) {
          preview.appendChild(node("p", `被拒绝的行：${data.rejected.map(item => item.row).join("、")}${counts.rowsRejected > data.rejected.length ? " 等" : ""}。`));
          for (const reason of new Set(data.rejected.map(item => item.reason))) preview.appendChild(node("p", errorMessage(reason)));
        }
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
      if (file) { path.value = file; invalidatePreview(); }
    });
    for (const id of ["qqImportPath", ...Object.values(mappingFields), "qqImportTarget"]) {
      get(id).addEventListener("input", invalidatePreview);
      get(id).addEventListener("change", invalidatePreview);
    }
    get("btnPreviewQQExport").addEventListener("click", () => {
      if (!path.value.trim()) { status.textContent = "请选择文件或填写本地完整路径。"; return; }
      if (lastPreviewPath !== null && lastPreviewPath !== path.value.trim()) {
        senderInputs.clear(); get("qqImportSenderMappings").replaceChildren(); get("qqImportTarget").value = "";
      }
      lastPreviewPath = path.value.trim();
      const mapping = mappingValue();
      void send("preview", { path: path.value.trim(), ...(Object.keys(mapping).length ? { mapping } : {}) });
    });
    get("btnMapQQImport").addEventListener("click", () => {
      if (!current || !ownerValue()) return;
      const body = { jobId: current.jobId, ownerUin: ownerValue() };
      if (["peer-uid-required", "group-code-required"].includes(current.reason)) body.peerUid = get("qqImportPeerUid").value.trim();
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
