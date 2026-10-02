// Network model adapters. The local Laya engine remains the application's default.
// Provider SDKs own their wire formats; this file owns validation and the network boundary.

export type Protocol = "anthropic" | "responses" | "chat_completions" | "gemini" | "ollama";

export interface ModelConfig {
  protocol: Protocol;
  /** Complete API prefix: e.g. /v1, /v1beta, or the Ollama server root. */
  baseUrl: string;
  model: string;
  apiKey?: string;
  contextTokens?: number;
}

export interface ListedModel {
  id: string;
  name?: string;
  contextTokens?: number;
}

export interface ModelListResult {
  models: ListedModel[];
  /** False means the gateway cannot enumerate models; a manually entered ID can still work. */
  supported: boolean;
}

export interface ModelUsage {
  inputTokens?: number;
  outputTokens?: number;
}

export interface GenerationRequest {
  system: string;
  prompt: string;
  /** Optional provider output cap. Omit to let the selected model use its default. */
  maxOutputTokens?: number;
  /** Reuse a provider Responses session when the gateway supports it. */
  previousResponseId?: string;
  /** Persist the provider response so a later batch can reference it. */
  store?: boolean;
  /** Consume provider SSE chunks when available and expose firstBodyMs. */
  stream?: boolean;
  onTextDelta?: (delta: string) => void;
  signal?: AbortSignal;
  /** A bounded per-request timeout; larger portrait batches may need longer than a probe. */
  timeoutMs?: number;
  /** Ask compatible providers for a JSON object; callers still validate every field. */
  jsonMode?: boolean;
}

export interface GenerationResult {
  /** Provider text, without parsing or assigning application scores. */
  text: string;
  usage?: ModelUsage;
  responseId?: string;
  timings?: { firstBodyMs?: number; connectorMs?: number };
}

export type ConnectorErrorCode =
  | "invalid-url" | "invalid-request" | "cancelled" | "timeout"
  | "response-too-large" | "context-too-long" | "auth" | "rate-limit" | "unsupported"
  | "provider-error" | "invalid-output" | "network" | "empty-response";

export class ModelConnectorError extends Error {
  constructor(
    readonly code: ConnectorErrorCode,
    message: string,
    readonly status?: number,
  ) {
    super(message);
    this.name = "ModelConnectorError";
  }
}

// OpenCode leaves generation timeout to the provider/stream lifecycle unless a
// caller explicitly supplies one. Probes and model discovery remain bounded.
const REQUEST_TIMEOUT_MS: number | undefined = undefined;
const LIST_TIMEOUT_MS = 12_000;
const MAX_RESPONSE_BYTES = 2 * 1024 * 1024;
const MAX_PROMPT_BYTES = 3 * 1024 * 1024;
const MAX_MODELS = 200;
const NO_KEY = "local-no-key";
const PROTOCOLS: readonly Protocol[] = [
  "anthropic", "responses", "chat_completions", "gemini", "ollama",
];

interface SafeConfig {
  protocol: Protocol;
  baseUrl: string;
  model: string;
  apiKey: string;
}

function invalid(message: string, code: ConnectorErrorCode = "invalid-request"): never {
  throw new ModelConnectorError(code, message);
}

function isLoopback(host: string): boolean {
  return host === "localhost" || host === "127.0.0.1" || host === "[::1]";
}

