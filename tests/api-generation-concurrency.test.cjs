const test = require('node:test');
const assert = require('node:assert/strict');
const http = require('node:http');
const path = require('node:path');
const { spawn } = require('node:child_process');

const root = path.resolve(__dirname, '..');
const wait = ms => new Promise(resolve => setTimeout(resolve, ms));
const gatewayObservations = new Map();

function portrait(summary) {
  return { summary, communication: '', emotionExpression: '', interactionPreferences: '',
    topics: [], patterns: [], boundaries: [], uncertain: [], affinity: null,
    mbtiAxes: { EI: null, SN: null, TF: null, JP: null },
    traits: { socialEnergy: null, humor: null, composure: null, initiative: null,
      care: null, affection: null } };
}

function completion(value) {
  return { choices: [{ message: { content: JSON.stringify(value) } }],
    usage: { prompt_tokens: 3, completion_tokens: 2 } };
}

async function gateway(t, respond) {
  const observations = { connections: 0, received: [], responded: [] };
  const server = http.createServer(async (request, response) => {
    try {
      const chunks = [];
      for await (const chunk of request) chunks.push(chunk);
      const body = JSON.parse(Buffer.concat(chunks).toString());
      const prompt = body.messages?.find(message => message.role === 'user')?.content || '';
      const tag = Number(/tag-(\d+)/.exec(prompt)?.[1]);
      observations.received.push({ tag, at: Date.now() });
      const answer = await respond({ body, prompt, tag });
      response.setHeader('Content-Type', 'application/json');
      response.statusCode = answer.status || 200;
      response.end(JSON.stringify(answer.body || completion(portrait(`tag-${tag}`))));
      observations.responded.push({ tag, at: Date.now() });
    } catch {
      response.statusCode = 500;
      response.end('{}');
    }
  });
  server.on('connection', () => { observations.connections++; });
  await new Promise(resolve => server.listen(0, '127.0.0.1', resolve));
  t.after(() => new Promise(resolve => {
    server.closeAllConnections();
    server.close(resolve);
  }));
  const baseUrl = `http://127.0.0.1:${server.address().port}/v1`;
  gatewayObservations.set(baseUrl, observations);
  t.after(() => gatewayObservations.delete(baseUrl));
  return baseUrl;
}

async function worker(t) {
  const child = spawn(process.execPath,
    ['--import', 'tsx', 'bridge/analysis_server.ts', '--api-only'],
    { cwd: root, stdio: ['pipe', 'pipe', 'pipe'], windowsHide: true });
  const replies = [];
  let buffer = '', stderr = '';
  let ready;
  const readyPromise = new Promise(resolve => { ready = resolve; });
  child.stdout.on('data', chunk => {
    buffer += chunk.toString();
    for (;;) {
      const end = buffer.indexOf('\n');
      if (end < 0) break;
      const line = buffer.slice(0, end);
      buffer = buffer.slice(end + 1);
      const value = JSON.parse(line);
      if ('ready' in value) ready();
      // Stream deltas are progress events, not terminal replies.
      else if (!('streamDelta' in value)) replies.push(value);
    }
  });
  child.stderr.on('data', chunk => { stderr += chunk.toString(); });
  t.after(async () => {
    if (child.exitCode === null) {
      child.stdin.end();
      await Promise.race([new Promise(resolve => child.once('exit', resolve)),
        new Promise(resolve => setTimeout(() => { child.kill(); resolve(); }, 2000).unref())]);
    }
  });
  await Promise.race([readyPromise, new Promise((_resolve, reject) =>
    setTimeout(() => reject(new Error(`API worker did not start: ${stderr}`)), 5000).unref())]);
  return {
    child, replies,
    status: () => ({ pid: child.pid, exitCode: child.exitCode, signalCode: child.signalCode,
                    terminalIds: replies.map(reply => reply.id), stderr }),
    send: request => child.stdin.write(JSON.stringify(request) + '\n'),
    async until(count, timeoutMs = 8000) {
      const deadline = Date.now() + timeoutMs;
      while (replies.length < count && Date.now() < deadline) await wait(10);
      assert.equal(replies.length >= count, true, `Expected ${count} replies, got ${replies.length}; ${JSON.stringify({ pid: child.pid, exitCode: child.exitCode, signalCode: child.signalCode, terminalIds: replies.map(reply => reply.id), stderr })}`);
      return replies;
    },
  };
}

