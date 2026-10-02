# 兼容表

| 项目 | 状态 | 证据边界 |
| --- | --- | --- |
| Windows 11 x64 | 本机已测 | 实际 ZIP、开发机；另机待 R9 |
| Windows 10 x64 | 待测 | 未宣称兼容验收通过 |
| macOS / Linux / ARM | 不在首发范围 | 无本轮程序包 |
| QQ + QCE 6.3.0 | 本机有限会话已测 | 一个授权单聊和指定群；其他 QQ 构建不泛化 |
| 未知 QCE/字段不兼容 | 有界契约检查已有合成证据 | 未知版可在提示下试用，结构不兼容停止在线写入 |
| Laya CPU | 本机实际模型已测 | 语义/画像质量另待审阅；不承诺最低 RAM |
| Laya WebGPU | 本机 Intel UHD 已测 | NVIDIA/AMD、多卡、其他驱动待测，非 CUDA |
| 五种 API 协议 | SDK 对本机假服务已测 | 不算任何真实服务通过 |
| 外部 Chat Completions / Ollama | 真实验收待条件 | 三路线首发承诺仍保留 |
| 单屏 125/150/200% 系统缩放 | R7 本机已测 | 不替代跨屏 |
| 其他 Windows 账号/电脑 | 待 R9 | 凭证需重新设置，DPAPI 密文不通用 |

API 协议保留 Anthropic、Responses、Chat Completions、Gemini、Ollama。具体服务和硬件只有实测后才加入已验证列。QQ/QCE 升级后先检查身份、版本及范围，失败保留本地记录。
