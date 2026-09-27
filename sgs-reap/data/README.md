# data/：miniF2F 唯一数据源

当前正式协议只使用 miniF2F。详细约束见 `docs/data-protocol.md`。

| 文件 | 角色 | 规模 | 是否允许影响引理库 |
|---|---|---:|---|
| `minif2f_valid.jsonl` | 原始 valid 源 | 244 | 不可直接使用，必须先分区 |
| `minif2f_c_build.jsonl` | C-build | 122 | 可产生候选 |
| `minif2f_c_measure.jsonl` | C-measure | 61 | 只测量复用，不产生候选 |
| `minif2f_dev.jsonl` | D | 61 | 否；只调参与审计 |
| `minif2f_test.jsonl` | T | 244 | 否；冻结后只运行一次 |
| `dataset_manifest.json` | 数据划分审计记录 | — | 记录种子、算法、计数和哈希 |

三个 valid 分区由现有入口确定性重建：

```powershell
& $py tools\prepare_domain_corpus.py
```

算法按 `sha256(seed, id, normalized_statement)` 排序后精确切为 50%/25%/25%，固定种子为
`sg-lean-minif2f-v1`。三者完整覆盖 valid 且两两不相交；test 原文件不被改写。

正式离线建库默认直接读取 `minif2f_c_build.jsonl` 和 `minif2f_c_measure.jsonl`。
D/T 无论改名还是复制，都不得进入建库或引理来源字段。
