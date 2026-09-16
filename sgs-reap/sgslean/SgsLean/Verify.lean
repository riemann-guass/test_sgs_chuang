/-
# 验证接口：整篇证明是否过

`Verify.verify stmt proof` 判定「`proof` 是 `stmt` 在当前上下文里的一篇完整证明」。

实现沿用 reap 已经验证过的「重放 + kernel 终检」路径，与
`Reap.Tactic.TreeSearch.checkProofScript` 同构：

1. 先过门检（语句必须能 elaborate 成命题）；
2. 把语句 elaborate 成一个**孤立**的义务目标 `⊢ <stmt>`，不依赖外层目标；
3. 用 `MCTS.wrapProofScriptAsTactic` 把证明脚本包成 `exact by ...`，
   交给 `evalTacticStrNoFinalCheck`（其中含 reap 的 `checkTacticSyntax` 守卫）；
4. 子目标清零后调用 reap 的 `checkProof`：赋值里不许有 `sorryAx`/残余 metavariable，
   并把赋值交给 kernel 复核。

第 4 步不是可选项。实测（见 `docs/phase3-log.md`）本版本 Lean 上
`sorry` / `admit` 的语法节点是 `Lean.Parser.Tactic.tacticSorry`，而 reap 的
`isQuestionTacticKind` 比的是 `` `sorry ``，**匹配不到**；只靠语法守卫会把
`have sgs_probe_ : P := by sorry` 判成成功。`checkProof` 的
`assignedProofHasMVarOrSorry` 才是那条负例真正的检测点。
-/
module

public meta import SgsLean.Basic
public meta import SgsLean.Gate

open Lean Meta Elab Tactic
open Reap.TreeSearch

public meta section

namespace SgsLean

/-- 验证结果。 -/
structure VerifyResult where
  /-- 判定：证明完整闭合了语句，且通过了 kernel 终检。 -/
  ok : Bool
  /-- 机器可读的判定码；通过时为 `"ok"`。 -/
  reason : String
  /-- 语句是否先过了门检（`Verify.verify` 通过的必要条件）。 -/
  gateOk : Bool := false
  /-- 归一化后的语句文本。 -/
  stmt : String := ""
  /-- 是否真的跑过 reap 的 kernel 终检（`checkProof`）。 -/
  finalChecked : Bool := false
  /-- 证明跑完后剩余的子目标数（0 = 整篇证明闭合）。 -/
  goalsLeft : Nat := 0
  /-- 失败详情（错误原文），仅供诊断与日志。 -/
  detail : String := ""
deriving ToJson, Repr, Inhabited

namespace Verify

/-- 验证 `proof` 是否是 `stmt` 的一篇完整证明。 -/
def verify (stmt proof : String) : TacticM VerifyResult := do
  let gate ← Gate.check stmt
  if !gate.ok then
    return {
      ok := false
      reason := gate.reason
      gateOk := false
      stmt := gate.stmt
      detail := gate.detail
    }
  match ← elabStmtType (normalizeNewlines stmt) with
  | .error detail =>
    return {
      ok := false
      reason := "type_elab_failed"
      gateOk := true
      stmt := gate.stmt
      detail := detail
    }
  | .ok ty =>
    withoutModifyingState do
      let obligation ← mkFreshExprSyntheticOpaqueMVar ty
      setGoals [obligation.mvarId!]
      let ctx ← mkProofCheckContext
      let action := MCTS.wrapProofScriptAsTactic (normalizeNewlines proof)
      let result ← evalTacticStrNoFinalCheck ctx action defaultHeartbeats
      let goalsLeft ← getUnsolvedGoals
      match result with
      | .error err =>
        return {
          ok := false
          reason := classifyError err
          gateOk := true
          stmt := gate.stmt
          goalsLeft := goalsLeft.length
          detail := toString err
        }
      | .ok _ =>
        if goalsLeft.length > 0 then
          -- 证明没闭合（正常情况下 `exact by ...` 会先报 unsolved goals，这里是兜底）。
          return {
            ok := false
            reason := "unclosed_goals"
            gateOk := true
            stmt := gate.stmt
            goalsLeft := goalsLeft.length
          }
        match ← checkProof ctx with
        | .ok _ =>
          return {
            ok := true
            reason := "ok"
            gateOk := true
            stmt := gate.stmt
            finalChecked := true
          }
        | .error err =>
          return {
            ok := false
            reason := classifyError err
            gateOk := true
            stmt := gate.stmt
            detail := toString err
          }

end Verify
end SgsLean
