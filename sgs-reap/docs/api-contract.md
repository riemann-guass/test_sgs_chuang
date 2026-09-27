# 服务层接口契约（v2，双尺度连接重定稿）

模型代理提供健康检查和三个业务端点：`/conjecture`、`/solve`、`/guide`。
契约冻结后，生成侧、调用侧与 Lean 验证侧可以独立开发与测试。

> v2 把引理库快照正式写入 `/solve` 契约，并区分产品提示词与中性测量提示词。
> `/guide` 保留为 A 组对照；证明真伪仍只由 Lean 侧判定。
> **迁移状态**：本文件描述目标契约；当前代码尚未完整实现 `snapshot_hash`、
> `prompt_mode` 与 `sample_salt`，正式实验前必须补齐并加入契约测试。

基址默认 `http://127.0.0.1:8765`，Lean 侧通过 `reap.conjecture_endpoint` / `reap.guide_endpoint` 配置。

## 通用约定

- 传输：HTTP/1.1 + `Content-Type: application/json; charset=utf-8`
- 超时：客户端连接 5s；`/conjecture` 读 60s；`/guide` 读 120s（含 CoT）
- 重试：连接失败或 5xx 重试 3 次，指数退避 1s/2s/4s；4xx 不重试
- 错误体：`{"error": {"code": "<机器可读码>", "message": "<人类可读说明>"}}`
  - 400 `bad_request`、422 `invalid_params`、500 `internal_error`、503 `backend_unavailable`、504 `backend_timeout`

## `GET /health`

响应：`{"status": "ok", "backend": "<后端名>", "version": "v2"}`

## `POST /conjecture`

请求：

```json
{
  "goal_state": "P Q : Prop\nh : P\nhimp : P → Q\n⊢ Q",
  "num_samples": 3,
  "depth": 0,
  "target": "P → Q",
  "request_id": "3f1c…"
}
```

| 字段 | 类型 | 必填 | 说明 |
|---|---|---|---|
| `goal_state` | string | 是 | reap 的 `Meta.ppProofState` 输出；多目标以换行分隔 |
| `num_samples` | int | 否 | 默认 3，范围 1–8 |
| `depth` | int | 否 | 当前搜索深度，用于成本统计与分层采样 |
| `target` | string | 否 | 主目标类型；缺省时服务端取 `goal_state` 中第一个 `⊢` 之后的内容 |
| `request_id` | string | 否 | 追踪用，服务端原样回传 |

响应：

```json
{
  "candidates": [
    {"index": 0, "type": "∀ (n : ℕ), 0 < n → n ≤ n * n", "raw": "<模型原始输出>", "review": 6.0}
  ],
  "meta": {"backend": "mock", "model": "mock-1", "latency_ms": 3, "cache_hit": false}
}
```

**`type` 的语义（重要）**：它是 **`have`-ready 的 Lean 命题**，只允许引用当前上下文中已存在的变量，或自行用 `∀` / `→` 引入新变量。客户端负责拼装成：

```lean
have <fresh_name> : <type> := ?_
```

因此服务端**不需要**输出 `theorem` 关键字、定理名与 `:= by sorry`。若模型输出的是完整定理语句，服务端负责规范化。

解析失败时返回 `{"candidates": []}`，不要返回错误——搜索侧会把空候选当作"本次无猜想"。

## `POST /guide`

请求：

```json
{
  "target": "theorem foo (x : ℝ) : x ≤ x * x := by",
  "candidates": [{"index": 0, "type": "∀ (x : ℝ), 0 ≤ x → x ≤ x * x"}],
  "request_id": "3f1c…"
}
```

响应：

```json
{
  "scores": [
    {"index": 0, "relevance": 5, "redundancy": 0, "complexity": 1, "review": 6.0}
  ],
  "meta": {"backend": "mock", "model": "mock-1", "latency_ms": 5, "cache_hit": false}
}
```

`review` 必须由 SGS 原式合成（`sgs/models/guide/llm_judge_guide.py:sub_scores_to_review`）：

```
if complexity ∈ {3, 4}: review = 0
else: review = max(0, relevance + (2 - complexity) + (1 - redundancy))
```

维度取值范围：`relevance ∈ [0,5]`（整数）、`redundancy ∈ {0,1}`、`complexity ∈ [0,4]`（整数）。

## 缓存

- 仅缓存成功响应。
- `/conjecture`：`sha256(goal_state + "|" + num_samples + "|" + depth)`
- `/guide`：`sha256(target + "|" + 各候选 type 按 index 拼接)`

## 客户端职责：Guide 分数 → 搜索先验

reap 的 `visitNode` 使用 `probability := (prior / priorTemperature).exp`，其中 policy 的 `prior` 是 token logprob 之和（量级约 −100…0）。因此 Guide 分数必须换算到同尺度，不能直接用 0–8 的原始分。

