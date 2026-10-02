import assert from "node:assert/strict";
import { it } from "node:test";

import {
  generateStructured, listModels, ModelConnectorError, testConnection,
  validateModelConfig, type ModelConfig, type Protocol,
} from "../electron/model-connectors";

const configs: Record<Protocol, ModelConfig> = {
  anthropic: { protocol: "anthropic", baseUrl: "https://example.invalid/v1", model: "test-model", apiKey: "secret-value" },
  responses: { protocol: "responses", baseUrl: "https://example.invalid/v1", model: "test-model", apiKey: "secret-value" },
  chat_completions: { protocol: "chat_completions", baseUrl: "https://example.invalid/v1", model: "test-model", apiKey: "secret-value" },
  gemini: { protocol: "gemini", baseUrl: "https://example.invalid/v1beta", model: "test-model", apiKey: "secret-value" },
  ollama: { protocol: "ollama", baseUrl: "http://127.0.0.1:11434", model: "test-model" },
};

type FetchHandler = (url: URL, init?: RequestInit) => Promise<Response> | Response;

async function withMockFetch<T>(handler: FetchHandler, task: () => Promise<T>): Promise<T> {
  const original = globalThis.fetch;
  globalThis.fetch = async (input, init) => handler(
    new URL(input instanceof Request ? input.url : String(input)), init,
  );
  try {
    return await task();
  } finally {
    globalThis.fetch = original;
  }
}

function json(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), {
    status, headers: { "content-type": "application/json" },
  });
}

function generationFixture(protocol: Protocol): unknown {
  const text = '{"ok":true}';
  switch (protocol) {
    case "responses": return {
      id: "resp_test", object: "response", status: "completed", model: "test-model",
      output: [{ type: "message", role: "assistant", content: [
        { type: "output_text", text, annotations: [] },
      ] }], usage: { input_tokens: 3, output_tokens: 2, total_tokens: 5 },
    };
    case "chat_completions": return {
      id: "chat_test", object: "chat.completion", model: "test-model",
      choices: [{ index: 0, message: { role: "assistant", content: text }, finish_reason: "stop" }],
      usage: { prompt_tokens: 3, completion_tokens: 2, total_tokens: 5 },
    };
    case "anthropic": return {
      id: "msg_test", type: "message", role: "assistant", model: "test-model",
      content: [{ type: "text", text }], usage: { input_tokens: 3, output_tokens: 2 },
      stop_reason: "end_turn", stop_sequence: null,
    };
    case "gemini": return {
      candidates: [{ content: { role: "model", parts: [{ text }] }, finishReason: "STOP" }],
      usageMetadata: { promptTokenCount: 3, candidatesTokenCount: 2 },
    };
    case "ollama": return {
      model: "test-model", done: true, message: { role: "assistant", content: text },
      prompt_eval_count: 3, eval_count: 2,
    };
  }
}

it("rejects unsafe endpoints and never lets SDKs read a key from the URL", () => {
  const base = configs.responses;
  for (const url of ["file:///tmp/api", "https://key:secret@example.invalid/v1",
    "https://example.invalid/v1?key=secret", "https://example.invalid/v1#fragment",
    "http://example.invalid/v1", "https://example.invalid/v1\\redirect"]) {
    assert.throws(() => validateModelConfig({ ...base, baseUrl: url }), ModelConnectorError);
  }
  assert.equal(validateModelConfig(configs.ollama).baseUrl, "http://127.0.0.1:11434");
  assert.throws(() => validateModelConfig({ ...configs.ollama,
    baseUrl: "http://192.168.1.2:11434" }), (error: unknown) => {
    assert.ok(error instanceof ModelConnectorError);
    assert.equal(error.code, "invalid-url");
    return true;
  });
});

it("accepts only integer context sizes within the supported token range", () => {
  for (const contextTokens of [4096, 1000000]) {
    assert.equal(validateModelConfig({ ...configs.responses, contextTokens }).model,
      "test-model");
  }
  for (const contextTokens of [4095, 1000001, 8192.5, "8192", true]) {
    assert.throws(() => validateModelConfig({ ...configs.responses,
      contextTokens: contextTokens as unknown as number }), (error: unknown) => {
      assert.ok(error instanceof ModelConnectorError);
      assert.equal(error.code, "invalid-request");
      return true;
    });
  }
});

