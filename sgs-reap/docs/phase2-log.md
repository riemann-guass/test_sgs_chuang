# 阶段 2 执行日志（真实模型接入）

后端：DeepSeek 官方 API，`https://api.deepseek.com`，模型 `deepseek-flash`（用户所指"v4.1 Flash"）。
`GET /models` 返回两个可用模型：`deepseek-flash`、`deepseek-v4-pro`。
密钥存放在 `service/.env`，已被 `.gitignore` 排除，仓库中只有 `.env.example`。

## 交付物

| 文件 | 作用 |
|---|---|
| `service/config.py` | `.env` 读取、UTF-8 控制台 |
| `service/backend.py` | OpenAI 兼容客户端：缓存、重试、token/延迟统计、thinking 开关 |
| `service/prompts.py` | SGS 移植的 conjecturer prompt / guide rubric / 三个 `<begin_*_score>` 解析器 |
| `service/proxy.py` | 实现 v1 契约的 HTTP 服务（`/health`、`/stats`、`/conjecture`、`/guide`） |
| `service/check_backend.py` | 探测模型列表（不打印密钥） |
| `service/smoke_chat.py` | 最小 chat 调用 |
| `service/probe_reasoning.py` | 推理预算与参数探测 |
| `service/probe_guide.py` / `probe_guide_variance.py` | Guide 行为诊断 |
| `tests/run_proxy_smoke.py` | 启停代理 + 真实调用 + 落盘 `experiments/proxy_smoke.json` |

## 实测结论

### 1. 推理预算：必须显式关闭 thinking

`deepseek-flash` 是推理模型，`max_tokens` 同时覆盖 reasoning 与可见回答。

| 配置 | completion tokens | reasoning tokens | 结果 |
|---|---:|---:|---|
| `max_tokens=8192` | 8192 | 8192 | `finish_reason=length`，**content 为空** |
| `max_tokens=16384` | 3252 | 3182 | 正常，3 条候选 |
| `max_tokens=32768` | 10410 | 10349 | 正常，3 条候选 |
| `+reasoning_effort: low` | 4798 | 4728 | 正常（降幅有限） |
| **`thinking: {"type":"disabled"}`** | **83** | 0 | 正常，候选质量抽查相当 |

因此 `backend.py` 默认发送 `thinking={"type":"disabled"}`，并把它作为可消融开关
（环境变量 `DISABLE_THINKING=0` 可打开对照）。单次调用成本因此降低约 **40–120 倍**。

### 2. 冒烟链路（1 个目标，3 条候选）

目标：`n : ℕ ⊢ 2 ∣ n ^ 2 + n`

```
/conjecture -> [0] ∀ k : ℕ, 2 ∣ k * (k + 1)
               [1] n % 2 = 0 → 2 ∣ n ^ 2 + n
               [2] n % 2 = 1 → 2 ∣ n ^ 2 + n
/guide      -> [0] review=3   [1] review=7   [2] review=7
/stats      -> calls=4 prompt_tokens=2687 completion_tokens=1292 reasoning=0 avg_latency=2033ms
```

候选 0 正是证明该目标的关键引理（`n²+n = n(n+1)`，相邻整数之积为偶数），
却被 Guide 判成 `review=3`。这是**首个值得深挖的异常**。

### 3. 异常归因：是采样方差，不是系统性失效

同一候选、同一 prompt 重复评测（`probe_guide_variance.py`，每个候选 5 次）：

| 候选 | T=1.0 的 reviews | T=1.0 stdev | T=0.0 的 reviews | T=0.0 stdev |
|---|---|---:|---|---:|
| `∀ k, 2 ∣ k*(k+1)` | 8, 8, **3**, 8, 8 | 2.00 | 8, 8, 8, 8, 8 | **0.00** |
| `n % 2 = 0 → 2 ∣ n^2+n` | 6, 7, 7, 7, 7 | 0.40 | 7, 7, 6, 7, 6 | 0.49 |
| `∀ a b, 2 ∣ a → 2 ∣ a*b` | 7, 6, 7, 6, 6 | 0.49 | 7, 7, 6, 7, 7 | 0.40 |

结论：

1. 冒烟里那个 `review=3` 是**长尾样本**，不是 Guide 系统性判错；
2. `temperature=0` 显著降低方差（最优候选从 stdev 2.00 降到 0.00），因此
   `GUIDE_TEMPERATURE` 默认改为 **0.0**；
