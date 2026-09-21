# tools —— 数据集转换与后端自检

这里只放**不必反复运行**的转换脚本与自检工具。一次性探针（`probe_*.py`、`smoke_chat.py`）
已于 2026-09-21 精简删除，其结论已固化在 `docs/phase2-log.md`。

| 文件 | 用途 | 结论/产物落在 |
|---|---|---|
| `minif2f_to_jsonl.py` | 把 miniF2F 的 Lean4 移植版转成统一的 `jsonl`（465/488 条过门检） | `data/minif2f_{valid,test}.jsonl` |
| `check_backend.py` | 后端连通性自检（不需要起代理） | 直接输出 |

**下一步**：P2 阶段要新增 `prepare_domain_corpus.py`（把选定领域的多来源题目转成
统一格式，并做命题级去重与同源检查），见 `docs/SG-Lean思路文档第二版.pdf` 7.2 节。
