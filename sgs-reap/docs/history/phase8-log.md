# 阶段 8 执行日志（P2 开工：Mathlib 级工作负载 + 轨迹层）

P2 的目标是 N1（需求驱动的条件化）：从**真实证明轨迹**里挖出反复出现的子目标状态，
按 `d(g) = freq(g) × cost(g)` 排序，作为 Conjecturer 的额外输入。本单元做完两件前置：

1. **工作负载换血**：初等引理太浅（G1 里 45/63 全解），轨迹里不会反复出现子目标；
   换成 Mathlib 级 `data/workload_mathlib.jsonl`；
2. **轨迹层**：`SgsLean/Trace.lean` + server 的 `trace` 命令，能给出**逐 tactic 的子目标签名流**。

## 交付物

| 文件 | 作用 |
|---|---|
| `data/workload_mathlib.jsonl` | 12 条 Mathlib 级目标（nat/real/int），每条自带参考证明 |
| `sgslean/SgsLean/Trace.lean` | `Trace.traceScript`：逐行跑证明，记录每步后的子目标签名/剩余目标数/判定码 |
| `sgslean/SgsLean/Server.lean`（改） | 协议新增 `trace` 命令（`stmt` + `proof` → `TraceResult`） |
| `sgslean/SgsLean.lean`（改） | 挂载 `SgsLean.Trace` |
| `experiments/results/lemma_refs_check.json`（覆盖） | 本单元 12 条参考证明的验证结果 |

## 工作负载换血：12 条 Mathlib 级目标，参考证明 12/12 通过

```
[lemma-refs] 批 1: 12 条判定完（本批 frontend 73386ms，累计 77s）
[lemma-refs] 通过 12/12，耗时 76.8s
```

覆盖：整除传递（两步 `obtain`）、`Nat.mul_eq_zero`、`Even n → Even (n^2)`（构造见证）、
`2 ∣ n^2 + n`（G1 里没过、这次用 `even_iff_two_dvd.mp` 修好）、`field_simp` 消去除法、
ℝ 三次展开 `ring`、`nlinarith` 非线性、`|x| ≥ x`、合取目标（两条都要证）等。

**坦白说**：这些仍然不是研究意义上的"难题"，而是**本环境里我能给出经验证参考证明的最硬的一批**。
它们比 G1 的初等引理强的地方在于：多数需要**多步 tactic**（`obtain` 后重写、`rw` 两次、
构造见证 + `ring`），因此会产生**有内容的子目标流**——这正是 N1 需要的原料。

## 轨迹层：`trace` 命令的真实输出

```powershell
{"id":"t1","cmd":"trace","stmt":"∀ (n : Nat), 2 ∣ n ^ 2 + n",
 "proof":"intro n\nrw [pow_two]\nhave h : n * n + n = n * (n + 1) := by ring\nrw [h]\nexact even_iff_two_dvd.mp (Nat.even_mul_succ_self n)"}
```

```
steps:
  1 intro n                                          goalsLeft=1  sig=2 ∣ n ^ 2 + n
  2 rw [pow_two]                                     goalsLeft=1  sig=2 ∣ n * n + n
  3 have h : n * n + n = n * (n + 1) := by ring      goalsLeft=1  sig=2 ∣ n * n + n
  4 rw [h]                                           goalsLeft=1  sig=2 ∣ n * (n + 1)
  5 exact even_iff_two_dvd.mp (...)                  goalsLeft=0  sig=""
verified=true
```

第二条（整除传递）：

```
  1 intro a b c hab hbc              goalsLeft=1  sig=a ∣ c
  2 obtain ⟨x, rfl⟩ := hab           goalsLeft=1  sig=a ∣ c
  3 obtain ⟨y, rfl⟩ := hbc           goalsLeft=1  sig=a ∣ a * x * y
  4 exact ⟨x * y, by ring⟩           goalsLeft=0  sig=""
verified=true
```

**这就是 N1 的信号**，两个现象都直接可见：

1. **同一签名在一条轨迹里反复出现**：`2 ∣ n * n + n` 连续出现在第 2、3 步；`a ∣ c` 出现在第 1、2 步。
   它们正是"本该存在、但库里没有"的中间引理的候选。
2. **中间签名就是可出题的需求**：`2 ∣ n * (n + 1)` 这条签名如果对应一条库引理
   （`Nat.even_mul_succ_self`），第 4、5 步就能被一条 `exact` 取代 —— 这正是
   "压缩收益 Δlen" 的可见形态。

代价：一次 `trace` 批处理 frontend **78.1 s**（Mathlib 模式，2 条轨迹）。
轨迹比 `verify` 贵（每条要多跑若干次 tactic），P2 正式跑时要按批摊薄。

## 已知局限（v1，写进代码注释）

* 轨迹按**行**切分逐行执行，只对"一行一个 tactic"的脚本成立；带 `·` bullet 或多行
  `cases ... with` 的脚本会被切成对不上块的片段，这些步骤如实标 `ok=false`（不静默丢弃）。
  后续版本改用真正的 tactic 序列解析。
* `cost` 目前是"步数 + 该步后剩余子目标数"的代理，不是真正的搜索展开数（那要接 MCTS，属 P3）。
* 轨迹的真值不自己判：`traceScript` 照旧调用 `Verify.verify`，每条轨迹都带 `verified` 结论，
  不会出现"有轨迹但没过验证"的假数据。

## 现在在哪 / 下一步

Mathlib 级工作负载（12 条，参考证明全过）与轨迹层（`trace` 命令 + 真实输出）就绪；
P2 的下一块是**需求挖掘与闸门 G2**：

1. `graph/schema.py` + `graph/demand.py`：`mine(traces) → [(sig, freq, cost, score)]`，
   含**分层统计**（成功轨迹 / 失败轨迹）与**跨目标一致性过滤**（同一签名要出现在 ≥ m 个不同目标上，
   挡掉 MCTS 死路伪影）；
2. `tests/run_gate_g2.py`：对工作负载跑 k 次采样 → `trace` 每条候选 → 挖需求 → 判 G2
   （重复子目标是否常见、过滤前后有何差别）；不过则 N1 降级，主线只留 N2 + N3。
