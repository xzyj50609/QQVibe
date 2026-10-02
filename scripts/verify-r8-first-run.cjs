"use strict";
// Actual extracted ZIP, bundled service/SDK, clean data, loopback fake API only.
const fs=require('node:fs'),path=require('node:path'),http=require('node:http'),assert=require('node:assert/strict');
const {openPackage,until}=require('./package-test-driver.cjs');
const [root,playwright,output,variant='standard',existingModel]=process.argv.slice(2);
if(!output)throw new Error('package playwright output standard|full [existing-model]');
const client=path.resolve(root,'resources/client');
const report={evidence:'actual-ZIP-on-development-machine-clean-first-run',variant,
 realApiOllamaAccepted:false,crossMachineAccepted:false,cloudCalls:0,qqConnected:false,
 nativeModelPickerAccepted:false,validationPassed:false};
let session,requests=0;
const fake=http.createServer((req,res)=>{
 let body='';req.on('data',b=>body+=b);req.on('end',()=>{
  requests++;res.setHeader('Content-Type','application/json');
  if(req.url==='/v1/models')return res.end(JSON.stringify({object:'list',data:[{id:'r8-synthetic-model',object:'model',owned_by:'synthetic'}]}));
  if(req.url==='/v1/chat/completions')return res.end(JSON.stringify({id:'synthetic',object:'chat.completion',created:1,model:'r8-synthetic-model',
   choices:[{index:0,message:{role:'assistant',content:'{"ok":true}'},finish_reason:'stop'}],usage:{prompt_tokens:1,completion_tokens:1,total_tokens:2}}));
  res.writeHead(404);res.end('{}');
 });
});
async function settings(page){
 const setup=page.getByRole('button',{name:'首次设置：连接 QQ、选择模型与会话',exact:true});
 await until(async()=>await page.locator('#settingsModal').isVisible()||await setup.isVisible()||
  !(await page.locator('#startupOverlay').isVisible()),20,'startup settings entry ready');
 if(!(await page.locator('#settingsModal').isVisible())){
  if(await setup.isVisible())await setup.click();else await page.locator('#btnSettings').click();
 }
 await page.waitForSelector('#settingsModal',{state:'visible'});
 await until(()=>page.locator('#selectModelSource').isEnabled(),15,'model settings enabled');
}
async function main(){
 try{
  assert.equal(fs.existsSync(path.join(client,'QQVibeData')),false,'clean data required');
  await new Promise(resolve=>fake.listen(0,'127.0.0.1',resolve));
  const base='http://127.0.0.1:'+fake.address().port+'/v1';
  session=await openPackage(root,playwright,39438,true);
  let {page,api}=session;
  assert.equal(await page.evaluate(()=>window.desktopHost.getAppVersion()),'0.1.0');
  assert.equal((await api('/api/qq/connection')).enabled,false,'no implicit QQ read');
  assert.equal((await api('/api/accounts')).accounts.length,0);
  report.firstWindowOpened=true;report.firstRunNoSavedAccounts=true;
  await settings(page);report.firstSettingsReachable=true;
  const local=await api('/api/local-model');
  if(variant==='standard'){
   assert.equal(local.state,'missing');assert.equal(fs.existsSync(path.join(client,'.models')),false);
   await until(()=>page.locator('#localModelStatus').textContent().then(x=>x==='未安装'),15,'missing model visible');
   assert.equal(await page.locator('#btnDownloadLocalModel').isEnabled(),true);
   assert.equal(await page.locator('#btnChooseLocalModelDir').isEnabled(),true);
   report.missingModelVisible=true;report.downloadAndLocateControlsReachable=true;
  }else{assert.equal(local.state,'ready');assert.equal(local.source,'bundled');report.bundledModelReady=true;}
  await page.locator('#selectModelSource').selectOption('api');
  await page.waitForSelector('#apiModelSettings',{state:'visible'});
  await page.locator('#selectApiProtocol').selectOption('chat_completions');
  await page.locator('#inputApiBaseUrl').fill(base);
  await page.locator('#inputApiKey').fill('synthetic-r8-not-a-real-key');
  await page.locator('#inputApiModelId').fill('r8-synthetic-model');
  await page.locator('#inputApiContextTokens').fill('8192');
  await page.locator('#btnFetchApiModels').click();
  await until(()=>page.locator('#apiModelCount').textContent().then(x=>/1 个模型/.test(x)),25,'fake API model discovery');
  await page.locator('#btnTestApiModel').click();
  await until(()=>page.locator('#apiModelTestStatus').textContent().then(x=>/连接成功/.test(x)),25,'fake API SDK probe');
  await page.locator('#btnActivateApi').click();
  await until(async()=> (await api('/api/model-source')).mode==='api',25,'API settings persisted');
  report.apiSettingsWithoutLayaPassed=variant==='standard';report.fakeBackendFlowPassed=true;
  const runtime=await api('/api/runtime');report.runtimeInApiMode=runtime.state||runtime.status;
  await page.screenshot({path:output+'.settings.png'});
  Object.assign(report,await session.close());session=null;
  assert.equal(report.ownedWorkersClosed,true);
  session=await openPackage(root,playwright,39438,true);({page,api}=session);
  const saved=await api('/api/model-source');assert.equal(saved.mode,'api');assert.equal(saved.api.model,'r8-synthetic-model');
  if(variant==='standard')assert.equal((await api('/api/local-model')).state,'missing');
  report.apiOnlyReopenPassed=true;
  await settings(page);assert.equal(await page.locator('#apiModelSettings').isVisible(),true);
  if(existingModel){
   await page.locator('#selectModelSource').selectOption('local');
   const chosen=await api('/api/local-model',{path:path.resolve(existingModel)});
   assert.equal(chosen.state,'ready');assert.equal(chosen.source,'custom');
   await page.reload();await settings(page);await page.locator('#selectModelSource').selectOption('local');
   await until(()=>page.locator('#localModelStatus').textContent().then(x=>x==='自选模型已就绪'),20,'selected model status in real UI');
   report.existingModelBackendSelectionPassed=true;
   report.existingModelUiStatusPassed=true;
   report.existingModelSelectionMethod='real-backend-API; native-picker-recorded-separately';
  }
  assert.equal(session.errors.length,0);report.rendererErrors=0;
  report.fakeBackendRequests=requests;assert.ok(requests>=2);
  report.manifest=JSON.parse(fs.readFileSync(path.join(client,'release-manifest.json'),'utf8'));
  report.validationPassed=true;
 }catch(error){report.failure={name:error.name,message:error.message};throw error;}
 finally{
  if(session)Object.assign(report,await session.close());
  await new Promise(resolve=>fake.close(resolve));
  fs.writeFileSync(output,JSON.stringify(report,null,2)+'\n');
  console.log(JSON.stringify({variant,validationPassed:report.validationPassed,ownedWorkersClosed:report.ownedWorkersClosed}));
 }
}
main().catch(e=>{console.error(e.name+': '+e.message);process.exitCode=1;});
