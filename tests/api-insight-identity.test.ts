import assert from 'node:assert/strict';
import { it } from 'node:test';
import { analyzeApiInsights, type ApiInsightInput } from '../electron/api-message-insights';
import { ModelConnectorError, type ModelConfig } from '../electron/model-connectors';

const config: ModelConfig = { protocol: 'chat_completions', baseUrl: 'http://127.0.0.1:1/v1',
  model: 'synthetic', apiKey: 'synthetic' };
const input: ApiInsightInput = { messages: [
  { id: 'native-first', sender: 'OTHER', text: '第一条合成消息' },
  { id: 'native-second', sender: 'OTHER', text: '第二条合成消息' },
], targetIds: ['native-first', 'native-second'] };
const label = (id: string, emotion = '开心') => ({ id, emotion, intent: '分享' });
const invalid = (error: unknown) => error instanceof ModelConnectorError && error.code === 'invalid-output';

it('maps out-of-order explicit short IDs to their exact original owners', async () => {
  const result = await analyzeApiInsights(config, input, async (_config, request) => {
    const wire = JSON.parse(request.prompt.split('\n').slice(1).join('\n'));
    return { text: JSON.stringify([label(wire.targetIds[1], '平静'), label(wire.targetIds[0], '开心')]) };
  });
  assert.deepEqual(result.insights.map(value => [value.id, value.affect?.feeling]),
    [['native-first', '开心'], ['native-second', '平静']]);
});

it('rejects unknown IDs rather than assigning their labels by response position', async () => {
  await assert.rejects(analyzeApiInsights(config, input, async () => ({
    text: JSON.stringify([label('unknown-second'), label('unknown-first')]),
  })), invalid);
});

it('rejects an omitted target instead of manufacturing a completed empty result', async () => {
  await assert.rejects(analyzeApiInsights(config, input, async () => ({
    text: JSON.stringify([label('native-second')]),
  })), invalid);
});

it('rejects conflicting duplicate answers', async () => {
  await assert.rejects(analyzeApiInsights(config, input, async () => ({
    text: JSON.stringify([label('native-first', '开心'), label('native-first', '生气'), label('native-second')]),
  })), invalid);
});

it('requires identifiers even when legacy text has exactly as many label pairs as targets', async () => {
  await assert.rejects(analyzeApiInsights(config, input, async () => ({
    text: '情感：开心\n意图：分享\n情感：平静\n意图：闲聊\n',
  })), invalid);
});

it('requires complete fields while accepting explicitly stated lack of evidence', async () => {
  await assert.rejects(analyzeApiInsights(config, input, async () => ({
    text: JSON.stringify([{ id: 'native-first', emotion: '开心' }, label('native-second')]),
  })), invalid);
  const result = await analyzeApiInsights(config, input, async () => ({
    text: JSON.stringify([{ id: 'native-first', status: 'uncertain' },
      { id: 'native-second', emotion: '无', intent: '无' }]),
  }));
  assert.equal(result.insights[0]!.status, 'uncertain');
  assert.deepEqual(result.insights[1]!.intents, []);
  assert.equal(result.insights[1]!.affect, undefined);
});

it('refuses ambiguous numeric aliases when native numeric IDs have a different owner', async () => {
  const numeric: ApiInsightInput = { messages: [
    { id: '2', sender: 'OTHER', text: '先发的合成消息' },
    { id: '1', sender: 'OTHER', text: '后发的合成消息' },
  ], targetIds: ['2', '1'] };
  await assert.rejects(analyzeApiInsights(config, numeric, async () => ({
    text: JSON.stringify([label('1'), label('2')]),
  })), invalid);
});

it('streaming emits complete canonical IDs only and cannot make a missing batch succeed', async () => {
  const deltas: string[] = [];
  await assert.rejects(analyzeApiInsights(config, input, async (_config, request) => {
    const wire = JSON.parse(request.prompt.split('\n').slice(1).join('\n'));
    request.onTextDelta?.('[{"id":"' + wire.targetIds[0] + '","emotion":"开');
    assert.deepEqual(deltas, []);
    request.onTextDelta?.('心","intent":"分享"}]');
    return { text: JSON.stringify([label(wire.targetIds[0])]) };
  }, delta => deltas.push(delta)), invalid);
  assert.equal(deltas.length, 1);
  assert.match(deltas[0]!, /^编号：native-first\n/u);
});
