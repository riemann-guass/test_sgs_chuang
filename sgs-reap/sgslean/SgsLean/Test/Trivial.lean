/-
# 非平凡性 `Trivial.isTrivial` 的离线测试

期望值**按真实输出标定**（见 `docs/phase10-log.md` 的标定记录），不是凭印象写死的：
在 Mathlib 模式下 8 条探针里，`True` 被 `decide` 秒掉，`∀ (n : Nat), n + 0 = n` 被 `simp` 秒掉，
而 `1 = 1` 与 `∀ (a b : Nat), a + b = b + a` **没有被任何一条秒掉**（反直觉，但实测如此）。

本文件按 lake 的构建环境（**无 Mathlib**）运行，所以只断言跨模式稳健的部分；
Mathlib 模式下 8 条探针的完整输出见 `docs/phase10-log.md` 的标定记录。
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

/-- 断言：判定为**非**平凡，且三条探针都跑过（结构断言，跨模式稳健）。 -/
def expectNonTrivial (stmt : String) : TacticM Unit := do
  let r ← Trivial.isTrivial stmt
  if r.trivial then
    throwError "「{stmt}」应判非平凡，实际被 {r.byTactic} 秒掉"
  unless r.tried.size == 3 do
    throwError "「{stmt}」应记录 3 条探针，实际 {r.tried.size}：{r.tried}"
  unless r.stmt == SgsLean.normalizeStmt stmt do
    throwError "「{stmt}」的 stmt 字段不是归一化文本：{r.stmt}"

/-- 正例：`True` 与"定义上成立"的加零被秒掉。 -/
example : True := by
  run_tac do
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
