# miniF2F 数据

当前只使用 miniF2F，共两份原始数据：valid 244 题和 test 244 题。

| 文件 | 通俗含义 | 数量 | 是否参与建库 |
|---|---|---:|---|
| `minif2f_valid.jsonl` | valid 原始文件 | 244 | 不可直接使用 |
| `minif2f_c_build.jsonl` | 建库集 | 122 | 产生候选引理 |
| `minif2f_c_measure.jsonl` | 复用测量集 | 61 | 只试用候选引理 |
| `minif2f_dev.jsonl` | 开发集 | 61 | 否 |
| `minif2f_test.jsonl` | 最终测试集 | 244 | 否 |
| `dataset_manifest.json` | 划分算法、数量和哈希 | — | 审计记录 |

重新生成派生分区：

```powershell
python tools\prepare_domain_corpus.py
```

工具按固定种子和 `id + 规范化命题` 的 SHA-256 排序，再精确切成 50%/25%/25%。相同源文件
一定得到相同分区。三个 valid 分区完整覆盖源文件且彼此不重叠；test 不被改写。

开发集和最终测试集即使复制或改名，也会被内容指纹守卫拒绝进入建库流程。
