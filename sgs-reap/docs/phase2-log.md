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

**1. reap 的模块系统不向下游传递 `Init` 的数据类型实例。**

只 `import` reap 的文件里，`n * n`（`HMul ℕ ℕ ℕ`）、`n ^ 2`、甚至 `0`、`1`
（`OfNat ℕ`）都无法 elaborate。做了 5 组对照后确认：问题只出现在 import 了 reap 模块的文件中，
无 import 或只 `import Lean` 的文件一切正常；显式补 `import Init.Data.Nat.Basic` 或在文件顶部加
`module` 都不能恢复。

因此本阶段的目标改用 **Prop 级表述**，并在文件头写清原因。这一条的直接后果是：
**要用 ℕ/ℝ 级目标做 M1 标定，必须先建立 Mathlib 工程**——它同时是阶段 4 的前置条件。

**2. 一个反直觉的观察：利用率 100% 不等于有用。**

在 Prop 级目标上，模型给出的三条候选都是重言式（`P → Q`、`P → (P → Q) → Q`、
`(P → Q) → P → Q`），全部可 elaborate；其中 `P → Q` 其实就是上下文里已有的假设 `himp`。
Guide 却给了它最高分 7。这说明 **M1 不能只看利用率**，还必须同时看候选的"非平凡性"
（是否只是重述已有假设或目标），否则指标会被重言式刷满。

## 下一步

阶段 2 剩余工作（闸门 M1）：在 20 道题上标定候选利用率 / 延迟 / token。
由于上面第 1 条，标定需要先能跑 ℕ/ℝ 级目标，因此下一步是：

1. 建立 Mathlib 工程（`require mathlib` + `lake exe cache get`），作为 reap fork 之外的独立测试包；
2. 从 `SGS/data/D_3k_prover_dataset.json` 抽 20 道题，跑 `/conjecture` + elaboration 检查，
   统计利用率、p50/p95 延迟、每类调用 token；
3. 同时记录"非平凡率"（候选不等于目标、也不等于任一已有假设）。
