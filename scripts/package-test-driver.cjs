"use strict";
// Acceptance driver only. Existing Playwright drives the actual packaged app.
const fs=require('node:fs');
const path=require('node:path');
const net=require('node:net');
const assert=require('node:assert/strict');
const {spawn,spawnSync}=require('node:child_process');
const {once}=require('node:events');
const {createHash}=require('node:crypto');
const delay=ms=>new Promise(resolve=>setTimeout(resolve,ms));
const hash=value=>createHash('sha256').update(JSON.stringify(value)).digest('hex');
async function until(fn,seconds,label){
 const end=Date.now()+seconds*1000;
 while(Date.now()<end){if(await fn())return;await delay(750);}
 throw new Error('Timeout: '+label);
}

async function openPackage(packageRoot,playwrightPath,port=39429,visible=false,launchArgs=[]){
 const root=path.resolve(packageRoot),client=path.join(root,'resources/client');
 const {chromium}=require(path.resolve(playwrightPath));
 await new Promise((resolve,reject)=>{const server=net.createServer();server.once('error',reject);server.listen(port,'127.0.0.1',()=>server.close(resolve));});
 const env={...process.env,PATH:path.join(client,'runtime/node')+path.delimiter+path.join(process.env.SystemRoot,'System32'),
  WECHATVIBE_PYTHON:'missing-foreign-python',WECHATVIBE_NODE:'missing-foreign-node',
  PYTHONPATH:'missing-foreign-modules',NODE_PATH:'missing-foreign-node-modules',LAYA_MODEL_DIR:'missing-foreign-model'};
 delete env.ELECTRON_RUN_AS_NODE;
 const app=spawn(path.join(root,'QQVibe.exe'),['--remote-debugging-port='+port,'--remote-debugging-address=127.0.0.1',...launchArgs],
  {cwd:root,env,windowsHide:!visible,stdio:'ignore'});
 let browser,page;
 const session={root,client,env,app,errors:[]};
 session.close=async()=>{
  if(page&&!page.isClosed())await page.evaluate(()=>window.close()).catch(()=>{});
  if(app.exitCode===null&&app.signalCode===null)await Promise.race([once(app,'exit'),delay(12000)]);
  if(browser)await browser.close().catch(()=>{});
  if(app.exitCode===null&&app.signalCode===null){const kill=spawn('taskkill.exe',['/PID',String(app.pid),'/T','/F'],{windowsHide:true,stdio:'ignore'});await Promise.race([once(kill,'exit'),delay(10000)]);}
  const cleanupEnv={...env,QQVIBE_CLIENT_ROOT:client,WECHATVIBE_CLIENT_ROOT:client,
   QQVIBE_PYTHON:path.join(client,'runtime/python/python.exe'),WECHATVIBE_PYTHON:path.join(client,'runtime/python/python.exe'),
   QQVIBE_NODE:path.join(client,'runtime/node/node.exe'),WECHATVIBE_NODE:path.join(client,'runtime/node/node.exe')};
  for(const name of ['PYTHONPATH','PYTHONHOME','VIRTUAL_ENV','NODE_PATH','NODE_OPTIONS','LAYA_MODEL_DIR'])delete cleanupEnv[name];
  const cleanup=spawnSync(cleanupEnv.QQVIBE_PYTHON,[path.join(client,'scripts/start-real-client.py'),'--stop-owned-bridge','--json'],
   {cwd:client,env:cleanupEnv,windowsHide:true,timeout:35000,encoding:'utf8'});
  const probe=spawnSync(cleanupEnv.QQVIBE_PYTHON,['-I','-c',`import os,sys,json,psutil
from pathlib import Path
root=str(Path(sys.argv[1]).resolve()).lower()
remaining=[]
for proc in psutil.process_iter(['pid','name','cmdline']):
 try:
  if proc.pid!=os.getpid() and proc.info['name'].lower() in ('python.exe','node.exe') and any(root in str(arg).lower() for arg in proc.info['cmdline'] or []):remaining.append(proc.pid)
 except (psutil.AccessDenied,psutil.NoSuchProcess):pass
print(json.dumps({'remainingOwnedWorkers':len(remaining)}))`,client],
   {cwd:client,env:cleanupEnv,windowsHide:true,timeout:15000,encoding:'utf8'});
  const closed=probe.status===0&&JSON.parse(probe.stdout).remainingOwnedWorkers===0;
  return {bridgeCleanupExit:cleanup.status,ownedWorkersClosed:closed};
 };
 try{
  await until(async()=>{try{browser=await chromium.connectOverCDP('http://127.0.0.1:'+port,{timeout:1500});return true;}catch{return false;}},45,'owned Electron CDP');
  await until(()=>{page=browser.contexts().flatMap(c=>c.pages()).find(p=>/^http:\/\/127\.0\.0\.1:\d+\/$/.test(p.url()));return !!page;},45,'formal packaged page');
  await page.waitForSelector('#qqSyncPanel',{state:'attached'});
  page.on('pageerror',error=>session.errors.push(error.name));
  session.browser=browser;session.page=page;
  session.api=(url,body)=>page.evaluate(async({url,body})=>{
   const response=await fetch(url,body===undefined?{}:{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body)});
   const value=await response.json();
   if(!response.ok)throw new Error('API '+new URL(url,location.href).pathname+' HTTP '+response.status+' '+(value.code||value.error||''));
   return value;
  },{url,body});
  session.isolation=()=>{
   const result=spawnSync(path.join(client,'runtime/python/python.exe'),['-I','-c',`import sys,json,importlib,psutil
from pathlib import Path
client=Path(sys.argv[1]).resolve();runtime=client/'runtime/python'
modules=[importlib.import_module(name) for name in ['cryptography','zstandard','psutil','jieba','numpy','cv2','PIL','win32api']]
inside=lambda value,root:Path(value).resolve().is_relative_to(root)
live=[]
for record in (client/'QQVibeData/real-client-runtime').glob('bridge-*.json'):
 data=json.loads(record.read_text());pid=data.get('pid')
 if pid and psutil.pid_exists(pid):live.append(psutil.Process(pid))
workers=[child for parent in live for child in parent.children(recursive=True) if child.name().lower()=='node.exe']
print(json.dumps({'pythonImportsInsideRuntime':all(inside(module.__file__,runtime) for module in modules),
 'bridgePythonBundled':bool(live) and all(Path(p.exe()).resolve()==runtime/'python.exe' for p in live),
 'bridgeScriptsBundled':bool(live) and all(any(inside(arg,client/'bridge') for arg in p.cmdline()[1:] if arg.endswith('.py')) for p in live),
 'analysisNodeBundled':bool(workers) and all(Path(p.exe()).resolve()==client/'runtime/node/node.exe' for p in workers),
 'analysisScriptsBundled':bool(workers) and all(any(inside(arg,client/'bridge') for arg in p.cmdline()[1:] if arg.endswith('.ts')) for p in workers)}))`,client],
    {cwd:client,env,windowsHide:true,timeout:20000,encoding:'utf8'});
   assert.equal(result.status,0,'independent runtime probe');
   return JSON.parse(result.stdout);
  };
  return session;
 }catch(error){await session.close();throw error;}
}
module.exports={openPackage,until,delay,hash};
