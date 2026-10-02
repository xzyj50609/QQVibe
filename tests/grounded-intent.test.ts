import assert from "node:assert/strict";
import { it } from "node:test";

import { groundedIntent, groundedIntentWithContext, type GroundedIntentLabel, type IntentEvidenceKind } from "../electron/laya/grounded-intent";

const positive: Array<[string, GroundedIntentLabel, IntentEvidenceKind]> = [
  ["早上好！", "greet", "greeting_phrase"],
  ["你好，小王。", "greet", "greeting_phrase"],
  ["谢谢你帮我检查。", "thank", "thanks_phrase"],
  ["谢谢你，帮了大忙。", "thank", "thanks_phrase"],
  ["好的，谢谢你了。", "thank", "thanks_phrase"],
  ["嗯呢。谢谢啊。", "thank", "thanks_phrase"],
  ["谢谢大家。", "thank", "thanks_phrase"],
  ["感谢大家的支持。", "thank", "thanks_phrase"],
  ["辛苦大家了。", "thank", "thanks_phrase"],
  ["好了", "confirm", "short_acknowledgement"],
  ["明白了。", "confirm", "short_acknowledgement"],
  ["收到啦", "confirm", "short_acknowledgement"],
  ["我看看", "inspect", "first_person_inspection"],
  ["我先看一下。", "inspect", "first_person_inspection"],
  ["没问题。", "agree", "explicit_acceptance"],
  ["我同意。", "agree", "explicit_acceptance"],
  ["嗯嗯", "agree", "explicit_acceptance"],
  ["这个可以。", "agree", "explicit_acceptance"],
  ["这样可以", "agree", "explicit_acceptance"],
  ["这个可以哦。", "agree", "explicit_acceptance"],
  ["我不去。", "reject", "explicit_refusal"],
  ["这次不参加了。", "reject", "explicit_refusal"],
  ["谢谢，不过我不参加。", "reject", "explicit_refusal"],
  ["周末要不要一起看电影？", "invite", "inclusive_invitation"],
  ["周六要不要一起去看展？", "invite", "inclusive_invitation"],
  ["下星期三要不要一起吃饭？", "invite", "inclusive_invitation"],
  ["咱们一起去吃饭吧。", "invite", "inclusive_invitation"],
  ["这个参数怎么设置？", "ask_question", "answer_seeking_question"],
  ["这个可以吗？", "ask_question", "answer_seeking_question"],
  ["是旧版还是新版啊", "ask_question", "answer_seeking_question"],
  ["配置文件在哪里", "ask_question", "answer_seeking_question"],
  ["好了吗？", "ask_question", "answer_seeking_question"],
  ["你们几个人", "ask_question", "answer_seeking_question"],
  ["他家几楼", "ask_question", "answer_seeking_question"],
  ["能不能帮我看一下日志？", "seek_help", "action_request"],
  ["请把文件发给我。", "seek_help", "action_request"],
  ["建议先备份配置。", "suggest_action", "advice_marker"],
  ["那就先备份一下。", "suggest_action", "advice_marker"],
  ["那就备份一下。", "suggest_action", "advice_marker"],
  ["那你先检查一下。", "suggest_action", "advice_marker"],
  ["配置大一点。", "suggest_action", "imperative_adjustment"],
  ["字体调大点。", "suggest_action", "imperative_adjustment"],
  ["让小王检查一下。", "suggest_action", "delegated_action"],
  ["别再改配置了。", "suggest_action", "negative_imperative"],
  ["不要删原文件。", "suggest_action", "negative_imperative"],
  ["我打算下周整理文档。", "plan", "first_person_intention"],
  ["我先试试。", "plan", "first_person_intention"],
  ["我先把日志保存一下。", "plan", "first_person_intention"],
  ["我们明天准备部署新版本。", "plan", "first_person_intention"],
  ["不对，版本号是 1.0.1。", "correct", "explicit_correction"],
  ["应该不是这个版本。", "correct", "explicit_correction"],
  ["qq_123456:\n应该不是这个版本。", "correct", "explicit_correction"],
  ["你说的“旧版本”不对，当前是新版。", "correct", "explicit_correction"],
  ["因为接口超时，所以请求失败。", "explain", "causal_explanation"],
  ["原因是服务器断开连接。", "explain", "causal_explanation"],
  ["我这算是一步一步引导CODX帮我破解了", "explain", "process_explanation"],
  ["我相当于逐步指导他完成了。", "explain", "process_explanation"],
  ["怎么又卡住了？", "complain", "negative_evaluation"],
  ["真无语，等了半天还没好。", "complain", "negative_evaluation"],
  ["文件已上传到共享盘。", "status_report", "progress_statement"],
  ["修好了。", "status_report", "progress_statement"],
  ["搞定了。", "status_report", "progress_statement"],
  ["部署完了。", "status_report", "progress_statement"],
  ["已经搞定了。", "status_report", "progress_statement"],
  ["还在处理中。", "status_report", "progress_statement"],
  ["刚刚跑通了。", "status_report", "progress_statement"],
  ["任务还没完成。", "status_report", "progress_statement"],
  ["告诉你个好消息，考试过了。", "share_news", "sharing_announcement"],
  ["我今天考过了。", "share_news", "sharing_announcement"],
  ["你好，今天几点开会？", "ask_question", "answer_seeking_question"],
  ["你要我怎么称呼你", "ask_question", "answer_seeking_question"],
];

