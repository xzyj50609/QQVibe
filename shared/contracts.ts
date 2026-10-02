// Frozen shared contract for 好感度显示器.
//
// Every type here is consumed by BOTH the renderer (`src/**`) and the Electron main process
// (`electron/**`). Treat the type shapes as frozen: new *optional* fields are allowed for
// backward compatibility, renames are not. Pure validation helpers live here too so the main
// process and the preload bridge validate IPC payloads against exactly the same rules.

/* ------------------------------------------------------------------ *
 * Core data types (frozen)
 * ------------------------------------------------------------------ */

/** Normalized rectangle, all values in 0..1 relative to the source frame. */
export type Rect = { x: number; y: number; width: number; height: number };

/** A capturable window, as reported by `desktopCapturer`. */
export type SourceInfo = { id: string; name: string };

/** One chat message. `time` is a Unix epoch in milliseconds. */
export type Message = {
  id: string;
  side: "self" | "other";
  text: string;
  time: number;
  groupContext?: import('./group-context').GroupContext;
};

/** One label with the model's real probability in 0..1 (never synthesised). */
export type LabelScore = { label: string; probability: number };

/** Local evidence about behaviour displayed in this message, not a personality score. */
export type StyleEvidence = {
  socialEnergy: number;
  humor: number;
  composure: number;
  initiative: number;
  care: number;
  affection: number;
};

/** Per-message model output. */
export type MessageResult = {
  messageId: string;
  emotion: LabelScore[];
  intent: LabelScore[];
  intentBroad?: LabelScore[];
  /** Display-only expression leaf IDs and their unmodified routed model probabilities. */
  expression?: LabelScore[];
  /** Display-only expression and interpersonal-wish combinations. */
  playfulIntent?: LabelScore[];
  styleEvidence?: StyleEvidence | null;
  personalityEvidence?: {
    EI: { E: number; I: number; insufficient: number };
    SN: { S: number; N: number; insufficient: number };
    TF: { T: number; F: number; insufficient: number };
    JP: { J: number; P: number; insufficient: number };
  } | null;
};

/** Full analysis output for one session. */
export type AnalysisResult = {
  sessionId: string;
  affinity: number;
  delta: number;
  grade: string;
  nextStep: string;
  messages: MessageResult[];
  model: string;
  durationMs: number;
  analysisVersion?: string;
};

/** Model lifecycle, surfaced to the UI. */
export type ModelStatus = {
  state: "missing" | "loading" | "ready" | "error";
  progress?: number;
  provider?: "cpu" | "webgpu";
  message: string;
};

/** OCR output for one frame. Cropped images only; never persisted. */
export type OcrResult = {
  header: string;
  messages: Array<{ side: "self" | "other"; text: string }>;
  text: string;
};

/** One capture frame, already cropped by the renderer. */
export type FrameRequest = {
  sessionId: string;
  headerImage: string;
  chatImage: string;
};

/** One incremental analysis request. */
export type AnalysisRequest = {
  sessionId: string;
  messages: Message[];
  previousAffinity?: number;
};

/* ------------------------------------------------------------------ *
 * Native WeChat integration (stage 1: additive contract only)
 * ------------------------------------------------------------------ */

export type NativeWechatState = 'disconnected'|'checking'|'wechat_closed'|'not_logged_in'|'select_account'|'unlocking'|'ready'|'paused'|'permission_denied'|'unsupported'|'error';
export type NativeAccount = {id:string; name:string; state:'available'|'locked'|'connected'|'error'; message?:string};
export type NativeWechatStatus = {state:NativeWechatState; message:string; accounts:NativeAccount[]; accountId?:string};
export type NativeContactKind = 'friend'|'group'|'service'|'unknown';
export type NativeHistoryState = 'queued'|'running'|'paused'|'completed'|'partial'|'failed';
export type NativeTrendPoint = {time:number; affinity:number; analyzed:number};
export type NativeContact = {id:string; accountId:string; name:string; kind:NativeContactKind; messageCount?:number; lastTime?:number; summary?:{state:NativeHistoryState; processed:number; total:number; analyzed:number; affinity?:number; grade?:string; error?:string; updatedAt:number}};
export type NativeHistoryJob = {id:string; accountId:string; contactId:string; contactName:string; state:NativeHistoryState; processed:number; total:number; analyzed:number; skippedNonText:number; skippedSystem:number; skippedUnknownDirection:number; failed:number; cutoffTime:number; startedAt:number; updatedAt:number; affinity?:number; grade?:string; nextStep?:string; trend:NativeTrendPoint[]; error?:string};
export type NativeOverlayRect = {x:number;y:number;width:number;height:number};
export type NativeOverlayAnchor = {messageId:string; bubble:NativeOverlayRect; annotationSlot:NativeOverlayRect};
export type NativeViewport = {visible:boolean; foreground:boolean; observedAt:number; validUntil:number; revision:number; width:number; height:number; headerSlot?:NativeOverlayRect; bottomSlot?:NativeOverlayRect; paneSlot?:NativeOverlayRect; anchors:NativeOverlayAnchor[]};
export type NativeWechatSnapshot = {status:NativeWechatStatus; contact:NativeContact|null; messages:Message[]; result:AnalysisResult|null; analyzing:boolean; error?:string; revision:number; viewport:NativeViewport; jobs:NativeHistoryJob[]};
export type NativeConnectRequest = {accountId?:string};
export type NativeSelectRequest = {accountId:string; contactId:string};
export type NativeHistoryStartRequest = {accountId:string; contactIds?:string[]};

