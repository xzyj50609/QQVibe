/**
 * High-precision speech acts supported by words in the target message alone.
 * This module does not read conversation history, call a model, or invent scores.
 * A null result means that the message does not supply enough unambiguous evidence.
 */
export type GroundedIntentLabel =
  | "greet"
  | "thank"
  | "confirm"
  | "inspect"
  | "agree"
  | "reject"
  | "deny"
  | "invite"
  | "ask_question"
  | "seek_help"
  | "suggest_action"
  | "plan"
  | "correct"
  | "explain"
  | "complain"
  | "status_report"
  | "share_news"
  | "close_chat";

export type IntentEvidenceKind =
  | "greeting_phrase"
  | "thanks_phrase"
  | "short_acknowledgement"
  | "first_person_inspection"
  | "explicit_acceptance"
  | "explicit_refusal"
  | "contextual_deferral"
  | "contextual_denial"
  | "inclusive_invitation"
  | "answer_seeking_question"
  | "action_request"
  | "advice_marker"
  | "negative_imperative"
  | "imperative_adjustment"
  | "delegated_action"
  | "first_person_intention"
  | "concrete_reschedule"
  | "conversation_pause"
  | "conditional_acceptance"
  | "explicit_correction"
  | "causal_explanation"
  | "process_explanation"
  | "negative_evaluation"
  | "progress_statement"
  | "sharing_announcement";

export interface GroundedIntent {
  label: GroundedIntentLabel;
  evidenceKind: IntentEvidenceKind;
}

function result(label: GroundedIntentLabel, evidenceKind: IntentEvidenceKind): GroundedIntent {
  return { label, evidenceKind };
}

// A quoted speech act belongs to its quoted speaker, not necessarily to this sender.
const QUOTED = /“[^”]*”|「[^」]*」|『[^』]*』|‘[^’]*’|"[^"]*"|`[^`]*`/gu;
const INVITATION_CONTEXT = /(?:要不要|一起|约|有空|安排|见面|出来|吃饭|看展|看电影|周六|周日|周末|明天|后天|下周)/u;
const DEFERRAL_TEXT = /改天|以后再说|再说吧|再约|看情况|过(?:几|两)天|有空再说|晚点再说/u;
const DENIAL_CONTEXT = /喜欢|爱|在乎|想我|暧昧|关系|要不要|是不是|会不会|是否/u;
const CONCRETE_TIME = /今天|明天|后天|今晚|明早|周[一二三四五六日天]|周末|下周|下个月|\d{1,2}[号日点]|上午|下午|晚上/u;

/** Transparent opinion framing does not change the action in the following clause. */
export function normalizeIntentTarget(targetText: string): string {
  if (typeof targetText !== "string") return "";
  const quotedResponse = /^(?:他说|她说|他们说|有人说|[^，,。！？?]{1,12}说)[:：]?\s*(?:“[^”]*”|「[^」]*」|『[^』]*』|‘[^’]*’|"[^"]*")[，,。.;；\s]*(?:但是|不过|可是|然而|但)?\s*((?:我|我们).+)$/u.exec(targetText.trim());
  let text = (quotedResponse ? quotedResponse[1] : targetText).replace(QUOTED, " ").replace(/\s+/gu, " ").trim()
    .replace(/^(?:qq_\d+|wxid_[\w-]+|[\w-]+@chatroom)\s*:\s*/u, "")
    .replace(/^(?:我的(?:想法|建议|看法)是|我觉得|在我看来)[：:，,\s]+/u, "").trim();
  const reported = /^(?:他说|她说|他们说|有人说|听说|转述|引用)/u.test(text)
    || !/^我(?:们)?/u.test(text) && /^[^，,。！？?]{1,20}(?:说|问我|让我|表示|提到|请求我)[：:，,]/u.test(text);
  if (reported && !/^[^，,。.;；]*[：:]\s*[^，,。.;；\s]/u.test(text)) {
    const response = /(?:[，,]\s*(?:但是|不过|可是|然而|但)|[。.;；]\s*(?:但是|不过|可是|然而|但)?)\s*((?:我|我们).+)$/u.exec(text);
    if (response) text = response[1].trim();
  }
  return text;
}