function portraitRequest(id, baseUrl) {
  return { id, cmd: 'model:portrait', protocol: 'chat_completions', baseUrl,
    apiKey: 'synthetic-key', model: 'synthetic', previous: null,
    messages: [{ id: `m-${id}`, sender: 'OTHER', target: true, text: `tag-${id}` }] };
}

function insightsRequest(id, baseUrl) {
  return { id, cmd: 'model:insights', protocol: 'chat_completions', baseUrl,
    apiKey: 'synthetic-key', model: 'synthetic',
    messages: [{ id: `m-${id}`, sender: 'OTHER', text: `tag-${id}` }],
    targetIds: [`m-${id}`] };
}

function axesRequest(id, baseUrl) {
  return { id, cmd: 'model:portrait-axes', protocol: 'chat_completions', baseUrl,
    apiKey: 'synthetic-key', model: 'synthetic', portrait: portrait(`tag-${id}`) };
}

async function exitCleanly(child) {
  if (child.exitCode === null) await Promise.race([
    new Promise(resolve => child.once('exit', resolve)),
    new Promise((_resolve, reject) =>
      setTimeout(() => reject(new Error('worker did not drain before exit')), 2000).unref()),
  ]);
  assert.equal(child.exitCode, 0);
}

test('a slow API portrait does not block a later fast portrait, including after stdin closes', async t => {
  const baseUrl = await gateway(t, async ({ tag }) => {
    await wait(tag === 1 ? 650 : 20);
    return { body: completion(portrait(`tag-${tag}`)) };
  });
  const running = await worker(t);
  running.send(portraitRequest(1, baseUrl));
  running.send(portraitRequest(2, baseUrl));
  running.child.stdin.end();
  await running.until(2);
  assert.deepEqual(running.replies.map(reply => reply.id), [2, 1]);
  assert.equal(running.replies[0].portrait.summary, 'tag-2');
  assert.equal(running.replies[1].portrait.summary, 'tag-1');
  await exitCleanly(running.child);
});

test('stdin close waits for a portrait that is still queued', async t => {
  const baseUrl = await gateway(t, async ({ tag }) => {
    await wait(300);
    return { body: completion(portrait(`tag-${tag}`)) };
  });
  const running = await worker(t);
  for (let id = 1; id <= 11; id++) running.send(portraitRequest(id, baseUrl));
  running.child.stdin.end();
  try {
    await running.until(11);
  } catch (error) {
    t.diagnostic(JSON.stringify({ gateway: gatewayObservations.get(baseUrl), worker: running.status() }));
    throw error;
  }
  assert.deepEqual(running.replies.map(reply => reply.id).sort((a, b) => a - b),
    Array.from({ length: 11 }, (_value, index) => index + 1));
  assert.ok(running.replies.every(reply => reply.portrait?.summary === `tag-${reply.id}`));
  await exitCleanly(running.child);
});