3. 真正需要警惕的不是噪声而是**动态范围过窄**：三个候选在 T=0 下是 8.0 / 6.6 / 6.8，
   全部挤在 6–8 之间。经 softmax 后近似均匀，作为搜索先验的区分度有限。
   这直接影响闸门 M1 的判据，需要在阶段 3 先验标定时一并处理（可选：改用 `relevance`
   子分数、批内减去均值、或提高 `conjecture_temperature` 的敏感度）。

## 步骤 2：真实模型端到端（Lean → proxy → DeepSeek）

新增 `reap-fork/tests/ConjectureRealE2E.lean` 与 `tests/run_real_e2e.py`。两个用例：

* **A**：经 HTTP 取回候选，逐条在当前上下文中按 `have sgs_aux_i : <type> := ?_` 做
  elaboration 检查——这就是"候选引理利用率"的 Lean 侧定义；
* **B**：`runMCTS` 走生产入口 `generatePolicyValueWithConjecture`（`maxSteps=1` 只展开根节点，
  用于控制 API 调用数），断言真实候选确实成为树边。

实测（目标 `P Q : Prop, h : P, himp : P → Q ⊢ Q`）：

```
[real-e2e] 候选 3 条，Lean 接受 3 条（利用率 100%）
           review=7    P → Q
           review=5    P → (P → Q) → Q
           review=5    (P → Q) → P → Q
[real-e2e] MCTS 根节点获得 3 条猜想边
[real-e2e] wall-clock: {conjecture: 1, guide: 1, tactic_eval: 3, tactic_gen: 1, value: 1, premise_select: 1}
```

### 本轮发现并修掉的问题

**1. `ℕ` 记法缺失被 autoImplicit 伪装成"实例缺失"。（一次误判的完整记录）**

首轮失败信息是 `failed to synthesize instance: HMul ℕ ℕ ?m.4` / `OfNat ℕ 0`，
看起来像"reap 的模块系统没有把 `Init` 的实例传给下游文件"。我据此做了 5 组对照
（换 import 顺序、补 `import Init.Data.Nat.Basic`、加 `module` 关键字……），全部无效。

真正的原因是**记法**：本项目（reap 的 `public meta import` + 未引入 Mathlib）环境里
没有 `ℕ` 这个 unicode 记法。缺少它时 Lean 的 `autoImplicit`（默认开启）把 `ℕ` 当成
**隐式绑定变量**，于是 `n : ℕ` 里的 `ℕ` 是一个未解析的元变量，
`n * n` 自然报 `HMul ℕ ℕ ?m.4`——错误信息里的 `ℕ` 是那个元变量，不是 `Nat`。

决定性对照（同一环境下只改一处）：

| 文件内容 | 结果 |
|---|---|
| `import Reap.Tactic.Conjecture` + `example (n : Nat) : n * n = n * n := rfl` | 通过 |
| 同上但把 `Nat` 换成 `ℕ` | `HMul ℕ ℕ ?m.4` 失败 |
| 只有 `example (n : ℕ) : n * n = n * n := rfl`（不 import 任何东西） | 同样失败 |
| 加一行 `notation "ℕ" => Nat` 后 | 通过，`^` / `%` / `∣` / `∀` 全部可用 |

结论：`Nat` 级别的算术记法一直是可用的，**根本不需要 Mathlib**；只需在测试文件里补一行
`notation "ℕ" => Nat`。这一行还有一个重要作用：让模型输出里的 `ℕ` 能被解析，
否则候选会因为"记法缺失"而不是"数学错误"被拒，利用率指标会被系统性拉低。

（教训：`autoImplicit` 会把"未知标识符"变成"看似合理的实例错误"，诊断时应先做
"把 unicode 换成 ASCII"这类最小对照。）

**2. 一个反直觉的观察：利用率 100% 不等于有用。**

在 Prop 级目标 `P Q : Prop, h : P, himp : P → Q ⊢ Q` 上，模型给出的三条候选都是重言式
（`P → Q`、`P → (P → Q) → Q`、`(P → Q) → P → Q`），全部可 elaborate；其中 `P → Q`
其实就是上下文里已有的假设 `himp`，Guide 却给了最高分 7。这说明 **M1 不能只看利用率**，
还必须同时看候选的"非平凡性"（是否只是重述已有假设或目标），否则指标会被重言式刷满。

### 修正后的 ℕ 级实测

补上 `notation "ℕ" => Nat` 后，用 ℕ 级目标 `2 ∣ n ^ 2 + n` 重跑：

