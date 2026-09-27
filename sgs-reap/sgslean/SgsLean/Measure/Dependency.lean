/-
# 软分第二件：依赖抽取

"这条引理到底有没有被用上"必须可判，否则 Δlen 与"被依赖率"都会被重述式候选刷满。
做法：先 `Verify.verify` 拿到**可信**的证明，再从**赋值后的证明项**里抽常量名
（复用 reap 的 `collectConstNames`），然后回答"是否引用了指定引理"。

**关键约束**：只有验证通过的证明才抽依赖。没过的证明本身就是垃圾，
从它里面抽出的"依赖"没有意义——这类情况直接返回 `verified=false` 且 `constants` 为空。

局限（v1）：
* 只抽**直接出现**在证明项里的常量，不做传递闭包（引理 A 用到 B 时，只报告 A 被引用）；
* 常量名以字符串比较（`Nat.add_comm` 这种全名），不做命名空间解析；
* 过滤掉以 `_` 开头的内部名（`_uniq.*`、aux decl），它们不是用户可见的引理。
-/
module

public meta import SgsLean.Basic
public meta import SgsLean.Verify

open Lean Meta Elab Tactic
open Reap.TreeSearch

public meta section

namespace SgsLean

/-- 依赖抽取结果。 -/
structure DependencyResult where
  stmt : String
  /-- 证明是否可信（来自 `Verify.verify`）；为 `false` 时下面的字段全为空。 -/
  verified : Bool
  /-- 证明项里直接出现的常量名（去重、排序、滤掉内部名）。 -/
  constants : Array String := #[]
  /-- 被查询的引理名（`target` 为空表示不做这项判断）。 -/
  target : String := ""
  /-- 是否引用了 `target`。 -/
  usesTarget : Bool := false
  /-- 验证失败时的判定码。 -/
  reason : String := ""
deriving ToJson, Repr, Inhabited

namespace Measure

/-- 抽 `proof` 对 `stmt` 的依赖；`target` 是可选的要查询的引理名。 -/
def dependencies (stmt proof : String) (target : String := "") : TacticM DependencyResult := do
  let verdict ← Verify.verify stmt proof
  if !verdict.ok then
    return { stmt := normalizeStmt stmt, verified := false, target := target,
             reason := verdict.reason }
  let constants ← withoutModifyingState do
    match ← elabStmtType (normalizeNewlines stmt) with
    | .error _ => pure #[]
    | .ok ty =>
      let obligation ← mkFreshExprSyntheticOpaqueMVar ty
      setGoals [obligation.mvarId!]
      let ctx ← mkProofCheckContext
      -- 必须与 `Verify` 一样先包成 `exact by ...`：多行脚本直接丢给
      -- `evalTacticStrNoFinalCheck` 只会执行第一行，因此
      -- 离线测试当场抓出来了：`intro a b` 之后 `exact Nat.add_comm a b` 根本没跑）。
      let action := MCTS.wrapProofScriptAsTactic (normalizeNewlines proof)
      match ← evalTacticStrNoFinalCheck ctx action (← getHeartbeats) with
      | .error _ => pure #[]
      | .ok _ =>
        let term ← instantiateMVars (mkMVar obligation.mvarId!)
        let names := collectConstNames term |>.map (fun n => n.toString)
        pure <| names.filter (fun s => !s.startsWith "_") |>.qsort (fun a b => a < b)
  return {
    stmt := normalizeStmt stmt
    verified := true
    constants := constants
    target := target
    usesTarget := if target.isEmpty then false else constants.contains target
  }

end Measure
end SgsLean