it("enumerates models through each official SDK with a bounded first page", async () => {
  const expected: Record<Protocol, string> = {
    responses: "/v1/models", chat_completions: "/v1/models", anthropic: "/v1/models",
    gemini: "/v1beta/models", ollama: "/api/tags",
  };
  for (const protocol of Object.keys(configs) as Protocol[]) {
    let calls = 0;
    let lastUrl = "";
    const found = await withMockFetch((url, init) => {
      calls++;
      lastUrl = url.toString();
      assert.equal(url.pathname, expected[protocol]);
      assert.equal(init?.redirect, "error");
      switch (protocol) {
        case "responses":
        case "chat_completions": return json({ data: [{ id: "test-model", context_window: 128000 }] });
        case "anthropic": return json({ data: [{ id: "test-model", display_name: "Test Model" }],
          has_more: false });
        case "gemini": return json({ models: [{ name: "models/test-model", displayName: "Test Model",
          inputTokenLimit: 128000 }] });
        case "ollama": return json({ models: [{ name: "test-model", model: "test-model",
          context_length: 128000 }] });
      }
    }, () => listModels(configs[protocol])).catch((error: unknown) => {
      throw new Error(`${protocol} list failed after ${calls} calls at ${lastUrl}`, { cause: error });
    });
    assert.equal(calls, 1, protocol);
    assert.equal(found.supported, true, protocol);
    assert.equal(found.models[0]?.id, "test-model", protocol);
    assert.equal(found.models[0]?.contextTokens,
      ["responses", "chat_completions", "gemini", "ollama"].includes(protocol) ? 128000 : undefined,
      protocol);
  }
});

it("allows manual model IDs when a compatible gateway has no models endpoint", async () => {
  const result = await withMockFetch(() => json({ error: "missing endpoint" }, 404),
    () => listModels(configs.chat_completions));
  assert.deepEqual(result, { models: [], supported: false });
});

it("reports a forbidden model list as an authorization error", async () => {
  await withMockFetch(() => json({ error: "forbidden" }, 403), async () => {
    await assert.rejects(() => listModels(configs.responses), (error: unknown) => {
      assert.ok(error instanceof ModelConnectorError);
      assert.equal(error.code, "auth");
      return true;
    });
  });
});

it("accepts the common Ollama /api URL without doubling that path", async () => {
  const result = await withMockFetch((url) => {
    assert.equal(url.pathname, "/api/tags");
    return json({ models: [{ model: "test-model", name: "test-model" }] });
  }, () => listModels({ ...configs.ollama, baseUrl: "http://127.0.0.1:11434/api" }));
  assert.equal(result.models[0]?.id, "test-model");
});

it("uses each SDK's generation format and returns only raw text and usage", async () => {
  const paths: Record<Protocol, string> = {
    responses: "/v1/responses", chat_completions: "/v1/chat/completions",
    anthropic: "/v1/messages", gemini: "/v1beta/models/test-model:generateContent",
    ollama: "/api/chat",
  };
  for (const protocol of Object.keys(configs) as Protocol[]) {
    let calls = 0;
    let lastUrl = "";
    const result = await withMockFetch(async (url, init) => {
      calls++;
      lastUrl = url.toString();
      assert.equal(url.pathname, paths[protocol]);
      assert.equal(init?.redirect, "error");
      const body = typeof init?.body === "string" ? init.body : "";
      assert.match(body, /synthetic prompt/u);
      assert.doesNotMatch(body, /private chat/u);
      return json(generationFixture(protocol));
    }, () => generateStructured(configs[protocol], {
      system: "Return JSON.", prompt: "synthetic prompt", maxOutputTokens: 64,
    })).catch((error: unknown) => {
      throw new Error(`${protocol} generation failed after ${calls} calls at ${lastUrl}`, { cause: error });
    });
    assert.equal(calls, 1, protocol);
    assert.equal(result.text, '{"ok":true}', protocol);
    assert.deepEqual(result.usage, { inputTokens: 3, outputTokens: 2 }, protocol);
  }
});

