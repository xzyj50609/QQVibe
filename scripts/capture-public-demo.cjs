"use strict";

// Captures the shipped renderer, served by the real QQ HTTP backend, using an
// isolated SQLite library of invented chats and the existing synthetic model
// fixture. No real QQ account, QCE, cloud model, credentials or user data is read.
// Images demonstrate the UI only; they are not model-quality evidence.
const fs = require("node:fs");
const path = require("node:path");
const assert = require("node:assert/strict");
const { spawn, spawnSync } = require("node:child_process");
const { once } = require("node:events");
const { createHash } = require("node:crypto");

const root = path.resolve(__dirname, "..");
const args = process.argv.slice(2);
const playwrightPath = args[0] || process.env.QQVIBE_PLAYWRIGHT;
if (!playwrightPath) throw new Error("Pass the already installed Playwright module path as the first argument.");
const python = args[1] || path.join(root, ".venv", "Scripts", "python.exe");
const modelDir = args[2] ? path.resolve(args[2]) : "";
const { chromium } = require(path.resolve(playwrightPath));
const stamp = new Date().toISOString().replace(/[:.]/g, "-");
const work = path.join(root, ".zwork", "public-demo-" + stamp);
const images = path.join(root, "docs", "public", "images");
const wait = ms => new Promise(resolve => setTimeout(resolve, ms));

