/-
# 闸门 G 的硬门第一件：候选语句能否 elaborate 成一个命题

`Gate.check` 只回答**一个**问题：把这条候选当作 `have <name> : <stmt> := ?_` 的前件，
在当前上下文里能不能成立（并且结果确实是一个命题）。

「非平凡」「新颖」「可证」这三件属于 G 的其余部分，留给后续阶段（P2/P3）：
这里刻意不做，保证本单元只有一个可验收的判定。
-/
module

public meta import SgsLean.Basic

open Lean Meta Elab Tactic
open Reap.TreeSearch

public meta section

namespace SgsLean

/-- 门检结果。字段全部可 JSON 化，便于 P1.3 的采样与落盘。 -/
structure GateResult where
  /-- 判定：语句能在当前上下文 elaborate，且其类型是命题。 -/
  ok : Bool
  /-- 机器可读的判定码；通过时为 `"ok"`。 -/
  reason : String
  /-- 语句的类型是否为命题（`Prop`）。 -/
  isProp : Bool
  /-- 归一化后的语句文本（折叠空白），供后续 α-等价/去重使用。 -/
  stmt : String
  /-- Lean 实际 elaborate 出的类型（pp 文本），可用于 α-等价比较。 -/
  elaboratedType : String := ""
  /-- 失败详情（错误原文），仅供诊断与日志。 -/
  detail : String := ""
deriving ToJson, Repr, Inhabited

namespace Gate

/-- 门检动作：`have sgs_probe_ : <stmt> := ?_`。

`?_` 是 synthetic hole，会留下一个真正的子目标，因此「动作成立」⟺「语句能在当前上下文里
作为类型出现」。这条动作与搜索里真实的猜想动作走**同一个** `evalTacticStrNoFinalCheck`
代码路径，所以门检通过 ⟺ 该候选进了搜索也能被组装成动作。 -/
def probeAction (stmt : String) : String :=
  s!"have {probeHypName} : {stmt} := ?_"

/-- 候选语句的门检。 -/
def check (stmt : String) : TacticM GateResult := do
  let text := normalizeNewlines stmt
  let normalized := normalizeStmt stmt
  withoutModifyingState do
    let ctx ← mkProofCheckContext
    let result ← evalTacticStrNoFinalCheck ctx (probeAction text) (← getHeartbeats)
    match result with
    | .error err =>
      return {
        ok := false
        reason := classifyError err
        isProp := false
        stmt := normalized
        detail := toString err
      }
    | .ok _ =>
      -- 探针成功 ⟹ 语句是一个合法类型；再单独取出它，判定是否为命题。
      match ← elabStmtType text with
      | .error detail =>
        return {
          ok := false
          reason := "type_elab_failed"
          isProp := false
          stmt := normalized
          detail := detail
        }
      | .ok ty =>
        let isProp ← Meta.isProp ty
        return {
          ok := isProp
          reason := if isProp then "ok" else "not_a_prop"
          isProp := isProp
          stmt := normalized
          elaboratedType := toString (← Meta.ppExpr ty)
        }

end Gate
end SgsLean
