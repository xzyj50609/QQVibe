import assert from 'node:assert/strict';
import {it} from 'node:test';
import {analyzeApiInsights,updateApiPortrait} from '../electron/api-insights';
import {packMessageBatch} from '../electron/laya/message-batch';
import {groupTargetState,type GroupContext} from '../shared/group-context';
const config={protocol:'responses' as const,baseUrl:'https://example.invalid/v1',model:'synthetic',apiKey:'synthetic'};
const role=(speaker:string,time:number):GroupContext=>({speaker,time,quote:null,mentions:[]});
const messages=[{id:'a',sender:'OTHER' as const,text:'甲说自己的事',conversationKind:'group' as const,groupContext:role('P_A',1000)},
 {id:'b',sender:'OTHER' as const,text:'乙安慰甲',conversationKind:'group' as const,groupContext:{...role('P_B',2000),quote:{speaker:'P_A',text:'甲说自己的事'},mentions:[{speaker:'P_A',kind:'user' as const}]}},
 {id:'s',sender:'SELF' as const,text:'我说自己的事',conversationKind:'group' as const,groupContext:role('P_SELF',3000)}];
it('group API preserves three distinct authors, quotes, mentions, and self target',async()=>{
 const out=await analyzeApiInsights(config,{messages,targetIds:['b','s']},async(_config,request)=>{
  const payload=JSON.parse(request.prompt.split('\n').slice(1).join('\n'));
  assert.equal(new Set(payload.messages.map((row:any)=>row.groupContext.speaker)).size,3);
  assert.equal(payload.messages[1].groupContext.quote.speaker,'P_A');
  assert.match(request.system,/不证明受话人/);
  return {text:JSON.stringify({items:[{id:'b',emotion:'安慰',intent:'支持'},{id:'s',emotion:'平静',intent:'分享'}]})};
 });
 assert.deepEqual(out.insights.map(row=>row.id),['b','s']);
});
it('QQ self single-chat labels reach real API adapter without changing legacy caller guard',async()=>{
 const response=await analyzeApiInsights(config,{messages:[{id:'s',sender:'SELF',text:'我先列计划',conversationKind:'friend'}],targetIds:['s']},async()=>({text:'情感：平静\n意图：计划'}));
 assert.equal(response.insights[0]?.id,'s');
});
it('group API refuses unknown or missing result IDs instead of assigning by output order',async()=>{
 await assert.rejects(()=>analyzeApiInsights(config,{messages,targetIds:['b','s']},async()=>({text:JSON.stringify({items:[{id:'a',emotion:'开心',intent:'分享'},{id:'s',emotion:'自然',intent:'说明'}]})})));
 await assert.rejects(()=>analyzeApiInsights(config,{messages,targetIds:['b','s']},async()=>({text:'情绪：开心\n意图：分享'})));
});
const portrait={summary:'表达简洁',communication:'简洁',emotionExpression:'温和',interactionPreferences:'',topics:[],patterns:[],boundaries:[],uncertain:[],affinity:77,
 mbtiAxes:{EI:null,SN:null,TF:null,JP:null},traits:{socialEnergy:null,humor:null,composure:null,initiative:null,care:null,affection:null}};
it('group API portrait accepts one author including self, isolates background and clears affinity',async()=>{
 const result=await updateApiPortrait(config,null,messages.map(row=>({...row,target:row.id==='s'})),async(_config,request)=>{
  assert.match(request.system,/quote 是他人原话/);return {text:JSON.stringify(portrait)};
 });
 assert.equal(result.portrait.affinity,null);
 await assert.rejects(()=>updateApiPortrait(config,null,messages.map(row=>({...row,target:true})),async()=>({text:JSON.stringify(portrait)})));
});
it('local group batch only accepts one target author and time-neighborhood background',()=>{
 const rows=messages.map(row=>({...row,side:row.sender==='SELF'?'self' as const:'other' as const,target:row.id==='b'}));
 const pack=packMessageBatch({sessionId:'synthetic',conversationKind:'group',messages:rows},text=>text.length,8000);
 assert.match(pack.state,/"speaker":"P_A"/);assert.match(pack.state,/"speaker":"P_B"/);assert.match(pack.state,/"role":"QUOTED"/);
 assert.throws(()=>packMessageBatch({sessionId:'synthetic',conversationKind:'group',messages:rows.map(row=>({...row,target:true}))},text=>text.length,8000));
 const state=groupTargetState(rows.map(row=>({...row,groupContext:{...row.groupContext,time:row.id==='a'?0:1000000}})),1);
 assert.equal(JSON.parse(state.split('\n').slice(1).join('\n')).background.length,0);
});
