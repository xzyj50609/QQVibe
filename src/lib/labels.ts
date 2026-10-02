import catalog from "../../chatui/data/analysis-catalog.json";

const keyOf = (value: string): string => value.trim().toLowerCase().replace(/\s+/g, "_");
const emotions = catalog.emotions as Array<{ id: string; label: string; modelLabel: string; aliases?: string[] }>;
const legacyIntentLabels = (catalog as typeof catalog & { legacyIntentLabels?: Record<string, string> }).legacyIntentLabels ?? {};

export const EMOTION_LABELS: Record<string, string> = {
  ...Object.fromEntries(emotions.flatMap(({ label, aliases }) =>
    (aliases ?? []).map((alias) => [keyOf(alias), label]))),
  ...Object.fromEntries(emotions.flatMap(({ id, label, modelLabel }) =>
    [[keyOf(id), label], [keyOf(modelLabel), label], [keyOf(label), label]])),
};

/** Generic speech acts supported by the explicit evidence classifier. */
export const GROUNDED_INTENT_LABELS: Readonly<Record<string, string>> = Object.freeze({
  greet: "问候", thank: "感谢", confirm: "确认", inspect: "查看",
  agree: "同意", reject: "拒绝", invite: "邀约",
  ask_question: "提问", seek_help: "求助", suggest_action: "建议或指令",
  plan: "计划", correct: "纠正或异议", explain: "解释", complain: "抱怨",
  status_report: "状态报告", share_news: "分享",
});

export const INTENT_LABELS: Record<string, string> = {
  ...Object.fromEntries(Object.entries(legacyIntentLabels).map(([alias, display]) => [keyOf(alias), display])),
  ...Object.fromEntries(catalog.intents.flatMap(({ id, label, displayLabel, modelLabel }) =>
    [[keyOf(id), displayLabel], [keyOf(modelLabel), displayLabel], [keyOf(label), displayLabel], [keyOf(displayLabel), displayLabel]])),
  small_talk: "闲聊",
  smalltalk: "闲聊",
  seek_comfort: "求安慰",
  give_comfort: "安慰",
  show_affection: "表达好感",
  inform: "告知事实",
  general_exchange: "一般交流",
  make_plan: "计划",
  flirt: "暧昧",
  apologize: "道歉",
  joke: "玩笑",
  distance: "保持距离",
  follow_up: "追问",
  clarify: "澄清",
  share_feeling: "表达感受",
  confide: "倾诉",
  seek_company: "求陪伴",
  show_care: "关心",
  encourage: "鼓励",
  praise: "称赞",
  celebrate: "祝贺",
  miss_you: "表达想念",
  test_feelings: "探询心意",
  set_boundary: "设定边界",
  reconcile: "缓和关系",
  show_material: "展示内容",
  offer_help: "提供帮助",
  tease: "调侃",
  close_chat: "告别",
  deny: "否认",
  ...GROUNDED_INTENT_LABELS,
};

export function emotionLabel(label: string): string {
  if (!label) return "";
  const key = keyOf(label);
  return EMOTION_LABELS[key] ?? EMOTION_LABELS[label] ?? label;
}

export function intentLabel(label: string): string {
  if (!label) return "";
  const key = keyOf(label);
  return INTENT_LABELS[key] ?? INTENT_LABELS[label] ?? label;
}

/** Convert a model probability (0..1) to a display percentage. Never invents a value. */
export function formatPercent(probability: number): string {
  if (!Number.isFinite(probability)) return "—";
  const clamped = Math.max(0, Math.min(1, probability));
  if (clamped > 0 && clamped < 0.005) return "<1%";
  return `${Math.round(clamped * 100)}%`;
}

export function formatDelta(delta: number): string {
  if (!Number.isFinite(delta)) return "±0";
  return `${delta > 0 ? "+" : ""}${Math.round(delta)}`;
}

/** Human label for the analysis engine. Demo data is never presented as a Laya result. */
export function modelLabel(model: string, demo: boolean): string {
  if (demo) return "示例数据";
  if (!model) return "Laya · 本地";
  return /laya/i.test(model) ? "Laya · 本地" : model;
}
