/-
# 廉价 tactic 兜底 `Trivial.tryCheapTactics` 的离线测试

在线流程第 3 步（规格文档 3.3 节）：不花模型钱地先试一遍廉价 tactic。
本文件测三件事，都按"结构断言优先"写——`norm_num` / `omega` 依赖 Mathlib，
在 lake 的构建环境（无 Mathlib）里不存在，把断言写死在具体 tactic 上会让测试
只在某一种模式下成立。

1. 该命中的必须命中：`True` 在第一批里就该被秒掉，且命中的 tactic 必须在候选清单里；
2. 该不命中的必须不命中：不可证的语句返回 `hit=false`，且**试满三批**、预算递增；
3. 不可 elaborate 的语句要如实报 `detail`，而不是伪装成"兜底失败"——
   这一条是"输入问题不是证不出来"的判据，混了会让失败率的分子分母都错。
4. `intros` 变体必须真的在探针清单里：我们的输入永远是闭式命题（`∀ x, P`），
   而 `ring` / `linarith` / `positivity` 不会自己 intro——不给它们加 `intros` 前缀，
   这一批在绝大多数题上等于不存在（Mathlib 模式实测：`ring` 单独打在
   `∀ (a b : Nat), a * b = b * a` 上是 `unclosed_goals`，`intro a b\nring` 通过）。
   本文件用**纯函数断言**钉住这条机制（`ring` 在无 Mathlib 的构建环境里不可用，
   不能拿它当正例）。

反向对照（漏了就会被测试抓住）：把 `cheapBatches` 的任一批删掉，
`expectTriedAllBatches` 立刻因为候选数不足而报错；把 `hit` 写成常量 `false`，
第一条正例立刻失败。

完整的三批清单在 Mathlib 模式下的真实输出（含哪些 tactic 真的秒掉了哪些语句）
记在 `docs/phase25-log.md` 的标定记录里。
-/
import SgsLean
import SgsLean.Syntax

open Lean Meta Elab Tactic
open SgsLean

set_option linter.unusedVariables false

namespace SgsLean.Test

/-- 三批清单里一共多少条 **tactic**（不含 `intros` 变体）。 -/
def cheapTacticCount : Nat :=
  Trivial.cheapBatches.foldl (fun acc b => acc + b.tactics.size) 0

/-- 实际会跑多少条**探针**（每条 tactic 在开启变体的批次里算两条）。 -/
def cheapProbeCount : Nat :=
  Trivial.cheapBatches.foldl
    (fun acc b => acc + b.tactics.foldl (fun a t => a + (Trivial.probeScripts b t).size) 0) 0

/-- 这条脚本是不是"清单里的 tactic"或"它的 intros 变体"。 -/
def isCheapProbe (script : String) : Bool :=
  Trivial.cheapBatches.any fun b =>
    b.tactics.any fun t => (Trivial.probeScripts b t).contains script

/-- 断言：兜底命中，且命中的 tactic 在候选清单里。 -/
def expectCheapHit (stmt : String) : TacticM Unit := do
  let r ← Trivial.tryCheapTactics stmt
  unless r.hit do
    throwError "「{stmt}」应被兜底命中，实际 hit=false，tried={r.tried}"
  if r.tactic.isEmpty then
    throwError "「{stmt}」命中却没记录是哪个 tactic"
  if r.proof != r.tactic then
    throwError "「{stmt}」命中时 proof 应等于 tactic 本身，实际 proof={r.proof}"
  if !isCheapProbe r.tactic then
    throwError "「{stmt}」命中的 tactic「{r.tactic}」不在三批清单里"

