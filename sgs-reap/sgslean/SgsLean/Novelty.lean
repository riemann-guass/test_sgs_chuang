/-
# 硬门第二件：新颖性

`Novelty.isNew` 判定"这条候选引理是不是新的"：**不 α-等价于父目标，也不 α-等价于库里的任何一条**。
它替代 SGS rubric 里那句"与目标等价的引理直接给 0 分"——在推理期同样没有价值，而且会在
辅助引理的子目标上产生自指猜想。

判定方式：把两边都 elaborate 成 `Expr`，用 `isDefEq` 双向比较。注意这比纯 α-等价**更严格**：
`isDefEq` 还会做定义展开（δ）与实例化简，所以"定义上相同但写法不同"的语句也会被判为重复。
这是**安全方向**——宁可把新引理误判成旧引理（少入库一条），也不要把重述当成新知识。

库检索（v1）：把库里已有引理的语句文本作为 `against` 传进来，本模块只负责"逐条比等价"。
真正的相关性检索由 Python 的 `pipeline/selection.py` 负责。

已知局限（如实记录）：
* `against` 是**文本**列表，每次调用都要重新 elaborate 一遍；库大时应该改成一次编译好的环境查表
  （那是 `materialize` + 库检索该做的事）。
* 若语句 elaborate 失败，返回 `detail` 非空且 `new=false`（**保守判重复**），
  由 `Gate` 去给出准确的拒绝码。
-/
module

public meta import SgsLean.Basic

open Lean Meta Elab Tactic

public meta section

namespace SgsLean

/-- 新颖性判定结果。 -/
structure NoveltyResult where
  stmt : String
  /-- 是否新颖（与 `against` 里任何一条都不等价）。 -/
  new : Bool
  /-- 判为不新颖时，命中的是第几条（`against` 的下标）。 -/
  matchedIndex : Option Nat := none
  /-- 命中的那条语句文本。 -/
  matchedStmt : String := ""
  /-- 与多少条做了比较（诊断用）。 -/
  compared : Nat := 0
  detail : String := ""
deriving ToJson, Repr, Inhabited

namespace Novelty

/-- 两条语句是否等价（α + δ）。任一边 elaborate 失败即返回 `false`（不判等价）。 -/
def equivalent (a b : String) : TacticM Bool := do
  match ← elabStmtType (normalizeNewlines a), ← elabStmtType (normalizeNewlines b) with
  | .ok ta, .ok tb =>
    withoutModifyingState do
      try
        let fwd ← isDefEq ta tb
        let bwd ← isDefEq tb ta
        return fwd && bwd
      catch _ => return false
  | _, _ => return false

/-- 判 `stmt` 相对 `against` 是否新颖。 -/
def isNew (stmt : String) (against : Array String) : TacticM NoveltyResult := do
  let normalized := normalizeStmt stmt
  -- 先自己 elaborate 一次：失败就保守判"不新"（由 Gate 给准确拒绝码）
  match ← elabStmtType (normalizeNewlines stmt) with
  | .error detail =>
    return { stmt := normalized, new := false, compared := 0, detail := detail }
  | .ok _ =>
    let mut compared := 0
    for idx in [:against.size] do
      let other := against[idx]!
      compared := compared + 1
      if ← equivalent stmt other then
        return { stmt := normalized, new := false, matchedIndex := some idx,
                 matchedStmt := normalizeStmt other, compared := compared }
    return { stmt := normalized, new := true, compared := compared }

end Novelty
end SgsLean
