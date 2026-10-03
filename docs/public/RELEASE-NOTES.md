# QQVibe 0.1.2 预发布

日期：2026-10-03。此版本扩展聊天记录 JSON 导入能力，保留现有 QCE 导入方式，并增加更多常见导出结构的识别与字段映射。

## 下载

**[QQVibe-0.1.2-windows-x64-full.zip：含 Laya 完整程序包](https://github.com/xzyj50609/QQVibe/releases/download/v0.1.2/QQVibe-0.1.2-windows-x64-full.zip)**

第一次使用优先下载完整包。已有 Laya 模型或计划配置 API/Ollama 的用户可下载[无模型标准包](https://github.com/xzyj50609/QQVibe/releases/download/v0.1.2/QQVibe-0.1.2-windows-x64.zip)。SHA256 和文件清单会随发布页提供。

## JSON 导入改进

- 保留 QCE V6 单文件、manifest 和分块 JSONL；新增顶层数组、嵌套消息列表、JSONL/NDJSON、QQ ChatLab 与常见 OneBot/NapCat 字段适配。
- 对结构不同的记录，可以指定消息列表、正文、发送者和时间字段，并手动映射昵称和时区。
- 大文件分批读取，预览使用本机临时存储。当前单次上限 2 GiB / 200 万条；文件仍需完整且身份映射正确。
- 预览会显示样本、有效/拒绝/冲突条数。修改字段后须重新预览；含坏行时须明确同意跳过。
- 不能保证任意 JSON 都能自动识别。平台、本人或会话身份不清时会提示补充确认，不编造 QQ 身份或日期。

已有 0.1.1 用户仍可按[备份升级回退说明](BACKUP-UPGRADE.md)备份、另目录安装并核对恢复结果。

## 试用范围

程序仍为 Windows x64 预发布。Windows 11 / QCE 6.3.0 / 本地 Laya / CPU 是本机优先路线；另一台电脑、Windows 10、其他显卡、多显示器以及真实 API/Ollama 仍需按条件验证。群聊标签和人物画像应结合原消息核对，结果不代表对人的事实判断。

# QQVibe 0.1.1

QQVibe 是一款 Windows 桌面应用，用来回顾自己的 QQ 单聊与群聊：查找旧消息、查看情绪与意图标签，以及自己或某位成员在这段会话中的人物画像。首次使用推荐随包提供的本地 Laya / CPU，分析在自己的电脑上完成。

## 下载哪个文件

**[QQVibe-0.1.1-windows-x64-full.zip：含 Laya 完整程序包](https://github.com/xzyj50609/QQVibe/releases/download/v0.1.1/QQVibe-0.1.1-windows-x64-full.zip)**

第一次使用优先下载完整包。全部解压后打开 **win-unpacked/QQVibe.exe**，不需要 Git、Node、Python 或编译工具。程序旁的 **「使用说明.html」**可直接打开查看离线图文手册。

[QQVibe-0.1.1-windows-x64.zip：无模型标准程序包](https://github.com/xzyj50609/QQVibe/releases/download/v0.1.1/QQVibe-0.1.1-windows-x64.zip)适合已有 Laya 或打算自己配置 API/Ollama 的人。**「Source code」是源码，不能直接当程序打开。** 下载页同时提供 SHA256 和发布清单，记录程序包与源码的对应关系。

## 这次更新

- 新用户默认改为浅色界面；恢复旧版深色设置后，可在「主题外观」中选「浅色模式」。
- 补齐项目介绍和浅色应用截图，清楚说明单聊、群聊与本地分析用途。
- 新增面向普通用户的使用手册与包内离线图文说明，重写下载、QCE 安装、模型选择与聊天查看步骤。
- 把兼容表、已知问题、一页试用清单和反馈模板整理成可直接照做的说明。

已有单聊、群聊、搜索、人物画像、分析控制、备份恢复和模型服务入口继续保留。本次沿用既有功能，没有通过删功能改变原来的验证任务。

## 开始使用

1. 下载完整程序包并全部解压，打开 **win-unpacked/QQVibe.exe**。
2. 按[首次使用说明](https://github.com/xzyj50609/QQVibe/blob/preview/docs/public/SETUP.md)准备 **QCE 6.3.0**，用自己的 QQ 登录并保持运行。
3. 在 QQVibe 点 **「连接本机 QQ / QCE」**，核对本人身份。
4. 选择 **「本地部署」**和 **「CPU」**，等待模型就绪。
5. 添加一个单聊或群聊并打开，查看消息标签与 **「人物画像」**。

[项目介绍与截图](https://github.com/xzyj50609/QQVibe) · [使用手册](https://github.com/xzyj50609/QQVibe/blob/preview/docs/public/USER-GUIDE.md)

## 使用范围与已知问题

**本机已验证，跨机正在测试。** 本版本供首批用户使用，GitHub 保留 Pre-release 标记；不宣称跨机或完整正式版验收已经完成。

当前优先路线是 **Windows 11 x64、QCE 6.3.0、本地 Laya / CPU**。开发机的 Intel UHD WebGPU 与单屏系统缩放已有实测，Windows 10、其他显卡、多显示器及非开发者安装仍在验证。真实 API/Ollama 也尚未完成验证，本机假服务结果只代表界面与流程。

**群聊标签是实验候选**，引用、转发、讽刺和多人语境可能误判。标签分数不是准确率，人物画像不是对人的事实；完整语义与画像质量仍需人工审阅。

程序尚未签名，采用手动更新；已读范围不代表 QQ 全部历史。QQVibe 不使用 WechatVibe 的应用更新源。升级前请备份，并将新版解压到新目录，保留旧目录到确认可用。

[兼容表](https://github.com/xzyj50609/QQVibe/blob/preview/docs/public/COMPATIBILITY.md) · [已知问题](https://github.com/xzyj50609/QQVibe/blob/preview/docs/public/KNOWN-ISSUES.md) · [备份与回退](https://github.com/xzyj50609/QQVibe/blob/preview/docs/public/BACKUP-UPGRADE.md)

## 试用与反馈

请按[一页试用清单](https://github.com/xzyj50609/QQVibe/blob/preview/docs/public/R9-TRIAL.md)检查启动、连接、身份归属、标签、自然新消息和关闭后重开。没有服务或设备的项目写「没试」即可。

遇到问题可按[反馈模板](https://github.com/xzyj50609/QQVibe/blob/preview/docs/public/FEEDBACK.md)发给提供软件的同学，或到 [GitHub Issues](https://github.com/xzyj50609/QQVibe/issues)提交。只需版本、失败步骤和匿名诊断，不上传聊天、账号、Key/token、数据库或完整备份。启动、数据安全和身份归属问题优先处理。

## 来源与许可

程序基于 [tswawa/WechatVibe](https://github.com/tswawa/WechatVibe)，沿用 Apache-2.0，并保留上游署名和第三方声明。完整包中的固定 Laya 模型已按对应来源和许可保留说明，详见[模型许可](https://github.com/xzyj50609/QQVibe/blob/preview/docs/public/MODEL-LICENSE.md)。QQ、QCE 与 NapCat 为外部软件，不随包提供。