const abstain = [
  "",
  "喊老板",
  "喊姐姐",
  "喊妈妈",
  "别人都喊我老板。",
  "“谢谢。”",
  "他说：“谢谢。”",
  "我不是在邀请你。",
  "假设我们明天一起去。",
  "不是说好了吗？",
  "难道你不知道吗？",
  "我什么时候说我同意了？",
  "？？",
  "我这不把文件发过来了吗？",
  "我想问一下。",
  "我准备好了。",
  "他好了。",
  "确认。",
  "我看看原因。",
  "我这算是一步一步修好了。",
  "我先试过了。",
  "我先试了。",
  "那就先这样。",
  "那就这样。",
  "房间大一点。",
  "他让我检查一下。",
  "让他检查了一下。",
  "是旧版还是新版的区别。",
  "别以为我同意了。",
  "别急。",
  "他打算下周离开。",
  "这个词叫“感谢”。",
  "我知道怎么修复这个问题。",
  "这说明为什么会失败。",
  "他解释了为什么没来。",
  "好的，谢谢你，我这就过去。",
  "好的，电话已经发给你了。",
  "这也太好找了。",
  "这些都是不错的选择呢。",
  "给你推荐几个地方吧。",
  "没什么。",
  "这个颜色不合适，我换一件。",
  "我还有几件。",
  "我买了几本。",
  "库存还有几件。",
];

for (const [message, label, evidenceKind] of positive) {
  it(`grounds ${JSON.stringify(message)}`, () => {
    assert.deepEqual(groundedIntent(message), { label, evidenceKind });
  });
}

for (const message of abstain) {
  it(`abstains on ${JSON.stringify(message)}`, () => {
    assert.equal(groundedIntent(message), null);
  });
}

it("uses invitation context to ground a deferral without changing target-only rules", () => {
  assert.deepEqual(groundedIntentWithContext("改天吧", "周六一起去看展吗？"),
    { label: "reject", evidenceKind: "contextual_deferral" });
  assert.equal(groundedIntentWithContext("改天吧", "今天忙吗？"), null);
  assert.deepEqual(groundedIntentWithContext("木有", "你是不是生气了？"),
    { label: "deny", evidenceKind: "contextual_denial" });
  assert.equal(groundedIntentWithContext("木有", "今天吃什么？"), null);
});
