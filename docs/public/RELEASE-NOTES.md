# QQVibe 0.1.0 Release 说明草稿

状态：R8-20261001-rc2，当前正在准备首批试用 Pre-release，发布链接将在检查后填写。未来 GitHub Release 应标记 Pre-release；仓库 https://github.com/xzyj50609/QQVibe，tag `v0.1.0-preview.1`，对应预发布页与程序资产。用户已授权先发布供首批用户试用的 Pre-release，再取得 R9 跨机证据，不宣称正式版验收通过。

提供 Windows x64 标准 ZIP，含运行环境，不含模型；可在首次设置下载/定位 Laya，也可直接配置 API。优先提供含 Laya 的完整 ZIP，保留固定版本许可、署名与转换来源。普通用户解压双击 QQVibe.exe，不必安装开发工具。

覆盖 QQ 单聊双方标签与我/对方画像、群有界读取及成员筛选、客观范围概览与选定成员群内画像、历史搜索和证据定位、分析暂停取消、匿名诊断及备份恢复。群本地标签明确为实验候选。

当前验收：本机有限真实 QQ/QCE 和 Laya、合成契约、恢复与单屏系统缩放已有证据；本轮候选首跑以实际 ZIP 定向检查，最终记录见随交付报告。真实 API/Ollama、完整语义/画像审阅、跨屏和 R9 跨机/非开发者仍未完成，不能写成三条路线全部可用。

附件建议：标准 ZIP、SHA256SUMS.txt、发布清单与设置说明。含模型 ZIP 已按固定模型来源声明与 Apache-2.0 条件整理许可，优先供首批试用。源码提交及包内 release-manifest 对应见发布清单；公开源码候选不含 .git 历史或真实聊天。

程序身份 QQVibe / com.local.qqvibe.real-client；数据在 resources/client/QQVibeData；自动更新关闭，手动升级先备份。未签名，Windows10与其他 GPU 待验证。详见 [设置](SETUP.md)、[兼容表](COMPATIBILITY.md)、[限制](KNOWN-ISSUES.md)、[回退](BACKUP-UPGRADE.md)、[反馈](FEEDBACK.md)。
