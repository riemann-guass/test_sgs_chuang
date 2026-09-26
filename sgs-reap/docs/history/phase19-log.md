# 阶段 19 执行日志（阶段 A 收尾 + 阶段 B：把猜想器接上）

本单元一次做两件事：**① 修掉"每个 chunk 重付一次 Mathlib 导入"的浪费；② 补上审计里最根本的偏离——
把 `/conjecture` 接进流水线**。

## ① 批处理导入浪费

`tests/lean_server.py` 的 `verify_all(chunk=0)` 现在表示**一次全喂**。原因是服务端每处理一个
flush 都会新起一个 `lean` 子进程（`Server.runBatch`），而每个子进程都要重新导入 Mathlib
（≈2 min 热 / ≈8 min 冷）。分 5 批就白付 5 次导入——phase18 的 164 条候选跑了 2549 s，
其中相当一部分是导入。

安全性由"逐条落盘"保证（`Server.runJobs` 每处理完一条就写 `out.json`）：即使某个候选打崩子进程，
也只丢它自己那一条。真正的 REPL 化（子进程跨 flush 常驻）留作后续；在"请求可一次算出来"的场景下，
一次全喂已经等价。

## ② 阶段 B：需求 → 候选引理 → 门检

### 交付物

| 文件 | 作用 |
|---|---|
| `service/prompts.py`（改） | `conjecture_prompt` 新增 `demand` / `seeds` 两个**可选**区块 |
| `service/proxy.py`（改） | `/conjecture` 透传 `demand` / `seeds`，meta 回报 `demand_used` / `seeds_used` |
| `service/mock_server.py`（改） | 回显 demand/seeds 条数（离线断言用） |
| `graph/conjecture.py`（新） | N1 的下游：组装请求、解析候选、读需求与库范例 |
| `tests/run_conjecture.py`（新） | 阶段 B harness：目标 + 需求 → 候选 → 门检 → 判定 |
| `tests/test_prompts.py`（新） | 提示词离线单测（**空需求必须等于没有需求**，否则 H1 对照不干净） |

### 离线链路（假服务）

```
[conj] 目标 3 条；需求 4 条；端点 http://127.0.0.1:8765/conjecture
[conj] 候选 9 条 / 过门检 9 条 / 有候选过门检的目标 3/3
[conj] 判定：pass
```

反向对照（`--no-demand`）：`{'all_zero': True, 'values': [0, 0]}` —— 端点上确实没有需求。

### 真模型第一次跑：**门检 15/15 通过，但一条都没用**

```
[conj] 候选 15 条 / 过门检 15 条 / 有候选过门检的目标 5/5 → pass
```

看上去完美。但把候选打出来看，**15 条全是把 DEMAND 原样抄回来的**：

```
∀ (n : ℕ), n + 0 = n
∀ (n : ℕ), n * 0 = 0
∀ (n : ℕ), n ≤ n * n + 1
∀ (n : ℕ), n < n + 1
```

而目标是 miniF2F 的 `ℝ`/对数题。四个原因叠在一起：

1. 第一版提示词把需求写成裸清单，措辞是"a lemma that discharges one of them is especially
   valuable"——模型把它理解成"**要输出的答案**"，而不是"背景证据"；
2. 需求数据本身**来自另一个语料**（初等引理集的 G2 轨迹），与这批 miniF2F 目标无关；
3. 门检只判"是不是合法命题"，**判不了相关性**——输出任意真命题都能刷满这个指标；
4. 需求签名写的是证明状态形式 `n : ℕ ⊢ n + 0 = n`，模型把它改写成 `∀ (n : ℕ), ...` 输出，
   这进一步印证是"抄写 + 改写"，不是"据此造引理"。

### 修法（两处，都属于方法论层面的修正）

**提示词**：区块改名为 `BACKGROUND EVIDENCE (not the answer)`，并加两条硬约束——
`Do NOT output these subgoals (or trivial rephrasings of them) as your answer.`
以及 "lemma **MUST mention at least one symbol** that occurs in the TARGET"。

**判定**：新增**相关度**要求（过门检 ∧ 与目标符号相关），并新增判定码 `fail_irrelevant`
——"过门检但与目标无关"单列一类，不允许混进 pass。相关度用的是文本符号重叠这个廉价代理
（排除 `∀`/`→`/`ℕ` 这类无处不在的符号），正式版本应改由 Lean 侧抽取常量集合求交。

### 真模型第二次跑（修正后）

```
[conj] 候选 15 条 / 过门检 15 条 / 有候选过门检的目标 5/5
[conj] 相关候选 15 / 15；有相关候选的目标 5/5
[conj] 判定：pass
```

候选样例（`[ok]` = 过门检，共享符号 = 与目标文本的交集）：

| 目标 | 候选（节选） | 共享符号 |
|---|---|---|
| `aime_1984_p5` | `∀ (a b : ℝ), a * b = 512 → Real.logb 8 (a * b) = Real.logb 8 512` | `Real.logb,a,b,ℝ` |
| `aime_1987_p8` | `∀ n k : ℕ, 0 < n → (8:ℝ)/15 < n/(n+k) → (n:ℝ)/(n+k) < 7/13 → 8 * (n+k) < 15 * n` | `k,n,ℝ` |
| `aime_1988_p3` | `∀ (x : ℝ) (h₀ : 0 < x), Real.logb 2 x = Real.logb 8 x * 3` | `Real.logb,h,x,ℝ` |
| `aime_1988_p4` | `∀ (n : ℕ) (a : ℕ → ℝ) (h₀ : …) (h₁ : …), ∑ k ∈ Finset.range n, abs (a k) < n` | `Finset.range,a,abs,k,n,ℝ,∑` |
| `aime_1990_p2` | `52 - 6 * Real.sqrt 43 = (Real.sqrt 43 - 3) ^ 2 * 4` | `Real.sqrt` |

这些是**真的能用**的中间引理形状（换底公式、不等式代数变形、根式配方），与第一版的原样抄写
形成鲜明对比。

## 现在在哪 / 下一步

**阶段 A 完成、阶段 B 完成。** `/conjecture` 第一次被接进流水线，并且跑通了
"hard 目标 + 需求 → 候选引理 → 门检"。

遗留（按优先级）：

1. **相关性仍可被"重言式"刷满**：`aime_1984_p5` 的第一条候选是
   `X → X = X` 这种恒真式——它提到了目标的符号，所以过了相关度这一关。
   这正是**硬门（非平凡/新颖）**要拦的东西 → 阶段 C 立刻要用上 `Trivial.isTrivial` 与
   `Novelty.isNew`（两件都已实现，只是还没接进任何流程）。
2. **需求数据与目标集不匹配**：现在的 `g2_demand.json` 来自初等引理集，而目标集是 miniF2F。
   要真正验证 H1，需求必须在**同一目标集**上挖（需要在该集合上跑一次 G2，会花 API 钱）。
3. 相关度的正式版本（Lean 侧常量集合求交）应在阶段 C 与硬门一起接线。
