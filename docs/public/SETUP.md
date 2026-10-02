# 固定设置步骤

1. 获取 QQVibe 程序 ZIP（[预发布下载页](https://github.com/xzyj50609/QQVibe/releases/tag/v0.1.0-preview.1)，优先完整包），核对随交付清单 SHA256。完整解压到当前 Windows 用户可写的目录，再打开 `win-unpacked/QQVibe.exe`。无需开发环境；程序未签名，若系统拦截先核对来源和哈希，不关闭安全软件作为通用解决办法。
2. 点击「首次设置：连接 QQ、选择模型与会话」。模型与 QQ 连接可以分别设置，缺少 Laya 不阻止进入 API 设置。
3. 选一条模型路线：本地部署 →「下载模型」；或「选择目录」定位完整 Laya 文件夹；或切换「API 接入」。无独显先选 CPU，模型下载慢或失败可以重试或切换 API。
4. 本地模型下载的是固定上游模型资产，不是 WechatVibe 程序更新。来源为 [固定模型 ZIP](https://github.com/tswawa/WechatVibe/releases/download/v1.2.0/WechatVibe-Laya-model-v1.zip)，约 599 MB，SHA256 `abdd8bc363726e40a94e77750d09828cd681e08876e13c9be6ef776a3a24c4af`。程序按长度及 SHA256 检查下载和每个模型文件。自动下载不可达时，从 [固定 ONNX revision](https://huggingface.co/mizchi/laya-multilingual-onnx/tree/d9d003d543e63d6d3375c21d44624136bd1e0bad) 获取 `model.onnx`、`onnx_config.json`、`rl_agent_config.json`、`README.md` 和 `tokenizer/tokenizer.json`、`tokenizer/tokenizer_config.json`，保留子目录，再用「选择目录」。不使用随机 GGUF 或其他版本文件。已存在完整模型也可定位；模型目录须继续保留。
5. API：选择实际服务协议，填写 Base URL、Key（仅本机保存）、模型 ID 和上下文大小，获取模型列表或手填，测试连接后「保存并启用」。Chat Completions 对应界面「Completions（旧版）」。本地 Ollama 也在 API 接入内选择「Ollama 兼容」，默认本机地址通常为 `http://127.0.0.1:11434`；先自行准备服务及本地模型。连接成功不等于分析质量通过；本候选未完成真实 API/Ollama 验收。不使用其他项目凭证。云端计费独立于编程助手订阅；暂停前已发出的请求仍可能计费。
6. 准备 QCE **6.3.0**：从[固定发布页](https://github.com/shuakami/qq-chat-exporter/releases/tag/v6.3.0)下载 **`QQChatExporter-Installer-v6.3.0.exe`**（Windows x64 一键安装包，外部独立软件，不在 QQVibe ZIP 内），双击按其向导安装并用自己的 QQ 扫码或快捷登录。保持 QCE 运行；先在它的界面确认本人身份、可以列出会话。需要浏览器时打开它提供的本机入口，默认 `http://localhost:40653/qce`。QQVibe 不要求普通用户翻日志找 token。已有桌面 QQ 且使用 Framework 模式的人，从同一发布页下载 `NapCat-Framework-QCE-v6.3.0.zip`，完整解压，完全退出 QQ 后运行 `napiLoader.bat`，再登录自己的 QQ；这一路线与一键安装是二选一，不要求先安装 LiteLoader。独立/无登录的 standalone 浏览模式不能提供在线新消息。

   回 QQVibe 点「连接本机 QQ / QCE」，标准安装由程序发现受控配置；核对本人身份与 QQ/QCE 版本。非标准目录/端口才在高级设置填写本机地址及自己的访问 token，勿发送给反馈者。不要因为未验证新版本就盲目换 QQ；遇不兼容先导出匿名诊断。以上固定安装方法与文件名已核对上游6.3.0发布说明；在第二台电脑由非开发者安装/发现仍待R9，若标准安装不能自动连接，按实际失败记录反馈。
7. 仅添加获准读取的单聊或群聊。先浏览，再打开分析；群画像选择具体成员。首次添加按最近90天最多200条有界读取，partial/缺口按页面状态理解，不等于完整漫游历史。
8. 设置的「停止读取」「暂停分析」「取消分析」含义不同：停止后续读取 / 保留待算范围 / 撤销待算范围。已完成结果保留。首次走通后立即[备份](BACKUP-UPGRADE.md)，按[R9清单](R9-TRIAL.md)记录结果。

下载/模型路径状态应可见；无模型时本地分析暂不可用，界面和 API 设置仍可操作。不要把缺模型提示当作整个程序不可启动。
