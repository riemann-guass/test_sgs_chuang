/-
# 软分第一件：压缩收益 Δlen

概念前身是 Colton 等人 2000 年的 compression-interestingness："好的引理让证明变短"。
本项目要把它做成**可计算的软分**：同一条语句，用两条证明比较——

* `longProof`：不借助目标引理时的证明（通常更长）；
* `shortProof`：借助目标引理后的证明。

度量两个 v1 指标（都从真实 elaboration 里取，不靠估算）：

1. **步数**：`Trace.traceScript` 记录到子目标清零为止的 tactic 步数（该值也顺带验证了
   两条证明是否真的通过——`longOk` / `shortOk`）；
2. **字符数**：证明脚本的字符数（token 数的廉价代理，中文环境里更稳，不依赖分词器）。

`Δlen = long − short`，正数表示"用了引理之后变短"。

**局限（v1，必须写清楚）**：
* 步数用"逐行 tactic"计数（`Trace` 的 v1 局限同样适用：bullet/多行结构会被切错）；
* 字符数不是 token 数，正式成本以模型后端报告的 token 为准；
* 这里的 Δlen 只是单题局部诊断，不作为引理晋升标准。
-/
module

public meta import SgsLean.Basic
public meta import SgsLean.Trace

open Lean Meta Elab Tactic

public meta section

namespace SgsLean

/-- 压缩收益结果。 -/
structure CompressionResult where
  stmt : String
  longSteps : Nat
  shortSteps : Nat
  /-- `long - short`（正数 = 用了引理后变短）。 -/
  deltaSteps : Int
  longChars : Nat
  shortChars : Nat
  deltaChars : Int
  /-- 长证明是否真的通过（来自 `Verify`）。 -/
  longOk : Bool
  /-- 短证明是否真的通过。 -/
  shortOk : Bool
deriving ToJson, Repr, Inhabited

namespace Measure

private def delta (a b : Nat) : Int := Int.ofNat a - Int.ofNat b

/-- 比较两条证明的"长度"，给出 Δlen。 -/
def compression (stmt longProof shortProof : String) : TacticM CompressionResult := do
  let long ← Trace.traceScript stmt longProof
  let short ← Trace.traceScript stmt shortProof
  let ls := long.steps.size
  let ss := short.steps.size
  return {
    stmt := normalizeStmt stmt
    longSteps := ls
    shortSteps := ss
    deltaSteps := delta ls ss
    longChars := (normalizeNewlines longProof).length
    shortChars := (normalizeNewlines shortProof).length
    deltaChars := delta (normalizeNewlines longProof).length (normalizeNewlines shortProof).length
    longOk := long.verified
    shortOk := short.verified
  }

end Measure
end SgsLean
