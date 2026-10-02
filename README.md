# QQVibe

QQ 单聊与群聊分析桌面客户端。Windows x64 发布候选版本 **0.1.0 / R8-20261001-rc2**，当前正在准备首批试用 Pre-release，发布链接将在检查后填写。

普通用户使用程序包，不需要 Git、Node、Python 或编译工具。仓库：[xzyj50609/QQVibe](https://github.com/xzyj50609/QQVibe)。[预发布下载页](https://github.com/xzyj50609/QQVibe/releases/tag/v0.1.0-preview.1)。

**优先下载：[含 Laya 的完整程序包](https://github.com/xzyj50609/QQVibe/releases/download/v0.1.0-preview.1/QQVibe-0.1.0-windows-x64-full.zip)**；[无模型标准程序包](https://github.com/xzyj50609/QQVibe/releases/download/v0.1.0-preview.1/QQVibe-0.1.0-windows-x64.zip)。支持范围：Windows 11 x64 本机已验证，跨机正在测试；Windows 10 x64 待测。本地 Laya / CPU 是优先试用路线。源码的「Download ZIP」不能当作程序包。

| 候选程序包 | 用途 |
| --- | --- |
| QQVibe-0.1.0-windows-x64.zip | 标准包，不含模型；首次设置可下载/定位 Laya，或直接配置 API/Ollama |
| QQVibe-0.1.0-windows-x64-full.zip | 含 Laya 的完整包，首批试用优先选择，先选 CPU |

完整解压到可写目录，双击 `win-unpacked/QQVibe.exe`。下载解压 → 按[固定设置步骤](docs/public/SETUP.md)准备 QCE 并登录自己的 QQ → QQVibe 连接本机 QCE → 选择本地 Laya / CPU → 选择获准读取的单聊或群聊 → 查看分析。程序不发送 QQ 消息。不要在 ZIP 内直接运行。

已读消息可搜索、定位、看标签与会话内画像；单聊分我/对方，群聊先看客观计数，再选具体成员。样本不足时不给确定 MBTI。群本地标签是**实验候选**，概率不是准确率，不能当作对人的事实。

本机 Windows 11、QCE 6.3.0 的有限单聊/群聊及 CPU 路线已有证据；跨机正在测试；完整模型质量、真实 API/Ollama、非开发者尚待验收。当前是首批试用预发布版，后续完整正式版验收任务保持。[兼容表](docs/public/COMPATIBILITY.md) · [已知问题](docs/public/KNOWN-ISSUES.md) · [R9 试用清单](docs/public/R9-TRIAL.md)。

数据位于 `win-unpacked/resources/client/QQVibeData`，与程序所在目录一起保留。[备份、升级、回退](docs/public/BACKUP-UPGRADE.md) · [反馈模板](docs/public/FEEDBACK.md) · [Release 草稿](docs/public/RELEASE-NOTES.md)。API 模式会将所需聊天片段发送到用户设置的服务，并可能计费；切换前确认服务、权限与预算。

开发者见[构建说明](docs/public/BUILD.md)。公开候选由冻结提交按 `scripts/public-source-allowlist.json` 导出，排除聊天、凭证、日志、环境和私人交接；只在研发仓库修改，再重新导出。

基于 [tswawa/WechatVibe](https://github.com/tswawa/WechatVibe) 1.2.2，基线 `02b770821a1cf46b8ecc46b4f874ac6f922bd586`。感谢原作者与贡献者提供的界面、分析和开源基础。QQ 账号存储、连接器、导入、同步及界面已改造，QQVibe 独立维护。沿用 [Apache-2.0](LICENSE)，署名见 [NOTICE](NOTICE) 与[第三方声明](THIRD_PARTY_NOTICES.md)。[模型许可与来源](docs/public/MODEL-LICENSE.md)。QQ/QCE/NapCat 为外部程序，不随包提供；QQVibe 与腾讯、上游项目或模型作者无官方隶属或背书关系。
