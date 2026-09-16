# 服务层接口契约（v1，阶段 1 冻结）

Lean 侧与 Python 服务层之间只有这两个端点。契约冻结后，两侧可以独立开发与测试。

基址默认 `http://127.0.0.1:8765`，Lean 侧通过 `reap.conjecture_endpoint` / `reap.guide_endpoint` 配置。

## 通用约定

- 传输：HTTP/1.1 + `Content-Type: application/json; charset=utf-8`
- 超时：客户端连接 5s；`/conjecture` 读 60s；`/guide` 读 120s（含 CoT）
- 重试：连接失败或 5xx 重试 3 次，指数退避 1s/2s/4s；4xx 不重试
- 错误体：`{"error": {"code": "<机器可读码>", "message": "<人类可读说明>"}}`
  - 400 `bad_request`、422 `invalid_params`、500 `internal_error`、503 `backend_unavailable`、504 `backend_timeout`

## `GET /health`

响应：`{"status": "ok", "backend": "<后端名>", "version": "v1"}`

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
