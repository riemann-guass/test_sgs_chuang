# 阶段 23 执行日志（把闭环写出来：`graph/runner.py`）

## 为什么需要这个单元

审计的结论是：**每一件都有实现与测试，但没有任何代码把它们按顺序连起来**——"环"只存在于文档里。
本单元补上的就是这一环。

## 交付物

| 文件 | 作用 |
|---|---|
| `graph/runner.py`（新） | **闭环编排**：一轮 = 采轨迹 → 需求 → 猜想 → 门检 → 硬门 → 求解 → 验证 → 软分 → 选择 → 物化 → 入库 |
| `tests/run_round.py`（新） | 命令行入口 + **数据角色守卫**（见下） |

### 一轮的十步（`RoundRunner.run_round`）

```
① collect     在 C 上跑 Solver → 用 `trace` 作业记录子目标签名（trace 内部已含验证结论，
              所以"解出哪些目标"直接从轨迹读，**不再单独起一批 verify**）
② demand      N1：分层统计 + 跨目标一致性过滤 → demand 桶
③ conjecture  /conjecture，条件化在 [未解目标 + 需求 + 库范例]
④ screen      门检 + 硬门（非平凡 ∧ 新颖，新颖性对比"父目标 + 库"）
⑤⑥⑦ prove/verify/measure
              /solve 出 k 篇 → 一批 `dependencies` 同时拿到**可证硬门**与**依赖软分**
              （用 dependencies 而不是 verify：它内部就调 Verify.verify）
⑧ select      N2：`parent_cover_greedy` —— 在库容 B 内覆盖尽量多**不同父目标**（并集→子模）
⑨ commit      追加进库 → **物化整库** → `lake build SgsLean.GeneratedLibrary`
⑩ 回到 ①（此时提示词里已经有库了）
```

### 数据角色在代码里强制

`tests/run_round.py` 里有一条硬守卫：**`--curriculum` 指向 miniF2F 就直接拒绝**：

```
[round] 拒绝：`minif2f_valid.jsonl` 是 miniF2F（开发/测试集）。
        按 docs/data-protocol.md，建库只能用课程集 C；
        miniF2F valid 用于调参（tests/run_gate_g3_real.py --select），test 只用于最终评测。
```

（这条守卫是照着 phase19–22 那次错误加的：当时我拿 miniF2F 的题建库、又在 miniF2F 上测。）

## 冒烟结果（离线假服务）

### Mathlib 模式：环在起作用

```
[round 0] 目标 2 条；库 0 条
[round 0] 解出目标 2/2
[round 0] 入库 1 条 → 库 1 条；漏斗 {candidates: 4, rejected_trivial: 2, passed_hard_gates: 2,
                                    proof_candidates: 2, verified: 2, library_written: 1,
                                    library_size: 1, greedy_gains: [1, 1]}
[round 1] 目标 2 条；库 1 条
[round 1] 解出目标 2/2；跨轮 cover 增量 0
[round 1] 入库 0 条 → 库 1 条；漏斗 {candidates: 4, rejected_trivial: 2, rejected_novelty: 2,
                                    library_written: 0, greedy_gains: []}
```

第二轮里 **2 条候选被"新颖性"硬门拦下**——因为它们与第一轮入库的引理等价。
这是"库真的参与了判据"的直接证据（假服务每次都返回同一批候选，所以第二轮必然撞车）。

### 快速模式（无 Mathlib）：编排本身

```
[round] 完成 2 轮；总耗时 110.8s
        round 0: 解出 2；候选 4；过硬门 2；验证通过 0；入库 0；lean_batches 3
        round 1: 解出 2；候选 4；过硬门 2；验证通过 0；入库 0
```

（快速模式下 mock 的答案表覆盖不到 mock 自己出的候选，所以没有验证通过——链路本身是通的。）

## 成本结构（写进报告，便于后续优化）

每轮需要几次 `lean` 子进程 = 几次 Mathlib 导入。本次优化把它压到 **3–4 次/轮**：

| 批次 | 干什么 | 能否合并 |
|---|---|---|
| trace | 记录子目标签名（含验证） | 不能（要有证明才能记） |
| screen | 门检 + 平凡 + 新颖 | 已合并成一批 |
| dependencies | 可证硬门 + 依赖软分 | 已合并成一批（原来 verify 与 dependencies 是两批） |
| materialize | 物化 + 编译 | 不能 |

Mathlib 模式下一次导入 ≈ 1.5–2 min（热），所以 **一轮 ≈ 6–8 min 是导入主导的**。
真正的解法是让服务端子进程**跨 flush 常驻**（真正的 REPL），那样一轮只需一次导入。
已记为下一步的优化项（`SgsLean/Server.lean` 的 `runJobs` 改成循环读 jobs）。

## 与框架的对应关系

| `docs/framework.md` 的层 | 在 runner 里的落点 |
|---|---|
| 生成层 | `collect`（Solver）、`conjecture`（Conjecturer，带需求与库范例） |
| 判据层 G | `screen`（门检 + 硬门）、`prove_verify_measure`（可证 + 依赖软分） |
| 选择层 N2 | `parent_cover_greedy`（父目标覆盖 + 子模贪心 + 库容 B） |
| 载体 | `commit`（入库 + 物化整库 + 下一轮自动注入提示词） |

## 遗留（明确没做的）

1. **压缩收益 Δlen 仍未接线**：它需要"有/无该引理"的配对证明，属评测阶段的对照组工作；
   runner 里只用"证明步数 ≤ 阈值"作为廉价旁证（记 `soft:cheap_proof`）。
2. **评测仍未做**：runner 只负责建库；`proved_cover` 的两臂评测在
   `tests/run_gate_g3_real.py`（且按协议只能用 D 调参、T 只跑一次）。
3. **服务端子进程不常驻**，每批一次导入（见上面的成本结构）。
4. **`parent_cover` 是代理**：它是"视图为哪些查询而建"的类比，不是实测覆盖；
   它与真 cover 的一致性正是最初计划里 G3 要回答的问题。
