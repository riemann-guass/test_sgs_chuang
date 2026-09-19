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

/-- 在孤立义务目标上跑一条 tactic，返回它是否把子目标清零。 -/
private def closesGoal (ty : Expr) (tactic : String) : TacticM Bool := do
  withoutModifyingState do
    let obligation ← mkFreshExprSyntheticOpaqueMVar ty
    setGoals [obligation.mvarId!]
    let ctx ← mkProofCheckContext
    match ← evalTacticStrNoFinalCheck ctx tactic (← getTrivialHeartbeats) with
    | .error _ => return false
    | .ok _ => return (← getUnsolvedGoals).isEmpty

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

end Trivial
end SgsLean