/**
 * The only surface exposed to the renderer via `contextBridge`. The renderer treats
 * `window.desktop` as optional so the same page can run in a plain browser for visual preview.
 */
export interface DesktopBridge {
  listSources(): Promise<SourceInfo[]>;
  selectSource(sourceId: string): Promise<void>;
  recognizeFrame(frame: FrameRequest): Promise<OcrResult>;
  analyze(request: AnalysisRequest): Promise<AnalysisResult>;
  modelStatus(): Promise<ModelStatus>;
  prepareModel(): Promise<ModelStatus>;
  setAlwaysOnTop(value: boolean): Promise<void>;
  minimize(): void;
  close(): void;

  // --- Native WeChat (stage 1: contract only; backend not implemented yet) ---
  nativeWechatStatus(): Promise<NativeWechatSnapshot>;
  /** Production WeChat client module status (module mode only). */
  nativeModuleStatus(): Promise<NativeModuleStatus>;
  /** Ask the module to stop and restore the gate; resolves with the post-stop status. */
  nativeModuleStop(): Promise<NativeModuleStatus>;
  nativeWechatConnect(request?: NativeConnectRequest): Promise<NativeWechatSnapshot>;
  nativeWechatListContacts(accountId?: string): Promise<NativeContact[]>;
  nativeWechatSelectContact(request: NativeSelectRequest): Promise<NativeWechatSnapshot>;
  nativeWechatPause(paused: boolean): Promise<NativeWechatSnapshot>;
  nativeWechatDisconnect(): Promise<NativeWechatSnapshot>;
  onNativeWechatSnapshot(callback: (snapshot: NativeWechatSnapshot) => void): () => void;
  nativeHistoryStart(request: NativeHistoryStartRequest): Promise<NativeHistoryJob[]>;
  nativeHistoryPause(jobId: string): Promise<NativeHistoryJob[]>;
  nativeHistoryResume(jobId: string): Promise<NativeHistoryJob[]>;
  nativeHistoryStatus(): Promise<NativeHistoryJob[]>;
}

/* ------------------------------------------------------------------ *
 * IPC channel names (internal, shared by main + preload)
 * ------------------------------------------------------------------ */

/** Honest capability + running status of the production WeChat client module. */
export interface NativeModuleStatus {
  running: boolean;
  state: string;
  capability: {
    nativeAttachment: boolean;
    observedTextAnalysis: boolean;
    senderDirection: boolean;
    stableMessageIds: boolean;
    qtRowHeight: boolean;
    fullHistory: boolean;
  };
  /** True when the app is running the WeChat client module entry (--wechat-module). */
  mode: boolean;
  sampled: number;
  analyzed: number;
  failed: number;
  pending: number;
  restore?: unknown;
}

export const IPC_CHANNELS = {
  listSources: "desktop:listSources",
  selectSource: "desktop:selectSource",
  recognizeFrame: "desktop:recognizeFrame",
  analyze: "desktop:analyze",
  modelStatus: "desktop:modelStatus",
  prepareModel: "desktop:prepareModel",
  setAlwaysOnTop: "desktop:setAlwaysOnTop",
  minimize: "desktop:minimize",
  close: "desktop:close",
  nativeWechatStatus: "desktop:nativeWechatStatus",
  nativeWechatConnect: "desktop:nativeWechatConnect",
  nativeWechatListContacts: "desktop:nativeWechatListContacts",
  nativeWechatSelectContact: "desktop:nativeWechatSelectContact",
  nativeWechatPause: "desktop:nativeWechatPause",
  nativeWechatDisconnect: "desktop:nativeWechatDisconnect",
  nativeWechatSnapshot: "desktop:nativeWechatSnapshot",
  nativeHistoryStart: "desktop:nativeHistoryStart",
  nativeHistoryPause: "desktop:nativeHistoryPause",
  nativeHistoryResume: "desktop:nativeHistoryResume",
  nativeHistoryStatus: "desktop:nativeHistoryStatus",
  nativeModuleStatus: "desktop:nativeModuleStatus",
  nativeModuleStop: "desktop:nativeModuleStop",
} as const;

