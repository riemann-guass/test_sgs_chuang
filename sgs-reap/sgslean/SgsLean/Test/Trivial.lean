/-
# 非平凡性 `Trivial.isTrivial` 的离线测试

期望值**按真实输出标定**（见 `docs/phase10-log.md` 的标定记录），不是凭印象写死的：
非平凡门必须先挡住 `rfl` 可直接闭合的定义等式；历史实现漏了这条探针，
使 `1 = 1` 一类命题能进入库。当前固定顺序为 `rfl` / `decide` / `simp` / `aesop`。

本文件按 lake 的构建环境（**无 Mathlib**）运行，所以只断言跨模式稳健的部分；
Mathlib 模式的历史标定记录见 `docs/phase10-log.md`。
-/
import SgsLean
import SgsLean.Syntax

open Lean Meta Elab Tactic
open SgsLean

set_option linter.unusedVariables false

namespace SgsLean.Test

/-- 断言：判定为平凡，且给出了是哪条 tactic 秒掉的。 -/
def expectTrivial (stmt : String) : TacticM Unit := do
  let r ← Trivial.isTrivial stmt
  unless r.trivial do
    throwError "「{stmt}」应判平凡，实际 trivial=false，tried={r.tried}"
  if r.byTactic.isEmpty then
    throwError "「{stmt}」判了平凡却没记录是哪条 tactic"

/-- 断言：判定为**非**平凡，且四条探针都跑过（结构断言，跨模式稳健）。 -/
def expectNonTrivial (stmt : String) : TacticM Unit := do
  let r ← Trivial.isTrivial stmt
  if r.trivial then
    throwError "「{stmt}」应判非平凡，实际被 {r.byTactic} 秒掉"
  unless r.tried.size == 4 do
    throwError "「{stmt}」应记录 4 条探针，实际 {r.tried.size}：{r.tried}"
  unless r.stmt == SgsLean.normalizeStmt stmt do
    throwError "「{stmt}」的 stmt 字段不是归一化文本：{r.stmt}"

/-- 正例：自反性等式必须由 rfl 第一时间拦下。 -/
example : True := by
  run_tac do
    let reflexive ← Trivial.isTrivial "1 = 1"
    unless reflexive.trivial && reflexive.byTactic == "rfl" do
      throwError "`1 = 1` 应被 rfl 拦下，trivial={reflexive.trivial}, byTactic={reflexive.byTactic}, tried={reflexive.tried}"
    expectTrivial "True"
    expectTrivial "∀ (n : Nat), n + 0 = n"
  trivial

/-- 负例：需要引理 / 需要结构处理的语句不算平凡。 -/
example : True := by
  run_tac do
    expectNonTrivial "∀ (n : Nat), 2 ∣ n ^ 2 + n"
    expectNonTrivial "∀ (n : Nat), n ≤ n * n + 1"
    -- `Nat` 是类型不是命题：三条 tactic 都不可能闭合它
    expectNonTrivial "Nat"
  trivial

/-- 边界：`elabStmtType` 成功但目标不可证时，必须如实返回非平凡（而不是抛错）。 -/
example : True := by
  run_tac do
    let r ← Trivial.isTrivial "∀ (P : Prop), P"
    if r.trivial then
      throwError "「∀ (P : Prop), P」不可能平凡，判成了 {r.byTactic}"
  trivial

end SgsLean.Test
