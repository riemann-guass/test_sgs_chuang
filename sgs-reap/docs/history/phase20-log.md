# 阶段 20 执行日志（阶段 C：硬门接线 + 第一个真实引理库）

本单元把 **G 的三件硬门 + 求解 + 验证 + 物化 + 入库** 串成一条漏斗。在此之前
`Trivial` / `Novelty` / `Materialize` 三个模块都实现了、也各自有离线测试，
但**没有任何 harness 调用过它们**（审计结论）——所以"硬门"实际上不存在，
阶段 B 就吃过亏：候选全是抄需求，门检照样 15/15 通过。

## 交付物

| 文件 | 作用 |
|---|---|
| `tests/build_library.py`（新） | 阶段 C harness：候选 → 硬门 → 求解 → 验证 → 物化 → 入库，每一步的淘汰数都进报告 |
| `tests/lean_server.py`（改） | `budget_for_jobs()`：心跳预算按批大小放大（见下面的"发现 1"） |

漏斗：

```
Phase B 的候选
  ↓ ① Gate.check        是不是合法命题
  ↓ ② Trivial.isTrivial 平凡（decide/simp/aesop 秒杀）则淘汰
  ↓ ③ Novelty.isNew     与父目标/库中等价则淘汰
  ↓ ④ /solve            Solver 出 k 篇证明
  ↓ ⑤ Verify.verify     至少一篇过 kernel 终检
  ↓ ⑥ Materialize.emit  落成有名常量 + 编译校验
  ↓ ⑦ graph/library.py  入库（带来源字段）
```

## 离线验证：平凡门确实在拦人

用假服务产出的候选（`True` / `1 = 1` / `∀ n, n = n`，9 条）跑漏斗：

```
漏斗：{'candidates': 9, 'rejected_trivial': 6, 'passed_hard_gates': 3, 'rejected_unprovable': 3}
淘汰原因：{'trivial:decide': 3, 'trivial:simp': 3, 'solve:no_valid_proof': 3}
判定：pass_no_survivor
```

6 条被平凡门拦下（`decide` 3 条、`simp` 3 条）——**这正是阶段 B 缺失的那一环**：
那些"提到目标符号的重言式"以前能一路绿灯，现在会被拦。

## 真跑：第一个引理库

```powershell
python tests\build_library.py --candidates experiments\runs\conj_demand_20260920T042755Z\candidates.jsonl ^
       --limit 5 --k 3 --endpoint http://127.0.0.1:8770/solve
```

```
漏斗：{'candidates': 15, 'passed_hard_gates': 13, 'rejected_trivial': 1, 'rejected_novelty': 1,
       'proof_candidates': 39, 'verified': 4, 'rejected_unprovable': 9,
       'materialized': 4, 'library_written': 4}
淘汰原因：{'trivial:aesop': 1, 'novelty:duplicate': 1, 'solve:no_valid_proof': 9}
[lib] 物化 4 条 → Library.lean；编译校验 True
[lib] 判定：pass
```

入库的 4 条（`experiments/library.jsonl` + `sgslean/generated/Library.lean`）：

| 引理 | 证明 | 评价 |
|---|---|---|
| `Real.logb 8 a + Real.logb 4 (b^2) = 5 → Real.logb 8 a + Real.logb 4 (b^2) = …` | `intro; rfl` | ⚠️ **重言式漏网**（见遗留 1） |
| `… = 5 → … = 7 → Real.logb 8 a + Real.logb 8 b + Real.logb 4 (a^2) + Real.logb 4 (b^2) = 12` | `linarith` | ✅ 两条已知等式合并，对目标有用 |
| `∀ (x:ℝ) (h₀ : 0 < x), Real.logb 2 x = Real.logb 8 x * 3` | `simp [Real.logb]; rw [log_pow]; field_simp; ring` | ✅ 换底公式 |
| `∀ x y, 0 ≤ x → 0 ≤ y → x^(3/2) * y^(3/2) = (x*y)^(3/2)` | `rw [← Real.mul_rpow]` | ✅ 幂运算分配律 |

**"候选 → 硬门 → 求解 → 验证 → 物化 → 入库"第一次端到端跑通，且生成的 Lean 文件编译通过。**

## 发现 1（重要）：心跳是**按批累计**的，固定预算会让批次尾部集体报 exception

离线漏斗第一版用 9 条候选（`True` / `1 = 1` 这种）配 4,000,000 心跳，结果
**8/9 条候选在门检就被判 `exception`**。错误原文：

```
(deterministic) timeout at `whnf`, maximum number of heartbeats (5800) has been reached
```

关键在于 **5800 不是预算，而是"剩余额度"**：一整批作业都跑在同一个
`example : True := by run_tac ...` 里，而 Lean 的心跳计数器是**按 command 累计**的。
第一条候选的 `novelty` 作业要对着 37 条 miniF2F 大语句逐条 `isDefEq`，几百万心跳瞬间烧光，
后面所有作业就集体超时。

**这回头解释了 phase17**：miniF2F 的 G1 每批 60 条作业配 4,000,000 心跳，
于是"每批后半段集体报 exception"——被误读成"模型证不出"。phase18 把预算提到 40,000,000、
164 条一批只剩 11 条 exception，与"累计"模型一致（≈240k 心跳/作业）。

修法两条：① `budget_for_jobs()` 按批大小放大预算；② **缩短 `against`**（见发现 2）。
修完批次耗时从 210 s 降到 65 s，`exception` 归零。

（真正彻底的做法是每个作业前后复位计数器，或改成每作业一个 command——`withCurrHeartbeats`
的作用域语义需要单独验证，留作后续。）

## 发现 2：`Novelty` 的 `against` 用错了，而且很贵

第一版把**整份 hard 目标集（37 条大语句）**都塞进 `against`。这既违背设计
（`Novelty` 拦的是"重述**父目标**"与"重述库中已有引理"），又把批次预算吃光。
改成"父目标 + 库语句（可选加全部目标）"后，语义与设计一致，成本降了一个量级。

## 遗留

1. **重言式仍会漏网**：`sgs_lem_1` 是 `X = 5 → X = X`，被 `rfl` 一秒证出。
   平凡门（`decide`/`simp`/`aesop`）没能拦住它——因为它的"平凡"体现在**证明极短**，
   而不是"命题本身能用 simp 秒杀"。硬门需要补一条"证明过于廉价则淘汰"（可用
   `Measure.compression` 里的步数/字符数，或直接看证明是否只含 `rfl`/`exact`/`trivial`）。
2. **相关度仍是文本代理**：正式版应由 Lean 侧抽取两侧常量集合求交。
3. **阶段 D 未做**：`cover` 仍是旧的 `sig_cover`（空转的那一套），真正的
   `proved_cover`（有库/无库的对照）还没实现。

## 现在在哪

**阶段 A / B / C 完成。** 项目终于有了 SG-Lean 的完整单轮链路：

```
hard 目标 → 需求 → 猜想引理 → 门检 → 硬门 → 求解 → 验证 → 物化 → 入库
```

下一步是**阶段 D**：把 N2 的 `cover` 换成真定义（`proved_cover(S) − proved_cover(∅)`，
在 hard 目标集上测），然后才是阶段 E 的多轮闭环与 H1/H2。
