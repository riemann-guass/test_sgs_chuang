/-
# 验证 `Verify.verify` 的离线测试

与 `Test/Gate.lean` 同构：断言全部在 `run_tac` 内执行，构建通过即断言通过。
-/
import SgsLean
import SgsLean.Syntax

open Lean Meta Elab Tactic
open SgsLean

set_option linter.unusedVariables false

namespace SgsLean.Test

/-- 断言：验证应当接受，并且必须真的跑过 kernel 终检。 -/
def expectVerifyOk (stmt proof : String) : TacticM Unit := do
  let r ← Verify.verify stmt proof
  unless r.ok do
    throwError "验证应当接受 {repr stmt} / {repr proof}，实际 reason={r.reason} detail={r.detail}"
  unless r.finalChecked do
    throwError "验证通过时必须跑过 kernel 终检，实际 finalChecked={r.finalChecked}"

/-- 断言：验证应当拒绝，且判定码恰好是指定值。 -/
def expectVerifyReject (stmt proof reason : String) : TacticM Unit := do
  let r ← Verify.verify stmt proof
  if r.ok then
    throwError "验证应当拒绝 {repr stmt} / {repr proof}，实际通过"
  unless r.reason == reason do
    throwError "验证 {repr stmt} / {repr proof} 的判定码应为 {reason}，实际为 {r.reason}（detail={r.detail}）"

/-! ## 正例 -/

/-- 单步证明、多步证明、bullet 结构、`ℕ` 级语句、CRLF 证明都能通过验证。 -/
example (P Q : Prop) (h : P) (himp : P → Q) : Q := by
  run_tac do
    expectVerifyOk "P → Q" "intro hp\nexact himp hp"
    expectVerifyOk "Q" "exact himp h"
    expectVerifyOk "P → Q" "intro hp; exact himp hp"
    expectVerifyOk "P ∧ Q → Q ∧ P" "intro hp\nconstructor\n· exact hp.right\n· exact hp.left"
    expectVerifyOk "∀ (n : ℕ), n + 0 = n" "intro n\nrfl"
    -- 行尾归一化的回归测试：`indentScript` 按 "\n" 切行，残留 `\r` 会变成解析失败。
    expectVerifyOk "P → Q" "intro hp\r\nexact himp hp"
  exact himp h

/-! ## 负例 -/

/-- 负例 1：证明含 `sorry`，或含等价的 `admit`。

`checkTacticSyntax` 在本版本 Lean 上匹配不到这两个节点（见 `SgsLean/Verify.lean` 文件头），
真正拦住它的是 `checkProof` 的 `assignedProofHasMVarOrSorry`。 -/
example (P Q : Prop) (h : P) (himp : P → Q) : Q := by
  run_tac do
    expectVerifyReject "P" "sorry" "mvar_or_sorry"
    expectVerifyReject "P" "admit" "mvar_or_sorry"
    expectVerifyReject "P → P" "intro hp\nexact sorry" "mvar_or_sorry"
  exact himp h

/-- 负例 2：证明未闭合（含残留 metavariable）。 -/
example (P Q : Prop) (h : P) (himp : P → Q) : Q := by
  run_tac do
    expectVerifyReject "P → Q" "intro hp" "unclosed_goals"
    expectVerifyReject "P ∧ Q" "constructor" "unclosed_goals"
    -- `exact ?_` 留下未合成的占位符：Lean 先报 `don't know how to synthesize placeholder`，
    -- 再报 `unsolved goals`，两者都归类为「没闭合」。
    expectVerifyReject "P" "exact ?_" "unclosed_goals"
  exact himp h

/-- 负例 3：证明与语句不匹配、证明为空。 -/
example (P Q : Prop) (h : P) (himp : P → Q) : Q := by
  run_tac do
    expectVerifyReject "P" "exact himp" "type_error"
    expectVerifyReject "P" "" "parse_error"
  exact himp h

/-- 负例 4：语句先过不了门检时，验证直接回传门检判定码。 -/
example (P Q : Prop) (h : P) (himp : P → Q) : Q := by
  run_tac do
    expectVerifyReject "Nat" "exact 0" "not_a_prop"
    expectVerifyReject "NotARealType" "exact h" "unknown_identifier"
    expectVerifyReject "fun h : P ∧ Q => h.right" "exact h" "type_error"
  exact himp h

end SgsLean.Test
