const test = require('node:test');
const assert = require('node:assert/strict');
const http = require('node:http');
const path = require('node:path');
const { spawn } = require('node:child_process');

test('a slow model list does not queue an API connection test behind it', async () => {
  const root = path.resolve(__dirname, '..');
  const gateway = http.createServer((request, response) => {
    response.setHeader('Content-Type', 'application/json');
    if (request.url === '/v1/models') {
      setTimeout(() => response.end(JSON.stringify({ data: [{ id: 'synthetic' }] })), 900);
    } else if (request.url === '/v1/chat/completions') {
      request.resume();
      response.end(JSON.stringify({ choices: [{ message: { content: '{"ok":true}' } }],
        usage: { prompt_tokens: 3, completion_tokens: 2 } }));
    } else {
      response.statusCode = 404;
      response.end('{}');
    }
  });
  await new Promise(resolve => gateway.listen(0, '127.0.0.1', resolve));
  const baseUrl = `http://127.0.0.1:${gateway.address().port}/v1`;
  const worker = spawn(process.execPath,
    ['--import', 'tsx', 'bridge/analysis_server.ts', '--api-only'],
    { cwd: root, stdio: ['pipe', 'pipe', 'pipe'], windowsHide: true });
  const replies = [];
  let buffer = '';
  let ready;
  const readyPromise = new Promise(resolve => { ready = resolve; });
  worker.stdout.on('data', chunk => {
    buffer += chunk.toString();
    for (;;) {
      const index = buffer.indexOf('\n');
      if (index < 0) break;
      const line = buffer.slice(0, index);
      buffer = buffer.slice(index + 1);
      try {
        const value = JSON.parse(line);
        if ('ready' in value) ready();
        else if (value.id) replies.push(value);
      } catch { }
    }
  });
  try {
    await Promise.race([readyPromise, new Promise((_resolve, reject) =>
      setTimeout(() => reject(new Error('API worker did not start')), 5000).unref())]);
    const common = { protocol: 'chat_completions', baseUrl,
      apiKey: 'synthetic-key', model: 'synthetic' };
    worker.stdin.write(JSON.stringify({ id: 1, cmd: 'model:list', ...common }) + '\n');
    worker.stdin.write(JSON.stringify({ id: 2, cmd: 'model:test', ...common }) + '\n');
    const deadline = Date.now() + 5000;
    while (replies.length < 2 && Date.now() < deadline)
      await new Promise(resolve => setTimeout(resolve, 20));
    assert.equal(replies.length, 2);
    assert.deepEqual(replies.map(reply => reply.id), [2, 1]);
    assert.equal(replies[0].ok, true);
    assert.equal(replies[1].supported, true);
  } finally {
    worker.stdin.end();
    await Promise.race([new Promise(resolve => worker.once('exit', resolve)),
      new Promise(resolve => setTimeout(() => { worker.kill(); resolve(); }, 2000).unref())]);
    await new Promise(resolve => gateway.close(resolve));
  }
});
