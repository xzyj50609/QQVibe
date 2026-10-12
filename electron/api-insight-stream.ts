import type { ApiInsight } from './api-message-insights';

/** Emit only complete, explicitly attributed labels in the existing UI wire format. */
export function insightStream(normalize: (value: unknown) => ApiInsight | null,
  emit?: (delta: string) => void) {
  let raw = '';
  let scanned = 0;
  let quoted = false;
  let escaped = false;
  const stack: Array<{ char: string; start: number }> = [];
  const emitted = new Map<string, string>();
  function publish(insight: ApiInsight) {
    const value = JSON.stringify(insight);
    if (emitted.has(insight.id)) return;
    emitted.set(insight.id, value);
    const emotion = insight.status === 'ok' ? insight.affect?.feeling || insight.affect?.tone || '' : '';
    const intent = insight.status === 'ok' ? insight.intents[0] || '' : '';
    emit?.(`编号：${insight.id}\n情感：${emotion || '无'}\n意图：${intent || '无'}\n`);
  }
  function accept(value: unknown) {
    if (!value || typeof value !== 'object' || Array.isArray(value)) return;
    const record = value as Record<string, unknown>;
    const terminal = ['routine', 'uncertain', 'insufficient'].includes(String(record.status));
    const pair = ((record.emotion !== undefined || record.emotionLabel !== undefined) &&
      (record.intent !== undefined || record.intentLabel !== undefined)) ||
      (record.affect !== undefined && record.intents !== undefined);
    if (!terminal && !pair) return;
    try { const insight = normalize(value); if (insight) publish(insight); }
    catch { /* Partial/malformed/unknown records stay pending. */ }
  }
  return {
    push(delta: string) {
      if (!emit || typeof delta !== 'string') return;
      raw += delta;
      // Completed leaf objects can arrive before the surrounding JSON array ends.
      for (; scanned < raw.length; scanned++) {
        const c = raw[scanned]!;
        if (quoted) {
          if (escaped) escaped = false;
          else if (c === '\\') escaped = true;
          else if (c === '"') quoted = false;
          continue;
        }
        if (stack.length && c === '"') { quoted = true; continue; }
        if (c === '{' || c === '[') stack.push({ char: c, start: scanned });
        else if (c === '}' || c === ']') {
          const open = stack.pop();
          if (!open || (open.char === '{') !== (c === '}')) { stack.length = 0; continue; }
          if (c === '}') {
            try { accept(JSON.parse(raw.slice(open.start, scanned + 1))); } catch { /* malformed object */ }
          }
        }
      }
      // Legacy text is accepted only with a complete explicit ID line and a full
      // line for BOTH fields. No positional guesses while generation is running.
      const markers = [...raw.matchAll(/(?:^|\n)\s*(?:编号|id)\s*[:：]\s*([^\r\n]+)\r?\n/giu)];
      for (let i = 0; i < markers.length; i++) {
        const marker = markers[i]!;
        const next = markers[i + 1]?.index;
        const block = raw.slice(marker.index! + marker[0].length, next === undefined ? undefined : next + 1);
        const emotion = block.match(/(?:^|\n)\s*(?:情感|情绪|emotion)\s*[:：]\s*([^\r\n]*)\r?\n/iu);
        const intent = block.match(/(?:^|\n)\s*(?:意图|intent)\s*[:：]\s*([^\r\n]*)\r?\n/iu);
        if (emotion && intent) accept({ id: marker[1]!.trim(), emotion: emotion[1], intent: intent[1] });
      }
    },
    finish(values: Iterable<ApiInsight>) { if (emit) for (const value of values) publish(value); },
  };
}