/* ------------------------------------------------------------------ *
 * Limits
 * ------------------------------------------------------------------ */

/** Session ids are opaque, single-token, and bounded. */
export const SESSION_ID_PATTERN = /^[A-Za-z0-9._-]{1,64}$/;
/** Hard cap on one cropped image (bytes, decoded). */
export const MAX_IMAGE_BYTES = 8 * 1024 * 1024;
/** Base64 inflates by ~4/3; keep a small margin for the `data:` prefix. */
export const MAX_IMAGE_DATAURL_CHARS = Math.ceil((MAX_IMAGE_BYTES * 4) / 3) + 64;
/** Longest accepted single message body (characters). */
export const MAX_MESSAGE_TEXT = 4000;
/** Most messages accepted in one analysis request. */
export const MAX_MESSAGES_PER_REQUEST = 200;
/** Most messages the UI keeps in the rolling context. */
export const MAX_CONTEXT_MESSAGES = 120;

const IMAGE_DATAURL_PATTERN = /^data:image\/(?:png|jpe?g|webp);base64,[A-Za-z0-9+/=]+$/i;

/* ------------------------------------------------------------------ *
 * Pure validation helpers
 * ------------------------------------------------------------------ */

export function isSessionId(value: unknown): value is string {
  return typeof value === "string" && SESSION_ID_PATTERN.test(value);
}

export function isImageDataUrl(value: unknown): value is string {
  return (
    typeof value === "string" &&
    value.length > 0 &&
    value.length <= MAX_IMAGE_DATAURL_CHARS &&
    IMAGE_DATAURL_PATTERN.test(value)
  );
}

export function isRect(value: unknown): value is Rect {
  if (!value || typeof value !== "object") return false;
  const rect = value as Record<string, unknown>;
  return (["x", "y", "width", "height"] as const).every((key) => {
    const n = rect[key];
    return typeof n === "number" && Number.isFinite(n) && n >= 0 && n <= 1;
  });
}

export class ContractError extends Error {
  constructor(message: string) {
    super(message);
    this.name = "ContractError";
  }
}

/** Validate + narrow a source id from the renderer. */
export function assertSourceId(value: unknown): string {
  if (typeof value !== "string" || value.length === 0 || value.length > 512) {
    throw new ContractError("无效的窗口 ID");
  }
  return value;
}

export function assertBoolean(value: unknown, field = "值"): boolean {
  if (typeof value !== "boolean") throw new ContractError(`${field} 必须是布尔值`);
  return value;
}

function assertImageDataUrl(value: unknown, field: string): string {
  if (!isImageDataUrl(value)) {
    throw new ContractError(`${field} 必须是有效的图片 data URL（png/jpeg/webp，≤ ${MAX_IMAGE_BYTES} 字节）`);
  }
  return value;
}

/** Validate a cropped frame request coming over IPC. */
export function assertFrameRequest(value: unknown): FrameRequest {
  if (!value || typeof value !== "object") throw new ContractError("截帧请求必须是对象");
  const frame = value as Record<string, unknown>;
  if (!isSessionId(frame.sessionId)) throw new ContractError("截帧请求缺少合法的会话 ID");
  const headerImage = assertImageDataUrl(frame.headerImage, "标题截图");
  const chatImage = assertImageDataUrl(frame.chatImage, "聊天截图");
  return { sessionId: frame.sessionId, headerImage, chatImage };
}

function assertMessage(value: unknown, index: number): Message {
  if (!value || typeof value !== "object") {
    throw new ContractError(`第 ${index + 1} 条消息不是对象`);
  }
  const message = value as Record<string, unknown>;
  if (typeof message.id !== "string" || message.id.length === 0 || message.id.length > 128) {
    throw new ContractError(`第 ${index + 1} 条消息缺少合法 ID`);
  }
  if (message.side !== "self" && message.side !== "other") {
    throw new ContractError(`第 ${index + 1} 条消息的说话人无效`);
  }
  if (typeof message.text !== "string" || message.text.length > MAX_MESSAGE_TEXT) {
    throw new ContractError(`第 ${index + 1} 条消息文本无效或过长`);
  }
  if (message.text.trim().length === 0) {
    throw new ContractError(`第 ${index + 1} 条消息文本为空`);
  }
  const time = typeof message.time === "number" && Number.isFinite(message.time) ? message.time : 0;
  return { id: message.id, side: message.side, text: message.text, time };
}

