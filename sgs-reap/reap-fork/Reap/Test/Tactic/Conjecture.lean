import Reap.Test.Tactic.MCTS

open Lean Meta Elab Tactic
open Reap.TreeSearch
open TreeSearch

set_option linter.unusedSimpArgs false

/-!
# 猜想动作（conjecture action）的纯 Lean 验证

阶段 0 的验收测试：在**不接任何外部服务、不花一分钱**的前提下，验证

> 猜想器给出一条辅助引理 → reap 把它当作普通 tactic 动作 → AND 节点 → proof script → kernel 终检

这条链路成立，并验证非法猜想动作会被安全丢弃而不是中断搜索。

猜想动作采用 SGS Conjecturer 在推理期的对应形态：

```lean
have aux : <辅助引理> := ?_
```

其中 `?_` 是 Lean 的 synthetic hole，会产生一个真正的子目标，而不是把目标标记为已解决。
-/

/-- 取出当前主目标的类型字符串，用于在假策略里区分不同节点。 -/
def goalTypeString (goals : List MVarId) : MetaM String := do
  match goals with
  | [] => return ""
  | goal :: _ => goal.withContext do
      return toString (← Meta.ppExpr (← goal.getTypeCleanup))

/-- 假猜想策略：在 `Q` 上只给出猜想动作，其余动作一概不给。 -/
def conjecturePolicyValue : PolicyValueEval := fun goals => do
  if ← hasLocalDeclNamed goals `aux then
    -- 辅助引理已进入上下文，用它收尾主目标
    return (0.0, #[("exact himp aux", #[], 1.0)])
  if (← goalTypeString goals) == "P" then
    -- 这是辅助引理自身的证明义务
    return (0.0, #[("exact h", #[], 1.0)])
  -- 根节点：只给猜想动作，不提供任何直接解题动作
  return (0.0, #[("have aux : P := ?_", #[], 1.0)])

/-- 含噪猜想策略：先抛两条非法猜想动作，再给一条可用的。 -/
def noisyConjecturePolicyValue : PolicyValueEval := fun goals => do
  if ← hasLocalDeclNamed goals `aux then
    return (0.0, #[("exact himp aux", #[], 1.0)])
  if (← goalTypeString goals) == "P" then
    return (0.0, #[("exact h", #[], 1.0)])
  return (0.0, #[
    ("have bad : NotARealType := ?_", #[], 1.0),
    ("this is not a tactic", #[], 1.0),
    ("have aux : P := ?_", #[], 1.0)
  ])

/-- 正例：猜想动作经 AND 节点组装出的证明脚本通过 kernel 终检。 -/
example (P Q : Prop) (h : P) (himp : P → Q) : Q := by
  run_tac do
    let saved ← saveState
    let ctx ← mkProofCheckContext
    let (some nodeIdx, nodes) ← runMCTSForTest conjecturePolicyValue (maxNodes := 32) (maxSteps := 32)
      | throwError "expected MCTS to solve the goal using the conjectured auxiliary lemma"
    saved.restore
    let expected := "have aux : P := ?_\n· exact himp aux\n· exact h"
    guardProofScriptEquals nodes nodeIdx expected
    guardProofScriptChecks ctx expected
  exact himp h

/-- 猜想动作确实产生了 AND 节点（两焦点子目标），且复用 reap 的 AND 拆分。 -/
example (P Q : Prop) (h : P) (himp : P → Q) : Q := by
  run_tac do
    let saved ← saveState
    let (_, nodes) ← runMCTSForTest conjecturePolicyValue (maxNodes := 32) (maxSteps := 32)
    saved.restore
    let some root := nodes[0]? | unreachable!
    let some (rootEdge, childIdx) := root.children[0]? | unreachable!
    unless rootEdge.tacticStr == "have aux : P := ?_" do
      throwError "unexpected conjecture action: {rootEdge.tacticStr}"
    let some child := nodes[childIdx]? | unreachable!
    unless child.data.toPlay == MCTS.NodeKind.andNode do
      throwError "expected the conjecture action to produce an AND node"
    let focusIdx := child.children.filterMap fun (e, _) => if e.isFocus then some e.focusIndex else none
    unless focusIdx.size == 2 do
      throwError "expected 2 focus children for the conjecture action, got {toString focusIdx.size}"
  exact himp h

/-- 负例：非法猜想动作（无法 elaborate / 解析失败）被丢弃，搜索仍能找到解。 -/
example (P Q : Prop) (h : P) (himp : P → Q) : Q := by
  run_tac do
    let saved ← saveState
    let ctx ← mkProofCheckContext
    let (some nodeIdx, nodes) ← runMCTSForTest noisyConjecturePolicyValue (maxNodes := 32) (maxSteps := 32)
      | throwError "expected MCTS to recover from invalid conjecture actions"
    saved.restore
    let some root := nodes[0]? | unreachable!
    let tactics := childTacticStrings root
    if tactics.contains "have bad : NotARealType := ?_" then
      throwError "invalid conjecture action (unknown identifier) should not become a tree edge"
    if tactics.contains "this is not a tactic" then
      throwError "unparsable conjecture action should not become a tree edge"
    let expected := "have aux : P := ?_\n· exact himp aux\n· exact h"
    guardProofScriptEquals nodes nodeIdx expected
    guardProofScriptChecks ctx expected
  exact himp h
