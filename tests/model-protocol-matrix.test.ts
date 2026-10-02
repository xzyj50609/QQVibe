// Formal SDK requests against a loopback HTTP fixture. No cloud, key or chat access.
import assert from "node:assert/strict";
import { createServer, type ServerResponse } from "node:http";
import { it } from "node:test";
import { generateStructured, ModelConnectorError, type Protocol } from "../electron/model-connectors";

const protocols: Protocol[] = ["responses", "chat_completions", "anthropic", "gemini", "ollama"];
const text = "合成结果";
const sse = (value: unknown, event?: string) =>
  `${event ? `event: ${event}\n` : ""}data: ${JSON.stringify(value)}\n\n`;
function chunks(protocol: Protocol): string[] {
  switch (protocol) {
    case "responses": return [
      sse({ type: "response.created", response: { id: "synthetic-response" } }, "response.created"),
      sse({ type: "response.output_text.delta", delta: "合成" }, "response.output_text.delta"),
      sse({ type: "response.output_text.delta", delta: "结果" }, "response.output_text.delta") +
        sse({ type: "response.completed", response: { id: "synthetic-response",
          usage: { input_tokens: 3, output_tokens: 2 } } }, "response.completed"),
    ];
    case "chat_completions": return [
      sse({ choices: [{ index: 0, delta: { role: "assistant" }, finish_reason: null }] }),
      sse({ choices: [{ index: 0, delta: { content: "合成" }, finish_reason: null }] }),
      sse({ choices: [{ index: 0, delta: { content: "结果" }, finish_reason: "stop" }] }) +
        sse({ choices: [], usage: { prompt_tokens: 3, completion_tokens: 2 } }) + "data: [DONE]\n\n",
    ];
    case "anthropic": return [
      sse({ type: "message_start", message: { id: "synthetic-message", type: "message",
        role: "assistant", model: "synthetic", content: [], usage: { input_tokens: 3, output_tokens: 0 } } }, "message_start") +
        sse({ type: "content_block_start", index: 0, content_block: { type: "text", text: "" } }, "content_block_start"),
      sse({ type: "content_block_delta", index: 0, delta: { type: "text_delta", text: "合成" } }, "content_block_delta"),
      sse({ type: "content_block_delta", index: 0, delta: { type: "text_delta", text: "结果" } }, "content_block_delta") +
        sse({ type: "content_block_stop", index: 0 }, "content_block_stop") +
        sse({ type: "message_delta", delta: { stop_reason: "end_turn" }, usage: { output_tokens: 2 } }, "message_delta") +
        sse({ type: "message_stop" }, "message_stop"),
    ];
    case "gemini": return [
      ": heartbeat\n\n",
      sse({ candidates: [{ content: { role: "model", parts: [{ text: "合成" }] } }] }),
      sse({ candidates: [{ content: { role: "model", parts: [{ text: "结果" }] }, finishReason: "STOP" }],
        usageMetadata: { promptTokenCount: 3, candidatesTokenCount: 2 } }),
    ];
    case "ollama": return [
      JSON.stringify({ model: "synthetic", done: false, message: { role: "assistant", content: "" } }) + "\n",
      JSON.stringify({ model: "synthetic", done: false, message: { role: "assistant", content: "合成" } }) + "\n",
      JSON.stringify({ model: "synthetic", done: true, message: { role: "assistant", content: "结果" },
        prompt_eval_count: 3, eval_count: 2 }) + "\n",
    ];
  }
}

async function fixture(protocol: Protocol, reply: (res: ServerResponse) => void,
  task: (config: any, bodies: any[]) => Promise<void>) {
  const bodies: any[] = [];
  const server = createServer(async (req, res) => {
    let raw = "";
    for await (const part of req) raw += part;
    bodies.push(JSON.parse(raw));
    const paths = { responses: "/v1/responses", chat_completions: "/v1/chat/completions",
      anthropic: "/v1/messages", gemini: "/v1beta/models/synthetic:streamGenerateContent", ollama: "/api/chat" };
    assert.equal(new URL(req.url!, "http://localhost").pathname, paths[protocol]);
    reply(res);
  });
  await new Promise<void>(resolve => server.listen(0, "127.0.0.1", resolve));
  const address = server.address() as { port: number };
  const prefix = protocol === "gemini" ? "/v1beta" : protocol === "ollama" ? "" : "/v1";
  try {
    await task({ protocol, baseUrl: `http://127.0.0.1:${address.port}${prefix}`,
      model: "synthetic", apiKey: "synthetic-key" }, bodies);
  } finally {
    server.closeAllConnections();
    await new Promise<void>(resolve => server.close(() => resolve()));
  }
}

const request = { system: "Synthetic only.", prompt: "Synthetic fixture.", stream: true, maxOutputTokens: 64 };
const isCode = (code: string) => (error: unknown) => {
  assert.ok(error instanceof ModelConnectorError);
  assert.equal(error.code, code);
  assert.doesNotMatch(error.message, /synthetic-key|provider-private-body/);
  return true;
};