/** An explicitly reported or negated act does not authorize its positive label. */
export function isUnattributableIntentTarget(targetText: string): boolean {
  const text = normalizeIntentTarget(targetText);
  return !text || /^(?:他说|她说|他们说|有人说|听说|转述|引用|比如|例如|假设|所谓)\s*[:：，,]?/u.test(text)
    || /^(?:我不是在|并不是在)(?:说|问|请求|邀请|感谢|拒绝)/u.test(text)
    || /^(?:我说的不是|我的意思不是|我不是说|并不是说)(?:请|让|帮|请求|邀请)/u.test(text)
    || !/^我(?:们)?/u.test(text) && /^[^，,。！？?]{1,20}(?:说|问我|让我|表示|提到|请求我)[：:，,]/u.test(text);
}

/** Describing a question or concern is different from requesting its answer. */
export function isEmbeddedQuestionStatement(targetText: string): boolean {
  const text = normalizeIntentTarget(targetText);
  if (/[？?]|[吗么呢][。！!\s]*$/u.test(text)) return false;
  if (/^(?:我|我们)(?:想|想要|希望)(?:知道|了解|弄清楚|确认)/u.test(text)) return false;
  return /(?:^|[，,。；;])(?:我|我们)(?:主要|更|最|也|很|已经|正在|先)?(?:在意|在乎|看重|关注|关心|担心|考虑|知道|明白|了解|清楚|讨论|研究|记录)[^。！？?]{0,24}(?:能不能|会不会|是否|怎么|为什么|哪里|如何|几|多少)/u.test(text)
    || /(?:知道|明白|了解|清楚|解释|说明|告诉|发现|讨论|研究|记录)[^。！？?]{0,16}(?:怎么|为什么|哪里|哪儿|什么时候|如何|是否|几|多少)/u.test(text);
}

