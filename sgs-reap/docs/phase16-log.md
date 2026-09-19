# 阶段 16 执行日志（P4 准备：miniF2F 测试集接入）

P4 是闭环与三组对照（G4 / H1 / H2），开工前要先把**测试集**换成公开基准。
用户拍板用 **miniF2F**，本单元把它接进来并过一遍我们的门检。

## 一、为什么不是 openai/miniF2F 的原版

先拉了原版，发现是 **Lean 3 语法**（`begin … end`、`open_locale big_operators`、
小写 `finset.Icc`），喂给 Lean 4 直接不成立。改用 **Lean 4 移植版**：

```powershell
cd sgs-reap\data\raw
git clone --depth 1 https://github.com/yangky11/miniF2F-lean4.git
```

一题一文件：`MiniF2F/Valid/<problem>.lean`（244 条）、`MiniF2F/Test/<problem>.lean`（244 条），
每条形如

```lean
theorem aime_1983_p1 (x y z w : ℕ) (ht : 1 < x ∧ …) (h0 : …) : Real.log w / Real.log z = 60 := by sorry
```

（原始语料放在 `data/raw/`，已 gitignore；转换结果入库。）

## 二、转换规则（机械、可复核）

`tools/minif2f_to_jsonl.py`：

1. 取 `theorem` 到第一个 `:=` 之间的部分，去掉定理名；
2. 在**第一个顶层 `:`** 切一刀（括号/花括号/方括号外），左边绑定组、右边结论；
3. 绑定组**原样**放进 `∀`：`∀ (x y z w : ℕ) (ht : …), <结论>`。之所以不把假设改写成 `→`：
   在我们的语境下两者等价，但原样保留能避免"依赖型假设"被拆错；
4. 每条语句**必须过服务端 `Gate.check`**（能 elaborate 成命题）才写进 JSONL，
   没过的记进 `experiments/results/minif2f_import.json`（附原因），绝不静默丢弃。

样例（转换结果）：

```
aime_1983_p9 -> ∀ (x : ℝ) (h₀ : 0 < x ∧ x < Real.pi), 12 ≤ (9 * (x ^ 2 * Real.sin x ^ 2) + 4) / (x * Real.sin x)
```

## 三、Gate 校验结果（Mathlib 模式，4 批）

```powershell
python tools\minif2f_to_jsonl.py --check --chunk 125
```

```
[minif2f] valid: 244 题，解析成功 244
[minif2f] valid 批 1: 已过 117 / 拒 8
[minif2f] valid 批 2: 已过 229 / 拒 15
[minif2f] 写入 data\minif2f_valid.jsonl（229 条）
[minif2f] test: 244 题，解析成功 244
[minif2f] test 批 1: 已过 118 / 拒 7
[minif2f] test 批 2: 已过 236 / 拒 8
[minif2f] 写入 data\minif2f_test.jsonl（236 条）
```

| split | 文件数 | 解析成功 | 过门检 | 被拒 | 入库文件 |
|---|---|---|---|---|---|
| valid | 244 | 244 | **229** | 15 | `data/minif2f_valid.jsonl` |
| test | 244 | 244 | **236** | 8 | `data/minif2f_test.jsonl` |

合计 **465/488 条**成为我们可用的闭式语句，23 条被 `Gate` 拒绝（原因逐条记在
`experiments/results/minif2f_import.json`，多为"语句按我们的闭式口径 elaborate 不通过"，
不是数学错误——这类差异必须在报告里如实标注，不能当成 miniF2F 的题目有问题）。

## 四、P4 的数据纪律（写进规划，不许破）

* `data/minif2f_valid.jsonl`（229 条）→ **工作负载 W**：库构建、需求挖掘、出题都用它；
* `data/minif2f_test.jsonl`（236 条）→ **held-out 评测**：只用于最后测 pass@k，
  **不许**进库构建、不许参与需求挖掘（和 `data/heldout.jsonl` 同一条纪律）；
* 现有小数据（`workload.jsonl` / `lemmas_g1.jsonl` / `workload_mathlib.jsonl`）保留，
  继续做管道级冒烟与回归，不再承担研究结论。

## 五、这次准备还差什么（P4 开工前）

1. **报告文件名防覆盖**：总览检查时已经真实踩到（小规模重跑覆盖了 G1 正式报告）。
   机制修法：报告名带 mode/规模后缀（如 `g1_real63.json`）——本单元**尚未做**，列为 P4 第一件事。
2. **用 miniF2F 跑一次 G1**：现在的 G1 结论（mean 0.873）是**初等引理集**上的；
   miniF2F 的 465 条是 Mathlib 级、明显更难，`solve_rate` 大概率大幅下降——
   这才是"SG-Lean 的环能不能转"的真实前提。跑它要花 API 费用（按 G1 的比例估：约 200–400 次调用、
   几万 token、几十分钟 Lean 时间），**跑之前先问**。
3. **runner 骨架**：`graph/runner.py`（轮次驱动：采集 → 需求 → 生成 → 门检 → 求解 → 选择 → 库更新 → 记忆注入 → held-out 评测）
   与 `tests/run_round.py`（三组对照 + 消融）。
