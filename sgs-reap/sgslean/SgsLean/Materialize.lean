/-
# 物化：把已验证引理落成**有名常量**（P3 收尾）

库的物理形态不该只是 Python 侧的一行 JSON——"被检索、被依赖、被复用"都要发生在 Lean 环境里。
`Materialize.emit` 把若干条**已验证**的引理写成一份自动生成的 Lean 文件：

```lean
theorem sgs_lem_0001 : <stmt> := by
  <缩进后的 proof>
```

随后用 `lake env lean <file>` 编译它——**能编译**才是"这批引理真的成立"的证据，
而不是"我写了个文件"。

三条硬约束：

1. 只接受 `verified=true` 的条目（未验证的引理不许进库，这是 N2/N3 的前提）；
2. 名字统一加 `sgs_lem_<n>` 前缀并去重（避免与 Mathlib 命名空间撞车）；
3. 文件头写明"自动生成，不要手改"，并把来源写进注释（可追溯）。

局限（v1）：不做增量更新（每次重写整份文件）；不检查新引理与已有库引理的**顺序依赖**
（若新引理依赖旧引理，需要按依赖序写入——目前靠调用方给对顺序，v2 应做拓扑排序）。
-/
module

public meta import SgsLean.Basic

open Lean Meta Elab Tactic

public meta section

namespace SgsLean

/-- 待物化的一条引理。 -/
structure LemmaEntry where
  stmt : String
  proof : String
  verified : Bool
  /-- 可追溯的来源（例如 `g1:g01` 或 `greedy:cover`）。 -/
  source : String := ""
deriving ToJson, FromJson, Repr, Inhabited

/-- 物化结果。 -/
structure MaterializeResult where
  path : String
  written : Nat
  skipped : Nat
  /-- 写进文件的名字列表（顺序即文件顺序）。 -/
  names : Array String := #[]
deriving ToJson, Repr, Inhabited

namespace Materialize

/-- 缩进一段证明脚本（每行加 2 空格）。 -/
def indentBlock (s : String) : String :=
  String.intercalate "\n" <| (normalizeNewlines s).splitOn "\n" |>.map fun line => "  " ++ line

/-- 生成文件内容。 -/
def render (imports : String) (entries : Array LemmaEntry) : String :=
  let headerLines : List String :=
    ["/- 自动生成：由 SgsLean.Materialize 落盘，请勿手改。 -/"]
    ++ (if imports.isEmpty then [] else ["import " ++ imports])
    ++ ["", "set_option autoImplicit true", ""]
  let body : List String := entries.toList.mapIdx fun n entry =>
    let tag := if entry.source.isEmpty then "" else s!"  -- 来源：{entry.source}"
    s!"theorem sgs_lem_{n + 1} : {normalizeNewlines entry.stmt} := by\n{indentBlock entry.proof}{tag}\n"
  String.intercalate "\n" (headerLines ++ body)

/-- 把已验证引理写进 `path`。返回写入/跳过条数与名字列表。 -/
def emit (path : System.FilePath) (entries : Array LemmaEntry) (imports : String)
    : IO MaterializeResult := do
  let good := entries.filter (fun e => e.verified)
  let skipped := entries.size - good.size
  -- 目标目录可能不存在（`writeFile` 不会自动建目录，第一次跑就踩到了）
  if let some dir := path.parent then IO.FS.createDirAll dir
  IO.FS.writeFile path (render imports good)
  return {
    path := path.toString
    written := good.size
    skipped := skipped
    names := good.mapIdx fun n _ => s!"sgs_lem_{n + 1}"
  }

end Materialize
end SgsLean
