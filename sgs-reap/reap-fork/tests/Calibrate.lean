/-
闸门 M1 标定：20 个含真实证明上下文的目标，逐个跑
`/conjecture` →（Lean 侧 elaboration 检查）→ `/guide`，结果写入 JSONL。

    cd sgs-reap/reap-fork
    lake env lean tests/Calibrate.lean

由 `sgs-reap/tests/run_m1_calibration.py` 启停代理并汇总。

指标定义：
  * 利用率 = 能被当前上下文接受的候选 / 候选总数；
  * 拒绝原因分类：parse_error / unknown_identifier（缺库）/ type_error / 其他；
  * 非平凡率 = 候选既不等同于目标、也不等同于任一已有假设的比例。

注意：本批目标是**流水线标定用**的初等目标，不代表领域难度；ℝ 级与 Mathlib 依赖目标
留到阶段 4。载体定理用 `sorry` 收尾，断言全部在 `run_tac` 于 elaboration 期执行。
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
set_option reap.conjecture_num_samples 2
set_option reap.wall_clock_log_path "calibrate_wall_clock.jsonl"

notation "ℕ" => Nat

/-- 把 `EvalError` 归类，供 M1 统计。 -/
def classifyError (err : EvalError) : String :=
  match err with
  | .parseError _ => "parse_error"
  | .forbiddenTactic _ => "forbidden_tactic"
  | .tacticTimeout => "timeout"
  | .tacticException _ => "exception"
  | .unassignedGoal => "unassigned_goal"
  | .assignedProofHasMVarOrSorry => "mvar_or_sorry"
  | .auxProofHasMVarOrSorry _ => "aux_mvar_or_sorry"
  | .auxProofKernelCheckFailed _ _ => "aux_kernel_check_failed"
  | .finalProofCheckFailed => "final_check_failed"
  | .tacticErrorMessages msgs =>
    let text := String.intercalate " " (msgs.map fun m => (toJson m).compress)
    if (text.contains "Unknown identifier") || (text.contains "unknown identifier")
        || (text.contains "Unknown constant") || (text.contains "unknown constant") then
      "unknown_identifier"
    else if text.contains "unknown namespace" then
      "unknown_identifier"
    else
      "type_error"

/-- 局部上下文里所有命题的类型（归一化后的文本），用于判断候选是否只是重述。 -/
def localHypTypes (goals : List MVarId) : MetaM (Array String) := do
  let some goal := goals.head? | return #[]
  goal.withContext do
    let mut out : Array String := #[]
    for decl in ← getLCtx do
      unless decl.isImplementationDetail do
        let ty ← instantiateMVars decl.type
        out := out.push (normalizeProp (toString (← Meta.ppExpr ty)))
    return out

/-- 单个候选的检查结果。 -/
structure CandidateOutcome where
  type : String
  review : Float
  accepted : Bool
  rejection : String
  nonTrivial : Bool
deriving ToJson

/-- 标定一个目标：请求候选、评审、逐条做 elaboration 检查并落盘。 -/
def calibrate (caseName : String) : TacticM Unit := do
  let goals ← getUnsolvedGoals
  if goals.isEmpty then
    throwError s!"[{caseName}] 没有未解决目标"
  let ppState := toString (← TacticGenerator.Meta.ppProofState goals)
  let target ← conjectureTarget goals
  let candidateUrl := reap.conjecture_endpoint.get (← getOptions)
  let guideUrl := reap.guide_endpoint.get (← getOptions)
  let numSamples := reap.conjecture_num_samples.get (← getOptions)

  let candidates ← ConjectureClient.proposeConjectures { apiUrl := candidateUrl } ppState numSamples
  let reviews ← GuideClient.scoreConjectures { apiUrl := guideUrl } target candidates
  let hypTypes ← localHypTypes goals
  let targetNorm := normalizeProp target

  let mut outcomes : Array CandidateOutcome := #[]
  for candidate in candidates do
    let saved ← saveState
    let ctx ← mkProofCheckContext
    let action := s!"have {conjectureHypPrefix}{candidate.index} : {candidate.type} := ?_"
    let result ← evalTacticStrNoFinalCheck ctx action 200000
    saved.restore
    let candNorm := normalizeProp candidate.type
    let review := if candidate.index < reviews.size then reviews[candidate.index]! else 0.0
    outcomes := outcomes.push {
      type := candidate.type
      review := review
      accepted := result.isOk
      rejection := match result with
        | .ok _ => ""
        | .error err => classifyError err
      nonTrivial := candNorm != targetNorm && !hypTypes.contains candNorm
    }

  let record := json% {
    case: $caseName,
    goal: $ppState,
    candidates: $(outcomes.map (·.type)),
    reviews: $(outcomes.map (·.review)),
    accepted: $(outcomes.filter (·.accepted) |>.size),
    rejections: $(outcomes.filter (!·.accepted) |>.map (fun o => json% {type: $(o.type), reason: $(o.rejection)})),
    non_trivial: $(outcomes.filter (·.nonTrivial) |>.size),
    total: $(outcomes.size)
  }
  let path : System.FilePath := "calibrate_results.jsonl"
  IO.FS.withFile path .append fun handle => handle.putStrLn record.compress

