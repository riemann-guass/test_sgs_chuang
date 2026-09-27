# tools —— 数据集转换与后端自检

这里只放**不必反复运行**的转换脚本与自检工具。一次性探针（`probe_*.py`、`smoke_chat.py`）
已于 2026-09-21 精简删除，其结论已固化在 `docs/history/phase2-log.md`。

| 文件 | 用途 | 结论/产物落在 |
|---|---|---|
| `minif2f_to_jsonl.py` | 把 miniF2F 的 Lean4 移植版转成统一的 `jsonl`（465/488 条过门检） | `data/minif2f_{valid,test}.jsonl` |
| `prepare_domain_corpus.py` | 生成 C、去重、检查与 D/T 同源，并写 manifest | `data/C.jsonl`、`data/corpus_manifest.json` |
| `check_backend.py` | 后端连通性自检（不需要起代理） | 直接输出 |

下一阶段将在现有语料工具内加入稳定的 C-build/C-measure 派生与题面形态筛选，不新开入口。
