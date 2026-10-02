const {test}=require('node:test');
const assert=require('node:assert/strict');
const fs=require('node:fs');
const vm=require('node:vm');
const source=fs.readFileSync(require('node:path').join(__dirname,'../chatui/app.js'),'utf8');
const block=source.slice(source.indexOf('let qqAnalysisControlBusy ='),source.indexOf('byId("btnRetryProfile").addEventListener'));
function fixture(){
 const nodes=new Map(),requests=[];
 const context=vm.createContext({requests,console,setInterval:()=>{},window:{ProductConfig:{key:'qq'}},
  byId:id=>{if(!nodes.has(id))nodes.set(id,{hidden:false,disabled:false,textContent:'',addEventListener(){}});return nodes.get(id);},
  api:async(path,options)=>{requests.push({path,options});return {analysisControl:options?JSON.parse(options.body).action==='resume'?'running':'paused':'running'};},
  text:(id,value)=>{context.byId(id).textContent=value;},retryAnalysis:()=>requests.push({retry:true}),
  chatState:{currentAccount:'a',currentUser:'g:1'},settingsState:{modelSourceSnapshot:{mode:'local'}}});
 vm.runInContext(block,context);return {context,nodes,requests};
}
test('pause and cancel send exact scope, no analysis request, and expose readable status',async()=>{
 const {context,nodes,requests}=fixture();await context.controlQQAnalysis('pause');
 assert.deepEqual(JSON.parse(requests[0].options.body),{account:'a',user:'g:1',action:'pause'});
 assert.equal(requests.length,1);assert.equal(nodes.get('btnPauseQQAnalysis').hidden,true);
 assert.equal(nodes.get('btnResumeQQAnalysis').hidden,false);assert.match(nodes.get('qqAnalysisControlStatus').textContent,/阅读和同步继续/);
});
test('resume explicitly submits a bounded local range and restores controls',async()=>{
 const {context,nodes,requests}=fixture();await context.controlQQAnalysis('resume');
 assert.equal(requests[1].path,'/api/analyze');assert.equal(JSON.parse(requests[1].options.body).limit,80);
 assert.equal(nodes.get('btnResumeQQAnalysis').hidden,true);assert.equal(nodes.get('btnPauseQQAnalysis').disabled,false);
});
test('late control response from another chat cannot submit work in the new chat',async()=>{
 const {context,nodes,requests}=fixture();let release;
 context.api=async(path,options)=>{requests.push({path,options});return new Promise(resolve=>{release=resolve;});};
 const work=context.controlQQAnalysis('resume');context.chatState.currentUser='g:2';release({analysisControl:'running'});await work;
 assert.equal(requests.length,1);assert.equal(nodes.get('btnPauseQQAnalysis').disabled,false);
});
