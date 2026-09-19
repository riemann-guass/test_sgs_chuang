/-
# 轨迹：子目标状态签名与逐 tactic 记录（P2 / N1 的原料）

N1（需求驱动的条件化）要从**真实证明轨迹**里聚合"反复出现的子目标状态"，
所以第一件事是让 Lean 侧能把一条候选证明**逐步**跑一遍，并给出每一步之后的
**子目标签名**。

签名定义（v1）：当前主目标的类型 pp 输出，经 `normalizeProp` 折叠空白。
这是"需求信号"的原子单位——`d(g) = freq(g) × cost(g)` 里的 g 就是它。

**已知局限（v1，必须先说清楚）**：

* 轨迹按**行**切分后逐行执行，因此只对"一行一个 tactic"的脚本成立；带 `·` bullet
  或多行 `cases ... with` 的脚本会被切成对不上块的片段，这些步骤会被如实标记
  `ok=false`（不静默丢弃），后续版本改用真正的 tactic 序列解析。
* `cost` 用"步数 + 该步之后剩余子目标数"作为代理，不做真正的搜索展开数统计
  （那需要把 MCTS 的展开计数接进来，属 P3）。

轨迹的**真值判定**不在这里：`traceScript` 照旧调用 `Verify.verify`，所以每条轨迹都带
"整篇证明过没过"的结论（`verified`），不会出现"看起来有轨迹但其实没过"的假数据。
-/
module

public meta import SgsLean.Basic
public meta import SgsLean.Verify

open Lean Meta Elab Tactic
open Reap.TreeSearch

public meta section

namespace SgsLean

/-- 一步轨迹：跑了哪条 tactic、之后还剩几个子目标、主目标签名是什么。 -/
structure TraceStep where
  tactic : String
  goalsLeft : Nat
  /-- 该步之后主目标的签名（无剩余子目标时为空串）。 -/
  signature : String := ""
  ok : Bool := true
  /-- `ok=false` 时的判定码（复用 `classifyError`）。 -/
  reason : String := ""
deriving ToJson, Repr, Inhabited

/-- 一条候选证明的完整轨迹。 -/
structure TraceResult where
  stmt : String
  /-- 整篇证明的验证结论（来自 `Verify.verify`，即 kernel 终检过的真值）。 -/
  verified : Bool
  reason : String
  steps : Array TraceStep := #[]
deriving ToJson, Repr, Inhabited

namespace Trace

/-- 当前主目标的签名（折叠空白后的类型文本）。 -/
def goalSignature (goals : List MVarId) : MetaM String := do
  match goals with
  | [] => return ""
  | goal :: _ =>
    goal.withContext do
      let ty ← instantiateMVars (← goal.getType)
      return normalizeProp (toString (← Meta.ppExpr ty))

/-- 逐步跑一遍 `proof`，记录每一步之后的子目标签名。 -/
def traceScript (stmt proof : String) : TacticM TraceResult := do
  let verdict ← Verify.verify stmt proof
  let steps ← withoutModifyingState do
    match ← elabStmtType (normalizeNewlines stmt) with
    | .error _ => pure #[]
    | .ok ty =>
      let obligation ← mkFreshExprSyntheticOpaqueMVar ty
      setGoals [obligation.mvarId!]
      let ctx ← mkProofCheckContext
      let lines := (normalizeNewlines proof).splitOn "\n"
        |>.map (fun l => l.trimAscii.toString) |>.filter (fun l => !l.isEmpty)
      let mut acc : Array TraceStep := #[]
      for line in lines do
        let result ← evalTacticStrNoFinalCheck ctx line defaultHeartbeats
        let goals ← getUnsolvedGoals
        let signature ← goalSignature goals
        acc := acc.push {
          tactic := line
          goalsLeft := goals.length
          signature := signature
          ok := result.isOk
          reason := match result with
            | .ok _ => "ok"
            | .error err => classifyError err
        }
      pure acc
  return { stmt := normalizeStmt stmt, verified := verdict.ok, reason := verdict.reason, steps := steps }

end Trace
end SgsLean
