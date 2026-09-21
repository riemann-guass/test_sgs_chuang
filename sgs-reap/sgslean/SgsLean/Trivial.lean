/-
# 硬门第一件：非平凡性

`Trivial.isTrivial` 判定"这条语句能不能被 `decide` / `simp` / `aesop` 在预算内**秒杀**"。
硬门里它是替代 SGS rubric 那句"如果平凡就给低分"的形式化版本——**Lean 直接判，不需要 LLM 猜**。

实现方式与 `Gate` / `Verify` 同源：造一个孤立义务目标 `⊢ <stmt>`，依次试三条 tactic，
任一条在预算内把子目标清零即判平凡；三条都不行判非平凡。每条 tactic 都在
`withoutModifyingState` 里跑，互不污染。

预算有两层，都要如实记录（P1.3 的教训）：
* 心跳 `trivialHeartbeats`（默认 200000 千次 = 2 亿次）：比 `Verify` 的默认预算紧一档，
  因为"秒杀"本来就该便宜；
* 墙钟由 `reap.timeout`（默认 200 s/次）兜底。

边界（写清楚，别误用）：
* `aesop` 来自 Mathlib；无 Mathlib 的快速模式下这条会失败并记入 `tried`，不会误判成平凡。
* 判"平凡"只说明这三条 tactic 能秒杀，**不**说明语句没价值——硬门第二件（新颖性）与软分另算。
-/
module

public meta import SgsLean.Basic

open Lean Meta Elab Tactic
open Reap.TreeSearch

public meta section

namespace SgsLean

/-- 非平凡性判定结果。 -/
structure TrivialResult where
  stmt : String
  /-- 是否被判定为平凡（三条 tactic 里有任意一条在预算内闭合）。 -/
  trivial : Bool
  /-- 判定为平凡时，是哪条 tactic 秒掉的。 -/
  byTactic : String := ""
  /-- 依次尝试过的 tactic 及其结果（`true` = 闭合了目标），用于诊断与标定。 -/
  tried : Array (String × Bool) := #[]
  /-- 语句本身 elaborate 失败时的原因（此时 `trivial=false`，但不是"非平凡"）。 -/
  detail : String := ""
deriving ToJson, Repr, Inhabited

namespace Trivial

/-- 秒杀预算的**默认值**（千次心跳）。比 `Verify` 的默认预算紧一档：平凡判定本来就该便宜。

运行时可被环境变量 `SGSLEAN_TRIVIAL_HEARTBEATS` 覆盖——这个预算是"非平凡"判据的定义本身
（"在预算 B 内解不出就算非平凡"），标定 B 时必须在报告里写明用的是哪个值。 -/
def defaultHeartbeats : Nat := 200000

/-- 运行时秒杀预算；读不到环境变量则退回 `defaultHeartbeats`。 -/
initialize trivialHeartbeatsRef : IO.Ref Nat ← do
  let value ← match (← IO.getEnv "SGSLEAN_TRIVIAL_HEARTBEATS") with
    | some raw => pure ((raw.trimAscii.toString.toNat?).getD defaultHeartbeats)
    | none => pure defaultHeartbeats
  IO.mkRef value

/-- 读取当前批的秒杀预算（泛化到任意可提升 IO 的 monad）。 -/
def getTrivialHeartbeats {m : Type → Type} [Monad m] [MonadLiftT IO m] : m Nat :=
  liftM (m := IO) trivialHeartbeatsRef.get

/-- 依次尝试的 tactic（顺序 = 从便宜到贵）。 -/
def probes : Array String := #["decide", "simp", "aesop"]

/-- 在孤立义务目标上、用指定心跳预算跑一条 tactic，返回它是否把子目标清零。 -/
private def closesGoalWithBudget (ty : Expr) (tactic : String) (budget : Nat) : TacticM Bool := do
  withoutModifyingState do
    let obligation ← mkFreshExprSyntheticOpaqueMVar ty
    setGoals [obligation.mvarId!]
    let ctx ← mkProofCheckContext
    match ← evalTacticStrNoFinalCheck ctx tactic budget with
    | .error _ => return false
    | .ok _ => return (← getUnsolvedGoals).isEmpty

/-- 在孤立义务目标上跑一条 tactic（用当前批的秒杀预算），返回它是否把子目标清零。 -/
private def closesGoal (ty : Expr) (tactic : String) : TacticM Bool :=
  do closesGoalWithBudget ty tactic (← getTrivialHeartbeats)

