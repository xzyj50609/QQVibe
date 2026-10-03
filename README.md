# QQVibe

**把 QQ 单聊和群聊整理成看得懂的聊天回顾。**

QQVibe 是一款 Windows 桌面应用。它连接你电脑上的 QQ Chat Exporter（QCE），读取你主动选择的聊天，帮助你查找旧消息、查看表达中的情绪与意图，并了解自己或某位成员在这段会话中的交流方式。你可以使用随包提供的 Laya 模型，在自己的电脑上分析聊天。

![QQVibe 浅色主界面：会话列表、聊天记录与分析结果](docs/public/images/overview.png)

*演示数据，分析内容仅作界面展示；截图不包含真实用户资料。*

## 下载并开始使用

**[下载 QQVibe 0.1.1 Windows 完整包](https://github.com/xzyj50609/QQVibe/releases/download/v0.1.1/QQVibe-0.1.1-windows-x64-full.zip)** · [查看下载页面](https://github.com/xzyj50609/QQVibe/releases/tag/v0.1.1)

完整包包含程序和本地 Laya 模型，适合第一次使用。无需安装 Git、Node、Python，也无需注册 GitHub 账号。**请下载上面的程序包；绿色「Code」按钮和「Source code」下载的是源码，不能直接运行。**

GitHub 的源码预览分支已加入更多聊天 JSON / JSONL 结构适配、字段映射和分批读取；这些变化还没有进入上方 0.1.1 下载包。格式说明见 [JSON 导入指南](docs/public/JSON-IMPORT.md)。

目前提供 **Windows x64** 程序。Windows 11 的本机使用已验证，其他电脑正在试用；Windows 10 还在验证。首次使用建议选择 **本地 Laya / CPU**，新用户默认使用浅色界面。

1. **下载、完整解压。** 在普通文件夹中解压 ZIP，打开里面的 `win-unpacked` 文件夹，双击 **QQVibe.exe**。不要直接在压缩包里运行。
2. **准备 QQ 连接工具。** 按[首次使用说明](docs/public/SETUP.md)安装 QCE 6.3.0，用你自己的 QQ 登录，并保持它运行。
3. **连接 QQ。** 在 QQVibe 的首次设置中点击「连接本机 QQ / QCE」，确认显示的是自己的账号。
4. **选择模型。** 在设置中选择「本地部署」和「CPU」，等待模型就绪。完整包不需要再下载模型。
5. **添加聊天并查看。** 添加一个单聊或输入群号添加群聊，回到会话列表打开它，查看消息、标签和「人物画像」。

解压后也可以直接双击程序旁的 **「使用说明.html」**查看离线图文手册，不必学习 GitHub。在线版见[首次使用说明](docs/public/SETUP.md)与[使用手册](docs/public/USER-GUIDE.md)。

## 可以做什么

| 你想做的事 | QQVibe 提供的功能 |
| --- | --- |
| 找到聊过的一句话 | 按关键词和日期搜索聊天记录，群聊还可以按成员筛选，打开结果查看上下文 |
| 回顾一对一交流 | 查看双方消息的情绪与意图标签，在人物画像中分别查看「我」和「对方」 |
| 看清群聊参与情况 | 查看已读取范围内的消息、文本和参与人数，选择某位成员查看他在这个群里的发言与画像 |
| 继续查看新消息 | 连接在线 QCE 后同步已选会话的自然新增消息，读取和分析分别进行 |
| 保留自己的聊天回顾 | 已读取消息与分析结果保存在本机，支持备份、恢复和关闭后重新打开 |
| 控制读取和分析 | 只添加自己选择的会话，分别停止读取、暂停分析或取消未完成分析 |
| 选择分析方式 | 优先使用本地 Laya，也保留自定义 API 和 Ollama 入口；实际服务兼容性见下方说明 |

QQVibe 用于读取和回顾聊天，**不会替你发送 QQ 消息**。本地 Laya 分析在自己的电脑上运行；切换到 API 后，分析所需的聊天片段会发送到你配置的服务，服务可能收费。

## 界面预览

### 单聊：分别查看我和对方的画像

![单聊人物画像与样本不足提示](docs/public/images/single-chat.png)

*演示数据，分析内容仅作界面展示。*

在「我 / 对方」之间切换，分别查看这段单聊中的画像。上方主界面截图展示了消息标签；需要更多细节时可切换「百分比详情」。样本不足时会明确提示，百分比也不是判断准确率。

### 群聊：选择具体成员，查看他在本群的发言

![选定群成员的画像](docs/public/images/group-chat.png)

*演示数据，分析内容仅作界面展示。*

群概览显示已读取范围内的客观计数；人物画像需要选择具体成员。**群聊标签是实验候选**，引用、讽刺和复杂多人语境可能判断错误，必须结合原文理解。人物画像也只是对当前会话的推测，不是对一个人的确定结论。

## 使用说明与常见问题

- [首次使用：下载、连接 QQ、选择本地模型](docs/public/SETUP.md)
- [使用手册：单聊、群聊、搜索、分析与数据管理](docs/public/USER-GUIDE.md)
- [系统与模型兼容表](docs/public/COMPATIBILITY.md)
- [已知问题与处理方法](docs/public/KNOWN-ISSUES.md)
- [备份、升级与回退](docs/public/BACKUP-UPGRADE.md)
- [第一次试用清单](docs/public/R9-TRIAL.md)
- [问题反馈模板](docs/public/FEEDBACK.md)

本机已验证，跨机正在测试。真实 API/Ollama、完整标签与画像质量、其他显卡及多显示器仍在验证中，具体状态见兼容表。当前 GitHub Release 保留 Pre-release 标记，便于说明首批使用范围。

遇到问题可以发给提供软件的同学，或在 [GitHub Issues](https://github.com/xzyj50609/QQVibe/issues)反馈。只需版本、失败步骤和匿名诊断，**不要上传真实聊天、QQ 号码、密码、token、API Key、数据库或完整备份**。

## 开发者入口

普通用户使用上面的程序包即可。需要研究或修改代码时，再看[源码构建说明](docs/public/BUILD.md)。公开源码通过白名单导出，包含必要源码、锁文件、构建脚本和合成测试；不包含研发仓库历史、真实聊天、凭证、日志或私人交接记录。

[0.1.1 更新说明](docs/public/RELEASE-NOTES.md) · [JSON 导入指南](docs/public/JSON-IMPORT.md) · [源码仓库](https://github.com/xzyj50609/QQVibe)

## 来源与许可

QQVibe 基于 [tswawa/WechatVibe](https://github.com/tswawa/WechatVibe) 1.2.2，基线提交 `02b770821a1cf46b8ecc46b4f874ac6f922bd586`。感谢原作者和贡献者提供的界面与分析基础。QQ 账号存储、连接、导入、同步和相关界面由 QQVibe 独立改造与维护。

程序沿用 [Apache-2.0](LICENSE)。上游署名与第三方说明见 [NOTICE](NOTICE)、[THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md)；随包 Laya 模型的来源、转换和再分发说明见[模型许可](docs/public/MODEL-LICENSE.md)。

QQ、QCE 和 NapCat 是外部软件，不随 QQVibe 程序包提供。QQVibe 与腾讯、WechatVibe 上游或模型作者没有官方隶属或背书关系。