for (const protocol of protocols) {
  it(`${protocol}: streams two deltas before completion and preserves final usage`, { timeout: 8000 }, async () => {
    let completed = false;
    await fixture(protocol, res => {
      res.writeHead(200, { "content-type": protocol === "ollama" ? "application/x-ndjson" : "text/event-stream" });
      const rows = chunks(protocol);
      res.write(rows[0]); res.write(rows[1]);
      const timer = setTimeout(() => { completed = true; res.end(rows[2]); }, 80);
      res.on("close", () => clearTimeout(timer));
    }, async (config, bodies) => {
      const deltas: string[] = [];
      const result = await generateStructured(config, { ...request, onTextDelta: delta => {
        if (!deltas.length) assert.equal(completed, false, "first delta must precede final chunk");
        deltas.push(delta);
      } });
      assert.equal(result.text, text);
      assert.deepEqual(deltas, ["合成", "结果"]);
      assert.deepEqual(result.usage, { inputTokens: 3, outputTokens: 2 });
      assert.equal(bodies.length, 1);
      if (protocol !== "gemini") assert.equal(bodies[0].stream, true);
      if (protocol === "chat_completions") assert.equal(bodies[0].stream_options.include_usage, true);
      assert.ok(result.timings!.firstBodyMs! <= result.timings!.connectorMs!);
    });
  });

  for (const [status, code] of [[401, "auth"], [429, "rate-limit"], [413, "context-too-long"], [503, "provider-error"]] as const) {
    it(`${protocol}: HTTP ${status} is sanitized and never retried`, { timeout: 8000 }, async () => {
      await fixture(protocol, res => { res.writeHead(status, { "content-type": "application/json" });
        res.end(JSON.stringify({ error: { code: status, message: "provider-private-body synthetic-key" } }));
      }, async (config, bodies) => {
        await assert.rejects(generateStructured(config, request), isCode(code));
        assert.equal(bodies.length, 1);
      });
    });
  }

  it(`${protocol}: cancellation after a delta prevents success and retry`, { timeout: 8000 }, async () => {
    await fixture(protocol, res => {
      res.writeHead(200, { "content-type": protocol === "ollama" ? "application/x-ndjson" : "text/event-stream" });
      const rows = chunks(protocol); res.write(rows[0]); res.write(rows[1]);
    }, async (config, bodies) => {
      const controller = new AbortController();
      await assert.rejects(generateStructured(config, { ...request, signal: controller.signal,
        onTextDelta: () => controller.abort() }), isCode("cancelled"));
      assert.equal(bodies.length, 1);
    });
  });

  it(`${protocol}: timeout while waiting for a stream is finite`, { timeout: 8000 }, async () => {
    await fixture(protocol, () => {}, async (config, bodies) => {
      await assert.rejects(generateStructured(config, { ...request, timeoutMs: 1000 }), isCode("timeout"));
      assert.equal(bodies.length, 1);
    });
  });

  it(`${protocol}: timeout after headers and a delta still rejects completion`, { timeout: 8000 }, async () => {
    await fixture(protocol, res => {
      res.writeHead(200, { "content-type": protocol === "ollama" ? "application/x-ndjson" : "text/event-stream" });
      const rows = chunks(protocol); res.write(rows[0]); res.write(rows[1]);
    }, async (config, bodies) => {
      await assert.rejects(generateStructured(config, { ...request, timeoutMs: 1000 }), isCode("timeout"));
      assert.equal(bodies.length, 1);
    });
  });

  it(`${protocol}: an already cancelled request sends nothing`, async () => {
    await fixture(protocol, () => { throw new Error("unexpected request"); }, async (config, bodies) => {
      const controller = new AbortController(); controller.abort();
      await assert.rejects(generateStructured(config, { ...request, signal: controller.signal }), isCode("cancelled"));
      assert.equal(bodies.length, 0);
    });
  });

  it(`${protocol}: streamed wire bytes are bounded`, { timeout: 8000 }, async () => {
    await fixture(protocol, res => {
      res.writeHead(200, { "content-type": protocol === "ollama" ? "application/x-ndjson" : "text/event-stream" });
      res.end(" ".repeat(2 * 1024 * 1024 + 1));
    }, async (config, bodies) => {
      await assert.rejects(generateStructured(config, request), isCode("response-too-large"));
      assert.equal(bodies.length, 1);
    });
  });

  it(`${protocol}: EOF after partial text cannot publish success`, { timeout: 8000 }, async () => {
    await fixture(protocol, res => {
      res.writeHead(200, { "content-type": protocol === "ollama" ? "application/x-ndjson" : "text/event-stream" });
      const rows = chunks(protocol); res.end(rows[0] + rows[1]);
    }, async (config, bodies) => {
      await assert.rejects(generateStructured(config, request), isCode("provider-error"));
      assert.equal(bodies.length, 1);
    });
  });
}

for (const protocol of ["responses", "chat_completions"] as const) {
  it(`${protocol}: a gateway's complete JSON is consumed once with usage`, async () => {
    await fixture(protocol, res => {
      res.writeHead(200, { "content-type": "application/json" });
      res.end(JSON.stringify(protocol === "responses" ? { id: "synthetic-response",
        output: [{ type: "message", content: [{ type: "output_text", text }] }],
        usage: { input_tokens: 3, output_tokens: 2 } } : {
        choices: [{ message: { content: text } }], usage: { prompt_tokens: 3, completion_tokens: 2 },
      }));
    }, async (config, bodies) => {
      const deltas: string[] = [];
      const result = await generateStructured(config, { ...request, onTextDelta: delta => deltas.push(delta) });
      assert.equal(result.text, text); assert.deepEqual(deltas, [text]);
      assert.deepEqual(result.usage, { inputTokens: 3, outputTokens: 2 }); assert.equal(bodies.length, 1);
    });
  });
}

it("Responses failed event after partial text cannot publish or retry", async () => {
  await fixture("responses", res => {
    res.writeHead(200, { "content-type": "text/event-stream" });
    const rows = chunks("responses");
    res.end(rows[0] + rows[1] + sse({ type: "response.failed", response: {
      error: { message: "provider-private-body" } } }, "response.failed"));
  }, async (config, bodies) => {
    await assert.rejects(generateStructured(config, request), isCode("provider-error"));
    assert.equal(bodies.length, 1);
  });
});