-- ── 20 个标定目标（初等、无 Mathlib 依赖）──────────────────────────────
-- 注意：必须分行写。`run_tac f; sorry` 会把 `; sorry` 解析进 run_tac 的 do 块。

example (n : ℕ) : 2 ∣ n ^ 2 + n := by
  run_tac calibrate "nat_sq_plus_n_even"
  sorry

example (n : ℕ) : n % 2 = 0 ∨ n % 2 = 1 := by
  run_tac calibrate "nat_mod_two"
  sorry

example (n : ℕ) : (n + 1) ^ 2 = n ^ 2 + 2 * n + 1 := by
  run_tac calibrate "nat_sq_succ"
  sorry

example (a b : ℕ) : (a + b) ^ 2 = a ^ 2 + 2 * a * b + b ^ 2 := by
  run_tac calibrate "nat_sq_add"
  sorry

example (n : ℕ) : n * 0 = 0 := by
  run_tac calibrate "nat_mul_zero"
  sorry

example (n : ℕ) : 0 + n = n := by
  run_tac calibrate "nat_zero_add"
  sorry

example (n : ℕ) : n ≤ n + n := by
  run_tac calibrate "nat_le_double"
  sorry

example (n : ℕ) : n ≤ n * n + 1 := by
  run_tac calibrate "nat_le_sq_plus_one"
  sorry

example (a b : ℕ) (h : a = b) : a * a = b * b := by
  run_tac calibrate "nat_eq_mul_congr"
  sorry

example (a b : ℕ) (h : a ∣ b) : a ∣ b * b := by
  run_tac calibrate "dvd_mul_self"
  sorry

example (a b : ℕ) (h : a ∣ b) : a ∣ 2 * b := by
  run_tac calibrate "dvd_two_mul"
  sorry

example (a b : ℕ) (h : a < b) : a + 1 ≤ b := by
  run_tac calibrate "lt_implies_succ_le"
  sorry

example (n : ℕ) : 3 ∣ n ^ 3 + 2 * n := by
  run_tac calibrate "three_dvd_cube_plus_two"
  sorry

example (n : ℕ) : n ^ 2 + n = n * (n + 1) := by
  run_tac calibrate "sq_plus_eq_mul"
  sorry

example (a b : ℕ) : a * b = b * a := by
  run_tac calibrate "mul_comm_nat"
  sorry

example (P Q R : Prop) (h1 : P → Q) (h2 : Q → R) : P → R := by
  run_tac calibrate "prop_trans"
  sorry

example (P Q : Prop) (h : P ∧ Q) : Q ∧ P := by
  run_tac calibrate "prop_and_comm"
  sorry

example (P Q : Prop) (h : P ∨ Q) (hp : ¬ P) : Q := by
  run_tac calibrate "prop_or_elim"
  sorry

example (α : Type) (a b : α) (h : a = b) : b = a := by
  run_tac calibrate "eq_symm"
  sorry

example (P : Prop) (h : ¬ ¬ P) : P := by
  run_tac calibrate "double_neg"
  sorry
