const { test } = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const path = require('node:path');
const app = fs.readFileSync(path.join(__dirname,'../chatui/app.js'),'utf8');
const switchSource = app.slice(app.indexOf('function switchView(target)'),app.indexOf('portraitState.activeMember = "";',app.indexOf('function switchView(target)')));
function navigation() {
  const nodes=new Map();
  const chatState={view:'chat',currentAccount:'a:one',currentUser:'u:one',messageSourceReady:true,historyState:null};
  let latest=0;
  const context={chatState,portraitState:{activeMember:''},byId(id){if(!nodes.has(id))nodes.set(id,{scrollTop:0,classList:{toggle(){}}});return nodes.get(id)},window:{ChatBeanUI:{setView(){}}},clearReplyPrediction(){},cancelApiPortraitPoll(){},loadProfile(){},scrollToLatest(){latest++}};
  vm.runInNewContext(switchSource+'\nglobalThis.switchView=switchView;',context);
  return {...context,nodes,latest:()=>latest};
}
test('chat to portrait and back restores reading position rather than jumping to latest',()=>{
  const h=navigation();h.byId('chatMessages').scrollTop=812;h.switchView('persona');
  h.byId('chatMessages').scrollTop=0;h.switchView('chat');
  assert.equal(h.byId('chatMessages').scrollTop,812);assert.equal(h.latest(),0);
});
test('a saved position never crosses an account or conversation boundary',()=>{
  for(const key of ['currentAccount','currentUser']){
    const h=navigation();h.byId('chatMessages').scrollTop=812;h.switchView('persona');
    h.chatState[key]='different';h.switchView('chat');assert.equal(h.latest(),1);assert.equal(h.chatState.returnChatScroll,null);
  }
});
test('portrait without a ready selected conversation does not change view',()=>{
  const h=navigation();h.chatState.messageSourceReady=false;h.switchView('persona');assert.equal(h.chatState.view,'chat');
});
const ui=fs.readFileSync(path.join(__dirname,'../chatui/chatbean-ui.js'),'utf8');
const headingSource=ui.slice(ui.indexOf('  function messageHeading('),ui.indexOf('  global.ChatBeanUI ='));
test('QQ message headings keep sender text literal and do not invent timestamps',()=>{
  const context={doc:{createElement(){return {}}}};vm.runInNewContext(headingSource+'\nglobalThis.heading=messageHeading;',context);
  const node=context.heading({side:'other',senderName:'<script>quoted</script>',time:null},{isGroup:true},{});
  assert.equal(node.textContent,'<script>quoted</script>');assert.equal(node.innerHTML,undefined);
  assert.equal(context.heading({side:'self'},null,{name:'我'}).textContent,'我');
});
