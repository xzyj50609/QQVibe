# QQVibe 第三方来源与许可证

## QQ 改造与分发边界（2026-09-30）

QQVibe 的预览图标由 `scripts/build-product-icons.py` 使用代码几何生成，SVG、PNG、ICO共用同一形状，不含外部图片或字体。图形沿用本项目 Apache-2.0；不使用腾讯企鹅或微信双气泡标志。原微信图标作为上游历史资源保留，QQ产品窗口、网页和构建配置选择独立图标。

QQVibe 基于 tswawa/WechatVibe 1.2.2，源码基线 `02b770821a1cf46b8ecc46b4f874ac6f922bd586`，沿用根 LICENSE 的 Apache-2.0。账号/消息存储、QCE协议和只读连接器、导入、同步、分析代次与QQ界面是新增或改动；逐文件哈希和基线差异由 `scripts/audit-delivery.py` 记录。原声明逐字保存在 `docs/migration/upstream/WechatVibe-1.2.2-THIRD_PARTY_NOTICES.md`；下列来源和许可全文继续保留。

QQ、qq-chat-exporter 和 NapCatQQ 是外部程序，未复制进白名单源码或包。QCE 固定参考版本 `7fcca88880c2eb8c12c51c2b6cc49ee805a53d0c` 仓库许可为GPL-3.0；NapCat固定参考元数据未能明确归类，不能据此认定可捆绑。接口互通和独立运行不等于所有分发方式均已解决许可问题。

Python staging 无论开发环境是否安装，都排除 winsdk、imageio-ffmpeg、PyAutoGUI 及仅由它们引入的传递依赖。当前选出18个发行包，与既有锁文件一致，不会带入可选GUI路径的MouseInfo。jieba 0.42.1 安装包没有完整许可文件，已补原版本MIT全文 `licenses/jieba-0.42.1-LICENSE.txt`，来源 https://raw.githubusercontent.com/fxsjy/jieba/v0.42.1/LICENSE 。

Node闭包逐项许可路径及哈希记录在审计清单，不能只看8个直接依赖。ONNX Runtime全文见下文；esbuild Windows二进制许可见同版本esbuild/LICENSE.md；data-uri-to-buffer全文在它的README.md。standardwebhooks 1.1.1 安装包缺独立许可，已核对其发布提交 `a7d19b4574ae62221042e76240648d0688a8c420` 的 libraries/javascript/package.json 与 libraries/LICENSE，子目录为MIT；仓库根Apache-2.0不适用于这一子目录。补齐全文 `licenses/standardwebhooks-1.1.1-LICENSE.txt`，来源与哈希见 `licenses/supplemental-sources.json`。本轮候选依赖与实际包核对结果见交付报告；这些检查不代表首发全部验收通过。

固定模型卡 https://huggingface.co/mizchi/laya-multilingual-onnx/blob/d9d003d543e63d6d3375c21d44624136bd1e0bad/README.md 声明Apache-2.0和独立移植身份，2026-09-30已核对；模型ZIP不是上游微信客户端安装包。

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

`wechatauto-replica` 声明的 `winsdk`、`imageio-ffmpeg`、`pyautogui` 在已验证的只读运行集合中不打包，分别属于其可选 GUI、OCR 或媒体路径。这里不声称这些路径可用。客户端不会发送微信消息。默认 Laya 模式在本机分析；用户主动启用 API 模式后，消息分析所需的聊天片段会发送到所配置的服务地址。

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

## R8 模型再分发状态

固定ONNX及原始模型卡均声明Apache-2.0；转换来源链及中间版本LICENSE/NOTICE已取得并保留，完整包按Apache-2.0条件再分发。详情与固定来源见[模型许可](docs/public/MODEL-LICENSE.md)。标准包不含模型。

## 可选视频运行时边界

QQVibe没有VideoCapture/VideoWriter或视频编解码调用。便携staging保留OpenCV图像运行时及全部附带许可证，排除其可选`cv2/opencv_videoio_ffmpeg*.dll`；不把仅安装在开发wheel里的FFmpeg视频DLL当成QQ客户端所需闭包。Windows图像导入和PNG相关功能仍保留；不承诺OpenCV视频IO。NumPy附带OpenBLAS及GCC运行时的许可/例外全文保留在安装包LICENSE中。
