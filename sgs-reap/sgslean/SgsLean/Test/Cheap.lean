/-
# 廉价 tactic 兜底 `Trivial.tryCheapTactics` 的离线测试

在线流程第 3 步（规格文档 3.3 节）：不花模型钱地先试一遍廉价 tactic。
本文件测三件事，都按"结构断言优先"写——`norm_num` / `omega` 依赖 Mathlib，
在 lake 的构建环境（无 Mathlib）里不存在，把断言写死在具体 tactic 上会让测试
只在某一种模式下成立。

1. 该命中的必须命中：`True` 在第一批里就该被秒掉，且命中的 tactic 必须在候选清单里；
2. 该不命中的必须不命中：不可证的语句返回 `hit=false`，且**试满三批**、预算递增；
3. 不可 elaborate 的语句要如实报 `detail`，而不是伪装成"兜底失败"——
   这一条是"输入问题不是证不出来"的判据，混了会让失败率的分子分母都错。

反向对照（漏了就会被测试抓住）：把 `cheapBatches` 的任一批删掉，
`expectTriedAllBatches` 立刻因为候选数不足而报错；把 `hit` 写成常量 `false`，
第一条正例立刻失败。

完整的三批清单在 Mathlib 模式下的真实输出（含哪些 tactic 真的秒掉了哪些语句）
记在 `docs/phase25-log.md` 的标定记录里。
-/
import SgsLean
import SgsLean.Syntax

open Lean Meta Elab Tactic
open SgsLean

set_option linter.unusedVariables false

namespace SgsLean.Test

/-- 三批清单里一共多少条 tactic。 -/
def cheapTacticCount : Nat :=
  Trivial.cheapBatches.foldl (fun acc b => acc + b.tactics.size) 0

/-- 断言：兜底命中，且命中的 tactic 在候选清单里。 -/
def expectCheapHit (stmt : String) : TacticM Unit := do
  let r ← Trivial.tryCheapTactics stmt
  unless r.hit do
    throwError "「{stmt}」应被兜底命中，实际 hit=false，tried={r.tried}"
  if r.tactic.isEmpty then
    throwError "「{stmt}」命中却没记录是哪个 tactic"
  if r.proof != r.tactic then
    throwError "「{stmt}」命中时 proof 应等于 tactic 本身，实际 proof={r.proof}"
  if !(Trivial.cheapBatches.any (fun b => b.tactics.contains r.tactic)) then
    throwError "「{stmt}」命中的 tactic「{r.tactic}」不在三批清单里"

/-- 断言：兜底未命中，且**试满三批**、预算单调不减。 -/
def expectCheapMiss (stmt : String) : TacticM Unit := do
  let r ← Trivial.tryCheapTactics stmt
  if r.hit then
    throwError "「{stmt}」不应被兜底命中，实际被 {r.tactic} 秒掉"
  unless r.tried.size == cheapTacticCount do
    throwError "「{stmt}」应试满 {cheapTacticCount} 条 tactic，实际 {r.tried.size}：{r.tried}"
  unless r.proof.isEmpty do
    throwError "「{stmt}」未命中时 proof 应为空串，实际 {r.proof}"
  let budgets := r.tried.map (fun t => t.2.2)
  unless budgets == budgets.qsort (· ≤ ·) do
    throwError "「{stmt}」的预算应单调不减，实际 {budgets}"

end SgsLean.Test

open SgsLean.Test

/-- 正例：定义上成立 / 可判定的语句在第一批就被秒掉。 -/
example : True := by
  run_tac do
    expectCheapHit "True"
    expectCheapHit "1 = 1"
    expectCheapHit "∀ (n : Nat), n + 0 = n"
  trivial

/-- 负例：不可证的语句不该被兜底命中，且必须试满三批。 -/
example : True := by
  run_tac do
    expectCheapMiss "∀ (P : Prop), P"
  trivial

/-- 边界：语句本身 elaborate 失败时必须如实报 detail，而不是伪装成未命中。 -/
example : True := by
  run_tac do
    -- 每个样本都必须：不命中、给出非空 detail、且一条 tactic 也没试
    -- （这三条一起才是"输入问题"与"兜底失败"的分界）
    for stmt in ["∀ (n : Nat, n = n", "∀ (:::)"] do
      let r ← Trivial.tryCheapTactics stmt
      if r.hit then
        throwError "不可解析的语句「{stmt}」被兜底判成命中：{r.tactic}"
      if r.detail.isEmpty then
        throwError "不可解析的语句「{stmt}」应给出 detail，实际为空"
      unless r.tried.isEmpty do
        throwError "不可解析的语句「{stmt}」不该试 tactic，实际试了 {r.tried.size} 条"
  trivial

/-- 反向对照：`Nat` 这种 Type 级目标**确实**能被 `constructor` 闭合，不是"非平凡"。 -/
example : True := by
  run_tac do
    let r ← Trivial.tryCheapTactics "Nat"
    unless r.hit do
      throwError "「Nat」应被 constructor 秒掉（这正说明它不该当负例），实际 hit=false"
  trivial