test('insights and portrait axes use independent lanes, and one gateway error leaves peers usable', async t => {
  const baseUrl = await gateway(t, async ({ prompt, tag }) => {
    if (tag === 1) await wait(550);
    if (tag === 4) return { status: 429, body: { error: { message: 'synthetic limit' } } };
    if (prompt.includes('CHAT_BATCH_JSON:\n'))
      return { body: completion({ items: [{ id: `m-${tag}`, status: 'ok',
        emotion: '开心', intent: '分享' }] }) };
    if (prompt.includes('"portrait"'))
      return { body: completion({ mbtiAxes: { EI: 55, SN: null, TF: null, JP: null },
        traits: { socialEnergy: 60, humor: null, composure: null, initiative: null,
          care: null, affection: null }, affinity: null }) };
    return { body: completion(portrait(`tag-${tag}`)) };
  });
  const running = await worker(t);
  running.send(portraitRequest(1, baseUrl));
  running.send(insightsRequest(2, baseUrl));
  running.send(axesRequest(3, baseUrl));
  await running.until(3);
  assert.deepEqual(new Set(running.replies.slice(0, 2).map(reply => reply.id)), new Set([2, 3]));
  const byId = new Map(running.replies.map(reply => [reply.id, reply]));
  assert.equal(byId.get(1).portrait.summary, 'tag-1');
  assert.equal(byId.get(2).insights[0].id, 'm-2');
  assert.equal(byId.get(3).axes.mbtiAxes.EI, 55);

  running.send(portraitRequest(4, baseUrl));
  await running.until(4);
  const afterError = new Map(running.replies.map(reply => [reply.id, reply]));
  assert.equal(afterError.get(4).error, 'rate-limit');
  assert.equal(Object.hasOwn(afterError.get(4), 'modelStatus'), false,
    'API-only errors must not report the unrelated local Laya loading state');
  running.send(portraitRequest(5, baseUrl));
  await running.until(5);
  assert.equal(running.replies.find(reply => reply.id === 5).portrait.summary, 'tag-5');
  running.send(portraitRequest(6, baseUrl));
  await running.until(6);
  assert.equal(running.replies.find(reply => reply.id === 6).portrait.summary, 'tag-6');
});

test('one lane runs at most ten requests and queues at most twenty before rate-limit', async t => {
  let inFlight = 0, maxInFlight = 0, gatewayCalls = 0;
  const baseUrl = await gateway(t, async ({ tag }) => {
    gatewayCalls++;
    inFlight++;
    maxInFlight = Math.max(maxInFlight, inFlight);
    await wait(350);
    inFlight--;
    return { body: completion(portrait(`tag-${tag}`)) };
  });
  const running = await worker(t);
  for (let id = 1; id <= 35; id++) running.send(portraitRequest(id, baseUrl));
  try {
    await running.until(35, 10000);
  } catch (error) {
    t.diagnostic(JSON.stringify({ gatewayCalls, inFlight, maxInFlight, worker: running.status() }));
    throw error;
  }
  const rejected = running.replies.filter(reply => reply.error === 'rate-limit').map(reply => reply.id).sort((a, b) => a - b);
  assert.deepEqual(rejected, [31, 32, 33, 34, 35]);
  assert.equal(gatewayCalls, 30);
  assert.ok(maxInFlight >= 2 && maxInFlight <= 10, `observed ${maxInFlight} concurrent gateway calls`);
  assert.equal(inFlight, 0);
  const replies = new Map(running.replies.map(reply => [reply.id, reply]));
  for (let id = 1; id <= 30; id++) assert.equal(replies.get(id).portrait.summary, `tag-${id}`);
  running.send(portraitRequest(36, baseUrl));
  await running.until(36);
  assert.equal(running.replies.find(reply => reply.id === 36).portrait.summary, 'tag-36');
});

test('a queued request expires after ten seconds and never becomes a paid gateway call', async t => {
  let release;
  const held = new Promise(resolve => { release = resolve; });
  const gatewayIds = [];
  const baseUrl = await gateway(t, async ({ tag }) => {
    gatewayIds.push(tag);
    if (tag <= 10) await held;
    return { body: completion(portrait(`tag-${tag}`)) };
  });
  const running = await worker(t);
  try {
    for (let id = 1; id <= 11; id++) running.send(portraitRequest(id, baseUrl));
    await running.until(1, 13000);
    assert.deepEqual(running.replies.map(reply => [reply.id, reply.error]), [[11, 'rate-limit']]);
    assert.equal(gatewayIds.includes(11), false);
  } finally {
    release();
  }
  await running.until(11);
  running.send(portraitRequest(12, baseUrl));
  await running.until(12);
  assert.equal(running.replies.find(reply => reply.id === 12).portrait.summary, 'tag-12');
  assert.equal(gatewayIds.includes(11), false);
});
