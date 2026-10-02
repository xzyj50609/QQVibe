const {test}=require('node:test');
const assert=require('node:assert/strict');
const fs=require('node:fs');
const path=require('node:path');
const vm=require('node:vm');
const source=fs.readFileSync(path.join(__dirname,'../chatui/app.js'),'utf8');
function block(from,to){return source.slice(source.indexOf(from),source.indexOf(to,source.indexOf(from)));}
function page(group){
 const context=vm.createContext({group});
 vm.runInContext(`
 const window={ProductConfig:{key:'qq'}};
 const chatState={historyState:null,generation:1,currentAccount:'a',currentUser:'g:1',view:'chat',messages:[{id:'old'},{id:'new'}],sessions:new Map([['g:1',{isGroup:group}]])};
 const portraitState={analysisGeneration:0,autoIncrementalState:new Map(),profilePending:false};
 const labelState={results:{old:{state:'done',labelSchema:'current'}},manualRecentDeferred:false};
 const canAnalyzeLocal=()=>true, fineWindow=()=>({limit:2});
 const api=async()=>({account:'a',analysisVersion:'v',dataRevision:2,job:{status:'running'},results:{new:{state:'done',labelSchema:'current'}}});
 const acceptResponseAccount=()=>true, visibleResults=x=>x;
 const cacheCurrentSession=()=>{},refreshLabels=()=>{},renderJob=()=>{},scheduleRecent=()=>{},text=()=>{};
 ${block('function localAnalysisScopeKey(', 'async function loadAnalysis(')}
 ${block('async function loadAnalysis(', 'async function analyzeRecent(')}
 `,context);
 return context;
}
test('group polling replaces cached labels with input-verified rows even while a job runs',async()=>{
 const context=page(true);
 await context.loadAnalysis('g:1',1,null);
 assert.equal(vm.runInContext("'old' in labelState.results",context),false);
 assert.equal(vm.runInContext("labelState.results.new.state",context),'done');
});
test('unaffected single-chat running-result merge retains its prior behavior',async()=>{
 const context=page(false);
 await context.loadAnalysis('g:1',1,null);
 assert.equal(vm.runInContext("'old' in labelState.results",context),true);
});
test('a revision-only context change admits a fresh recent request with unchanged visible text',()=>{
 const context=vm.createContext({});
 vm.runInContext(`const portraitState={activeAnalysisScope:'rev1'};const analyzableMessages=x=>x.candidates;
 ${block('function fineWindowSignature(', 'function scheduleRecent(')}
 const sample={limit:1,candidates:[{id:'m',text:'same',senderId:'p',time:1,kind:'text'}]};
 const first=fineWindowSignature(sample);portraitState.activeAnalysisScope='rev2';const second=fineWindowSignature(sample);`,context);
 assert.notEqual(vm.runInContext('first',context),vm.runInContext('second',context));
});
test('published, pending, backlog and invalidation counts are visible independently of 1/1 progress',()=>{
 const context=vm.createContext({});
 vm.runInContext(`
 const portraitState={},labelState={manualRecentAwaitingPost:false};const nodes={};
 const byId=id=>nodes[id]||(nodes[id]={});const text=(id,value)=>byId(id).textContent=value;
 const settleInlineIntentPending=()=>{},renderRecentAction=()=>{},usingLocalFine=()=>false,renderApiInsightStatus=()=>{},updateProfileProgress=()=>{};
 ${block('function renderJob(', 'function setIntentActionState(')}
 renderJob({status:'running',publicationUnit:'message',total:1,processed:1,published:2,pending:4,backlog:3,invalidated:1,recomputed:1});`,context);
 const text=vm.runInContext("nodes.analysisStatus.textContent",context);
 for(const expected of ['已发布 2','当前待算 4','队列积压 3','输入失效 1','重算 1'])assert.ok(text.includes(expected),text);
});