/** Validates a user-supplied endpoint before any SDK can send a request. */
export function validateModelConfig(config: ModelConfig, requireModel = true): SafeConfig {
  if (!config || !PROTOCOLS.includes(config.protocol)) invalid("不支持的模型接入协议");
  if (typeof config.baseUrl !== "string" || !config.baseUrl.trim() ||
      config.baseUrl.length > 2048 || /[\s\\]/u.test(config.baseUrl)) {
    invalid("Base URL 格式不正确", "invalid-url");
  }
  let url: URL;
  try {
    url = new URL(config.baseUrl);
  } catch {
    invalid("Base URL 格式不正确", "invalid-url");
  }
  if (!url! || !["http:", "https:"].includes(url.protocol) || !url.hostname ||
      url.username || url.password || url.search || url.hash) {
    invalid("Base URL 只能包含 HTTP(S) 地址和路径", "invalid-url");
  }
  if (url.protocol === "http:" && !isLoopback(url.hostname)) {
    invalid("远程服务请使用 HTTPS", "invalid-url");
  }
  if (typeof config.model !== "string" || (requireModel && !config.model.trim()) ||
      config.model.length > 256 || /[\u0000-\u001f\u007f]/u.test(config.model)) {
    invalid("模型 ID 格式不正确");
  }
  if (config.apiKey !== undefined && (typeof config.apiKey !== "string" ||
      config.apiKey.length > 8192 || /[\r\n]/u.test(config.apiKey))) {
    invalid("API Key 格式不正确");
  }
  if (config.contextTokens !== undefined &&
      (!Number.isSafeInteger(config.contextTokens) || config.contextTokens < 4096 ||
       config.contextTokens > 1000000)) invalid("上下文大小格式不正确");
  return {
    protocol: config.protocol,
    baseUrl: url.toString().replace(/\/+$/u, ""),
    model: config.model.trim(),
    apiKey: config.apiKey?.trim() ?? "",
  };
}

function checkedSignal(requestSignal?: AbortSignal, callerSignal?: AbortSignal,
                       timeoutMs?: number): AbortSignal {
  const signals: AbortSignal[] = [];
  if (typeof timeoutMs === "number" && timeoutMs > 0) signals.push(AbortSignal.timeout(timeoutMs));
  if (requestSignal) signals.push(requestSignal);
  if (callerSignal) signals.push(callerSignal);
  if (!signals.length) return new AbortController().signal;
  return AbortSignal.any(signals);
}

async function boundedResponse(response: Response): Promise<Response> {
  const declared = Number(response.headers.get("content-length"));
  if (Number.isFinite(declared) && declared > MAX_RESPONSE_BYTES) {
    await response.body?.cancel();
    throw new ModelConnectorError("response-too-large", "服务响应超过大小限制");
  }
  const chunks: Uint8Array[] = [];
  let size = 0;
  if (response.body) {
    const reader = response.body.getReader();
    for (;;) {
      const { done, value } = await reader.read();
      if (done) break;
      size += value.byteLength;
      if (size > MAX_RESPONSE_BYTES) {
        await reader.cancel();
        throw new ModelConnectorError("response-too-large", "服务响应超过大小限制");
      }
      chunks.push(value);
    }
  }
  const headers = new Headers(response.headers);
  headers.delete("content-length");
  headers.delete("content-encoding");
  const body = chunks.length && ![204, 205, 304].includes(response.status)
    ? Buffer.concat(chunks) : null;
  return new Response(body, {
    status: response.status,
    statusText: response.statusText,
    headers,
  });
}

