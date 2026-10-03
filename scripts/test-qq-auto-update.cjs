"use strict";
const assert = require("node:assert/strict");
const fs = require("node:fs");
const os = require("node:os");
const path = require("node:path");
const crypto = require("node:crypto");
const { execFileSync } = require("node:child_process");
const updater = require("./real-client-update.cjs");
const { UpdatePreferences, updateConfig } = require("./real-client-update-config.cjs");
const { createUpdateController } = require("./real-client-update-controller.cjs");
const { runOperation, recoverOperation } = require("./real-client-update-helper.cjs");
const profile = require("./product-identity.cjs").productProfile("qq");
const sha = value => crypto.createHash("sha256").update(value).digest("hex");
const put = (file, value = "synthetic") => { fs.mkdirSync(path.dirname(file), { recursive: true }); fs.writeFileSync(file, value); };
let count = 0;
function check(value, message) { assert.ok(value, message); count++; }
function removeOwnedTree(root) {
  for (const entry of fs.readdirSync(root, {withFileTypes: true})) {
    const child = path.join(root, entry.name);
    if (entry.isDirectory() && !entry.isSymbolicLink()) removeOwnedTree(child); else fs.unlinkSync(child);
  }
  fs.rmdirSync(root);
}
function release(version, prerelease = true, bodies = new Map()) {
  const base = updateConfig(profile).releasesUrl;
  return {tag_name: "v" + version, html_url: base + "/tag/v" + version, draft: false, prerelease,
    body: "Test notes <script>unsafe</script>", assets: [...bodies].map(([name, bytes]) => ({name, size: bytes.length,
      digest: "sha256:" + sha(bytes), state: "uploaded", browser_download_url: base + "/download/v" + version + "/" + name}))};
}
function client(root, version, data = false) {
  for (const item of ["QQVibe.exe", "resources/app.asar", "resources/client/runtime/python/python.exe",
    "resources/client/runtime/node/node.exe", "resources/client/scripts/start-real-client.py",
    "resources/client/scripts/real-client-update-helper.cjs", "resources/client/scripts/real-client-update-extract.py",
    "resources/client/scripts/update-signing.pub", "resources/client/scripts/qq-update-signing.pub",
    "resources/client/scripts/real-client-update-config.cjs", "resources/client/bridge/chat_server.py", "resources/client/chatui/index.html"]) put(path.join(root,item), item.endsWith(".exe") ? "MZsynthetic" : "synthetic");
  put(path.join(root,"resources/client/package.json"),JSON.stringify({name:"qqvibe-runtime",version}));
  put(path.join(root,"resources/client/scripts/product-identity.json"),fs.readFileSync(path.join(__dirname,"product-identity.json")));
  if (data) put(path.join(root,"resources/client/QQVibeData/chat.db"),"private synthetic chat");
}
async function main() {
  const temp = fs.realpathSync.native(fs.mkdtempSync(path.join(os.tmpdir(),"qqvibe-update-")));
  try {
    const prefs = new UpdatePreferences(temp,profile);
    check(prefs.get().channel === "stable" && prefs.due(),"new formal-release users stay on the stable channel");
    prefs.set({autoCheck:false,autoDownload:true}); check(!prefs.due(),"disabled auto check stays disabled");
    prefs.set({autoCheck:true,lastCheckedAt:Date.now()}); check(!new UpdatePreferences(temp,profile).due(),"check time persists across restarts");
    assert.throws(()=>prefs.set({channel:"evil"})); count++;
    assert.throws(()=>updateConfig({...profile,updateRepository:"tswawa/WechatVibe"})); count++;
    const candidateRoot=path.join(temp,"source","win-unpacked"); client(candidateRoot,"0.1.4");
    const archivePath=path.join(temp,"QQVibe-0.1.4-windows-x64.zip");
    execFileSync("python",["-c","from pathlib import Path; import sys,zipfile; root=Path(sys.argv[1]); z=zipfile.ZipFile(sys.argv[2],'w',zipfile.ZIP_DEFLATED); [z.write(p,p.relative_to(root.parent).as_posix()) for p in root.rglob('*') if p.is_file()]; z.close()",candidateRoot,archivePath],{timeout:20000});
    const zip=fs.readFileSync(archivePath), keys=crypto.generateKeyPairSync("ed25519");
    const manifest=Buffer.from(JSON.stringify({schema:1,product:"QQVibe",version:"0.1.4",platform:"win32",arch:"x64",layout:"win-unpacked",dataSchema:"real-client-v1",archive:{name:path.basename(archivePath),size:zip.length,sha256:sha(zip)}}));
    const signature=crypto.sign(null,manifest,keys.privateKey);
    const sums=Buffer.from(`${sha(zip)}  ${path.basename(archivePath)}\n${sha(manifest)}  update-manifest.json\n${sha(signature)}  update-manifest.sig\n`);
    const bodies=new Map([[path.basename(archivePath),zip],["update-manifest.json",manifest],["update-manifest.sig",signature],["UPDATE-SHA256SUMS.txt",sums]]);
    const latest=release("0.1.4",true,bodies);
    let list=[release("0.1.3",false),latest,release("0.1.2",true)];
    let archiveRequests=0;
    const fetchImpl=async url=>{
      check(!url.includes("tswawa"),"QQ never reaches upstream WeChat feed");
      if(url.startsWith("https://api.github.com/repos/xzyj50609/QQVibe/releases?"))return new Response(JSON.stringify(list));
      const name=decodeURIComponent(url.split("/").at(-1));
      if(name.endsWith(".zip"))archiveRequests++;
      check(bodies.has(name),"only declared assets are fetched");
      const body=bodies.get(name); return new Response(body,{headers:{"content-length":String(body.length)}});
    };
    const options={profile,channel:"preview",publicKey:keys.publicKey,fetchImpl};
    const builderDir=path.join(temp,"builder"), builderAssets=path.join(builderDir,"assets");
    fs.mkdirSync(builderAssets,{recursive:true});
    for(const file of ["build-update-manifest.cjs","real-client-update-config.cjs"]) fs.copyFileSync(path.join(__dirname,file),path.join(builderDir,file));
    put(path.join(builderDir,"qq-update-signing.pub"),keys.publicKey.export({type:"spki",format:"pem"}));
    const syntheticKey=path.join(builderDir,"test-private.pem");put(syntheticKey,keys.privateKey.export({type:"pkcs8",format:"pem"}));
    fs.copyFileSync(archivePath,path.join(builderAssets,path.basename(archivePath)));
    const builderOutput=execFileSync(process.execPath,[path.join(builderDir,"build-update-manifest.cjs"),"--product","QQVibe","--version","0.1.4","--archive",path.join(builderAssets,path.basename(archivePath)),"--output-dir",builderAssets],{env:{...process.env,QQVIBE_UPDATE_SIGNING_KEY_FILE:syntheticKey},encoding:"utf8",timeout:10000});
    check(JSON.parse(builderOutput).assets.includes("UPDATE-SHA256SUMS.txt"),"builder advertises QQ checksums separately from other release attachments");
    const builtManifest=fs.readFileSync(path.join(builderAssets,"update-manifest.json"));
    check(crypto.verify(null,builtManifest,keys.publicKey,fs.readFileSync(path.join(builderAssets,"update-manifest.sig"))),"QQ builder signs with corresponding pinned key");
    check((await updater.checkForUpdates("0.1.3",options)).latestVersion==="0.1.4","preview detects prerelease by version, not API list order");
    check(archiveRequests===0,"checks download metadata only");
    check((await updater.checkForUpdates("0.1.3",{...options,channel:"stable"})).status==="current","stable excludes newer preview");
    check((await updater.checkForUpdates("0.1.5",options)).status==="current","no downgrade");
    list=[];check((await updater.checkForUpdates("0.1.3",options)).status==="no-release","empty feed is not latest");list=[latest];
    check((await updater.checkForUpdates("0.1.3",{...options,publicKey:crypto.generateKeyPairSync("ed25519").publicKey})).status==="invalid-release","wrong signature rejected");
    check((await updater.checkForUpdates("0.1.3",{...options,fetchImpl:async()=>{throw Object.assign(new Error("offline"),{code:"ENOTFOUND"});}})).status==="offline","network failure distinct from current");
    const installRoot=path.join(temp,"installed");client(installRoot,"0.1.3",true);
    const staged=await updater.downloadAndStageUpdate("0.1.3",installRoot,()=>{}, {...options,pythonExe:"python",extractorPath:path.join(__dirname,"real-client-update-extract.py")});
    check(fs.existsSync(path.join(staged.candidatePath,"QQVibe.exe")),"signed QQ ZIP extracts with QQ executable");
    check(fs.readFileSync(path.join(installRoot,"resources/client/QQVibeData/chat.db"),"utf8")==="private synthetic chat","staging leaves installed data intact");
    let quit=0;
    const controller=createUpdateController({app:{getVersion:()=>"0.1.3",isPackaged:false},root:path.join(installRoot,"resources/client"),profile,splitDownload:true,port:45678,instanceId:"a".repeat(64),quit:()=>quit++,checkImpl:async()=>({status:"available",latestVersion:"0.1.4"}),stageImpl:async()=>staged});
    await controller.check();check((await controller.begin()).phase==="ready" && quit===0,"download never restarts or installs");
    check((await controller.check()).phase==="ready","recheck retains staged update");
    const helperDir=path.join(staged.workDir,"helper");put(path.join(helperDir,"node.exe"));put(path.join(helperDir,"real-client-update-helper.cjs"));
    const op={schema:1,action:"install",productName:"QQVibe",installRoot,candidatePath:staged.candidatePath,workDir:staged.workDir,expectedVersion:"0.1.4",previousVersion:"0.1.3",parentPid:99999,port:45678,instanceId:"a".repeat(64)};
    const operationFile=path.join(staged.workDir,"install-test.json");put(operationFile,JSON.stringify(op));
    const calls=[];
    const deps={waitParent:async()=>{},portVacant:async()=>true,healthy:async()=>true,startBridge:async()=>({created:true}),
      launchValidation:async()=>({pid:77777}),waitForValidation:async()=>{},stopValidation:async()=>{},
      launchFinalClient:async()=>({pid:88888,unref(){}}),waitForFinal:async()=>{},stopFinal:async()=>{},
      launchClient:async(_root,_spawn,product)=>{calls.push(product);return {pid:77777};},
      stopOwnedBridge:()=>{},setRunOnce:()=>{},clearRunOnce:()=>{},runOnceAbsent:()=>true};
    check(await runOperation(op,operationFile,deps)==="succeeded","QQ helper completes filesystem swap with mocked process boundaries");
    check(fs.readFileSync(path.join(installRoot,"resources/client/QQVibeData/chat.db"),"utf8")==="private synthetic chat","chat bytes survive installation");
    check(fs.existsSync(path.join(staged.workDir,"backup/QQVibe.exe")),"QQ backup retained");
    put(path.join(installRoot,"resources/client/QQVibeData/chat.db"),"new synthetic chat after upgrade");
    const rollback={...op,action:"rollback",candidatePath:path.join(staged.workDir,"backup"),expectedVersion:"0.1.3",previousVersion:"0.1.4"};
    const rollbackFile=path.join(staged.workDir,"rollback-test.json");put(rollbackFile,JSON.stringify(rollback));
    check(await runOperation(rollback,rollbackFile,deps)==="succeeded","QQ rollback completes");
    check(JSON.parse(fs.readFileSync(path.join(installRoot,"resources/client/package.json"))).version==="0.1.3","rollback restores exact previous version");
    check(fs.readFileSync(path.join(installRoot,"resources/client/QQVibeData/chat.db"),"utf8")==="new synthetic chat after upgrade","explicit rollback preserves post-upgrade data");
    // Install a fresh candidate whose validation fails: old version must remain usable.
    const failedWork=path.join(temp,".wechatvibe-update-failed");client(path.join(failedWork,"win-unpacked"),"0.1.4");put(path.join(failedWork,"helper/node.exe"));put(path.join(failedWork,"helper/real-client-update-helper.cjs"));
    const failed={...op,workDir:failedWork,candidatePath:path.join(failedWork,"win-unpacked")};const failedFile=path.join(failedWork,"install-test.json");put(failedFile,JSON.stringify(failed));
    await assert.rejects(runOperation(failed,failedFile,{...deps,waitForValidation:async()=>{throw new Error("synthetic UI failure");}}),/synthetic UI failure/);count++;
    check(JSON.parse(fs.readFileSync(path.join(installRoot,"resources/client/package.json"))).version==="0.1.3","failed UI validation restores old QQ program");
    check(calls.includes("QQVibe"),"rollback relaunch selects QQ executable");
    check(fs.readFileSync(path.join(installRoot,"resources/client/QQVibeData/chat.db"),"utf8")==="new synthetic chat after upgrade","failed update preserves chat bytes");
  } finally {removeOwnedTree(temp);}
  console.log(`QQ_AUTO_UPDATE_CHECKS_PASSED (${count} assertions)`);
}
main().catch(error=>{console.error(error);process.exitCode=1;});
