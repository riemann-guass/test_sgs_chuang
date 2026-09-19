/-
# 新颖性 `Novelty.isNew` 的离线测试（无 Mathlib 也能跑）

核心用例是 **α-等价**：`∀ (n : Nat), n + 0 = n` 与 `∀ (m : Nat), m + 0 = m` 只是变量名不同，
必须被判成"不新"。这条如果错了，N1/N2 会反复把重述当新知识入库。
-/
import SgsLean
import SgsLean.Syntax

open Lean Meta Elab Tactic
open SgsLean

set_option linter.unusedVariables false

namespace SgsLean.Test

/-- 断言：判为新颖。 -/
def expectNew (stmt : String) (against : Array String) : TacticM Unit := do
  let r ← Novelty.isNew stmt against
  unless r.new do
    throwError "「{stmt}」应判新颖，实际命中第 {r.matchedIndex} 条：{r.matchedStmt}（detail={r.detail}）"

/-- 断言：判为不新颖，且命中下标符合预期。 -/
def expectDuplicate (stmt : String) (against : Array String) (idx : Nat) : TacticM Unit := do
  let r ← Novelty.isNew stmt against
  if r.new then
    throwError "「{stmt}」应判重复，实际判成新颖（compared={r.compared}）"
  if r.matchedIndex != some idx then
    throwError "「{stmt}」应命中第 {idx} 条，实际 {r.matchedIndex}（matchedStmt={r.matchedStmt}）"

example : True := by
  run_tac do
    -- 空库 ⇒ 一定新颖
    expectNew "∀ (n : Nat), n + 0 = n" #[]
    -- 与不同名的同类语句：α-等价 ⇒ 不新
    expectDuplicate "∀ (n : Nat), n + 0 = n" #["∀ (m : Nat), m + 0 = m"] 0
    -- 只是"像"但不等价 ⇒ 新
    expectNew "∀ (n : Nat), n + 0 = n" #["∀ (n : Nat), n * 2 = n"]
    -- 两条库里，命中第二条
    expectDuplicate "True" #["1 = 1", "True"] 1
    -- 与自身等价（自比）⇒ 不新
    expectDuplicate "∀ (P : Prop), P → P" #["∀ (Q : Prop), Q → Q"] 0
    -- 比较条数要如实统计
    let r ← Novelty.isNew "∀ (n : Nat), n + 0 = n" #["1 = 1", "2 = 2", "3 = 3"]
    unless r.compared == 3 do
      throwError "compared 应为 3，实际 {r.compared}"
  trivial

end SgsLean.Test