约定：

```
p_i     = softmax(review_i / T)          # T 默认 1.0，批内归一化
prior_i = beta * ln(p_i)                 # beta 默认 10
```

`beta` 与 `T` 暴露为 `reap.conjecture_weight` / `reap.conjecture_temperature`，其标定方法在阶段 3 用 `reap.raw_tree_path` 导出的 policy logprob 分布完成：先测出 policy prior 的典型尺度，再让 `beta * ln(p)` 落在同一量级。

## `POST /solve`（v1.1 新增）

`/conjecture` 出的是**命题**（`have`-ready），`/solve` 出的是**证明脚本**。两者都只做文本生成与
轻量规范化，"对不对"一律由 Lean 侧判定——solve 产出的每篇证明都要经 `SgsLean/Server.lean`
的 `verify` 走 reap 的重放 + kernel 终检，成功才算解出。

请求：

```json
{
  "statement": "∀ (n : Nat), n + 0 = n",
  "num_samples": 4,
  "library": [{"name": "sgs_lem_ab12cd34", "stmt": "∀ n : Nat, n + 0 = n"}],
  "snapshot_hash": "sha256:...",
  "prompt_mode": "measurement",
  "sample_salt": "round-2:target-17",
  "request_id": "3f1c…"
}
```

| 字段 | 类型 | 必填 | 说明 |
|---|---|---|---|
| `statement` | string | 是 | 要证的语句；**闭式**（只引用全局常量，或用 `∀`/`→` 自己引入变量） |
| `num_samples` | int | 否 | 默认 4，范围 1–8 |
| `library` | array | 否 | 已物化且在当前 Lean import 中可用的 active 引理；每项只含稳定名与命题 |
| `snapshot_hash` | string | 有库时是 | 冻结库快照哈希；进入响应与实验报告，防止库版本漂移 |
| `prompt_mode` | string | 否 | `measurement`（中性陈列）或 `product`（允许建议优先检查）；默认 `measurement` |
| `sample_salt` | string | 重复采样时是 | 只用于区分独立采样，防止代理缓存把多轮变成同一输出重放 |
| `request_id` | string | 否 | 追踪用，原样回传 |

响应：

```json
{
  "proofs": [
    {"index": 0, "proof": "intro n\nrfl", "raw": "<模型原始输出>"}
  ],
  "meta": {"backend": "mock", "model": "mock-1", "latency_ms": 3,
           "cache_hit": false, "parsed_proofs": 3,
           "snapshot_hash": "sha256:...", "prompt_mode": "measurement"}
}
```

**`proof` 的语义**：它是 `by` 的 **body**（tactic 脚本），不带 `theorem`/`lemma`/`example` 头、
不带 `by` 关键字。Lean 侧会把它包成 `exact by\n  <缩进后的 proof>` 再验证，因此：

* 多步证明用换行分隔；`·` bullet 的缩进由 Lean 侧统一处理；
* `sorry` / `admit` / `?_` 一律判负（分别对应 `mvar_or_sorry` 与 `unclosed_goals`），
  **服务端不做过滤**——过滤会让 `solve_rate` 失去意义；
* 解析不出证明时返回 `{"proofs": []}`，不返回错误（与 `/conjecture` 的软失败口径一致）。

**采样与成本**

| 环境变量 | 默认 | 说明 |
|---|---|---|
| `SOLVE_MAX_TOKENS` | 16384 | 单次调用输出预算（thinking 关闭时 16k 绰绰有余） |
| `SOLVE_TEMPERATURE` | 0.6 | solve 要的是**多样性**（`solve_rate` = k 次采样成功几次），取值由闸门 G1 标定 |

### 测量提示词与产品提示词

`measurement` 模式只说明列出的引理在环境中可用，不得出现“优先引用”“引用更便宜”或
“更不容易出错”等诱导措辞。它用于 probation 曝光、A/B/C 和有库/无库配对。

`product` 模式允许建议模型先检查相关引理，用于最终产品体验，但其引用率不得回写 reuse，
也不得替代严格方法指标。

### 库字段的前置条件

调用方发送 `library` 前必须完成：快照哈希校验、`SgsLean.GeneratedLibrary` 编译、
当前 Lean 会话 import 预检。服务端只负责把已经筛选好的相关 active 引理渲染进提示词，
不负责检索，也不接受 probation/cold 条目。

**判定码**：由 `SgsLean/Server.lean` 的 `verify` 返回，沿用 `SgsLean.classifyError` 的码表
（`ok` / `not_a_prop` / `unclosed_goals` / `mvar_or_sorry` / `type_error` / `unknown_identifier` /
`parse_error` / `forbidden_tactic` / `timeout` / `exception` / `unassigned_goal` /
`final_check_failed`）。客户端统计 `solve_rate` 时**只看 `ok`**。