/-- 判定语句是否平凡。 -/
def isTrivial (stmt : String) : TacticM TrivialResult := do
  match ← elabStmtType (normalizeNewlines stmt) with
  | .error detail => return { stmt := normalizeStmt stmt, trivial := false, detail := detail }
  | .ok ty =>
    let mut tried : Array (String × Bool) := #[]
    for tactic in probes do
      let closed ← closesGoal ty tactic
      tried := tried.push (tactic, closed)
      if closed then
        return { stmt := normalizeStmt stmt, trivial := true, byTactic := tactic, tried := tried }
    return { stmt := normalizeStmt stmt, trivial := false, tried := tried }

/-! ## 廉价 tactic 兜底（在线第 3 步） -/

/-- 一批廉价 tactic：`heartbeatScale` 是相对 `getTrivialHeartbeats` 的预算倍数。 -/
structure CheapBatch where
  /-- 本批依次尝试的 tactic（顺序 = 从便宜到贵）。 -/
  tactics : Array String
  /-- 本批的心跳预算是 `(← getTrivialHeartbeats) * heartbeatScale`。 -/
  heartbeatScale : Nat
deriving Repr, Inhabited

/-- 廉价兜底的结果。 -/
structure CheapResult where
  /-- 是否有 tactic 在预算内清空了子目标。 -/
  hit : Bool
  /-- 命中时的 tactic 原文；未命中时为空串。 -/
  tactic : String := ""
  /-- 命中时的证明脚本（v1：就是该 tactic 本身）。 -/
  proof : String := ""
  /-- 依次尝试过的 `(tactic, 是否闭合, 用的预算)`，供标定与诊断。 -/
  tried : Array (String × Bool × Nat) := #[]
  /-- 语句 elaborate 失败时的错误原文（此时 `hit=false`，但**不是**"兜底失败"）。 -/
  detail : String := ""
deriving ToJson, Repr, Inhabited

/-- 三批 tactic 清单（规格文档 3.3 节）。

顺序与预算都必须与文档一致：第一批零参数、最便宜；第二批是 Mathlib 的中等成本 tactic；
第三批把 `aesop` 的预算放到最大。每批的预算是**相对** `SGSLEAN_TRIVIAL_HEARTBEATS`
的倍数——这样标定"非平凡"用的那个预算一变，兜底的三档预算跟着一起变，两者不会脱节。

注意 `first` 不在清单里：它会在同一份预算下把每条 tactic 都试一遍，
与"逐条试、命中即停"的记账口径冲突（`tried` 就不再是"试过哪些"）。
复杂度留在 Python 侧的 repair 步骤，本函数只做"一次一条"的探针。 -/
def cheapBatches : Array CheapBatch := #[
  { tactics := #["rfl", "decide", "simp", "norm_num", "omega"], heartbeatScale := 3 },
  { tactics := #["ring", "field_simp", "positivity", "linarith", "nlinarith",
                 "constructor", "aesop"], heartbeatScale := 10 },
  { tactics := #["aesop"], heartbeatScale := 50 }
]

/-- 廉价 tactic 兜底：依次试三批，返回第一条清空子目标的 tactic。

与 `isTrivial` 走同一条 `evalTacticStrNoFinalCheck` 路径，区别只有两点：
试哪些 tactic（`cheapBatches` vs `probes`）与预算档（三档递增 vs 单档）。

本函数**不做**内核终检——命中的脚本仍要过 `Verify.verify` 才能进输出。
这是刻意的分工：兜底是「不花模型钱地弄出一个候选」，终检是唯一的放行口。
否则一条 `decide` 产出的带残余元变量的脚本会绕过整个验收层。 -/
def tryCheapTactics (stmt : String) : TacticM CheapResult := do
  match ← elabStmtType (normalizeNewlines stmt) with
  | .error detail => return { hit := false, detail := detail }
  | .ok ty =>
    let base ← getTrivialHeartbeats
    let mut tried : Array (String × Bool × Nat) := #[]
    for batch in cheapBatches do
      let budget := base * batch.heartbeatScale
      for tactic in batch.tactics do
        let closed ← closesGoalWithBudget ty tactic budget
        tried := tried.push (tactic, closed, budget)
        if closed then
          return { hit := true, tactic := tactic, proof := tactic, tried := tried }
    return { hit := false, tried := tried }

end Trivial
end SgsLean
