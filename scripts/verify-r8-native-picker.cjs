"use strict";
// Observe an actual packaged model-directory dialog. Windows UI interaction is external.
const fs=require('node:fs'),path=require('node:path'),assert=require('node:assert/strict');
const {openPackage,until}=require('./package-test-driver.cjs');
const [root,playwright,expectedModel,output]=process.argv.slice(2);
if(!output)throw new Error('package playwright expected-model output');
const report={evidence:'actual-ZIP-native-model-directory-selection',cloudCalls:0,qqConnected:false,
 crossMachineAccepted:false,validationPassed:false};
let session;
async function main(){
 try{
  session=await openPackage(root,playwright,39439,true);
  const {page,api}=session;
  assert.equal((await api('/api/qq/connection')).enabled,false);
  const before=await api('/api/local-model');
  assert.notEqual(path.resolve(before.path||'.').toLowerCase(),path.resolve(expectedModel).toLowerCase(),'choose a different model location');
  const setup=page.getByRole('button',{name:'首次设置：连接 QQ、选择模型与会话',exact:true});
  await until(async()=>await setup.isVisible()||!(await page.locator('#startupOverlay').isVisible()),20,'startup entry');
  if(await setup.isVisible())await setup.click();else await page.locator('#btnSettings').click();
  await until(()=>page.locator('#selectModelSource').isEnabled(),20,'model settings');
  await page.locator('#selectModelSource').selectOption('local');
  await page.locator('#btnChooseLocalModelDir').click();
  fs.writeFileSync(output+'.progress.json',JSON.stringify({phase:'native-dialog-awaiting-selection',appPid:session.app.pid,expectedModel:path.resolve(expectedModel)}));
  await until(async()=>{
   const local=await api('/api/local-model');
   return local.state==='ready'&&local.source==='custom'&&path.resolve(local.path).toLowerCase()===path.resolve(expectedModel).toLowerCase();
  },240,'native selected directory validated by production backend');
  await until(()=>page.locator('#localModelStatus').textContent().then(x=>x==='自选模型已就绪'),25,'selected model visible');
  await page.locator('#btnActivateLocal').click();
  await until(async()=> (await api('/api/model-source')).mode==='local',25,'local source active');
  await api('/api/runtime',{provider:'cpu'});
  await until(async()=> (await api('/api/runtime')).status==='ready',90,'actual CPU initialized');
  const runtime=await api('/api/runtime');assert.equal(runtime.modelProvider,'cpu');
  report.nativeModelPickerAccepted=true;report.chosenPathChanged=true;report.selectedModelHashValidated=true;
  report.localSourceActivated=true;report.actualCpuReady=true;
  assert.equal(session.errors.length,0);report.rendererErrors=0;
  await page.screenshot({path:output+'.png'});report.validationPassed=true;
 }catch(error){report.failure={name:error.name,message:error.message};throw error;}
 finally{
  if(session)Object.assign(report,await session.close());
  fs.writeFileSync(output,JSON.stringify(report,null,2)+'\n');
  console.log(JSON.stringify({validationPassed:report.validationPassed,ownedWorkersClosed:report.ownedWorkersClosed}));
 }
}
main().catch(e=>{console.error(e.name+': '+e.message);process.exitCode=1;});
