const { test } = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const vm = require("node:vm");
const path = require("node:path");
const source = fs.readFileSync(path.join(__dirname, "../chatui/app.js"), "utf8");
function block(from, to) { return source.slice(source.indexOf(from), source.indexOf(to, source.indexOf(from))); }
function harness() {
  const context = vm.createContext({});
  vm.runInContext(`
    const calls = [];
    const portraitState = {autoIncrementalState: new Map(), activeAnalysisScope: null};
    const chatState = {currentAccount:'account'};
    const settingsState = {suppressedLocalAccounts:new Set()};
    const canAnalyzeLocal = () => true;
    const startIncremental = (_user,_token,_signal,key) => calls.push(key);
    ${block("function incrementalState(", "async function startIncremental(")}
    ${block("function scheduleIncremental(", "function localAnalysisScopeKey(")}
    ${block("function localAnalysisScopeKey(", "async function loadAnalysis(")}
    function poll(data) {
      portraitState.activeAnalysisScope=localAnalysisScopeKey('account','friend',data);
      scheduleIncremental('friend',1,null,data,false,'unchanged-recent-window');
      return calls.slice();
    }
  `, context);
  return context;
}
test("a changed QQ revision schedules work even when the latest chat window did not change", () => {
  const page = harness();
  assert.equal(page.poll({analysisVersion:'v1',dataRevision:1,job:{status:'done'}}).length, 1);
  assert.equal(page.poll({analysisVersion:'v1',dataRevision:1,job:{status:'done'}}).length, 1);
  assert.equal(page.poll({analysisVersion:'v1',dataRevision:2,job:{status:'idle'}}).length, 2);
});
test("an old revision failure is isolated, while the same failed revision waits for a user retry", () => {
  const page = harness();
  assert.equal(page.poll({analysisVersion:'v1',dataRevision:1,job:{status:'error'}}).length, 0);
  assert.equal(page.poll({analysisVersion:'v1',dataRevision:1,job:{status:'idle'}}).length, 0);
  assert.equal(page.poll({analysisVersion:'v1',dataRevision:2,job:{status:'idle'}}).length, 1);
});
test("sources without dataRevision preserve the established account/model cache key", () => {
  const page = harness();
  assert.equal(page.localAnalysisScopeKey('a','u',{analysisVersion:'v'}), '["a","u","v"]');
});