```
[real-e2e] 候选 3 条，Lean 接受 2 条（利用率 67%）
           review=8    ∀ n : ℕ, 2 ∣ n * (n + 1)
           review=8    ∀ n : ℕ, Even (n * (n + 1))
           review=7    ∀ n : ℕ, n ^ 2 + n = n * (n + 1)
           拒绝: Even ... :: Unknown identifier `Even`
[real-e2e] MCTS 根节点获得 2 条猜想边
```

两点值得记入 M1 的方法论：

* 被拒的那条**不是数学错误**，而是 `Even` 属于 Mathlib、本项目未引入。因此 M1 统计利用率时
  必须把拒绝原因分类：`记法/解析失败`、`未知标识符（缺库）`、`类型错误`。
  只有第三类才是模型真正的数学问题。
* 三条候选的 Guide 评分是 8 / 8 / 7，区分度依然偏窄（与前面方差实验的结论一致）。

## 步骤 3：闸门 M1 标定（20 个目标）

`reap-fork/tests/Calibrate.lean` + `tests/run_m1_calibration.py`。20 个含真实证明上下文的
初等目标（ℕ 与 Prop 级），每目标请求 2 条候选，逐条做 elaboration 检查并分类拒绝原因。

### 结果

```
目标数 20  候选 40  接受 36  利用率 90.0%
非平凡候选 39  非平凡率 97.5%
拒绝原因分布 {unknown_identifier: 1, exception: 1, type_error: 2}

conjecture  calls=20  p50=852ms  p95=1215ms   prompt=5102   completion=916
guide       calls=20  p50=7806ms p95=14693ms  prompt=32156  completion=45104
合计 40 次调用，prompt=37258，completion=46020，平均每目标 4164 token
```

### 闸门判定：通过

判据是"候选利用率低于 ~20% 就先换 prompt 或换模型"。实测 **90%**，远高于阈值，
且 40 条候选只有 4 条被拒，其中只有 2 条属于数学/类型问题。**M1 通过，可以进入阶段 3。**

### 但结果暴露了三个必须在阶段 3 处理的问题

**1. Guide 是成本与延迟的绝对瓶颈。**

| 端点 | 调用 | p50 | p95 | completion tokens |
|---|---:|---:|---:|---:|
| conjecture | 20 | 852ms | 1215ms | 916 |
| guide | 20（含 40 次候选评分） | 7806ms | 14693ms | **45104** |

Guide 的 completion token 是 conjecture 的 **49 倍**（因为 rubric 需要逐条打分推理），
延迟高一个数量级。而我们的目标是"用 Guide 当先验"，先验只是锦上添花——
这条成本结构不合理。

**2. Guide 的分数与候选可用性几乎不相关。**

* `prop_and_comm`：两条候选都拿了 review=6，但**两条都不可用**；
* `nat_mul_zero`：reviews=[3, 0]，而两条候选都被 Lean 接受；
* 接受率最高的一组（`nat_le_double`、`nat_le_sq_plus_one`）反而是 [3, 8]。

这与前面的方差实验互为印证：**Guide 的信号弱，且窄**。阶段 3 必须先做先验标定，
不能默认"review 高就是好先验"。

**3. 模型在 Prop 目标上会输出"证明项"而不是"命题"。**

`prop_and_comm` 的两条候选是 `fun h : P ∧ Q => h.right` 与 `... .left`——
这是证明项，不是命题，因此 `have sgs_aux_i : <它> := ?_` 必然失败。
这属于 prompt 约束不足，而不是模型数学能力问题，需要加固 prompt。

### 由此确定的阶段 3 改动（按收益排序）

1. **把 elaboration 检查提到 Guide 之前**：先滤掉不可用的候选，再对通过者调用 Guide。
   当前实现是"先花 Guide 的钱、再发现候选不可用"，`prop_and_comm` 那两条就是白花钱。
2. **Guide 批量化**：一次请求给一条目标的多条候选打分，而不是每条候选一次调用。
3. **加固 conjecture prompt**：明确"输出命题（type），不要输出证明项（proof term）"。
4. 先验标定：用 `reap.raw_tree_path` 统计 policy logprob 的实际尺度，再定 β。

## 阶段 2 完成

代理、提示词、解析器、真实模型端到端、M1 标定全部完成并入库。

## 下一步

阶段 3：把上述四项改动落到 fork 与 service，新增 `sgsMCTS` tactic（搜索开始时重置
猜想预算与熔断计数），并让四组对照配置全部通过 option 切换。
