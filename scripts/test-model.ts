import assert from "node:assert/strict";
import path from "node:path";

import {
  ANALYSIS_VERSION,
  MODEL_LABEL,
  analyzeConversation,
  configureModelDir,
  disposeAnalysisModel,
  prepareModel,
} from "../electron/analysis";

function assertProbabilityScores(
  values: Array<{ probability: number }>,
  label: string,
  requireComplete = false,
): void {
  assert.ok(values.length > 0, `${label} should not be empty`);
  const total = values.reduce((sum, item) => sum + item.probability, 0);
  assert.ok(total > 0 && total <= 1.01, `${label} probabilities should be bounded, got ${total}`);
  if (requireComplete) {
    assert.ok(Math.abs(total - 1) < 0.01, `${label} probabilities should sum to 1, got ${total}`);
  }
  for (const item of values) {
    assert.ok(item.probability >= 0 && item.probability <= 1, `${label} probability out of range`);
  }
}

async function main(): Promise<void> {
  configureModelDir(path.resolve(process.env.LAYA_MODEL_DIR ?? path.join(process.cwd(), ".models", "laya")));
  const startedAt = Date.now();
  try {
    const status = await prepareModel();
    assert.equal(status.state, "ready", status.message);
    const result = await analyzeConversation({
      sessionId: "model-smoke",
      messages: [
        { id: "other-1", side: "other", text: "周末一起去看电影吗？", time: 1 },
        { id: "self-1", side: "self", text: "好啊，我很期待。", time: 2 },
      ],
    });
    assert.equal(result.model, MODEL_LABEL);
    assert.equal(result.analysisVersion, ANALYSIS_VERSION);
    assert.equal(result.messages.length, 2);
    assert.ok(Number.isFinite(result.affinity));
    assert.ok(Number.isFinite(result.durationMs));
    assert.ok(result.durationMs >= 0);
    for (const message of result.messages) {
      assert.ok(message.messageId);
      assertProbabilityScores(message.emotion, `${message.messageId} emotion`);
      assertProbabilityScores(message.intent, `${message.messageId} intent`);
    }
    assertProbabilityScores(result.relationship, "conversation relationship", true);
    assert.ok(result.selfQuality.messageId === "self-1");
    assertProbabilityScores(result.selfQuality.probabilities, "self quality", true);
    console.log(JSON.stringify({
      model: result.model,
      affinity: result.affinity,
      messageCount: result.messages.length,
      durationMs: result.durationMs,
      elapsedMs: Date.now() - startedAt,
    }));
  } finally {
    await disposeAnalysisModel();
  }
}

main().catch((error) => {
  console.error(error instanceof Error ? error.message : String(error));
  process.exitCode = 1;
});
