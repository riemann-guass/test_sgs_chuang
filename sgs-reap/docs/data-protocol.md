# miniF2F 数据协议

> 本文件是当前数据角色的权威定义。2026-09-27 起，项目只使用 miniF2F 作为
> 题目来源；旧 C1/C2/Mathlib 课程集只是历史材料。

## 一、四个互斥角色

miniF2F valid 的 244 题按固定种子 `sg-lean-minif2f-v1` 和
`sha256(seed, id, normalized_statement)` 排序后精确切分：

| 角色 | 文件 | 规模 | 允许 | 禁止 |
|---|---|---:|---|---|
| C-build | `data/minif2f_c_build.jsonl` | 122 | 基线轨迹、需求挖掘、候选生成和证明 | 为自己贡献 reuse |
| C-measure | `data/minif2f_c_measure.jsonl` | 61 | probation 中性曝光、constants 计数、晋升 | 需求挖掘和候选来源 |
| D | `data/minif2f_dev.jsonl` | 61 | 调参、装置检查、忠实度审计 | 入库、需求、候选来源 |
| T | `data/minif2f_test.jsonl` | 244 | 框架冻结后的最终一次评测 | 调参、建库、提前看结果 |

三个 valid 分区必须完整覆盖 244 题且题目身份两两不相交。T 保留 miniF2F
原始 test 分割，不参与任何再划分。`data/dataset_manifest.json` 记录算法、种子、
计数、来源哈希、分区哈希和零重叠检查。

## 二、reuse 口径

```text
reuse(l) = 在 C-measure 上实际引用 l 的不同目标数
```

一次引用只有同时满足以下条件才有效：

1. 目标属于 C-measure；
2. 目标不是该引理的 `source_target`；
3. 证明通过 Lean 内核终检；
4. 引用来自 Lean 证明项的 `constants`；
5. 该引理对该目标确有中性提示词曝光；
6. 同一目标多篇证明或多次引用只计 1。

证明文本出现库名、失败证明引用、D/T 上的引用都不得计入 reuse。

## 三、标准流程

```text
1. 只在 C-build 上生成和证明候选，进入 probation
2. 在 C-measure 上做带盐、中性、逐目标可审计的曝光
3. 按 constants 与逐目标曝光证据计 reuse，晋升 active
4. 物化、编译并发布冻结 LibrarySnapshot
5. 在 D 上选择 k、预算、阈值和统计方案，不回写库
6. 冻结代码、数据 manifest、配置、库快照和报表 schema
7. 在 T 上只运行一次正式 A/B/C
```

正式方法比较的主指标仍是首轮、模型生成、无 cheap、无 repair 的严格 pass@k。

## 四、来源与入库字段

C-build/C-measure 行标记 `source_corpus=MF_VALID_C`；D 标记
`source_corpus=MF_VALID_D`。引理库只允许 `MF_VALID_C`。原始未分区
`minif2f_valid.jsonl`、D 分区和 T 都不能直接作为 Runner 的建库输入。

## 五、历史数据的定位

- 旧 C1/C2/Mathlib 课程集已从当前 `data/` 删除，仅在 Git 历史和阶段日志中保留。
- 旧 `C_build.jsonl` 和基于它的难度结论不再属于当前数据协议。
- `experiments/library.jsonl` 仍是开发期迁移资产，正式 miniF2F 库必须重建。
