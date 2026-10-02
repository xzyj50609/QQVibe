const {test}=require('node:test');
const assert=require('node:assert/strict'),fs=require('node:fs'),vm=require('node:vm');
const source=fs.readFileSync(require('node:path').join(__dirname,'../chatui/app.js'),'utf8');
const begin=source.indexOf('async function loadAnalysis('),end=source.indexOf('async function analyzeRecent(',begin);
test('opening an empty selected QQ group requests a legal empty analysis scope',async()=>{
 const calls=[];
 const context=vm.createContext({window:{ProductConfig:{key:'qq'}},
  chatState:{historyState:null,generation:1,currentAccount:'a',sessions:new Map([['g:20001',{isGroup:true}]])},
  portraitState:{analysisGeneration:0},canAnalyzeLocal:()=>true,fineWindow:()=>({limit:0,candidates:[]}),
  api:async url=>{calls.push(url);return {account:'a'};},acceptResponseAccount:()=>false});
 vm.runInContext(source.slice(begin,end),context);
 await context.loadAnalysis('g:20001',1,null);
 assert.deepEqual(calls,['/api/analysis?user=g%3A20001&limit=1']);
});
