/-
降级行为：猜想服务不可达时，搜索必须回到基础策略，不得崩溃、不得产生猜想动作。
-/
import Reap.Test.Tactic.Conjecture

open Lean Meta Elab Tactic
open Reap.TreeSearch
open TreeSearch

set_option linter.unusedSimpArgs false

set_option reap.policy_endpoint "http://127.0.0.1:1/v1"
set_option reap.value_endpoint "http://127.0.0.1:1/v1"
set_option reap.conjecture_enabled true
-- 两个端点都指向必然拒绝连接的端口
set_option reap.conjecture_endpoint "http://127.0.0.1:1/conjecture"
set_option reap.guide_endpoint "http://127.0.0.1:1/guide"
set_option reap.conjecture_num_samples 3

/-- 基础策略：直接解题，不依赖任何猜想。 -/
def degradeBasePolicy : PolicyValueEval := fun _ => do
  return (0.0, #[("exact himp h", #[], 1.0)])

/-- 服务不可达时：不产生猜想动作，搜索仍由基础策略完成并过终检。 -/
example (P Q : Prop) (h : P) (himp : P → Q) : Q := by
  run_tac do
    let saved ← saveState
    let ctx ← mkProofCheckContext
    let (some nodeIdx, nodes) ←
      runMCTSForTest (generatePolicyValueWithConjectureUsing degradeBasePolicy)
        (maxNodes := 32) (maxSteps := 32)
      | throwError "expected MCTS to fall back to the base policy"
    saved.restore
    let some root := nodes[0]? | unreachable!
    let tactics := childTacticStrings root
    if tactics.any (fun t => t.startsWith "have sgs_aux_") then
      throwError "conjecture action must not appear when the service is unreachable"
    let expected := "exact himp h"
    guardProofScriptEquals nodes nodeIdx expected
    guardProofScriptChecks ctx expected
  exact himp h