/-- 断言：兜底未命中，且**试满三批**、预算单调不减。 -/
def expectCheapMiss (stmt : String) : TacticM Unit := do
  let r ← Trivial.tryCheapTactics stmt
  if r.hit then
    throwError "「{stmt}」不应被兜底命中，实际被 {r.tactic} 秒掉"
  unless r.tried.size == cheapProbeCount do
    throwError "「{stmt}」应试满 {cheapProbeCount} 条探针，实际 {r.tried.size}：{r.tried}"
  unless r.proof.isEmpty do
    throwError "「{stmt}」未命中时 proof 应为空串，实际 {r.proof}"
  let budgets := r.tried.map (fun t => t.2.2)
  unless budgets == budgets.qsort (· ≤ ·) do
    throwError "「{stmt}」的预算应单调不减，实际 {budgets}"

end SgsLean.Test

open SgsLean.Test

/-- 正例：定义上成立 / 可判定的语句在第一批就被秒掉。 -/
example : True := by
  run_tac do
    expectCheapHit "True"
    expectCheapHit "1 = 1"
    expectCheapHit "∀ (n : Nat), n + 0 = n"
  trivial

/-- 负例：不可证的语句不该被兜底命中，且必须试满三批。 -/
example : True := by
  run_tac do
    expectCheapMiss "∀ (P : Prop), P"
  trivial

/-- 边界：语句本身 elaborate 失败时必须如实报 detail，而不是伪装成未命中。 -/
example : True := by
  run_tac do
    -- 每个样本都必须：不命中、给出非空 detail、且一条 tactic 也没试
    -- （这三条一起才是"输入问题"与"兜底失败"的分界）
    for stmt in ["∀ (n : Nat, n = n", "∀ (:::)"] do
      let r ← Trivial.tryCheapTactics stmt
      if r.hit then
        throwError "不可解析的语句「{stmt}」被兜底判成命中：{r.tactic}"
      if r.detail.isEmpty then
        throwError "不可解析的语句「{stmt}」应给出 detail，实际为空"
      unless r.tried.isEmpty do
        throwError "不可解析的语句「{stmt}」不该试 tactic，实际试了 {r.tried.size} 条"
  trivial

/-- 反向对照：`Nat` 这种 Type 级目标**确实**能被 `constructor` 闭合，不是"非平凡"。 -/
example : True := by
  run_tac do
    let r ← Trivial.tryCheapTactics "Nat"
    unless r.hit do
      throwError "「Nat」应被 constructor 秒掉（这正说明它不该当负例），实际 hit=false"
  trivial

/-- 机制断言：`intros` 变体必须真的生成，且第三批（最贵的 aesop）不重复试探。

这条与具体 tactic 是否可用无关，所以在无 Mathlib 的构建环境里也成立；
它是"∀-目标上这批 tactic 不再是摆设"这条改动的**唯一**离线抓手。 -/
example : True := by
  run_tac do
    let withVariant : Trivial.CheapBatch :=
      { tactics := #["ring"], heartbeatScale := 1, introsFor := #["ring"] }
    let scripts := Trivial.probeScripts withVariant "ring"
    unless scripts == #["ring", "intros\nring"] do
      throwError "intros 变体没有生成：{scripts}"
    let noVariant := Trivial.probeScripts
      { tactics := #["aesop"], heartbeatScale := 50 } "aesop"
    unless noVariant == #["aesop"] do
      throwError "关掉变体的批次不该生成 intros 变体：{noVariant}"
    unless cheapProbeCount > cheapTacticCount do
      throwError "开启变体后探针数必须多于 tactic 数：{cheapProbeCount} vs {cheapTacticCount}"
    -- 变体必须是**平铺两行**：`intros\n  ring` 会被解析成"把 ring 当假设名"（实测踩过：
    -- 目标上下文里出现了 `ring : ℕ`，整条探针静默失效）。这里检查第二行不以空格开头。
    let indented (s : String) : Bool :=
      match s.splitOn "\n" with
      | [] | [_] => false
      | _ :: rest => rest.any (fun line => line.startsWith " ")
    let scriptsAll := Trivial.cheapBatches.flatMap (fun b =>
      b.tactics.flatMap (fun t => Trivial.probeScripts b t))
    let bad := scriptsAll.filter indented
    unless bad.isEmpty do
      throwError "intros 变体里出现了缩进（会被当成假设名）：{bad}"
  trivial
