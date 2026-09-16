/-
端到端联调：Lean 包装器 --HTTP--> 假服务。

本文件**不属于** `Reap` / `Reap.Test` 库（不在 lake 的 glob 范围内），
因此 `lake build` 不会构建它。它需要假服务在 127.0.0.1:8765 上运行，由
`sgs-reap/tests/run_e2e.py` 负责启动与执行：

    cd sgs-reap/reap-fork
    lake env lean tests/ConjectureE2E.lean

policy / value 端点被故意指向不可达端口，于是搜索里所有动作只能来自 `/conjecture`，
这样一旦证明成功，就只可能是 HTTP 猜想链路在工作。
-/
import Reap.Test.Tactic.Conjecture

open Lean Meta Elab Tactic
open Reap.TreeSearch
open TreeSearch

set_option linter.unusedSimpArgs false

set_option reap.policy_endpoint "http://127.0.0.1:1/v1"
set_option reap.value_endpoint "http://127.0.0.1:1/v1"
set_option reap.conjecture_enabled true
set_option reap.conjecture_endpoint "http://127.0.0.1:8765/conjecture"
set_option reap.guide_endpoint "http://127.0.0.1:8765/guide"
set_option reap.conjecture_num_samples 3
-- 可观测性：复用 reap 已有的 wall-clock 日志与 raw tree 导出
set_option reap.wall_clock_log_path "e2e_wall_clock.jsonl"
set_option reap.raw_tree_path "e2e_raw_tree.json"

/-- 基础策略：只使用猜想引入的假设 `sgs_aux_0`，并区分 AND 的两个焦点子目标。 -/
def e2eBasePolicy : PolicyValueEval := fun goals => do
  if ← hasLocalDeclNamed goals `sgs_aux_0 then
    return (0.0, #[("exact himp sgs_aux_0", #[], 1.0)])
  if (← goalTypeString goals) == "P" then
    return (0.0, #[("exact h", #[], 1.0)])
  return (0.0, #[])

/-- 主链路：候选引理经 HTTP 取回，进入动作集，组装出的脚本通过 kernel 终检。

这里刻意走 `runMCTS`（而非测试辅助的 `mctsForTest`），以便同时验证
`reap.wall_clock_log_path` 与 `reap.raw_tree_path` 这两个既有可观测性出口。 -/
example (P Q : Prop) (h : P) (himp : P → Q) : Q := by
  run_tac do
    let saved ← saveState
    let result ←
      runMCTS (generatePolicyValueWithConjectureUsing e2eBasePolicy)
        (maxNodes := 32) (maxSteps := 32)
    saved.restore
    let some nodeIdx := result.solution?
      | throwError "expected MCTS to solve the goal through the HTTP conjecture service"
    let some root := result.nodes[0]? | unreachable!
    let tactics := childTacticStrings root
    unless tactics.any (fun t => t.startsWith "have sgs_aux_") do
      throwError "expected a conjecture action from the service, got: {tactics}"
    let expected := "have sgs_aux_0 : P := ?_\n· exact himp sgs_aux_0\n· exact h"
    guardProofScriptEquals result.nodes nodeIdx expected
    guardProofScriptChecks result.ctx expected
  exact himp h

/-- 路径标记：同一条分支上不会重复猜想（第二层节点不再产生猜想动作）。 -/
example (P Q : Prop) (h : P) (himp : P → Q) : Q := by
  run_tac do
    let saved ← saveState
    let (_, nodes) ←
      runMCTSForTest (generatePolicyValueWithConjectureUsing e2eBasePolicy)
        (maxNodes := 32) (maxSteps := 32)
    saved.restore
    let conjectureEdges :=
      nodes.toList.flatMap fun n => n.children.toList.filterMap fun (e, _) =>
        if e.tacticStr.startsWith "have sgs_aux_" then some e.tacticStr else none
    unless conjectureEdges.length == 1 do
      throwError "expected exactly 1 conjecture edge in the whole tree, got {toString conjectureEdges.length}"
  exact himp h
