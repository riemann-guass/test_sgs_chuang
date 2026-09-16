module
public meta import Batteries.Lean.Meta.Basic
public meta import Reap.Conjecture.API
public meta import Reap.Options
public meta import Reap.Tactic.Generator
public meta import Reap.Tactic.TreeSearch
public meta import Reap.Tactic.WallClock

public meta section

open Lean Meta Elab Tactic
open Reap.WallClock

namespace Reap.TreeSearch

/-- 猜想引入的辅助引理使用的名字前缀，同时充当「本路径是否已经猜想过的」标记。 -/
def conjectureHypPrefix : String := "sgs_aux_"

/-- 当前上下文里是否已存在猜想引入的辅助引理。

由于 `have aux : P := ?_` 会把 `aux` 带进该分支后续所有节点的局部上下文，
这条检查等价于「每条搜索路径最多猜想一次」，从而避免猜想调用随深度指数增长。 -/
def hasConjecturedHypothesis (goals : List MVarId) : MetaM Bool := do
  let some goal := goals.head? | return false
  goal.withContext do
    for localDecl in ← getLCtx do
      if let .str _ s := localDecl.userName then
        if s.startsWith conjectureHypPrefix then
          return true
    return false

/-- 当前主目标的类型，作为 `/guide` 的 target。 -/
def conjectureTarget (goals : List MVarId) : MetaM String := do
  let some goal := goals.head? | return ""
  goal.withContext do
    return toString (← Meta.ppExpr (← goal.getTypeCleanup))

def conjectureAction (candidate : ConjectureCandidate) : String :=
  s!"have {conjectureHypPrefix}{candidate.index} : {candidate.type} := ?_"

/-- 归一化命题文本：把所有空白折叠成单个空格并去掉首尾空白。

用于挡掉"对当前目标重述一遍"的候选（SGS 的 Guide rubric 对与目标等价的引理直接给 0 分，
在推理期同样没有价值，而且会在辅助引理的子目标上产生自指猜想）。 -/
def normalizeProp (s : String) : String :=
  let rec go (cs : List Char) (out : List Char) (prevSpace : Bool) : List Char :=
    match cs with
    | [] => out.reverse
    | c :: rest =>
      if c.isWhitespace then
        go rest (if prevSpace then out else ' ' :: out) true
      else
        go rest (c :: out) false
  (String.ofList (go s.toList [] false)).trimAscii.toString

/-- 在 policy 动作之外，追加密集猜想出的辅助引理动作。

新动作用 `have <fresh> : <prop> := ?_` 表示，会由 reap 已有的 OR/AND 机制自动拆成
「用引理收尾」与「证引理」两个 focus 子目标，并接受同一套 proof replay 与 kernel 终检。

任何一步失败（服务不可用、无候选、打分失败）都退回基础策略行为。

基础策略作为参数注入，便于在无 model 服务的情况下做端到端测试：
`generatePolicyValueWithConjecture` 只是它绑定到真实 policy 客户端的版本。 -/
def generatePolicyValueWithConjectureUsing (base : PolicyValueEval) : PolicyValueEval := fun goals => do
  let (value, tactics) ← base goals
  let opts ← getOptions
  let enabled := reap.conjecture_enabled.get opts
  if !enabled || goals.isEmpty then
    return (value, tactics)
  if ← hasConjecturedHypothesis goals then
    return (value, tactics)

  let numSamples := reap.conjecture_num_samples.get opts
  let target ← conjectureTarget goals
  let ppState := toString (← TacticGenerator.Meta.ppProofState goals)
  let candidates ← withLogWallClockTime "conjecture"
      (fun (r : Array ConjectureCandidate) => json%{ num_candidates: $(r.size), goal: $ppState }) do
    ConjectureClient.proposeConjectures
      { apiUrl := reap.conjecture_endpoint.get opts } ppState numSamples
  if candidates.isEmpty then
    return (value, tactics)

  let reviews ← withLogWallClockTime "guide"
      (fun (r : Array Float) => json%{ reviews: $r, target: $target }) do
    GuideClient.scoreConjectures
      { apiUrl := reap.guide_endpoint.get opts } target candidates
  let beta := (reap.conjecture_weight.get opts).toFloat / 1000.0
  let temp := (reap.conjecture_temperature.get opts).toFloat / 1000.0
  let priors := conjecturePriors reviews beta temp

  let mut extra : Array (String × Array PremiseSelectionResult × Float) := #[]
  for i in [:candidates.size] do
    let candidate := candidates[i]!
    let candType := normalizeProp candidate.type
    if !candType.isEmpty && candType != normalizeProp target then
      let prior := if i < priors.size then priors[i]! else 0.0
      extra := extra.push (conjectureAction candidate, #[], prior)
  return (value, tactics ++ extra)

/-- 生产入口：在 `TacticGenerator.generatePolicyValue` 之上追加猜想动作。 -/
def generatePolicyValueWithConjecture : PolicyValueEval :=
  generatePolicyValueWithConjectureUsing TacticGenerator.generatePolicyValue

end Reap.TreeSearch