function guardedFetch(config: SafeConfig, callerSignal?: AbortSignal,
                      timeoutMs = REQUEST_TIMEOUT_MS, bounded = true,
                      streamProtocol?: "responses" | "chat_completions"): typeof fetch {
  const base = new URL(config.baseUrl);
  const basePath = base.pathname.replace(/\/+$/u, "");
  return async (input, init) => {
    const target = new URL(input instanceof Request ? input.url : String(input));
    if (target.origin !== base.origin ||
        (basePath && target.pathname !== basePath && !target.pathname.startsWith(`${basePath}/`))) {
      throw new ModelConnectorError("invalid-url", "SDK 请求超出了配置的服务地址");
    }
    const signal = checkedSignal(init?.signal ?? (input instanceof Request ? input.signal : undefined),
      callerSignal, timeoutMs);
    const response = await globalThis.fetch(input, { ...init, redirect: "error", signal });
    if (!bounded && response.ok && streamProtocol &&
        !/text\/event-stream|application\/x-ndjson/iu.test(response.headers.get("content-type") || "")) {
      // Some compatible gateways ignore stream:true but still return a complete
      // JSON completion. Turn that one body into a local SSE envelope so the
      // caller observes one request and the normal incremental parser can run.
      const raw = await (await boundedResponse(response)).text();
      const headers = new Headers(response.headers);
      headers.set("content-type", "text/event-stream");
      headers.delete("content-length");
      let body = raw;
      try {
        const parsed = JSON.parse(raw) as Record<string, any>;
        if (streamProtocol === "chat_completions") {
          const content = parsed.choices?.[0]?.message?.content;
          if (typeof content === "string") {
            body = `data: ${JSON.stringify({
              id: parsed.id, object: "chat.completion.chunk",
              choices: [{ index: 0, delta: { content }, finish_reason: "stop" }],
              usage: parsed.usage,
            })}\n\ndata: [DONE]\n\n`;
          }
        } else {
          const content = typeof parsed.output_text === "string" ? parsed.output_text :
            (Array.isArray(parsed.output) ? parsed.output.flatMap((item: any) =>
              Array.isArray(item?.content) ? item.content : []).filter((item: any) =>
              item?.type === "output_text" && typeof item.text === "string")
              .map((item: any) => item.text).join("") : "");
          if (content) {
            body = `event: response.output_text.delta\ndata: ${JSON.stringify({
              type: "response.output_text.delta", delta: content,
            })}\n\n` +
              `event: response.completed\ndata: ${JSON.stringify({
                type: "response.completed", response: {
                  id: parsed.id, output_text: content, usage: parsed.usage,
                },
              })}\n\n`;
          }
        }
      } catch {
        // Preserve the body. The connector will make its single compatibility
        // fallback only when the provider response is not parseable at all.
      }
      return new Response(body, { status: response.status, statusText: response.statusText, headers });
    }
    if (bounded) return boundedResponse(response);
    // Keep streamed requests under the same byte limit without buffering them.
    if (!response.body) return response;
    const declared = Number(response.headers.get("content-length"));
    if (Number.isFinite(declared) && declared > MAX_RESPONSE_BYTES) {
      await response.body.cancel();
      throw new ModelConnectorError("response-too-large", "服务响应超过大小限制");
    }
    let received = 0;
    return new Response(response.body.pipeThrough(new TransformStream<Uint8Array, Uint8Array>({
      transform(chunk, controller) {
        received += chunk.byteLength;
        if (received > MAX_RESPONSE_BYTES) {
          controller.error(new ModelConnectorError("response-too-large", "服务响应超过大小限制"));
        } else controller.enqueue(chunk);
      },
    })), { status: response.status, statusText: response.statusText, headers: response.headers });
  };
}

// Anthropic's SDK owns the /v1 path; most compatible-provider UIs expose the full /v1 URL.
function anthropicSdkBase(config: SafeConfig): string {
  return config.baseUrl.replace(/\/v1$/u, "");
}

// Ollama's SDK owns the /api path.
function ollamaSdkBase(config: SafeConfig): string {
  return config.baseUrl.replace(/\/api$/u, "");
}

function errorStatus(error: unknown): number | undefined {
  if (!error || typeof error !== "object") return undefined;
  const value = error as Record<string, unknown>;
  const status = value.status ?? value.statusCode ?? value.status_code;
  return typeof status === "number" && Number.isInteger(status) ? status : undefined;
}

function streamFormatError(error: unknown): boolean {
  let current: unknown = error;
  for (let depth = 0; depth < 5 && current && typeof current === "object"; depth++) {
    if ((current as { streamFormat?: boolean }).streamFormat) return true;
    if (current instanceof Error && /stream response is not an event stream/iu.test(current.message)) return true;
    current = (current as { cause?: unknown }).cause;
  }
  return false;
}

function streamFormatFailure(): Error {
  const error = new Error("stream response is not an event stream");
  (error as Error & { streamFormat?: boolean }).streamFormat = true;
  return error;
}

