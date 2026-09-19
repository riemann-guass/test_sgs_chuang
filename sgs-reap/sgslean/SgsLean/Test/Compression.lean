/-
# 压缩收益 Δlen 的离线测试

用例：同一条语句的两条证明——长的那条自己 `have` 出等价事实再 `exact`，
短的那条直接 `rfl`。Δlen 必须**恰好**是两者步数/字符数之差（整数比较，不用浮点）。
-/
import SgsLean
import SgsLean.Syntax

open Lean Meta Elab Tactic
open SgsLean

set_option linter.unusedVariables false

namespace SgsLean.Test

example : True := by
  run_tac do
    let stmt := "∀ (n : Nat), n + 0 = n"
    let long := "intro n\nhave h : n + 0 = n := rfl\nexact h"
    let short := "intro n\nrfl"
    let r ← Measure.compression stmt long short
    unless r.longOk && r.shortOk do
      throwError "两条证明都应通过：longOk={r.longOk} shortOk={r.shortOk}"
    unless r.longSteps == 3 && r.shortSteps == 2 do
      throwError "步数应为 3 / 2，实际 {r.longSteps} / {r.shortSteps}"
    unless r.deltaSteps == 1 do
      throwError "deltaSteps 应为 1，实际 {r.deltaSteps}"
    unless r.deltaChars > 0 do
      throwError "长证明字符数应更多，实际 deltaChars={r.deltaChars}"
    -- 反向（交换两条证明）⇒ Δ 必须变号
    let r2 ← Measure.compression stmt short long
    unless r2.deltaSteps == -1 do
      throwError "交换后 deltaSteps 应为 -1，实际 {r2.deltaSteps}"
  trivial

end SgsLean.Test
