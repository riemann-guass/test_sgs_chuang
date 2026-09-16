module
public meta import Reap.Options
public meta import Requests

public meta section

open Lean

/-!
# 猜想与评审服务的客户端

实现 `docs/api-contract.md` 的 v1 契约。两个端点：

* `POST /conjecture`：给定证明状态，返回 `have`-ready 的辅助引理命题。
* `POST /guide`：给定目标与候选命题，返回 SGS rubric 三维评分与合成分数。

设计原则与 `PremiseSelectionClient` 一致：**失败即软失败**。服务不可用、返回格式不合法、
网络出错，一律返回空数组，绝不中断证明搜索——搜索应当退回纯 policy 行为。
-/

structure ConjectureRequest where
  goal_state : String
  num_samples : Nat
  /-- v1 未透传真实搜索深度（`PolicyValueEval` 签名里没有深度信息），
  服务端不应据此做分层采样。阶段 3 会通过扩展该签名补上。 -/
  depth : Nat := 0
  target : String := ""
deriving ToJson

structure ConjectureCandidate where
  index : Nat
  /-- `have`-ready 的 Lean 命题：只引用当前上下文已有的变量，或用 `∀`/`→` 自行引入。 -/
  type : String
  raw : String := ""
  review : Float := 0.0
deriving ToJson, FromJson, Repr, Inhabited

structure ConjectureResponse where
  candidates : Array ConjectureCandidate := #[]
  /-- 服务端返回的 `meta` 字段（后端名、模型、延迟、缓存命中），原样保留供日志使用。 -/
  metaJson : Json := Json.null

instance : FromJson ConjectureResponse where
  fromJson? j := do
    -- 缺少 candidates 字段时退回空列表，而不是解析失败：
    -- 服务端用 `{}` 表示"本次无猜想"是允许的。
    let candidates := (j.getObjValAs? (Array ConjectureCandidate) "candidates").toOption.getD #[]
    return { candidates := candidates, metaJson := j.getObjValD "meta" }

instance : ToJson ConjectureResponse where
  toJson r := Json.mkObj [("candidates", toJson r.candidates), ("meta", r.metaJson)]

structure GuideScore where
  index : Nat
  relevance : Float
  redundancy : Float
  complexity : Float
  review : Float
deriving ToJson, FromJson, Repr, Inhabited

structure GuideResponse where
  scores : Array GuideScore := #[]
  metaJson : Json := Json.null

instance : FromJson GuideResponse where
  fromJson? j := do
    let scores := (j.getObjValAs? (Array GuideScore) "scores").toOption.getD #[]
    return { scores := scores, metaJson := j.getObjValD "meta" }

instance : ToJson GuideResponse where
  toJson r := Json.mkObj [("scores", toJson r.scores), ("meta", r.metaJson)]

structure GuideCandidate where
  index : Nat
  type : String
deriving ToJson

structure GuideRequest where
  target : String
  candidates : Array GuideCandidate
deriving ToJson

structure ConjectureClient where
  apiUrl : String

structure GuideClient where
  apiUrl : String

namespace ConjectureClient

initialize cache :
  IO.Ref (Std.HashMap (String × Nat) (Array ConjectureCandidate)) ← IO.mkRef {}

/-- SGS 原式打分合成（`sgs/models/guide/llm_judge_guide.py:sub_scores_to_review`）。 -/
def subScoresToReview (relevance complexity redundancy : Float) : Float :=
  if complexity == 3.0 || complexity == 4.0 then
    0.0
  else
    max 0.0 (relevance + (2.0 - complexity) + (1.0 - redundancy))

/-- 查询猜想服务；任何失败都返回空数组。 -/
def proposeConjectures (client : ConjectureClient) (goalState : String) (numSamples : Nat) :
    CoreM (Array ConjectureCandidate) := do
  let key := (goalState, numSamples)
  match (← cache.get).get? key with
  | some result => return result
  | none =>
    let req : ConjectureRequest := { goal_state := goalState, num_samples := numSamples }
    let result ←
      try
        let res : ConjectureResponse ← Requests.post client.apiUrl req
        pure res.candidates
      catch _ => pure #[]
    if !result.isEmpty then
      cache.modify fun m => m.insert key result
    return result

end ConjectureClient

namespace GuideClient

initialize cache : IO.Ref (Std.HashMap String (Array Float)) ← IO.mkRef {}

/-- 查询评审服务，返回与 `candidates` 等长、按 index 对齐的 review 分数。
缺失或解析失败的条目记 0 分；任何失败整体返回全 0，使先验退化为均匀分布。 -/
def scoreConjectures (client : GuideClient) (target : String)
    (candidates : Array ConjectureCandidate) : CoreM (Array Float) := do
  if candidates.isEmpty then return #[]
  let key := target ++ "|" ++ String.intercalate "|" (candidates.toList.map (·.type))
  match (← cache.get).get? key with
  | some scores => return scores
  | none =>
    let req : GuideRequest := {
      target := target
      candidates := candidates.map fun c => { index := c.index, type := c.type }
    }
    let mut scores := Array.replicate candidates.size 0.0
    let res? ←
      try
        let res : GuideResponse ← Requests.post client.apiUrl req
        pure (some res)
      catch _ => pure none
    if let some res := res? then
      for s in res.scores do
        for i in [:candidates.size] do
          if candidates[i]!.index == s.index then
            scores := scores.set! i s.review
    cache.modify fun m => m.insert key scores
    return scores

end GuideClient

/-- review 分数 → 与 policy logprob 同尺度的搜索先验。

`p_i = softmax(review_i / T)`，`prior_i = β · ln p_i`。
所有分数相同（或全 0）时退化为均匀分布，此时 `prior_i = β · ln(1/n)`，仍保留组内相对次序。 -/
def conjecturePriors (reviews : Array Float) (beta T : Float) : Array Float := Id.run do
  if reviews.isEmpty then return #[]
  let temp := if T <= 0.0 then 1.0 else T
  let scaled := reviews.map fun r => r / temp
  let maxScore := scaled.foldl (fun acc s => max acc s) (-1.0e18)
  let exps := scaled.map fun s => Float.exp (s - maxScore)
  let total := exps.foldl (fun acc e => acc + e) 0.0
  if total <= 0.0 then
    return scaled.map fun _ => 0.0
  return exps.map fun e => beta * Float.log (e / total)

end