function safeError(error: unknown, signal?: AbortSignal): never {
  if (signal?.aborted) throw new ModelConnectorError("cancelled", "请求已取消");
  let current: unknown = error;
  let status: number | undefined;
  let timedOut = false;
  let contextRejected = false;
  // SDKs may wrap fetch failures. Only inspect their shape, never render raw messages.
  for (let depth = 0; depth < 5 && current && typeof current === "object"; depth++) {
    if (current instanceof ModelConnectorError) throw current;
    status ??= errorStatus(current);
    if (current instanceof Error && /Timeout|Abort/u.test(current.name)) timedOut = true;
    if (current instanceof Error && /Did not receive done or success response in stream/u.test(current.message))
      throw new ModelConnectorError("provider-error", "模型流式响应中断");
    const record = current as Record<string, unknown>;
    const nested = record.error && typeof record.error === "object" ?
      record.error as Record<string, unknown> : null;
    const code = record.code ?? nested?.code;
    if (typeof code === "string" &&
        /^(?:context[_-]?length[_-]?exceeded|prompt[_-]?too[_-]?long|input[_-]?too[_-]?long|max[_-]?context[_-]?length[_-]?exceeded)$/iu.test(code))
      contextRejected = true;
    current = (current as { cause?: unknown }).cause;
  }
  if (status === 413 || contextRejected && [400, 413, 422].includes(status ?? -1))
    throw new ModelConnectorError("context-too-long", "模型拒绝了当前上下文长度", status);
  if (status === 401) throw new ModelConnectorError("auth", "API Key 无效或已过期", status);
  if (status === 403) throw new ModelConnectorError("auth", "服务拒绝访问", status);
  if (status === 429) throw new ModelConnectorError("rate-limit", "服务请求过于频繁", status);
  if ([404, 405, 501].includes(status ?? -1)) {
    throw new ModelConnectorError("unsupported", "服务不支持此接口", status);
  }
  if (status) throw new ModelConnectorError("provider-error", `服务返回 HTTP ${status}`, status);
  if (timedOut) throw new ModelConnectorError("timeout", "连接服务超时");
  throw new ModelConnectorError("network", "无法连接模型服务");
}

function normalizedModels(models: Array<{ id: string; name?: string; contextTokens?: number }>): ListedModel[] {
  const seen = new Set<string>();
  const result: ListedModel[] = [];
  for (const item of models) {
    const id = typeof item?.id === "string" ? item.id.trim() : "";
    if (!id || id.length > 256 || seen.has(id)) continue;
    seen.add(id);
    const name = typeof item.name === "string" ? item.name.trim().slice(0, 256) : "";
    const contextTokens = Number.isSafeInteger(item.contextTokens) &&
      item.contextTokens! >= 4096 && item.contextTokens! <= 1000000 ? item.contextTokens : undefined;
    result.push({ id, ...(name && name !== id ? { name } : {}),
      ...(contextTokens ? { contextTokens } : {}) });
    if (result.length >= MAX_MODELS) break;
  }
  return result;
}

function modelContextTokens(item: unknown): number | undefined {
  if (!item || typeof item !== "object") return undefined;
  const model = item as Record<string, unknown>;
  for (const value of [model.context_window, model.context_length, model.max_context_tokens,
    model.max_context_length, model.input_token_limit, model.inputTokenLimit]) {
    if (Number.isSafeInteger(value) && (value as number) >= 4096 &&
        (value as number) <= 1000000) return value as number;
  }
  return undefined;
}