const fixture = String.raw`
import copy,json,os,sys,time
from pathlib import Path
from http.server import ThreadingHTTPServer
import threading
repo=Path(sys.argv[1]).resolve(); work=Path(sys.argv[2]).resolve()
sys.path.insert(0,str(repo/'bridge')); os.environ['QQVIBE_PRODUCT']='qq'
from qq_identity import account_key
from qq_message_store import QQMessageStore,database_path
from qq_source import QQSource
from product_profile import load_product
from result_store import ResultStore
from backend_service import Backend
from conversation_selection import ConversationSelectionStore
from model_source import ModelSourceStore
from qq_account_api import QQAccountAPI
from real_http import make_handler,build_account_api
from test_qq_message_store import record,CONV
from test_qq_analysis_revision import Analyzer
from profile_signals import keyword_counts
from local_model_source import ModelSource

keyword_counts(['合成界面演示'])
account=account_key('10001'); group='g:20001'
store=QQMessageStore(database_path(account,work))
store.ensure_conversation(account,CONV,peer_uid='u_peer_synth01',display_name='小林（演示）')
store.ensure_conversation(account,group,kind='group',group_code='20001',display_name='学习小组（演示）')
people=[{'uin':'10001','uid':'u_synthetic_self','nick':'演示我','cardName':'演示我'},
        {'uin':'10002','uid':'u_peer_synth01','nick':'小林（演示）','cardName':'小林（演示）'},
        {'uin':'10003','uid':'u_peer_synth02','nick':'小夏（演示）','cardName':'小夏（演示）'}]
store.set_members(account,group,people,source='synthetic-demo')
BASE=1790886600000
single=[('peer','今晚一起复习数学吗？'),('self','可以，我把笔记整理好。'),
        ('peer','太好了，我最近在练函数题。'),('self','先做十道题，再一起对答案。'),
        ('peer','这道题我想试试换元法。'),('self','好，写下思路比只看答案有用。'),
        ('peer','有道理，明天继续！'),('self','没问题，今天辛苦啦。')]
rows=[]
for i,(direction,text) in enumerate(single):
    person=people[0] if direction=='self' else people[1]
    row=record('demo-single-'+str(i),time_ms=BASE+i*60000,text=text,direction=direction,sender_uin=person['uin'])
    row.update(sender_uid=person['uid'],send_type='2' if direction=='self' else '0')
    rows.append(row)
store.ingest(account,CONV,rows)
group_rows=[(0,'今天的复习安排发在这里，大家量力而行。'),
            (1,'我先做数学题，晚上分享错题。'),(2,'我准备整理英语笔记。'),
            (0,'好，我们八点一起交流。'),(1,'有一题终于做出来了，开心！'),
            (2,'太棒了，我也把单词复习完了。'),(0,'今天都很认真，记得休息。'),
            (1,'收到，明天继续加油。'),(2,'明天见！')]
rows=[]
for i,(person_index,text) in enumerate(group_rows):
    person=people[person_index]; direction='self' if person_index==0 else 'peer'
    row=record('demo-group-'+str(i),time_ms=BASE+i*60000,text=text,direction=direction,sender_uin=person['uin'])
    row.update(conversation_key=group,sender_uid=person['uid'],send_type='2' if direction=='self' else '0')
    rows.append(row)
store.ingest(account,group,rows)
source=QQSource(uin='10001',store=store,root=work,profile=load_product('qq'))
analyzer=Analyzer()
original_fine=analyzer.analyze; original_batch=analyzer.analyze_batch
def fine(session,context,target,**kwargs):
    result=original_fine(session,context,target,**kwargs)
    if session.endswith(':'+group) or next(row for row in context if row['id']==target)['side']=='self': result['score']=None
    return result
def batch(session,payload,context):
    result=original_batch(session,payload,context)
    result['consumed']=[{'start':row['offset'],'end':len(row['text']),'complete':True} for row in payload]
    if session.endswith(':'+group) or all(row['side']=='self' for row in payload if row['target']): result['result']['score']=None
    return result
analyzer.analyze=fine; analyzer.analyze_batch=batch
# Cached fixture results are prepared before opening the application. They use
# the existing unit-test Analyzer and cannot represent real Laya inference.
analyzer.local_model_status=lambda:{'state':'ready','source':'synthetic-fixture','path':''}
analyzer.runtime_status=lambda:{'requestedProvider':'cpu','modelProvider':None,'status':'missing'}
(work/'QQVibeData'/'real-client-data').mkdir(parents=True,exist_ok=True)
results=ResultStore(work/'QQVibeData'/'real-client-data'/'synthetic-demo.sqlite3')
backend=Backend(source,analyzer=analyzer,store_factory=lambda *_:results,
    model_source_store=ModelSourceStore(work/'QQVibeData'/'real-client-runtime'/'models.json',root=work),
    selection_store=ConversationSelectionStore(work/'QQVibeData'/'real-client-data'))
def drain():
    with backend.tasks.all_tasks_done:
        if not backend.tasks.all_tasks_done.wait_for(lambda:backend.tasks.unfinished_tasks==0,timeout=20):
            raise RuntimeError('synthetic worker deadline')
for user in [CONV,group]:
    backend.set_conversation_selected(account,user,True)
    backend.start(user,'recent',40,expected_account=account); drain()
    if len(backend.analysis(user,40)['results'])!=(8 if user==CONV else 9):
        raise RuntimeError('synthetic labels did not match invented text count: '+json.dumps(backend.analysis(user,40)['job']))
for user,member in [(CONV,None),(CONV,'self'),(group,'uin:10001'),(group,'uin:10002'),(group,'uin:10003')]:
    backend.profile(user,member); drain()
# The public screenshots show the true fixture environment: optional existing
# model files, no real inference runtime, and no QCE connection. Cached offline
# UI remains available, exactly as in the product.
analyzer.model={'state':'missing','provider':None}
local_source=ModelSource(work)
if len(sys.argv)>3 and sys.argv[3]: local_source.select(sys.argv[3])
analyzer.local_model_status=local_source.status
analyzer.runtime_status=lambda:{'requestedProvider':'cpu','modelProvider':None,'status':'idle'}
# This is the same static release feature flag that enables the QQ controls.
# It does not claim an active source connection; no connection is configured.
api=build_account_api(backend,load_product('qq'),work,live_connector_validated=True); api.observe(source.sessions())
server=ThreadingHTTPServer(('127.0.0.1',0),make_handler(backend,api))
empty_root=work/'empty-first-run'
empty_source=QQSource(root=empty_root,profile=load_product('qq'))
empty_analyzer=Analyzer(); empty_analyzer.model={'state':'missing'}
empty_analyzer.local_model_status=ModelSource(empty_root).status
empty_analyzer.runtime_status=lambda:{'requestedProvider':'cpu','modelProvider':None,'status':'missing'}
empty_backend=Backend(empty_source,analyzer=empty_analyzer,
    model_source_store=ModelSourceStore(empty_root/'QQVibeData'/'real-client-runtime'/'models.json',root=empty_root),
    selection_store=ConversationSelectionStore(empty_root/'QQVibeData'/'real-client-data'))
empty_api=build_account_api(empty_backend,load_product('qq'),empty_root,live_connector_validated=True)
empty_server=ThreadingHTTPServer(('127.0.0.1',0),make_handler(empty_backend,empty_api))
for item in [server,empty_server]: threading.Thread(target=item.serve_forever,daemon=True).start()
(work/'server.json').write_text(json.dumps({'port':server.server_port,'emptyPort':empty_server.server_port,
    'synthetic':True,'modelFilesValidated':local_source.status()['state']=='ready','conversations':[CONV,group]}),encoding='utf-8')
print('Synthetic public-demo backend ready',flush=True)
try:
    deadline=time.monotonic()+180
    while time.monotonic()<deadline and not (work/'stop').exists(): time.sleep(.2)
finally:
    for item in [server,empty_server]: item.shutdown(); item.server_close()
    empty_api.sync_manager.close(); api.sync_manager.close()
    empty_backend.shutdown(); empty_source.close(); backend.shutdown(); source.close(); store.close()
`;

