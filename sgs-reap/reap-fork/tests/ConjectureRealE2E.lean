/-
真实模型端到端：Lean --HTTP--> service/proxy.py --HTTPS--> DeepSeek。

    cd sgs-reap/reap-fork
    lake env lean tests/ConjectureRealE2E.lean

由 `sgs-reap/tests/run_real_e2e.py` 负责启停代理并汇总结果。

两点环境说明：

1. 载体定理的结论用 `sorry` 收尾：本文件要的是"真实的证明上下文"，而所有断言都在
   `run_tac` 里于 elaboration 期执行，载体自身的证明与本文件所测的东西无关。
2. 需要 `notation "ℕ" => Nat`。本项目（reap 的 `public meta import` + 无 Mathlib）环境里
   没有 `ℕ` 这个 unicode 记法；缺少它时 Lean 的 `autoImplicit` 会把 `ℕ` 当成隐式绑定变量，
   报出极具误导性的 `failed to synthesize HMul ℕ ℕ ?m.4`。ASCII 的 `Nat` 一直可用。
   这一行同时让模型输出的 `ℕ` 记法能被解析——否则候选会因为"记法缺失"而不是"数学错误"被拒。
-/
import Reap.Test.Tactic.Conjecture

open Lean Meta Elab Tactic
open Reap.TreeSearch
open TreeSearch

set_option linter.unusedSimpArgs false

set_option reap.policy_endpoint "http://127.0.0.1:1/v1"
set_option reap.value_endpoint "http://127.0.0.1:1/v1"
set_option reap.conjecture_enabled true
set_option reap.conjecture_endpoint "http://127.0.0.1:8770/conjecture"
set_option reap.guide_endpoint "http://127.0.0.1:8770/guide"
set_option reap.conjecture_num_samples 3
set_option reap.wall_clock_log_path "real_e2e_wall_clock.jsonl"

-- 让模型惯用的 ℕ 记法在本文件中可用（见文件头说明 2）
notation "ℕ" => Nat

/-- 测试结果落盘（JSONL），供 runner 汇总。 -/
def appendResult (obj : Json) : MetaM Unit := do
  let path : System.FilePath := "real_e2e_results.jsonl"
  IO.FS.withFile path .append fun handle => handle.putStrLn obj.compress

/-- 真实服务给出的候选，能否在当前上下文中作为 `have ... := ?_` 通过 Lean 解析与 elaboration。

这就是"候选引理利用率"的 Lean 侧定义——闸门 M1 的核心指标。 -/
def checkCandidates (candidates : Array ConjectureCandidate) : TacticM (Nat × Array String) := do
  let mut accepted := 0
  let mut rejected : Array String := #[]
  for candidate in candidates do
    let saved ← saveState
    let ctx ← mkProofCheckContext
    let action := s!"have {conjectureHypPrefix}{candidate.index} : {candidate.type} := ?_"
    match ← evalTacticStrNoFinalCheck ctx action 200000 with
    | .ok _ => accepted := accepted + 1
    | .error err => rejected := rejected.push (toString candidate.type ++ " :: " ++ toString err)
    saved.restore
  return (accepted, rejected)

/-- 用例 A：真实服务的候选可以被 Lean 接受（HTTP + 解析 + elaboration 全链路）。 -/
example (n : ℕ) : 2 ∣ n ^ 2 + n := by
  run_tac do
    let goals ← getUnsolvedGoals
    let ppState := toString (← TacticGenerator.Meta.ppProofState goals)
    let target ← conjectureTarget goals
    let candidates ← ConjectureClient.proposeConjectures
      { apiUrl := "http://127.0.0.1:8770/conjecture" } ppState 3
    if candidates.isEmpty then
      throwError "真实服务未返回任何候选命题"
    let reviews ← GuideClient.scoreConjectures
      { apiUrl := "http://127.0.0.1:8770/guide" } target candidates
    let priors := conjecturePriors reviews
      ((reap.conjecture_weight.get (← getOptions)).toFloat / 1000.0)
      ((reap.conjecture_temperature.get (← getOptions)).toFloat / 1000.0)
    let (accepted, rejected) := ← checkCandidates candidates
    appendResult (json% {
      case: "http_and_elaboration",
      goal: $ppState,
      candidates: $(candidates.map (·.type)),
      reviews: $reviews,
      priors: $priors,
      accepted: $accepted,
      rejected: $rejected
    })
    if accepted == 0 then
      throwError "所有候选都无法在当前上下文中 elaborate"
  sorry

/-- 用例 B：真实候选进入 MCTS 动作集，成为树边（`maxSteps=1` 只展开根节点以控制 API 调用数）。 -/
example (n : ℕ) : 2 ∣ n ^ 2 + n := by
  run_tac do
    let saved ← saveState
    let result ← runMCTS generatePolicyValueWithConjecture (maxNodes := 8) (maxSteps := 1)
    saved.restore
    let some root := result.nodes[0]? | unreachable!
    let conjectureEdges := root.children.toList.filter fun (edge, _) =>
      edge.tacticStr.startsWith "have sgs_aux_"
    appendResult (json% {
      case: "mcts_edge",
      conjecture_edges: $(conjectureEdges.length),
      all_edges: $(root.children.toList.map fun (e, _) => e.tacticStr)
    })
    if conjectureEdges.isEmpty then
      throwError "真实候选没有进入 MCTS 动作集"
  sorry
