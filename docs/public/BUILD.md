# 开发者构建与公开快照

普通用户使用程序 ZIP。本页仅用于源码构建。研发在原 QQVibe 仓库，公开候选是冻结提交白名单快照，不是第二套开发工程；修改回研发仓库，再重新导出。快照无 .git 历史，`PUBLIC_SOURCE_MANIFEST.json` 记录来源提交、精确文件与哈希，导出脚本不会创建远端或发布。

构建输入：Windows x64、Node.js **24.11.1** 官方 win-x64 runtime、其完整 LICENSE、Python **3.14**（本机已验证3.14.4）、npm 及网络可获取锁定依赖。项目现有锁文件固定 Node 直接/传递依赖与18个 Python 包。Python/Node/Electron 自身不随源码候选复制，程序 ZIP 才包含运行环境。

```powershell
npm ci
py -3.14 -m venv .venv
.venv\Scripts\python.exe -m pip install --no-deps -r python-requirements.lock.txt
.venv\Scripts\python.exe scripts/build-prerequisites.py --source-node C:/your-node-24.11.1/node.exe --source-license C:/your-node-24.11.1/LICENSE
.venv\Scripts\python.exe scripts/build-portable-clean.py --without-model
```

`--no-deps` 因锁文件已完整列出选定运行闭包；不自动引入上游可选 GUI/OCR/media 依赖。首次 npm ci 的 postinstall 准备 Electron 44.4.3；请自行检查软件来源和安装权限。本轮没有新增依赖或执行新的外部安装。

构建输出由命令给出，在忽略的 `QQVibeData/portable-builds/build-*/release/win-unpacked`；manifest 检查精确 runtime/client/asar 文件和哈希。新目录保留失败现场，不覆写旧包。标准包无需模型目录。若需要完整包，先自行准备固定模型，再使用 `--models-dir C:/your-laya`。

```powershell
.venv\Scripts\python.exe scripts/build-windows-release.py --input QQVibeData/portable-builds/build-REPLACE/release/win-unpacked --version 0.1.0 --output-dir QQVibeData/releases/local-candidate --node-exe .local/build-runtime/node-24.11.1/node.exe
# 完整包：追加 --with-model
```

研发冻结候选构建可追加 `--source-commit <40位提交>`，逐项核对 Git 冻结输入并生成包内 `release-manifest.json`。无 Git 的公开快照也可按 PUBLIC_SOURCE_MANIFEST.json 中的来源提交传入 --source-commit，逐文件检查快照 SHA256；不伪造新来源提交。构建产物不会保证不同 Windows/Electron 打包环境字节相同，需保留源码提交、工具版本和ZIP SHA256。

合成检查：`npm run typecheck`、`npm run test:node`、`npm run test:scripts`，Python `scripts/run-python-tests.py`。Windows 本项目历史有阻塞型测试，研发环境继续用宿主 detached_gate 有界运行；其他环境也使用外部超时并保留日志。测试不需要真实聊天/账号/API Key，服务测试绑定本机假后端。实际包验收使用 `scripts/package-test-driver.cjs` 与 R8 首跑驱动，已有 Playwright 才能运行，不作为用户运行依赖。

公开导出使用 `scripts/export-public-source.py --commit <冻结提交> --output <新目录>`。其白名单每项必须审阅；自动扫描不能证明没有私人内容，导出目录亦需人工检查。`scripts/check-public-source.py <候选目录>` 检查清单、内部 import、staging 输入和文档链接；私有交接/报告不会复制。GitHub 仓库为 https://github.com/xzyj50609/QQVibe，GitHub 预发布按用户授权从独立公开快照初始化新历史并发布，原研发历史不上传。
