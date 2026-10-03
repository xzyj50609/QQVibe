# 句豆 · ChatBean

**回看 QQ 聊天，找到旧消息，了解这段交流。**

句豆是一款 Windows 桌面应用，支持 QQ 单聊和群聊的消息检索、情绪与意图标签，以及本人、对方或群成员的人物画像。可以连接本机 QQ Chat Exporter（QCE），也可以导入已有 JSON / JSONL 记录；完整包提供的 Laya 模型在本机运行。

**1.0.0 是首个正式版本**，整合原 0.1.3 候选中的新界面、可拖动分栏、自动更新、QCE 检测工具和 JSON 导入修复。项目原名 QQVibe，程序名与数据目录保持兼容。

![句豆主界面：会话列表、QQ 风格消息气泡与情绪/意图标签](docs/public/images/overview.png)

*截图使用合成聊天和演示分析缓存，仅展示实际界面，不包含真实用户聊天，也不作为模型判断质量的证明。*

## 下载并开始使用

| 程序包 | 适合谁 |
| --- | --- |
| **[Windows x64 完整包（含 Laya）](https://github.com/xzyj50609/QQVibe/releases/download/v1.0.0/QQVibe-1.0.0-windows-x64-full.zip)** | 第一次使用，希望在本机分析 |
| [Windows x64 标准包（不含模型）](https://github.com/xzyj50609/QQVibe/releases/download/v1.0.0/QQVibe-1.0.0-windows-x64.zip) | 已有模型，或自行配置 API / Ollama |

[1.0.0 发布页与校验文件](https://github.com/xzyj50609/QQVibe/releases/tag/v1.0.0) · [更新日志](CHANGELOG.md) · [完整更新说明](docs/public/RELEASE-NOTES.md)

下载 ZIP 后**全部解压**，打开 `win-unpacked/QQVibe.exe`。程序包自带运行环境，无需安装 Git、Python 或 Node，也不需要注册 GitHub 账号。绿色「Code」按钮和「Source code」下载的是源码，不能直接当程序运行。

1. 打开程序，在首次设置中选择连接本机 QCE，或导入已有聊天 JSON。
2. 使用自己的 QQ 账号；连接在线 QCE 时，先核对账号再添加联系人或群聊。
3. 完整包推荐「本地部署 → CPU」；标准包可下载模型、选择已有目录或自行配置接口。
4. 从左侧打开聊天，顶部切换「聊天 / 画像」；需要找旧消息时打开「聊天记录」。

程序旁的 **使用说明.html** 是可离线阅读的图文手册。[首次设置](docs/public/SETUP.md) · [在线手册](docs/public/USER-GUIDE.md)

## 1.0.0 带来了什么

- **句豆新界面与图标**：浅蓝渐变、圆头像、QQ 风格消息和引用气泡；聊天与画像可在顶部切换，设置与表单统一。
- **会话栏宽度可调整**：拖动分界线调整宽度，关闭重开后记忆；双击恢复默认，也支持键盘调整。
- **快捷添加会话**：搜索旁的「+」可添加联系人或群聊，复用已有连接和身份核对流程。
- **标签详情**：新用户默认显示百分比，已有简洁显示选择保留。百分比是模型分数，不是判断准确率。
- **应用内更新**：自动检查、可选自动下载，下载后确认重启；签名和哈希校验、数据/模型保留及失败回退。
- **QCE 检测工具**：遇到二维码或连接问题，可一键检查安装、端口、启动与登录关口，有适用修复时保留配置备份后复检。
- **JSON 导入改进**：主流嵌套结构、JSONL、字段映射和分批读取；自动适配 QCE 6.3.0/6.3.1 的毫秒时间戳、消息编号与文本类型。

## 单聊与群聊

![单聊画像：分别查看本人和对方，样本不足时保持待判断](docs/public/images/single-chat.png)

![群成员画像：选择具体成员，查看他在当前群中的消息与统计](docs/public/images/group-chat.png)

画像只针对当前会话的已读范围。群标签、情绪、意图和 MBTI 都属于模型推测，需要结合原文理解；样本不足时不会强行给出确定人格。句豆不会替你发送 QQ 消息。

## 已有用户怎样升级

从 0.1.2 或更早版本升级到 1.0.0，先在旧版备份，保留原目录，再把新包解压到另一个目录并恢复备份。旧版没有启用自动更新，所以这一步仍需手动完成。[备份升级步骤](docs/public/BACKUP-UPGRADE.md)

安装 1.0.0 后，在「设置 → 关于 → 当前版本」检查后续更新。默认使用正式版渠道，也可选择预发布；已有本地模型通过标准更新包保留。[自动更新说明](docs/public/AUTO-UPDATE.md)

## 使用范围与问题反馈

当前提供 Windows x64 包，本机 Windows 11 是已验证路线。Windows 10、其他电脑和显卡、多显示器、真实 API/Ollama 服务仍按实际条件继续验证。[兼容表](docs/public/COMPATIBILITY.md)

聊天导入和本地 Laya 分析在自己的电脑上处理。切换到 API 后，分析所需的聊天片段会发送到你配置的服务，服务可能收费。备份含私人聊天和本机加密设置，请自己保管。

在 [GitHub Issues](https://github.com/xzyj50609/QQVibe/issues)反馈版本、失败步骤和匿名诊断即可。不要公开真实聊天、QQ 号码、密码、token、API Key 或完整备份。[已知问题](docs/public/KNOWN-ISSUES.md) · [反馈模板](docs/public/FEEDBACK.md) · [QCE 检测工具](docs/public/QCE-DOCTOR.md)

## 开发者与许可

[源码构建](docs/public/BUILD.md) · [设计规则](design.md) · [JSON 导入指南](docs/public/JSON-IMPORT.md)

仓库仍使用 `xzyj50609/QQVibe`；`QQVibe.exe`、`QQVibeData`、账号/会话身份和更新协议保持兼容。公开源码按审核清单从冻结提交导出，不包含研发历史、真实聊天、凭证或私人交接记录。

句豆基于 [tswawa/WechatVibe](https://github.com/tswawa/WechatVibe) 1.2.2（基线 `02b770821a1cf46b8ecc46b4f874ac6f922bd586`），感谢原作者及贡献者。程序采用 [Apache-2.0](LICENSE)，上游署名和第三方说明见 [NOTICE](NOTICE)、[THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md)，随包模型见[模型许可](docs/public/MODEL-LICENSE.md)。

QQ、QCE 和 NapCat 是外部软件，不随本程序提供。本项目与腾讯、上游应用和模型作者没有官方隶属或背书关系。
