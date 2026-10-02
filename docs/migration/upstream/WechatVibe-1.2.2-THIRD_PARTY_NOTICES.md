# WechatVibe 第三方来源与许可证

本项目本体采用 [Apache-2.0](LICENSE)。WechatVibe 是独立项目，与微信、腾讯、Laya 或以下上游项目不存在官方隶属或背书关系。本文件说明这份源码和构建后的便携目录直接使用的组件；传递依赖仍以各自附带的许可证为准。

## 内嵌 Laya 源码

`electron/laya/` 中的 `types.ts`、`pyjson.ts`、`tokenizer.ts`、`questions.ts`、`prompt.ts`、`calibration.ts` 和 `agent.ts` 来自 [mizchi/laya-mlx](https://github.com/mizchi/laya-mlx) 的 `web/packages/laya-web/src`，提交 `dc3aa6b150cb861d0788fbd421cfd1303de4ed57`。上游采用 Apache-2.0；完整条款和上游声明保留在 [electron/laya/LICENSE](electron/laya/LICENSE) 与 [electron/laya/NOTICE](electron/laya/NOTICE)。这些文件的相对 import 扩展名和来源头注释经过适配；目录中其余 TypeScript 文件为本项目实现。

## 分析模型

源码**不含模型权重**。`scripts/setup-models.ts` 下载 [mizchi/laya-multilingual-onnx](https://huggingface.co/mizchi/laya-multilingual-onnx) revision `d9d003d543e63d6d3375c21d44624136bd1e0bad`，逐文件检查长度和 SHA-256。该模型的仓库元数据为 Apache-2.0，模型卡说明它是独立移植，并非 Convai Innovations 官方发布。便携版的模型文件及上游 README 放在 `resources/client/.models/laya`。

## 运行依赖

| 组件 | 锁定版本 | 许可证与保留位置 |
| --- | --- | --- |
| `@huggingface/tokenizers` | 0.2.0 | Apache-2.0；安装包内 `LICENSE` |
| `@anthropic-ai/sdk` | 0.128.0 | MIT；安装包内 `LICENSE`，用于 Anthropic 兼容接口 |
| `@google/genai` | 2.24.0 | Apache-2.0；安装包内 `LICENSE`，用于 Gemini 兼容接口 |
| `openai` | 7.23.0 | Apache-2.0；安装包内 `LICENSE`，用于 Responses 和 Chat Completions 接口 |
| `ollama` | 0.6.3 | MIT；安装包内 `LICENSE`，用于 Ollama 接口 |
| `onnxruntime-node` / `onnxruntime-common` | 1.30.0 | MIT；上游 [LICENSE](https://github.com/microsoft/onnxruntime/blob/v1.30.0/LICENSE)，全文见下文 |
| `tsx` | 4.23.15 | MIT；安装包内 `LICENSE` |
| `undici` | 7.29.1 | MIT；安装包内 `LICENSE`，用于应用内更新的 HTTP 代理连接 |
| Electron | 44.4.3 | MIT 及 Chromium 第三方条款；便携目录内 `LICENSE.electron.txt`、`LICENSES.chromium.html` |
| Electron Builder | 26.15.3 | MIT；仅构建期使用，安装包内 `LICENSE` |
| TypeScript / `@types/node` | 7.0.2 / 26.6.2 | Apache-2.0 / MIT；仅开发期使用，安装包内许可证 |
| Node.js | 24.11.1 | Node.js 及其第三方条款；源码保留 [完整许可证](licenses/node-LICENSE-24.11.1.txt)，便携版复制到 `resources/client/runtime/node/LICENSE` |
| Python | 3.14 | PSF License；便携版复制基础运行时的 `LICENSE.txt` |
| `wechatauto-replica` | 1.2.2.6 | Apache-2.0；安装发行包 `wechatauto_replica-1.2.2.6.dist-info/licenses/LICENSE` |

完整 Python 版本集合见 [python-requirements.lock.txt](python-requirements.lock.txt)，Node 直接及传递版本见 [package-lock.json](package-lock.json)。读取层的来源说明见 [native-reader/THIRD_PARTY_NOTICES.md](native-reader/THIRD_PARTY_NOTICES.md)。便携构建只复制所需的 Python 包与 Node 包闭包，并保留包内实际附带的许可文件；它不复制整个开发环境。

`wechatauto-replica` 声明的 `winsdk`、`imageio-ffmpeg`、`pyautogui` 在已验证的只读运行集合中未安装，分别属于其可选 GUI、OCR 或媒体路径。这里不声称这些路径可用。客户端不会发送微信消息。默认 Laya 模式在本机分析；用户主动启用 API 模式后，消息分析所需的聊天片段会发送到所配置的服务地址。

## README 演示头像

README 两张演示图中的头像来自 Lisa Wischofsky 的 [Adventurer](https://www.dicebear.com/styles/adventurer/) 插画，采用 [Creative Commons Attribution 4.0 International](https://creativecommons.org/licenses/by/4.0/)（CC BY 4.0）。演示通过 DiceBear 组合角色、调整配色，并在应用截图中缩放展示。人物、对白及分析数值为虚构演示；头像素材不适用本项目代码的 Apache-2.0 许可，继续分发时须保留上述署名和许可说明。

## ONNX Runtime 的 MIT 许可证

`onnxruntime-node` 与 `onnxruntime-common` 的 npm 包内未见独立 LICENSE 文件，故保留其上游 MIT 全文：

```text
MIT License

Copyright (c) Microsoft Corporation

Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"), to deal
in the Software without restriction, including without limitation the rights
to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
copies of the Software, and to permit persons to whom the Software is
furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in all
copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
SOFTWARE.
```
