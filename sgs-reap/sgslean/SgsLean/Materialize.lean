/-
# 物化：把已发布引理写成有名常量

库的物理形态不该只是 Python 侧的一行 JSON——"被检索、被依赖、被复用"都要发生在 Lean 环境里。
`Materialize.emit` 把若干条**已验证**的引理写成一份自动生成的 Lean 文件：

```lean
theorem sgs_lem_3f9a1c07 : <stmt> := by
  <缩进后的 proof>
```

随后用 `lake env lean <file>` 编译它——**能编译**才是"这批引理真的成立"的证据，
而不是"我写了个文件"。

三条硬约束：

1. 只接受 `verified=true` 的条目（未验证的引理不许进库，这是 N2/N3 的前提）；
2. 名字统一加 `sgs_lem_` 前缀（避免与 Mathlib 命名空间撞车）。**名字由调用方给出**
   （Python 侧按语句内容取 sha256 前 8 位），只有缺失时才退回按下标编号；
3. 文件头写明"自动生成，不要手改"，并把来源写进注释（可追溯）。

## 为什么名字不能按下标编

按下标命名 + 从库中间淘汰（`coverage.evict`）会让剩下的引理**整体改名**：
第 5 条被冷存后，原来的第 6 条就成了 `sgs_lem_5`。历史记录里的 `constants`、
上一轮提示词里给模型的名字、已经验证过的证明文本会一起张冠李戴。名字必须由
**内容**决定，并且与库行里的 `name` 字段逐字一致（`missingName` 会如实计数，
调用方应当把它当错误处理）。

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
  /-- 物化后的常量名。**由调用方给出**（内容哈希）；空串时退回 `sgs_lem_<下标+1>`。 -/
  name : String := ""
deriving ToJson, FromJson, Repr, Inhabited

/-- 物化结果。 -/
structure MaterializeResult where
  path : String
  written : Nat
  skipped : Nat
  /-- 写进文件的名字列表（顺序即文件顺序）。 -/
  names : Array String := #[]
  /-- 有多少条缺 `name`、被迫按位置编号（应当恒为 0；非 0 说明调用方与库的名字不一致）。 -/
  missingName : Nat := 0
deriving ToJson, Repr, Inhabited

namespace Materialize

/-- 缩进一段证明脚本（每行加 2 空格）。 -/
def indentBlock (s : String) : String :=
  String.intercalate "\n" <| (normalizeNewlines s).splitOn "\n" |>.map fun line => "  " ++ line

/-- 引理名：优先用调用方给的名字，缺了才退回按位置编号（并会被 `emit` 计数）。 -/
def entryName (index : Nat) (entry : LemmaEntry) : String :=
  if entry.name.isEmpty then s!"sgs_lem_{index + 1}" else entry.name

/-- 生成文件内容。

`imports` 是**模块列表**而不是一段拼接好的字符串：Lean 的 `import` 每条只能跟一个模块，
`import Mathlib SgsLean.GeneratedLibrary` 是语法错误（实测：物化文件编译失败，
报 `unexpected identifier; expected command`）。一个模块一行。 -/
def render (imports : Array String) (entries : Array LemmaEntry) : String :=
  let headerLines : List String :=
    ["/- 自动生成：由 SgsLean.Materialize 落盘，请勿手改。 -/"]
    ++ imports.toList.map (fun m => "import " ++ m)
    ++ ["", "set_option autoImplicit true", ""]
  let body : List String := entries.toList.mapIdx fun n entry =>
    let tag := if entry.source.isEmpty then "" else s!"-- 来源：{entry.source}\n"
    s!"{tag}theorem {entryName n entry} : {normalizeNewlines entry.stmt} := by\n{indentBlock entry.proof}\n"
  String.intercalate "\n" (headerLines ++ body)

/-- 把已验证引理写进 `path`。返回写入/跳过条数与名字列表。 -/
def emit (path : System.FilePath) (entries : Array LemmaEntry) (imports : Array String)
    : IO MaterializeResult := do
  let good := entries.filter (fun e => e.verified)
  let skipped := entries.size - good.size
  let missing := good.foldl (fun acc e => if e.name.isEmpty then acc + 1 else acc) 0
  -- 目标目录可能不存在（`writeFile` 不会自动建目录，第一次跑就踩到了）
  if let some dir := path.parent then IO.FS.createDirAll dir
  IO.FS.writeFile path (render imports good)
  return {
    path := path.toString
    written := good.size
    skipped := skipped
    names := good.mapIdx fun n entry => entryName n entry
    missingName := missing
  }

end Materialize
end SgsLean
