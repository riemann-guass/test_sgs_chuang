/-
LeanReuse 验证层的公共定义。

本文件只放三样东西：

1. `ℕ` 记法补丁。本项目环境不含 Mathlib，`ℕ` 不在记法表里；缺这一行时 Lean 的
   `autoImplicit` 会把 `ℕ` 当成**隐式绑定变量**，把「记法缺失」伪装成
   `failed to synthesize HMul ℕ ℕ ?m` 之类的实例错误（阶段 2 已完整踩过）。
2. `EvalError` → 机器可读码的分类器。
3. 门检/验证共用的文本工具与资源预算。
-/
module

public meta import Batteries.Lean.Meta.Basic
public meta import Reap.Tactic.Conjecture
public meta import Reap.Tactic.Step
public meta import Reap.Tactic.TreeSearch

open Lean Meta Elab Tactic
open Reap.TreeSearch

-- 注意：`ℕ` 记法补丁**不在这里**，而在 `SgsLean/Syntax.lean`。
-- 原因：Mathlib 自带 `termℕ`，重复声明是硬错误（`environment already contains 'termℕ'`），
-- 所以它必须与 Mathlib 的 import 互斥——详见 `SgsLean/Syntax.lean` 的文件头。

public meta section

namespace SgsLean

/-- 门检与验证共用的**默认**心跳预算（单位与 Lean 的 `maxHeartbeats` 选项一致：千次心跳）。

早期版本使用 200000（= 2 亿次心跳），只够应付 Nat/Prop 级的初等判定。引入 Mathlib 后
必须放宽，原因很具体：我们的判定跑在 `lean` 驱动里，**Mathlib 的 tactic 代码是解释执行的**
（不像 `lake build` 那样有原生代码），`ring` / `omega` 这类 tactic 的实际开销比编译版高
一个量级。实测：ℝ 上的 `(x+y)^2 = x^2+2xy+y^2` 配 `ring`，在 2 亿次心跳下报
`timeout at isDefEq`，放宽后才判为通过。

真正的墙钟上限是 `reap.timeout`（默认 200 s/次 tactic），评测时两者都要如实记录。

**运行时可覆盖**：环境变量 `SGSLEAN_HEARTBEATS`（见 `getHeartbeats`）。
早期 miniF2F 运行中有 74% 的失败是 `exception`（含心跳耗尽），到底是"模型证不出"还是
"预算掐死"必须能不改代码地分离——这是阶段 A 诊断的前提。 -/
def defaultHeartbeats : Nat := 4000000

/-- 运行时心跳预算。

每批的 `lean` 子进程启动时读**一次**环境变量 `SGSLEAN_HEARTBEATS`，之后整批复用；
读不到或解析失败则退回 `defaultHeartbeats`。

不要把它做成"每条作业各读一次环境变量"：判定的成本极高，读环境变量的开销虽小，
但把预算变成逐条可变会让"同一批里判定标准不一致"，日志也就没法解释。 -/
initialize heartbeatsRef : IO.Ref Nat ← do
  let value ← match (← IO.getEnv "SGSLEAN_HEARTBEATS") with
    | some raw => pure ((raw.trimAscii.toString.toNat?).getD defaultHeartbeats)
    | none => pure defaultHeartbeats
  IO.mkRef value

/-- 读取当前批的心跳预算。写成 `{m}` 泛化形式，便于在 `TacticM` / `MetaM` / `CoreM` 里直接用
（与 `Reap.WallClock` 的 `MonadLiftT IO` 写法一致）。 -/
def getHeartbeats {m : Type → Type} [Monad m] [MonadLiftT IO m] : m Nat :=
  liftM (m := IO) heartbeatsRef.get

/-- 探针假设名。用独立前缀，避免与 reap 的 `sgs_aux_` 以及被验证语句里的名字冲突。 -/
def probeHypName : String := "sgs_probe_"

/-- 行尾归一化：模型输出与 JSON 往返都可能带 `\r`。

这不是可有可无的美化：reap 的 `MCTS.indentScript` 按 `"\n"` 切行，
残留的 `\r` 会留在行尾，把「证明未闭合」伪装成「解析失败」。 -/
def normalizeNewlines (s : String) : String :=
  (s.replace "\r\n" "\n").replace "\r" "\n"

/-- 归一化语句文本（折叠空白），复用 reap 的 `normalizeProp`，
保证与搜索侧「候选同型过滤/去重」的口径一致。 -/
def normalizeStmt (s : String) : String :=
  normalizeProp (normalizeNewlines s)

/-- `EvalError` → 机器可读码。

与 `reap-fork/tests/Calibrate.lean` 的 `classifyError` 同构：后者位于测试驱动文件
（不在 lake 的 glob 范围内，无法被库代码导入），因此这里复刻一份，两处的码表改动必须同步。

相对早期版本新增一条 `"unclosed_goals"`：LeanReuse 的验证判定里「证明没闭合」是高频且必须
与「类型错误」区分的一类（`solve_rate` 统计直接依赖它）。 -/
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
    if text.contains "Unknown identifier" || text.contains "unknown identifier"
        || text.contains "Unknown constant" || text.contains "unknown constant"
        || text.contains "unknown namespace" then
      "unknown_identifier"
    else if text.contains "unsolved goals" then
      "unclosed_goals"
    else
      "type_error"

/-- 把语句文本 elaborate 成类型表达式；失败时返回错误文本。

只做「类型」这一半：`<stmt>` 能不能在当前上下文里成为一个合法的类型。
「它是不是命题」与「它能不能被证明」分别由 `Gate` 与 `Verify` 负责。 -/
def elabStmtType (stmt : String) : TacticM (Except String Expr) := do
  match Parser.runParserCategory (← getEnv) `term stmt with
  | .error e => return .error s!"parse error: {e}"
  | .ok stx =>
    try
      return .ok (← Term.elabType stx)
    catch ex =>
      return .error (← ex.toMessageData.toString)

end SgsLean
