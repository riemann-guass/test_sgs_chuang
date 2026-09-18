/-
# 门检 `Gate.check` 的离线测试

全部断言在 `run_tac` 里于 elaboration 期执行：**构建通过 ⟺ 断言通过**，
不需要 Mathlib，也不需要任何外部服务。反向对照（故意改错一条期望值使构建失败）
见 `docs/phase3-log.md`。
-/
import SgsLean
import SgsLean.Syntax

open Lean Meta Elab Tactic
open SgsLean

set_option linter.unusedVariables false

namespace SgsLean.Test

/-- 断言：门检应当接受该语句。 -/
def expectGateOk (stmt : String) : TacticM Unit := do
  let r ← Gate.check stmt
  unless r.ok do
    throwError "门检应当接受 {repr stmt}，实际 reason={r.reason} detail={r.detail}"

/-- 断言：门检应当拒绝该语句，且判定码恰好是指定值。 -/
def expectGateReject (stmt reason : String) : TacticM Unit := do
  let r ← Gate.check stmt
  if r.ok then
    throwError "门检应当拒绝 {repr stmt}，实际通过（elaboratedType={r.elaboratedType}）"
  unless r.reason == reason do
    throwError "门检对 {repr stmt} 的判定码应为 {reason}，实际为 {r.reason}（detail={r.detail}）"

/-! ## 正例 -/

/-- 上下文里的命题、复合命题、自带新变量的命题、以及 `ℕ` 级语句都通过门检。 -/
example (P Q : Prop) (h : P) (himp : P → Q) : Q := by
  run_tac do
    expectGateOk "Q"
    expectGateOk "P ∧ Q"
    expectGateOk "P → Q"
    expectGateOk "P ∨ ¬ P"
    expectGateOk "∀ (n : Nat), 2 ∣ n * (n + 1)"
    -- `ℕ` 记法补丁的回归测试：少了它这条会报 `HMul ℕ ℕ ?m` 之类的误导性错误。
    expectGateOk "∀ (n : ℕ), n + 0 = n"
    expectGateOk "∀ (n : ℕ), n ≤ n * n + 1"
  exact himp h

/-- 门检**不**判可证性：`False` 这类不可证命题也过门（可证性属于 G 的第三件）。

这条是边界声明，不是疏漏：门检的职责只有「能 elaborate 成一个命题」。 -/
example (P Q : Prop) (h : P) (himp : P → Q) : Q := by
  run_tac do
    expectGateOk "False"
    expectGateOk "¬ (P ∧ ¬ P)"
  exact himp h

/-! ## 负例 -/

/-- 负例 1：候选是**证明项**而不是命题。 -/
example (P Q : Prop) (h : P) (himp : P → Q) : Q := by
  run_tac do
    expectGateReject "fun h : P ∧ Q => h.right" "type_error"
    expectGateReject "fun (p : P) => himp p" "type_error"
  exact himp h

/-- 负例 2：候选是**类型**而不是命题。 -/
example (P Q : Prop) (h : P) (himp : P → Q) : Q := by
  run_tac do
    expectGateReject "Nat" "not_a_prop"
    expectGateReject "Prop" "not_a_prop"
    expectGateReject "Nat → Nat" "not_a_prop"
  exact himp h

/-- 负例 3：未知标识符（缺库）与解析失败要区分开。 -/
example (P Q : Prop) (h : P) (himp : P → Q) : Q := by
  run_tac do
    expectGateReject "NotARealType" "unknown_identifier"
    expectGateReject "∀ (n : Nat), Even n" "unknown_identifier"
    expectGateReject "∀ (" "parse_error"
  exact himp h

/-- 归一化：折叠空白（含 `\r\n`），供后续去重/α-等价使用。 -/
example (P Q : Prop) (h : P) (himp : P → Q) : Q := by
  run_tac do
    let r ← Gate.check "  P   →\r\n  Q "
    unless r.ok do
      throwError "带 CRLF 与多余空白的语句应当通过门检，实际 reason={r.reason}"
    unless r.stmt == "P → Q" do
      throwError "归一化后的语句应为 \"P → Q\"，实际为 {repr r.stmt}"
  exact himp h

end SgsLean.Test
