# Laya 模型来源与再分发条件

核对日期：2026-10-01。固定 [ONNX 模型卡](https://huggingface.co/mizchi/laya-multilingual-onnx/blob/d9d003d543e63d6d3375c21d44624136bd1e0bad/README.md)、[中间 MLX 模型卡](https://huggingface.co/aac6fef/laya-multilingual-mlx/blob/f2b4faf51023039425946074e2cf1361d2db11d5/README.md)、[原始模型卡](https://huggingface.co/convaiinnovations/laya-multilingual/blob/052592a15d198d9ad47da779604259b10b47b7aa/README.md) 都明确声明 Apache-2.0。中间版本的完整 LICENSE 与 NOTICE 已取得并随包保留，固定URL及SHA256见 `licenses/laya-model-sources.json`。此前网页工具访问中间模型失败，已直接从该固定 revision 读取并核对，没有把工具未取得当作不存在。

转换链：原始 revision `052592a15d198d9ad47da779604259b10b47b7aa` → MLX snapshot `f2b4faf51023039425946074e2cf1361d2db11d5` → ONNX revision `d9d003d543e63d6d3375c21d44624136bd1e0bad`。模型来自 Convai Innovations 与贡献者；MLX/ONNX 是独立转换，不代表官方背书。本项目未训练或替换权重，包内固定文件按 `scripts/model-files.json` 校验。

Apache-2.0允许满足条件的再分发：提供许可全文、保留适用版权及NOTICE、注明修改/转换来源、不暗示商标或官方背书。本候选保留根 Apache-2.0、原始模型卡、MLX LICENSE/NOTICE/模型卡与固定 ONNX README，另在根 NOTICE 说明转换链；依据已查材料，本轮完整包可按这些条件提供给首批用户。后续换权重/revision必须重新核对，不能沿用本结论。

标准包不含权重，下载/定位路线仍保留。内嵌 Laya 库沿用 `electron/laya/LICENSE` 与 `NOTICE`。上游转换一致性和性能说明不是 QQ 聊天任务语义验收，群标签仍是实验候选。
