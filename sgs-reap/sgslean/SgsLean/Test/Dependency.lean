/-
# 依赖抽取的离线测试

只断言**确定**的事实：`exact Nat.add_comm a b` 这条证明必然引用 `Nat.add_comm`、
必然不引用 `Nat.mul_comm`；验证失败的证明不抽依赖。
-/
import SgsLean
import SgsLean.Syntax

open Lean Meta Elab Tactic
open SgsLean

set_option linter.unusedVariables false

namespace SgsLean.Test

example : True := by
  run_tac do
    let stmt := "∀ (a b : Nat), a + b = b + a"
    let proof := "intro a b\nexact Nat.add_comm a b"
    let r ← Measure.dependencies stmt proof "Nat.add_comm"
    unless r.verified do
      throwError "这条证明应验证通过，实际 reason={r.reason}"
    unless r.usesTarget do
      throwError "应检测到引用了 Nat.add_comm，实际 constants={r.constants}"
    unless r.constants.contains "Nat.add_comm" do
      throwError "constants 里应含 Nat.add_comm：{r.constants}"
    let r2 ← Measure.dependencies stmt proof "Nat.mul_comm"
    if r2.usesTarget then
      throwError "不应检测到引用了 Nat.mul_comm"
    -- 验证失败的证明不抽依赖
    let r3 ← Measure.dependencies "∀ (n : Nat), n + 0 = n" "sorry" "Nat.add"
    if r3.verified || !r3.constants.isEmpty then
      throwError "验证失败的证明不应抽依赖：verified={r3.verified} constants={r3.constants}"
  trivial

end SgsLean.Test