async function main() {
  fs.mkdirSync(work, { recursive: true });
  fs.mkdirSync(images, { recursive: true });
  fs.writeFileSync(path.join(work, "fixture.py"), fixture);
  const stdout = fs.openSync(path.join(work, "fixture-stdout.log"), "w");
  const stderr = fs.openSync(path.join(work, "fixture-stderr.log"), "w");
  const server = spawn(python, [path.join(work, "fixture.py"), root, work, modelDir], {
    cwd: work, windowsHide: true, stdio: ["ignore", stdout, stderr],
    env: { ...process.env, QQVIBE_PRODUCT: "qq", PYTHONUTF8: "1" }
  });
  let browser, captured = false;
  const report = { synthetic: true, realQCEUsed: false, realModelUsed: false,
    actualRenderer: "chatui", actualBackend: "real_http/QQSource/SQLite",
    cachedAnalysisFixture: "bridge/test_qq_analysis_revision.py:Analyzer",
    screenshots: [], externalRequests: 0, defaultLight: false, pageErrors: [] };
  try {
    const deadline = Date.now() + 45000;
    while (!fs.existsSync(path.join(work, "server.json"))) {
      if (server.exitCode !== null) throw new Error("Synthetic server exited: " + fs.readFileSync(path.join(work, "fixture-stderr.log"), "utf8"));
      if (Date.now() > deadline) throw new Error("Synthetic server startup deadline");
      await wait(200);
    }
    const { port, emptyPort, modelFilesValidated } = JSON.parse(fs.readFileSync(path.join(work, "server.json"), "utf8"));
    report.modelFilesValidated = modelFilesValidated;
    const origin = "http://127.0.0.1:" + port;
    const emptyOrigin = "http://127.0.0.1:" + emptyPort;
    browser = await chromium.launch({ headless: true });
    const context = await browser.newContext({ viewport: { width: 1440, height: 1000 }, deviceScaleFactor: 1 });
    await context.route("**/*", route => {
      const url = route.request().url();
      if (url.startsWith(origin + "/") || url.startsWith(emptyOrigin + "/") || url.startsWith("data:")) return route.continue();
      report.externalRequests++;
      return route.abort();
    });
    const page = await context.newPage();
    page.setDefaultTimeout(15000);
    page.on("pageerror", error => report.pageErrors.push(error.name + ": " + error.message));
    await page.goto(origin, { waitUntil: "networkidle", timeout: 30000 });
    await page.waitForSelector("#sessionList .session-item", { timeout: 25000 });
    await page.waitForFunction(() => document.body.classList.contains("theme-light"));
    report.defaultLight = true;
    const single = "u:u_peer_synth01", group = "g:20001";
    const select = async id => {
      await page.locator('#sessionList .session-item[data-id="' + id + '"]').click();
      await page.waitForFunction(expected => chatState.currentUser === expected, id);
      await page.waitForTimeout(1200);
    };
    const shot = async (filename, description) => {
      assert.equal(await page.locator("#qqSyncToken").inputValue(), "");
      assert.equal(await page.locator("#inputApiKey").inputValue(), "");
      await page.screenshot({ path: path.join(images, filename), animations: "disabled" });
      const bytes = fs.readFileSync(path.join(images, filename));
      report.screenshots.push({ filename, description, width: 1440, height: 1000,
        sha256: createHash("sha256").update(bytes).digest("hex") });
    };
    await select(single);
    await shot("overview.png", "单聊消息与情绪/意图标签；纯合成演示缓存，离线状态");
    await page.locator("#btnToolbarPersona").click();
    await page.waitForSelector("#personaView.active");
    await page.waitForTimeout(1400);
    await shot("single-chat.png", "单聊人物画像入口、本人/对方切换与合成统计");
    await page.locator("#navChat").click();
    await select(group);
    await page.locator("#btnToolbarPersona").click();
    await page.waitForSelector("#personaView.active");
    await page.getByRole("button", { name: "选择成员", exact: true }).click();
    await page.getByRole("searchbox", { name: "搜索群成员", exact: true }).fill("小林");
    await page.locator("#groupMemberTabs .member-option").first().click();
    await page.waitForTimeout(1600);
    await shot("group-chat.png", "群聊具体成员画像、统计与实验候选说明");
    await page.locator("#btnSettings").click();
    await page.waitForSelector("#settingsModal.show");
    await page.waitForTimeout(800);
    await shot("settings.png", modelFilesValidated ?
      "浅色设置、本地Laya/CPU入口；模型文件实际校验，推理运行时尚未加载" :
      "浅色设置、本地Laya/CPU入口及真实缺模型状态");
    // Use the product's existing expanded settings state so both explicit
    // conversation buttons fit. This is a real UI action, not altered CSS.
    await page.locator("#btnManageConversations").click();
    await page.locator("#qqSyncPanel").evaluate(node => { node.open = true; node.scrollIntoView({ block: "start" }); });
    await page.waitForTimeout(500);
    await shot("settings-connection.png", "连接本机QQ/QCE及明确添加单聊、群聊入口；未连接状态");
    await page.locator("#btnCloseSettings").click();
    await page.locator("#navChat").click();
    await select(group);
    await shot("group-labels.png", "合成群聊正文与作者、实验情绪/意图标签");
    await page.goto(emptyOrigin, { waitUntil: "networkidle", timeout: 30000 });
    await page.getByRole("button", { name: "首次设置：连接 QQ、选择模型与会话", exact: true }).waitFor();
    await page.waitForTimeout(600);
    await shot("first-run.png", "实际空数据目录首次启动与首次设置入口，无模型/QCE连接");
    assert.deepEqual(report.pageErrors, []);
    captured = true;
  } finally {
    if (browser) await browser.close().catch(() => {});
    fs.writeFileSync(path.join(work, "stop"), "stop\n");
    await Promise.race([once(server, "exit"), wait(7000)]);
    if (server.exitCode === null && server.signalCode === null)
      spawnSync("taskkill.exe", ["/PID", String(server.pid), "/T", "/F"], { windowsHide: true, timeout: 10000, stdio: "ignore" });
    fs.closeSync(stdout); fs.closeSync(stderr);
    if (captured) assert.equal(server.exitCode, 0, "Owned synthetic server must exit cleanly");
  }
  report.fixtureExitCode = server.exitCode;
  report.validationPassed = captured;
  fs.writeFileSync(path.join(work, "capture-report.json"), JSON.stringify(report, null, 2) + "\n");
  console.log(JSON.stringify({ output: images, report: path.join(work, "capture-report.json"), ...report }));
}
main().catch(error => { console.error(error.stack); process.exitCode = 1; });