it("connection test sends only a fixed synthetic prompt", async () => {
  const result = await withMockFetch((_url, init) => {
    const body = typeof init?.body === "string" ? init.body : "";
    assert.match(body, /Reply with JSON/u);
    assert.doesNotMatch(body, /private chat/u);
    return json(generationFixture("chat_completions"));
  }, () => testConnection(configs.chat_completions));
  assert.deepEqual({ ok: result.ok, model: result.model }, { ok: true, model: "test-model" });
  assert.ok(result.latencyMs >= 0);
});

it("keeps official DeepSeek structured requests out of default thinking mode", async () => {
  const config = { ...configs.responses, baseUrl: "https://api.deepseek.com/" };
  const result = await withMockFetch((_url, init) => {
    const body = JSON.parse(String(init?.body || "{}"));
    assert.equal(body.text?.format?.type, "json_object");
    assert.equal(body.reasoning?.effort, "none");
    return json(generationFixture("responses"));
  }, () => generateStructured(config, {
    system: "Return JSON.", prompt: "synthetic prompt", maxOutputTokens: 64, jsonMode: true,
  }));
  assert.equal(result.text, '{"ok":true}');
});

it("requests JSON mode and falls back once when a compatible gateway rejects it", async () => {
  const bodies: Record<string, unknown>[] = [];
  const result = await withMockFetch((_url, init) => {
    bodies.push(JSON.parse(String(init?.body || "{}")) as Record<string, unknown>);
    return bodies.length === 1
      ? json({ error: { message: "format unsupported" } }, 400)
      : json(generationFixture("responses"));
  }, () => generateStructured(configs.responses, {
    system: "Return JSON.", prompt: "synthetic prompt", maxOutputTokens: 64, jsonMode: true,
  }));
  assert.equal((bodies[0].text as { format: { type: string } }).format.type, "json_object");
  assert.equal(bodies[1].text, undefined);
  assert.equal(result.text, '{"ok":true}');
});

it("keeps official DeepSeek thinking disabled when JSON mode falls back", async () => {
  const config = { ...configs.responses, baseUrl: "https://api.deepseek.com/v1" };
  const bodies: Record<string, unknown>[] = [];
  await withMockFetch((_url, init) => {
    bodies.push(JSON.parse(String(init?.body || "{}")) as Record<string, unknown>);
    return bodies.length === 1
      ? json({ error: { message: "synthetic format rejection" } }, 422)
      : json(generationFixture("responses"));
  }, () => generateStructured(config, {
    system: "Return JSON.", prompt: "synthetic prompt", maxOutputTokens: 64, jsonMode: true,
  }));
  assert.equal(bodies.length, 2);
  assert.equal((bodies[0].reasoning as { effort: string }).effort, "none");
  assert.equal(bodies[1].text, undefined);
  assert.equal((bodies[1].reasoning as { effort: string }).effort, "none");
});

it("keeps provider error bodies and keys out of application errors", async () => {
  await withMockFetch(() => json({ error: { message: "secret-value" } }, 401), async () => {
    await assert.rejects(() => generateStructured(configs.responses, {
      system: "", prompt: "synthetic prompt", maxOutputTokens: 64,
    }), (error: unknown) => {
      assert.ok(error instanceof ModelConnectorError);
      assert.equal(error.code, "auth");
      assert.doesNotMatch(error.message, /secret-value/u);
      return true;
    });
  });
});

it("stops before buffering a response larger than the connector limit", async () => {
  await withMockFetch(() => new Response(null, {
    status: 200, headers: { "content-length": String(3 * 1024 * 1024) },
  }), async () => {
    await assert.rejects(() => listModels(configs.responses), (error: unknown) => {
      assert.ok(error instanceof ModelConnectorError);
      assert.equal(error.code, "response-too-large");
      return true;
    });
  });
});
