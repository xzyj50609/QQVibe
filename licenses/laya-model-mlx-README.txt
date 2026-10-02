---
license: apache-2.0
library_name: mlx
pipeline_tag: text-classification
base_model: convaiinnovations/laya-multilingual
tags:
- multilingual
- mlx
- laya
- modernbert
- apple-silicon
- decision-model
---

# laya-multilingual-mlx

Native **MLX FP16** conversion of [convaiinnovations/laya-multilingual](https://huggingface.co/convaiinnovations/laya-multilingual) for Apple silicon.

This checkpoint uses **mmBERT-base**, a **1024-token total context**, and Laya's decision Transformer, scoring head and action head. It supports `choice`, ordinal `score`, and boolean `noul` questions. All model computation runs in MLX; the runtime does not require PyTorch or Transformers.

## Usage

Install the dedicated runtime on an Apple silicon Mac with macOS 14+ and Python 3.11+:

```bash
python -m pip install laya-mlx
```

```python
import laya_mlx as laya

agent = laya.load("aac6fef/laya-multilingual-mlx")
result = agent.predict(
    "I was billed twice. Please refund the duplicate today.",
    {
        "department": {
            "type": "choice",
            "instructions": "Which department should handle this request?",
            "criteria": ["billing", "technical", "sales"],
        },
        "refund": {
            "type": "noul",
            "instructions": "Does the customer ask for money back?",
        },
    },
)
print(result["answers"])
```

Use `dtype="float32"` for closer agreement with upstream FP32 arithmetic. The source weights themselves are FP16. Question formatting, tokenizer behavior, calibration temperatures and output schema are preserved.

This is a bidirectional decision encoder loaded with `laya_mlx`. The package provides the custom architecture needed to interpret the checkpoint. The repository does not include a generative language model or training implementation.

## Validation

Tested locally on Apple M3 Max, 40-core GPU, 128 GB unified memory, macOS 27.2, Python 3.12.13 and MLX 0.32.2.

- FP16 agrees with upstream PyTorch MPS FP32 on the argmax of **63/63** decision distributions across 16 cases.
- Maximum calibrated probability difference: **0.0012887**.
- **100 repeated calls** produced finite, deterministic public outputs; measured MLX active-memory growth after clearing caches was **0 bytes**.
- Every exported tensor was checked for exact equality with the corresponding source tensor cast to FP16.

The included `validation.json` contains numerical and stability measurements for both FP32 and FP16 arithmetic. [Full performance report and raw timing samples](https://github.com/mizorewww/laya-mlx/blob/main/BENCHMARKS.md) compare MLX with the original runtime on the same machine. These checks establish port fidelity, not that every model answer is correct.

## Provenance and limits

- Source checkpoint: `convaiinnovations/laya-multilingual` at `052592a15d198d9ad47da779604259b10b47b7aa`.
- Upstream code: [NandhaKishorM/laya](https://github.com/NandhaKishorM/laya), commit `6a5819129eb220570792e417e49723d697efd76f`.
- Conversion changes parameter names for MLX and preserves FP16 weights. It does not retrain or quantize to fewer bits.
- This is an independent port. Model quality, calibration and language/task limitations remain those of the original checkpoint. Questions and options share the context budget with the input state.
- The typed-decisions checkpoint is specialized for upstream workflows; the multilingual checkpoint is the intended choice for non-English text.

Apache-2.0. Original Laya models and code are by Convai Innovations and contributors. See `LICENSE`, `NOTICE`, `mlx_config.json` and `manifest.json` for attribution and export details.