/** Lists the first bounded page. Gateways without a list endpoint can use a manual model ID. */
export async function listModels(config: ModelConfig): Promise<ModelListResult> {
  const safe = validateModelConfig(config, false);
  try {
    let models: ListedModel[];
    switch (safe.protocol) {
      case "responses":
      case "chat_completions": {
        const { default: OpenAI } = await import("openai");
        const client = new OpenAI({ apiKey: safe.apiKey || NO_KEY, baseURL: safe.baseUrl,
          organization: null, project: null, adminAPIKey: null,
          fetch: guardedFetch(safe, undefined, LIST_TIMEOUT_MS), maxRetries: 0,
          timeout: LIST_TIMEOUT_MS, logLevel: "off" });
        const page = await client.models.list();
        models = normalizedModels(page.data.map((item) => ({ id: item.id,
          contextTokens: modelContextTokens(item) })));
        break;
      }
      case "anthropic": {
        const { default: Anthropic } = await import("@anthropic-ai/sdk");
        const client = new Anthropic({ apiKey: safe.apiKey || NO_KEY, authToken: null,
          credentials: null, config: null, profile: null, baseURL: anthropicSdkBase(safe),
          fetch: guardedFetch(safe, undefined, LIST_TIMEOUT_MS), maxRetries: 0,
          timeout: LIST_TIMEOUT_MS, logLevel: "off" });
        const page = await client.models.list({ limit: 100 });
        models = normalizedModels(page.data.map((item) => ({ id: item.id, name: item.display_name,
          contextTokens: modelContextTokens(item) })));
        break;
      }
      case "gemini": {
        const { GoogleGenAI } = await import("@google/genai");
        const client = new GoogleGenAI({ apiKey: safe.apiKey || NO_KEY, vertexai: false,
          httpOptions: { baseUrl: safe.baseUrl, apiVersion: "", timeout: LIST_TIMEOUT_MS,
            fetch: guardedFetch(safe, undefined, LIST_TIMEOUT_MS) } });
        const page = await client.models.list({ config: { pageSize: 100 } });
        models = normalizedModels(page.page.map((item) => ({
          id: item.name?.replace(/^models\//u, "") ?? "", name: item.displayName,
          contextTokens: modelContextTokens(item),
        })));
        break;
      }
      case "ollama": {
        const { Ollama } = await import("ollama");
        const client = new Ollama({ host: ollamaSdkBase(safe),
          fetch: guardedFetch(safe, undefined, LIST_TIMEOUT_MS),
          headers: safe.apiKey ? { Authorization: `Bearer ${safe.apiKey}` } : undefined });
        const page = await client.list();
        models = normalizedModels(page.models.map((item) => ({ id: item.model || item.name,
          contextTokens: modelContextTokens(item) })));
        break;
      }
    }
    return { models, supported: true };
  } catch (error) {
    // A proxy can support generation while denying or omitting its models endpoint.
    if ([404, 405, 501].includes(errorStatus(error) ?? -1)) {
      return { models: [], supported: false };
    }
    return safeError(error);
  }
}

function usage(input?: number, output?: number): ModelUsage | undefined {
  const inputTokens = Number.isFinite(input) && input! >= 0 ? input : undefined;
  const outputTokens = Number.isFinite(output) && output! >= 0 ? output : undefined;
  return inputTokens === undefined && outputTokens === undefined
    ? undefined : { inputTokens, outputTokens };
}

/** Sends a bounded prompt and returns raw model text for the caller to validate. */
export async function generateStructured(
  config: ModelConfig, request: GenerationRequest,
): Promise<GenerationResult> {
  const safe = validateModelConfig(config);
  const outputLimit = request?.maxOutputTokens;
  if (!request || typeof request.system !== "string" || typeof request.prompt !== "string" ||
      !request.prompt.trim() ||
      (outputLimit !== undefined && (!Number.isInteger(outputLimit) ||
        outputLimit < 1 || outputLimit > 8192)) ||
      (request.jsonMode !== undefined && typeof request.jsonMode !== "boolean") ||
      (request.stream !== undefined && typeof request.stream !== "boolean") ||
      (request.timeoutMs !== undefined && (!Number.isInteger(request.timeoutMs) ||
        request.timeoutMs < 1000 || request.timeoutMs > 120_000)) ||
      Buffer.byteLength(request.system, "utf8") + Buffer.byteLength(request.prompt, "utf8") > MAX_PROMPT_BYTES) {
    invalid("生成请求超出允许范围");
  }
  if (request.signal?.aborted) throw new ModelConnectorError("cancelled", "请求已取消");
  const timeoutMs = request.timeoutMs ?? REQUEST_TIMEOUT_MS;
  const deadline = timeoutMs ? AbortSignal.timeout(timeoutMs) : undefined;
  const signal = checkedSignal(request.signal, deadline);
  const connectorStarted = performance.now();
  let firstBodyMs: number | undefined;
  let emittedText = false;
  const markFirstBody = () => { firstBodyMs ??= performance.now() - connectorStarted; };
  const emit = (delta: string) => {
    if (!delta) return;
    signal.throwIfAborted();
    markFirstBody();
    emittedText = true;
    request.onTextDelta?.(delta);
  };
  try {
    let result: GenerationResult;
    switch (safe.protocol) {
      case "responses": {
        const { default: OpenAI } = await import("openai");
        const client = new OpenAI({ apiKey: safe.apiKey || NO_KEY, baseURL: safe.baseUrl,
          organization: null, project: null, adminAPIKey: null,
          fetch: guardedFetch(safe, signal, undefined, !request.stream,
            safe.protocol === "responses" ? "responses" : undefined), maxRetries: 0,
          timeout: timeoutMs, logLevel: "off" });
        const params = { model: safe.model, input: request.prompt,
          instructions: request.system, store: request.store ?? false,
          ...(request.previousResponseId ?
            { previous_response_id: request.previousResponseId } : {}),
          ...(request.maxOutputTokens !== undefined ?
            { max_output_tokens: request.maxOutputTokens } : {}) };
        // DeepSeek's official Responses API enables thinking by default, and its
        // output cap includes reasoning tokens. Structured analysis needs room
        // for the final JSON rather than spending the cap before any text appears.
        const officialDeepSeekJson = request.jsonMode &&
          new URL(safe.baseUrl).hostname.toLowerCase() === "api.deepseek.com";
        let response;
        try {
          const requestBody = { ...params,
            ...(request.jsonMode ? { text: { format: { type: "json_object" as const } } } : {}),
            ...(officialDeepSeekJson ? { reasoning: { effort: "none" as const } } : {}) };
          if (!request.stream) {
            response = await client.responses.create(requestBody, { signal });
          } else {
            const stream = await client.responses.create({ ...requestBody, stream: true },
              { signal });
            let text = "";
            let responseId: string | undefined;
            let responseUsage: any;
            let completed = false;
            for await (const event of stream as AsyncIterable<any>) {
              if (event?.type === "response.output_text.delta" && typeof event.delta === "string") {
                text += event.delta;
                emit(event.delta);
              }
              if (event?.type === "response.created" || event?.type === "response.completed") {
                responseId = typeof event.response?.id === "string" ? event.response.id : responseId;
                responseUsage = event.response?.usage ?? responseUsage;
                if (!text && typeof event.response?.output_text === "string") {
                  text = event.response.output_text;
                }
              }
              if (event?.type === "response.completed") completed = true;
              if (["error", "response.failed", "response.incomplete"].includes(event?.type))
                throw new ModelConnectorError("provider-error", "模型流式响应未完成");
            }
            if (!text.trim()) throw streamFormatFailure();
            if (!completed) throw new ModelConnectorError("provider-error", "模型流式响应中断");
            response = { output_text: text, id: responseId, usage: responseUsage };
          }
        } catch (error) {
          // Compatible gateways may support the non-stream Responses endpoint but
          // reject SSE. Fall back once without the stream flag; do not loop it.
          const status = errorStatus(error);
          const streamFormat = streamFormatError(error);
          const streamRejected = request.stream && [400, 404, 405, 422].includes(status ?? -1);
          const jsonRejected = request.jsonMode && [400, 422].includes(status ?? -1);
          if (emittedText || request.signal?.aborted ||
              !streamFormat && !streamRejected && !jsonRejected) throw error;
          response = await client.responses.create({ ...params,
            ...(officialDeepSeekJson ? { reasoning: { effort: "none" as const } } : {}) },
          { signal });
        }
        if (response.output_text) markFirstBody();
        result = { text: response.output_text || "",
          responseId: typeof response.id === "string" ? response.id : undefined,
          usage: usage(response.usage?.input_tokens, response.usage?.output_tokens) };
        break;
      }
      case "chat_completions": {
        const { default: OpenAI } = await import("openai");
        const client = new OpenAI({ apiKey: safe.apiKey || NO_KEY, baseURL: safe.baseUrl,
          organization: null, project: null, adminAPIKey: null,
          fetch: guardedFetch(safe, signal, undefined, !request.stream,
            safe.protocol === "chat_completions" ? "chat_completions" : undefined), maxRetries: 0,
          timeout: timeoutMs, logLevel: "off" });
        const params = { model: safe.model,
          messages: [{ role: "system" as const, content: request.system },
            { role: "user" as const, content: request.prompt }],
          ...(request.maxOutputTokens !== undefined ?
            { max_tokens: request.maxOutputTokens } : {}) };
        let response;
        try {
          const requestBody = { ...params,
            ...(request.jsonMode ? { response_format: { type: "json_object" as const } } : {}) };
          if (!request.stream) {
            response = await client.chat.completions.create(requestBody, { signal });
          } else {
            const stream = await client.chat.completions.create({ ...requestBody, stream: true,
              stream_options: { include_usage: true } },
              { signal });
            let text = "";
            let responseUsage: any;
            let completed = false;
            for await (const chunk of stream as AsyncIterable<any>) {
              const delta = chunk?.choices?.[0]?.delta?.content;
              if (typeof delta === "string") {
                text += delta; emit(delta);
              }
              responseUsage = chunk?.usage ?? responseUsage;
              if (chunk?.choices?.[0]?.finish_reason) completed = true;
            }
            if (!text.trim()) throw streamFormatFailure();
            if (!completed) throw new ModelConnectorError("provider-error", "模型流式响应中断");
            response = { choices: [{ message: { content: text } }], usage: responseUsage };
          }
        } catch (error) {
          const status = errorStatus(error);
          const streamFormat = streamFormatError(error);
          const streamRejected = request.stream && [400, 404, 405, 422].includes(status ?? -1);
          const jsonRejected = request.jsonMode && [400, 422].includes(status ?? -1);
          if (emittedText || request.signal?.aborted ||
              !streamFormat && !streamRejected && !jsonRejected) throw error;
          response = await client.chat.completions.create(params, { signal });
        }
        if (response.choices[0]?.message.content) markFirstBody();
        result = { text: response.choices[0]?.message.content ?? "",
          usage: usage(response.usage?.prompt_tokens, response.usage?.completion_tokens) };
        break;
      }
      case "anthropic": {
        const { default: Anthropic } = await import("@anthropic-ai/sdk");
        const client = new Anthropic({ apiKey: safe.apiKey || NO_KEY, authToken: null,
          credentials: null, config: null, profile: null, baseURL: anthropicSdkBase(safe),
          fetch: guardedFetch(safe, signal, undefined, !request.stream), maxRetries: 0,
          timeout: timeoutMs, logLevel: "off" });
        const params = { model: safe.model,
          system: request.system,
          // The Messages API requires this field even when the caller omits a cap.
          max_tokens: request.maxOutputTokens ?? 8192,
          messages: [{ role: "user" as const, content: request.prompt }] };
        if (!request.stream) {
          const response = await client.messages.create(params, { signal });
          result = { text: response.content.filter((item) => item.type === "text")
            .map((item) => item.text).join("\n"),
            usage: usage(response.usage.input_tokens, response.usage.output_tokens) };
        } else {
          const stream = await client.messages.create({ ...params, stream: true }, { signal });
          let text = "", input: number | undefined, output: number | undefined;
          let completed = false;
          for await (const event of stream) {
            if (event.type === "message_start") {
              input = event.message.usage.input_tokens;
              output = event.message.usage.output_tokens;
            } else if (event.type === "content_block_start" && event.content_block.type === "text") {
              text += event.content_block.text; emit(event.content_block.text);
            } else if (event.type === "content_block_delta" && event.delta.type === "text_delta") {
              text += event.delta.text; emit(event.delta.text);
            } else if (event.type === "message_delta") {
              input = event.usage.input_tokens ?? input;
              output = event.usage.output_tokens ?? output;
            } else if (event.type === "message_stop") completed = true;
          }
          if (!completed) throw new ModelConnectorError("provider-error", "模型流式响应中断");
          result = { text, usage: usage(input, output) };
        }
        break;
      }
      case "gemini": {
        const { GoogleGenAI } = await import("@google/genai");
        const client = new GoogleGenAI({ apiKey: safe.apiKey || NO_KEY, vertexai: false,
          httpOptions: { baseUrl: safe.baseUrl, apiVersion: "", timeout: timeoutMs,
            fetch: guardedFetch(safe, signal, undefined, !request.stream) } });
        const params = { model: safe.model,
          contents: request.prompt, config: { systemInstruction: request.system,
            ...(request.jsonMode ? { responseMimeType: "application/json" } : {}),
            ...(request.maxOutputTokens !== undefined ?
              { maxOutputTokens: request.maxOutputTokens } : {}),
            abortSignal: signal } };
        if (!request.stream) {
          const response = await client.models.generateContent(params);
          result = { text: response.text ?? "",
            usage: usage(response.usageMetadata?.promptTokenCount,
              response.usageMetadata?.candidatesTokenCount) };
        } else {
          const stream = await client.models.generateContentStream(params);
          let text = "", metadata: any;
          let completed = false;
          for await (const chunk of stream) {
            const delta = chunk.text ?? "";
            text += delta; emit(delta);
            metadata = chunk.usageMetadata ?? metadata;
            if (chunk.candidates?.[0]?.finishReason) completed = true;
          }
          if (!completed) throw new ModelConnectorError("provider-error", "模型流式响应中断");
          result = { text, usage: usage(metadata?.promptTokenCount, metadata?.candidatesTokenCount) };
        }
        break;
      }
      case "ollama": {
        const { Ollama } = await import("ollama");
        const client = new Ollama({ host: ollamaSdkBase(safe),
          fetch: guardedFetch(safe, signal, undefined, !request.stream),
          headers: safe.apiKey ? { Authorization: `Bearer ${safe.apiKey}` } : undefined });
        const params = { model: safe.model,
          ...(request.jsonMode ? { format: "json" } : {}),
          messages: [{ role: "system", content: request.system },
            { role: "user", content: request.prompt }],
          options: { ...(request.maxOutputTokens !== undefined ?
            { num_predict: request.maxOutputTokens } : {}) } };
        if (!request.stream) {
          const response = await client.chat({ ...params, stream: false });
          result = { text: response.message?.content ?? "",
            usage: usage(response.prompt_eval_count, response.eval_count) };
        } else {
          const stream = await client.chat({ ...params, stream: true });
          let text = "", input: number | undefined, output: number | undefined;
          let completed = false;
          for await (const chunk of stream) {
            const delta = chunk.message?.content ?? "";
            text += delta; emit(delta);
            if (chunk.done) {
              completed = true; input = chunk.prompt_eval_count; output = chunk.eval_count;
            }
          }
          if (!completed) throw new ModelConnectorError("provider-error", "模型流式响应中断");
          result = { text, usage: usage(input, output) };
        }
        break;
      }
    }
    if (request.signal?.aborted) throw new ModelConnectorError("cancelled", "请求已取消");
    if (!result.text.trim()) throw new ModelConnectorError("empty-response", "模型没有返回文本");
    return { ...result, timings: { firstBodyMs, connectorMs: performance.now() - connectorStarted } };
  } catch (error) {
    if (!request.signal?.aborted && deadline?.aborted)
      throw new ModelConnectorError("timeout", "连接服务超时");
    return safeError(error, request.signal);
  }
}

/** The only connection probe contains fixed synthetic text, never conversation content. */
export async function testConnection(config: ModelConfig): Promise<{
  ok: true; latencyMs: number; model: string;
}> {
  const start = performance.now();
  await generateStructured(config, {
    system: "This is a connection test. Return only a JSON object.",
    prompt: 'Reply with JSON {"ok":true}.',
    maxOutputTokens: 128,
    jsonMode: true,
    // Keep the probe finite so the settings buttons never stay busy for long.
    timeoutMs: 15_000,
  });
  return { ok: true, latencyMs: Math.round(performance.now() - start), model: config.model.trim() };
}
