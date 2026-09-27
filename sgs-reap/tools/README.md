# tools —— 数据集转换与后端自检

这里只放**不必反复运行**的转换脚本与自检工具。一次性探针（`probe_*.py`、`smoke_chat.py`）
已于 2026-09-21 精简删除，其结论已固化在 `docs/history/phase2-log.md`。

| 文件 | 用途 | 结论/产物落在 |
|---|---|---|
| `minif2f_to_jsonl.py` | 把 miniF2F 的 Lean4 移植版转成统一的 `jsonl`（valid/test 各 244 题） | `data/minif2f_{valid,test}.jsonl` |
| `prepare_domain_corpus.py` | 将 miniF2F valid 确定性拆成 C-build/C-measure/D，并检查与 T 隔离 | 三个分区文件、`data/dataset_manifest.json` |
| `check_backend.py` | 后端连通性自检（不需要起代理） | 直接输出 |

`prepare_domain_corpus.py` 是当前唯一的数据分区入口；它只做本地确定性整理，不调用模型、Lean 或 Mathlib。
