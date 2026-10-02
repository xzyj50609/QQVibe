"use strict";
// Label renderers shared by local/API paths; expansion state only owns live DOM nodes.
// The factory receives its DOM and formatting dependencies and owns no account,
// task or cache state; it never reads localStorage, fetches or sets timers.
(function () {
  function create(dependencies) {
    const element = dependencies.element;
    function closeDetails() {} // Detail mode is global; no per-message disclosure state.
    function validDetail(detail) {
      return dependencies.showDetails && detail && ["accountKey", "conversationKey", "messageKey"]
        .every(key => typeof detail.scope?.[key] === "string" && detail.scope[key]) &&
        ["laya", "api"].includes(detail.modelSource?.kind) &&
        (!dependencies.isCurrentScope || dependencies.isCurrentScope(detail));
    }
    function percentageRow(detail, messageId) {
      const row = element("div", "inline-intent-row percentage-label-row");
      row.title = "本地模型的原始候选概率，未重新归一化。";
      for (const [label, items, emotion] of [["情绪", detail.emotionCandidates, true], ["意图", detail.intentCandidates, false]]) {
        if (!items.length) continue;
        const line = element("div", "intent-line percentage-label-line");
        line.appendChild(element("span", "intent-label", label));
        for (const [index, item] of items.slice(0, 3).entries()) {
          const pill = element("span", index === 0 ? "intent-pill-primary" : "intent-item");
          if (emotion && index === 0) {
            const face = detail.kaomoji || dependencies.pickKaomoji?.(item, messageId);
            if (face) pill.appendChild(element("span", "kaomoji-mood", face));
          }
          pill.appendChild(element("span", "intent-name", item.label));
          const score = element("span", "intent-pct message-analysis-probability", (item.rawProbability * 100).toFixed(2) + "%");
          score.title = "原始概率：" + String(item.rawProbability);
          pill.appendChild(score);line.appendChild(pill);
        }
        row.appendChild(line);
      }
      return row;
    }
    // One template for both sources: an emotion line followed by an intent line.
    function render(view, messageId, detail = null) {
      const row = element("div", "inline-intent-row");
      const localCandidates = validDetail(detail) && detail.modelSource.kind === "laya";
      if (localCandidates && dependencies.detailedMode?.()) return percentageRow(detail, messageId);
      const emotionCandidate = !view.emotions.length && localCandidates
        ? detail.emotionCandidates.find(item => item.rawProbability > 0) : null;
      const intentCandidate = !view.intents.length && localCandidates
        ? detail.intentCandidates.find(item => item.rawProbability > 0) : null;
      const emotions = emotionCandidate ? [emotionCandidate] : view.emotions;
      const intents = intentCandidate ? [intentCandidate] : view.intents;
      if (!emotions.length && !intents.length) {
        return row;
      }
      const line = element("div", `intent-line${emotions.length ? " emotion-line" : ""}${intents.length ? " intent-score-line" : ""}`);
      if (emotions.length) {
        line.appendChild(element("span", "intent-label", emotionCandidate ? "情绪候选" : "情绪"));
        for (const [index, entry] of emotions.slice(0, 1).entries()) {
          const candidate = element("span", `intent-item${index === 0 ? " primary" : ""}`);
          candidate.appendChild(element("span", "intent-name", String(entry.label).trim()));
          line.appendChild(candidate);
        }
      }
      if (intents.length) {
        line.appendChild(element("span", "intent-label intent-label-intent", intentCandidate ? "意图候选" : "意图"));
        for (const [index, entry] of intents.slice(0, 1).entries()) {
          const item = element("span", `intent-item${index === 0 ? " primary" : ""}`);
          item.appendChild(element("span", "intent-name", String(entry.label).trim()));
          line.appendChild(item);
        }
      }
      if (emotionCandidate || intentCandidate) {
        row.title = "这是原始模型候选；点击工具栏的百分比开关查看概率。";
      }
      row.appendChild(line);
      return row;
    }
    // Compatibility helpers keep the old single-line entry points without a second template.
    function appendScoreLine(container, label, scores, messageId, emotion = false) {
      if (!scores.length) return;
      const view = emotion
        ? { emotions: scores.slice(0, 3).map(({ item }) => ({ label: String(item.label).trim() })), intents: [] }
        : { emotions: [], intents: scores.slice(0, 3).map(({ item }) => ({ label: String(item.label).trim() })) };
      const row = render(view, messageId);
      for (const line of [...row.children]) {
        line.children[0].textContent = label;
        container.appendChild(line);
      }
    }
    function appendIntentLine(container, candidates) {
      if (!candidates.length) return;
      const view = { emotions: [], intents: candidates.slice(0, 3).map((candidate) => ({
        label: String(candidate.label).trim(),
      })) };
      const row = render(view, "");
      for (const line of [...row.children]) container.appendChild(line);
    }
    return { render, renderApiInsightResult: render, appendScoreLine, appendIntentLine, closeDetails };
  }
  window.MessageLabels = Object.freeze({ create });
})();