/** Return an evidenced generic intent, or abstain. This is deliberately a pure function. */
export function groundedIntent(targetText: string): GroundedIntent | null {
  if (typeof targetText !== "string" || !targetText.trim() || targetText.length > 240) return null;
  let text = normalizeIntentTarget(targetText);
  if (!/[\p{L}\p{N}\p{Extended_Pictographic}]/u.test(text)) return null;
  if (!text) return null;

  // Reported speech, examples, and hypothetical premises are not the sender's act.
  if (isUnattributableIntentTarget(targetText) || /^(?:如果|假如)\s*[:：，,]?/u.test(text)
    || /^(?:我不是在(?:说|问|邀请|感谢|拒绝)|并不是(?:在)?(?:说|问|邀请|感谢|拒绝))/u.test(text)) return null;

  // The clause after an explicit turn normally carries the actionable decision.
  const turn = /(?:但是|不过|可是|但)(?!是)/gu;
  const turns = [...text.matchAll(turn)];
  const lastTurn = turns.at(-1);
  if (lastTurn?.index !== undefined) {
    const tail = text.slice(lastTurn.index + lastTurn[0].length).trim();
    if (tail) text = tail;
  }

  // A courtesy opener does not turn a subsequent question or decision into a greeting.
  // Retain it as a fallback when the remainder is only an addressee or small talk.
  let courtesy: GroundedIntent | null = null;
  const opener = /^(你好|您好|嗨|哈喽|早上好|上午好|下午好|晚上好|谢谢(?:你|您)?|感谢(?:你|您)?|不好意思)[，,。!！\s]+(?=\S)/u.exec(text);
  if (opener) {
    if (/^(?:谢谢|感谢)/u.test(opener[1])) courtesy = result("thank", "thanks_phrase");
    else if (opener[1] !== "不好意思") courtesy = result("greet", "greeting_phrase");
    text = text.slice(opener[0].length).trim();
  }

  if (/^(?:难道|谁说|我什么时候说|我有说|哪有).*[？?吗呢]?[？?]?$/u.test(text)
    || /^不是.{1,80}吗[？?]?$/u.test(text)
    || /^(?:[^。！？?]{0,12})?这不[^。！？?]{1,50}(?:了|过)吗[？?]?$/u.test(text)) return null;
  if (/^(?:我想问(?:一下)?|想问一下|请问)[。！!]?$/u.test(text)) return null;
  if (/^(?:没什么|没啥)(?:事|意思|好说的)?[。！!\s]*$/u.test(text)) return null;

  // These complete short utterances acknowledge or offer to inspect. Anchoring the
  // whole message keeps questions and longer statements out of these categories.
  if (/^(?:好[了啦]|(?:我)?(?:知道|明白|了解)了|收到(?:了|啦)?)[。！!\s]*$/u.test(text)) {
    return result("confirm", "short_acknowledgement");
  }
  if (/^我(?:先)?(?:看(?:看|一下)|查(?:看|一下))[。！!\s]*$/u.test(text)) {
    return result("inspect", "first_person_inspection");
  }

  // A direct refusal outranks an invitation or an interrogative used in the same turn.
  if (/^(?:不行|不了|不去了|不参加了|不同意|不赞成|没门|我拒绝|我不同意|我不赞成|我(?:这次|今天|明天|周末)?不(?:去|参加)(?:了)?|这次不(?:去|参加)(?:了)?)(?:[。！!，,\s]|$)/u.test(text)
    || /^(?:这个|这样|这么做|这个方案|这个办法)(?:我觉得|我看)?不行[。！!\s]*$/u.test(text)) {
    return result("reject", "explicit_refusal");
  }
  if (/^(?:(?:好(?:的)?|行|收到|嗯+|嗯呢)[，,。！!\s]*)?(?:谢谢(?:你|您)?(?:啦|了|啊)?|感谢(?:你|您)?|谢了|多谢(?:你|您)?|感激不尽)[。！!\s]*$/u.test(text)) {
    return result("thank", "thanks_phrase");
  }
  if (/^(?:(?:谢谢|感谢|多谢)(?:大家|各位|老师|同学|朋友|你们|宝贝)(?:的[^。！？?]{1,25})?|辛苦(?:大家|各位|你们|老师)(?:了)?)[。！!\s]*$/u.test(text)) {
    return result("thank", "thanks_phrase");
  }
  if ((/^(?:嗯+|对(?:的|啊)?|是的|没错|确实|好(?:的|啊|呀|吧)?|行(?:的|啊|呀|吧)?|可以(?:的|啊|呀)?|没问题|同意|我同意|我赞成|当然可以|OK|ok)[。！!，,\s]*$/u.test(text)
    || /^(?:(?:这个|这样|这么做|这个方案|这个办法|这版)(?:我觉得|我看)?|我觉得(?:这个|这样|这么做|这个方案|这个办法|这版))(?:可以|行|没问题|不错)(?:了|的|吧|啊|呀|哦|噢)?[。！!\s]*$/u.test(text)
    || /^这个我同意[。！!\s]*$/u.test(text)) && !/[？?]$/u.test(text)) {
    return result("agree", "explicit_acceptance");
  }

  if (/^(?:不对|说错了|纠正一下|我更正一下|你说的.{1,30}不对|不是.{1,40}(?:而是|是)|不是这样(?:的)?|应该不是.{1,50}|应该是.{1,30}不是)/u.test(text)
    && !/[？?]$/u.test(text)) return result("correct", "explicit_correction");
  if ((/^(?:我|我们)(?:并)?不是[^，,。！？?]{1,60}[，,]\s*(?:而是|只是|是)[^。！？?]{1,100}/u.test(text)
      || /^(?:我|我们)(?:并)?(?:没有|没)说[^，,。！？?]{1,60}[，,]\s*(?:只是|而是|我说的是)[^。！？?]{1,100}/u.test(text))
      && !/[？?]$/u.test(text)) return result("correct", "explicit_correction");

  // Asking someone to act differs from asking for information.
  const request = /(?:^|[，,。；;])\s*(?:请(?!问)|麻烦(?:你|您)?|帮我|请你|请您|(?:你|您)?能不能(?:帮我|替我|给我|把)|(?:你|您)?能否(?:帮我|替我|给我|把)|(?:你|您)?可以(?:帮我|替我|给我|把)|你能(?:帮我|替我|给我|把)|给我(?=(?:一|两|三|几|\d)[^。！？?]{1,30}))[^。！？?]{1,70}/u.exec(text);
  if (request && !/(?:说|问我|让我|请求我|提到|转述)[：:，,。；;]\s*$/u.test(text.slice(0, request.index + 1))) {
    return result("seek_help", "action_request");
  }

  const reschedule = /(?:^|[，,。；;])\s*(?:只是|那就)?(?:(?:我|我们|咱们)(?:把(?:时间|日期|安排|会议|见面|聚会|活动))?|(?:把)?(?:时间|日期|安排|会议|见面|聚会|活动)|(?:那|要不|不如)(?:我们|咱们)?|)(?:改(?:到|成|为|在)|换(?:到|成|在))(?:了)?[^。！？?]{1,40}/u.exec(text);
  if (reschedule && CONCRETE_TIME.test(reschedule[0]) && !/[？?]$/u.test(text)
      && !/不同意|不接受|拒绝|只是举例|只是例子|没有发生|没发生/u.test(reschedule[0])) {
    return result("plan", "concrete_reschedule");
  }
  if (/(?:^|[，,。；;])(?:(?:我)?(?:想|要)?先(?:去)?休息(?:一下|一会儿)?|先不聊了|先聊到这(?:里)?|今天就聊到这(?:里)?|明天再聊|下次再聊|回头再聊|晚点再聊)(?:[，,。！!\s]|$)/u.test(text)
      && !/[？?]$/u.test(text)
      && (/(?:不聊|聊到|再聊)/u.test(text) || /(?:明天|下次|改天|回头|晚点)再(?:接着|继续)?(?:说|聊|讨论)/u.test(text))) {
    return result("close_chat", "conversation_pause");
  }
  if (/^(?:愿意|可以|同意|行|没问题)[，,]\s*(?:只要|前提是|条件是)[^。！？?]{2,80}[。！!]?$/u.test(text)) {
    return result("agree", "conditional_acceptance");
  }

  if (/^(?:(?:明天|后天|今天|周末|下周|有空时|(?:这|本|下)?周[一二三四五六日天]|(?:这|本|下)?星期[一二三四五六日天]|(?:这|本|下)?礼拜[一二三四五六日天])[，,\s]*)?(?:要不要|你要不要|咱们|我们|一起|约个时间|我邀请你)[^。！？?]{0,60}(?:一起|吃|喝|看|玩|逛|去|见|聚|聊|打球)[^。！？?]{0,30}(?:[吧吗？?！!。]|$)/u.test(text)
    && (/(?:一起|咱们|要不要|邀请你|约个时间)/u.test(text))) {
    return result("invite", "inclusive_invitation");
  }

  if (/^(?:真无语|烦死了|太离谱了|受不了|我(?:很|非常)?不满|这也太(?:离谱|糟糕|难|烦|过分|不合理|差|坑人|贵|慢)[^。！？?]{0,25}(?:了|吧)|怎么(?:又|还)[^。！？?]{1,50}|等了[^。！？?]{1,35}还没[^。！？?]{1,35})/u.test(text)) {
    return result("complain", "negative_evaluation");
  }

  // These forms normally request an answer. Rhetorical negatives were removed above.
  const embeddedQuestionWord = isEmbeddedQuestionStatement(text);
  if (/[？?]$/u.test(text)
    || /^是[^。！？?]{1,50}还是[^。！？?]{1,50}(?:啊|呀|呢|吗)[。]?$/.test(text)
    || /^(?:谁|什么|哪里|哪儿|哪天|何时|什么时候|为什么|怎么|如何|是否|能否)[^。！？?]{1,90}[。]?$/.test(text)
    || /^(?:你(?:们|家)?|您(?:们|家)?|他家|她家|这家|那家|这里|那里|一共|总共)[^。！？?]{0,12}几(?:个人|位|楼|件|份|次|辆|本|条|张)[。]?$/.test(text)
    || (!embeddedQuestionWord && /(?:怎么|为什么|哪里|哪儿|什么时候|如何|是否|几(?:点|号|岁|时|月|年)|多少)[^。！？?]{0,50}[吗呢]?[。]?$/.test(text))
    || /^[^。！？?]{1,80}(?:吗|么)[。]?$/.test(text)) {
    return result("ask_question", "answer_seeking_question");
  }

  if (/(?:^|[，,。；;])\s*(?:我建议|建议|最好|不妨|可以先|你可以先|你最好|试着|试试|先(?:列出|整理|确认|检查|比较|完成))[^。！？?]{2,90}/u.test(text)) {
    return result("suggest_action", "advice_marker");
  }
  if (/(?:^|[，,。；;])\s*(?:那就|那你|那我们|你|大家|咱们)(?:先)?(?:把|将|去|做|试|看|检查|处理|改|发|上传|提交|部署|测试|确认|弄|等|备份)[^。！？?]{0,60}[。！!]?$/.test(text)
    || /^可以试试[^。！？?]{1,60}[。！!]?$/.test(text)) {
    return result("suggest_action", "advice_marker");
  }
  if (/^(?:配置|参数|字号|字体|字|音量|亮度|间距|边距|尺寸|图片|按钮|窗口|图标|文字|标题|阈值|速度|声音|界面)(?:调|设|放|改)?(?:大|小|高|低|快|慢|长|短)(?:一点|一些|点)[吧。！!]*$/.test(text)) {
    return result("suggest_action", "imperative_adjustment");
  }
  if (/^让[^，,。！？?]{1,8}(?:先)?(?:看|试|查|检查|发|改|做|处理|确认|测试|跑|用|打开|关闭|点|等|准备|核对|联系)[^。！？?了]{0,24}一下[吧。！!]*$/.test(text)) {
    return result("suggest_action", "delegated_action");
  }
  if (/^(?:别|不要)(?:再|先)?(?:把|将|给|发|删|改|做|去|来|说|问|动|碰|忘|催|等|关|开|上传|提交|处理|解释|测试|试|跑|部署|告诉|写|看|听)[^。！？?]{0,60}[。！!]?$/.test(text)) {
    return result("suggest_action", "negative_imperative");
  }

  if (/^(?:我|我们)(?:明天|后天|下周|周末|今天)?(?:打算|计划|准备|决定)[^。！？?]{2,90}/u.test(text)
    && !/^(?:我|我们)准备好(?:了|啦)/u.test(text)) return result("plan", "first_person_intention");
  if (/^我先(?:把|去|做|试|看|检查|处理|改|发|上传|提交|部署|测试|确认|弄|跑|等)[^。！？?]{0,60}[。！!]?$/.test(text)
    && !/(?:了|过)[。！!]?$/.test(text)) return result("plan", "first_person_intention");
  if (/(?:^|[，,。；;])\s*(?:我|我们)(?:会(?:认真|先)?|负责|来)(?:改|修改|整理|检查|确认|保存|发送|发|准备|联系|查|订|带)[^。！？?]{0,60}[。！!]?$/.test(text)
      && !/(?:已经|昨天|刚才|改完|做完|发过|发了)/u.test(text)) return result("plan", "first_person_intention");

  if (/^(?:因为|原因是|之所以)[^。！？?]{2,120}/u.test(text)
    || /因为[^。！？?]{2,90}(?:所以|导致)[^。！？?]{1,90}/u.test(text)) {
    return result("explain", "causal_explanation");
  }
  if (/^我(?:这)?(?:算是|相当于)(?:一步一步|一步步|逐步)[^。！？?]{0,40}(?:引导|指导|带着|教|提示)[^。！？?]{1,60}(?:了|的)[。！!]*$/u.test(text)) {
    return result("explain", "process_explanation");
  }

  if (/^(?:(?:我|我们|这个|这项|文件|报告|任务|项目|版本|代码|数据|进度)[^。！？?]{0,10})?(?:已经|已|正在|还在|尚未|还没|刚才|刚刚)[^。！？?]{0,18}(?:完成|提交|上传|发送|处理|部署|修复|更新|开始|做完|跑完|通过|解决|搞定|跑通|测完|改完|修好|弄好|发出|发过去)[^。！？?]{0,40}[。！!]?$|^(?:完成了|提交了|上传了|处理好了|部署好了|修好了)[。！!]?$/u.test(text)) {
    return result("status_report", "progress_statement");
  }
  if (/^(?:修好|修复好|改好|弄好|搞定|跑通|测完|发出|发过去|上传完|提交完|部署完|处理完)(?:了|啦)?[。！!\s]*$/u.test(text)) {
    return result("status_report", "progress_statement");
  }

  if (/^(?:分享(?:一下|给你|给大家)|给你分享|告诉你(?:个|一件|一个)?(?:好消息|消息|事)|好消息[，,：:]|我(?:刚|今天|昨天)(?:发现|看到|听到|收到|拿到|通过|考过))[^。！？?]{1,100}/u.test(text)) {
    return result("share_news", "sharing_announcement");
  }

  if (/^(?:你好|您好|嗨|哈喽|早上好|上午好|下午好|晚上好|早安|晚安|hello|hi)(?:[，,。!！\s]|$)/iu.test(text)) {
    return result("greet", "greeting_phrase");
  }
  if (/^(?:谢谢|感谢|多谢|谢了|辛苦了)(?:[你您，,。!！\s]|$)/u.test(text)) {
    return result("thank", "thanks_phrase");
  }
  return courtesy;
}

