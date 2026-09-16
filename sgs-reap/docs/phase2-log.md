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

## 下一步

阶段 2 剩余工作：把上述链路接到 Lean 侧（`reap.conjecture_endpoint` 指向 proxy），
在 20 道题上标定候选利用率 / 延迟 / token，形成闸门 M1 的判定数据。
