# tools —— 一次性探针与数据转换

这些脚本**不在主流程里**，是当时为了搞清楚某个事实而写的一次性工具；结论都已写进
对应的 `docs/phase*-log.md`，脚本留着是为了结论可复核。

| 文件 | 当时回答的问题 | 结论落在 |
|---|---|---|
| `check_backend.py` | 后端有哪些模型可用 | `phase2-log.md`（`deepseek-flash` / `deepseek-v4-pro`） |
| `smoke_chat.py` | 最小 chat 调用长什么样、`max_tokens` 够不够 | `phase2-log.md`（推理模型会吃光预算） |
| `probe_reasoning.py` | 输出上限、能否降低推理强度 | `phase2-log.md`（`thinking: disabled` 把 3k–10k token 降到 83） |
| `probe_guide.py` / `probe_guide_variance.py` | Guide 打分稳不稳、动态范围多宽 | `phase2-log.md`（T=1.0 方差 2.0；T=0 收敛） |
| `minif2f_to_jsonl.py` | 把 miniF2F 的 Lean 源文件转成 JSONL | `phase16-log.md` |
