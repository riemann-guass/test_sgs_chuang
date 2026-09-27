/-
# 轨迹：子目标状态签名与逐 tactic 记录

N1（需求驱动的条件化）要从**真实证明轨迹**里聚合"反复出现的子目标状态"，
所以第一件事是让 Lean 侧能把一条候选证明**逐步**跑一遍，并给出每一步之后的
**子目标签名**。

签名定义（**v1.1**）：`局部上下文 ⊢ 主目标`，经 `normalizeProp` 折叠空白；
上下文最多取前 6 条假设（避免签名无限长）。这是"需求信号"的原子单位——
`d(g) = freq(g) × cost(g)` 里的 g 就是它。

v1 只取主目标类型，实测会把 `P` 这类**裸变量名**当成跨目标重复的需求（不同引理恰好用了同名变量），
见 `docs/phase9-log.md` 限制 1。v1.1 把上下文并进来：`n : Nat ⊢ 2 ∣ n * n + n`
这类签名才是有意义的"子目标状态"。代价是签名更具体、跨目标重复**应当变少**——
这正是预期的方向（宁可少而真，不要多而虚）。

**已知局限（v1，必须先说清楚）**：

* 轨迹按**行**切分后逐行执行，因此只对"一行一个 tactic"的脚本成立；带 `·` bullet
  或多行 `cases ... with` 的脚本会被切成对不上块的片段，这些步骤会被如实标记
  `ok=false`（不静默丢弃），后续版本改用真正的 tactic 序列解析。
* `cost` 用"步数 + 该步之后剩余子目标数"作为代理，不做真正的搜索展开数统计
  （这需要另行接入搜索树展开计数）。

轨迹的**真值判定**不在这里：`traceScript` 照旧调用 `Verify.verify`，所以每条轨迹都带
"整篇证明过没过"的结论（`verified`），不会出现"看起来有轨迹但其实没过"的假数据。
-/
module

public meta import SgsLean.Basic
public meta import SgsLean.Measure.Dependency
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
  /-- 证明项里直接出现的常量名（来自 `Measure.dependencies`）。

  复用判据 `reuse(l)` 要数"l 被多少个不同目标的**通过验收的**证明实际引用"，
  这个字段就是那个"实际引用"的**唯一**来源。以前轨迹里没有它，闭环只能去数
  **候选引理**的依赖（而那些候选是在没有库的条件下求解的），于是 reuse 恒为 0。 -/
  constants : Array String := #[]
  steps : Array TraceStep := #[]
deriving ToJson, Repr, Inhabited

namespace Trace

/-- 当前主目标的签名（折叠空白后的类型文本）。 -/
def goalSignature (goals : List MVarId) : MetaM String := do
  match goals with
  | [] => return ""
  | goal :: _ =>
    goal.withContext do
      let mut ctx : Array String := #[]
      for decl in ← getLCtx do
        unless decl.isImplementationDetail do
          if ctx.size < 6 then
            let ty ← instantiateMVars decl.type
            ctx := ctx.push s!"{decl.userName} : {toString (← Meta.ppExpr ty)}"
      let ty ← instantiateMVars (← goal.getType)
      let goalText := toString (← Meta.ppExpr ty)
      let ctxText := String.intercalate ", " ctx.toList
      if ctxText.isEmpty then
        return normalizeProp s!"⊢ {goalText}"
      else
        return normalizeProp s!"{ctxText} ⊢ {goalText}"

/-- 逐步跑一遍 `proof`，记录每一步之后的子目标签名。 -/
def traceScript (stmt proof : String) : TacticM TraceResult := do
  -- 用 `Measure.dependencies` 而不是裸 `Verify.verify`：它内部**同样**跑
  -- `Verify.verify`（判定不变），额外把证明项里的常量抽出来。两条判定路径合成一条，
  -- 既不增加导入也不增加一次重放。
  let dep ← Measure.dependencies stmt proof
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
        let result ← evalTacticStrNoFinalCheck ctx line (← getHeartbeats)
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
  return {
    stmt := normalizeStmt stmt
    verified := dep.verified
    reason := if dep.verified then "ok" else dep.reason
    constants := dep.constants
    steps := steps
  }

end Trace
end SgsLean