/** Validate an analysis request coming over IPC. */
export function assertAnalysisRequest(value: unknown): AnalysisRequest {
  if (!value || typeof value !== "object") throw new ContractError("分析请求必须是对象");
  const request = value as Record<string, unknown>;
  if (!isSessionId(request.sessionId)) throw new ContractError("分析请求缺少合法的会话 ID");
  if (!Array.isArray(request.messages)) throw new ContractError("分析请求的消息必须是数组");
  if (request.messages.length === 0) throw new ContractError("分析请求的消息不能为空");
  if (request.messages.length > MAX_MESSAGES_PER_REQUEST) {
    throw new ContractError(`消息数量超过上限（${MAX_MESSAGES_PER_REQUEST} 条）`);
  }
  const messages = request.messages.map((message, index) => assertMessage(message, index));
  const result: AnalysisRequest = { sessionId: request.sessionId, messages };
  if (request.previousAffinity !== undefined) {
    const previous = request.previousAffinity;
    if (typeof previous !== "number" || !Number.isFinite(previous) || previous < 0 || previous > 100) {
      throw new ContractError("previousAffinity 必须在 0..100 之间");
    }
    result.previousAffinity = previous;
  }
  return result;
}

/* ------------------------------------------------------------------ *
 * Native WeChat validation (stage 1)
 *
 * Renderer-supplied ids are opaque ASCII tokens; never trust renderer data. These are additive and
 * leave every original validator untouched.
 * ------------------------------------------------------------------ */

/** Opaque native id token: 1..256 chars of `A-Za-z0-9._:-`. */
export const NATIVE_ID_PATTERN = /^[A-Za-z0-9._:-]+$/;
export const NATIVE_ID_MIN_LENGTH = 1;
export const NATIVE_ID_MAX_LENGTH = 256;
/** Upper bound for a `contactIds` selection. */
export const MAX_NATIVE_CONTACT_IDS = 100000;

export function isNativeId(value: unknown): value is string {
  return (
    typeof value === "string" &&
    value.length >= NATIVE_ID_MIN_LENGTH &&
    value.length <= NATIVE_ID_MAX_LENGTH &&
    NATIVE_ID_PATTERN.test(value)
  );
}

export function assertNativeId(value: unknown, field: string): string {
  if (!isNativeId(value)) {
    throw new ContractError(
      `${field} 必须是 1..256 个字符的 ASCII 不透明 token（^[A-Za-z0-9._:-]+$）`,
    );
  }
  return value;
}

/** `nativeWechatConnect` accepts an optional request (undefined/null allowed). */
export function assertNativeConnectRequest(value: unknown): NativeConnectRequest {
  if (value === undefined || value === null) return {};
  if (typeof value !== "object" || Array.isArray(value)) {
    throw new ContractError("连接请求必须是对象或省略");
  }
  const request = value as Record<string, unknown>;
  if (request.accountId === undefined) return {};
  return { accountId: assertNativeId(request.accountId, "accountId") };
}

/** `nativeWechatSelectContact` requires both accountId and contactId. */
export function assertNativeSelectRequest(value: unknown): NativeSelectRequest {
  if (!value || typeof value !== "object" || Array.isArray(value)) {
    throw new ContractError("选择联系人请求必须是对象");
  }
  const request = value as Record<string, unknown>;
  return {
    accountId: assertNativeId(request.accountId, "accountId"),
    contactId: assertNativeId(request.contactId, "contactId"),
  };
}

/** `nativeHistoryStart` requires accountId; optional contactIds must be nonempty + unique. */
export function assertNativeHistoryStartRequest(value: unknown): NativeHistoryStartRequest {
  if (!value || typeof value !== "object" || Array.isArray(value)) {
    throw new ContractError("历史任务请求必须是对象");
  }
  const request = value as Record<string, unknown>;
  const result: NativeHistoryStartRequest = {
    accountId: assertNativeId(request.accountId, "accountId"),
  };
  if (request.contactIds !== undefined) {
    if (!Array.isArray(request.contactIds)) throw new ContractError("contactIds 必须是数组");
    if (request.contactIds.length === 0) throw new ContractError("contactIds 指定时不能为空");
    if (request.contactIds.length > MAX_NATIVE_CONTACT_IDS) {
      throw new ContractError(`contactIds 超过上限（${MAX_NATIVE_CONTACT_IDS} 条）`);
    }
    const seen = new Set<string>();
    const contactIds: string[] = [];
    request.contactIds.forEach((raw, index) => {
      const id = assertNativeId(raw, `contactIds[${index}]`);
      if (seen.has(id)) throw new ContractError(`contactIds 存在重复项：${id}`);
      seen.add(id);
      contactIds.push(id);
    });
    result.contactIds = contactIds;
  }
  return result;
}

/** `nativeHistoryPause` / `nativeHistoryResume` job id (same 1..256 token bound). */
export function assertNativeJobId(value: unknown): string {
  return assertNativeId(value, "jobId");
}

/** `nativeWechatPause` boolean. */
export function assertNativePause(value: unknown): boolean {
  return assertBoolean(value, "paused");
}