/** Context-aware fine-message evidence. Portrait analysis remains target-only. */
export function groundedIntentWithContext(targetText: string, contextHint = ""): GroundedIntent | null {
  const target = normalizeIntentTarget(targetText);
  const context = String(contextHint || "").replace(QUOTED, " ").trim();
  if (!target || !context) return groundedIntent(targetText);
  const direct = groundedIntent(targetText);
  if (direct && !["greet", "thank", "confirm", "agree", "status_report"].includes(direct.label)) return direct;
  if (direct?.evidenceKind === "conditional_acceptance") return direct;
  const latestContext = context.split(/\n/u).at(-1) || "";
  const latestQuestion = groundedIntent(latestContext);
  if (/^(?:不是|并非)[，,]\s*我(?!没有|没说|不是)[^。！？?]{2,100}[。！!]?$/u.test(target) &&
      latestQuestion?.label === "ask_question" &&
      /^(?:(?:所以|那|那么)[，,\s]*)?(?:你|您|我们|咱们)/u.test(latestContext) &&
      /是不是|是否|会不会|吗[？?。！!\s]*$/u.test(latestContext) &&
      !/^(?:他说|她说|有人说|引用|比如|例如|假设|如果|假如)/u.test(latestContext)) {
    return result("deny", "contextual_denial");
  }
  if (INVITATION_CONTEXT.test(context) && DEFERRAL_TEXT.test(target) &&
      !/(?:今天|明天|后天|周[一二三四五六日天]|周末|下周).{0,12}(?:请|一起|约|见面)/u.test(target)) {
    return result("reject", "contextual_deferral");
  }
  if (/^(?:木有|没有)[。！!？?\s]*$/u.test(target) && DENIAL_CONTEXT.test(context)) {
    return result("deny", "contextual_denial");
  }
  return direct;
}
