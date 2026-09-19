/-
# `SgsLean/Server.lean`：stdio JSON 服务（协议 v1）

Python 侧唯一入口，协议见 `docs/implementation-blueprint.md`。要点：

* **传输**：stdin / stdout 各一行一条 JSON（JSONL），UTF-8；诊断只走 stderr，
  **stdout 只放响应**，调用方可以放心逐行 `json.loads`。
* **命令**：`ping` / `check` / `verify` / `flush`。写若干条请求后发 `{"cmd":"flush"}` 取回本批响应，
  或在 EOF 时自动 flush；每条请求恰好一条响应，顺序与请求一致。
* **判定**：直接复用 P1.1 的 `Gate.check` / `Verify.verify`（`TacticM`），服务端与库内同源。

## 执行模型：每批 spawn 一个 `lean` 子进程

`Gate` / `Verify` 是 `TacticM` 动作，需要 elaboration 上下文，而 standalone 可执行文件里没有环境。
父进程（本文件编译出的 `sgslean-server`）因此：

1. 在工作目录写 `jobs.json`（本批请求）与 `sgslean_snippet.lean`（**内容固定**的片段）；
2. `spawn` 一个 `lean sgslean_snippet.lean` 子进程（`cwd` = 工作目录）；
3. 片段里的 `run_tac SgsLean.Server.runJobs` 读 `jobs.json`、逐条判定、把响应数组写 `out.json`；
4. 父进程读 `out.json`，逐条回响应。

工作目录里还有一份 `lean-toolchain`，保证 elan 在 `cwd` 下选出**与项目一致**的工具链
（否则会落到 elan 默认工具链上，版本不匹配）。

为什么不是进程内 `Lean.Elab.runFrontend`（P1.2 首版做法，已废弃）：

* 导入 Mathlib 时进程内 frontend 直接崩：`cannot evaluate [init] declaration
  'Mathlib.pp.mathlib.binderPredicates' in the same module`；换成 `lean` 驱动（Mathlib 开发的
  标准姿势）就正常了；
* frontend 的消息默认打到 **stdout**（`IO.print`），会污染协议流；子进程方案把孩子进程的
  stdout/stderr 一起挂到父进程的 stderr 上，协议流天然干净；
* 子进程崩溃不会带走服务进程（首版 Mathlib 导入崩溃时整个 server 都没了）。

代价：每批多一次进程启动（≈1–2 s）；Mathlib 模式下一次批处理的固定成本 = 导入 Mathlib
（本机实测 ≈100–420 s，冷热差异大）。所以**必须批量喂请求**，别一条一条走。
-/
import SgsLean

open Lean

namespace SgsLean.Server

/-- 协议版本。 -/
def protocolVersion : String := "v1"

/-- 工作目录里的三个文件（片段固定用相对名，父进程把 `cwd` 设为工作目录）。 -/
def snippetFileName : String := "sgslean_snippet.lean"
def jobsFileName : String := "jobs.json"
def outFileName : String := "out.json"
/-- 子进程的启动脚本（把 lean 的输出重定向到 child.log）。 -/
def childCmdFileName : String := "run_child.cmd"

/-- 子进程默认导入的库。`SGSLEAN_IMPORTS=none` 切回无 Mathlib 的快速模式。

注意不能用空串表示"不导入"：Windows 上把环境变量设成空串等于**删除**该变量
（`SetEnvironmentVariable(name, "")` 的语义），父进程会退回默认值。实测踩过：
Python 侧 `os.environ["SGSLEAN_IMPORTS"] = ""` 后，服务端仍按默认的 `Mathlib` 跑。 -/
def defaultImports : String := "Mathlib"

/-- 解析 import 列表；`none`/`-` 表示"不额外导入任何库"。 -/
def parseImports (raw : String) : Array String :=
  let trimmed := raw.trimAscii.toString
  if trimmed == "none" || trimmed == "-" then #[]
  else (trimmed.splitOn "," |>.map (fun s => s.trimAscii.toString) |>.filter (fun s => !s.isEmpty)).toArray

private def okResponse (id : Json) (result : Json) : Json :=
  Json.mkObj [("id", id), ("ok", Json.bool true), ("result", result)]

private def errResponse (id : Json) (code message : String) : Json :=
  Json.mkObj [
    ("id", id),
    ("ok", Json.bool false),
    ("error", Json.mkObj [("code", toJson code), ("message", toJson message)])]

/-! ## 子进程侧：判定层（meta，直接复用 Gate / Verify） -/

meta section

open Lean Meta Elab Tactic

/-- `ping` 的答案：**子进程**里真实加载了什么（调用方据此判断能否做 Mathlib 级判定）。
注意 ping 也是一条作业，因此它要跑一次 frontend（Mathlib 模式下就是一次完整导入）。 -/
def environmentInfo : TacticM Json := do
  let env ← getEnv
  return Json.mkObj [
    ("status", toJson "ok"),
    ("version", toJson protocolVersion),
    ("importedModules", toJson env.header.moduleNames.size),
    ("mathlib", toJson (env.header.moduleNames.any (fun n => n == `Mathlib)))]

/-- 处理一条请求 → 一条响应。任何内部失败都变成 `internal_error`，不向外抛。 -/
def handleJob (job : Json) : TacticM Json := do
  let id := job.getObjValD "id"
  let cmd := (job.getObjValAs? String "cmd").toOption.getD ""
  try
    match cmd with
    | "ping" => return okResponse id (← environmentInfo)
    | "check" =>
      match (job.getObjValAs? String "stmt").toOption with
      | none => return errResponse id "invalid_params" "check 需要字符串字段 stmt"
      | some stmt => return okResponse id (toJson (← Gate.check stmt))
    | "verify" =>
      match (job.getObjValAs? String "stmt").toOption, (job.getObjValAs? String "proof").toOption with
      | some stmt, some proof => return okResponse id (toJson (← Verify.verify stmt proof))
      | _, _ => return errResponse id "invalid_params" "verify 需要字符串字段 stmt 与 proof"
    | "trace" =>
      match (job.getObjValAs? String "stmt").toOption, (job.getObjValAs? String "proof").toOption with
      | some stmt, some proof => return okResponse id (toJson (← Trace.traceScript stmt proof))
      | _, _ => return errResponse id "invalid_params" "trace 需要字符串字段 stmt 与 proof"
    | "trivial" =>
      match (job.getObjValAs? String "stmt").toOption with
      | some stmt => return okResponse id (toJson (← Trivial.isTrivial stmt))
      | none => return errResponse id "invalid_params" "trivial 需要字符串字段 stmt"
    | "novelty" =>
      match (job.getObjValAs? String "stmt").toOption with
      | none => return errResponse id "invalid_params" "novelty 需要字符串字段 stmt"
      | some stmt =>
        let against := (job.getObjValAs? (Array String) "against").toOption.getD #[]
        return okResponse id (toJson (← Novelty.isNew stmt against))
    | "compression" =>
      match (job.getObjValAs? String "stmt").toOption,
            (job.getObjValAs? String "longProof").toOption,
            (job.getObjValAs? String "shortProof").toOption with
      | some stmt, some longProof, some shortProof =>
        return okResponse id (toJson (← Measure.compression stmt longProof shortProof))
      | _, _, _ => return errResponse id "invalid_params" "compression 需要 stmt/longProof/shortProof"
    | "dependencies" =>
      match (job.getObjValAs? String "stmt").toOption, (job.getObjValAs? String "proof").toOption with
      | some stmt, some proof =>
        let target := (job.getObjValAs? String "target").toOption.getD ""
        return okResponse id (toJson (← Measure.dependencies stmt proof target))
      | _, _ => return errResponse id "invalid_params" "dependencies 需要字符串字段 stmt 与 proof"
    | _ => return errResponse id "bad_request" s!"unknown cmd: {repr cmd}"
  catch ex =>
    return errResponse id "internal_error" (← ex.toMessageData.toString)

/-- **子进程入口**：读 `jobs.json`（JSON 数组），逐条判定，把响应数组写 `out.json`。 -/
def runJobs : TacticM Unit := do
  let text ← IO.FS.readFile jobsFileName
  let jobs := (Json.parse text).toOption.bind (fun j => j.getArr?.toOption) |>.getD #[]
  let mut out : Array Json := #[]
  for job in jobs do
    out := out.push (← handleJob job)
  IO.FS.writeFile outFileName (Json.arr out).compress

end

/-! ## 父进程侧：片段生成、子进程调度、主循环 -/

/-- 片段源码：**内容固定**（import 头由环境配置决定，请求数据永不参与拼字符串）。 -/
def snippetSource (imports : String) : String :=
  let mods := parseImports imports
  -- 没有 Mathlib 时要自己补 `ℕ` 记法；有 Mathlib 时**不能**补（重复声明 termℕ 是硬错误）
  let notationPatch := if mods.contains "Mathlib" then #[] else #["import SgsLean.Syntax"]
  -- `linter.unusedTactic` 是 Mathlib 提供的 linter；核心环境里没这个选项，设了会直接报错
  let linterPatch := if mods.contains "Mathlib" then #["  set_option linter.unusedTactic false"] else #[]
  let header := (mods.map (fun m => s!"import {m}")) ++ notationPatch ++
    #["import SgsLean", "import SgsLean.Server"]
  String.intercalate "\n" ((header ++ #[
    "open Lean Meta Elab Tactic",
    "set_option autoImplicit true",
    "set_option Elab.async false",
    "set_option maxHeartbeats 4000000",
    "example : True := by",
    "  run_tac SgsLean.Server.runJobs"] ++ linterPatch ++ #[
    "  trivial",
    ""]).toList)

/-- 项目工具链字符串（写进工作目录的 `lean-toolchain`，让 elan 选对版本）。 -/
def projectToolchain : IO String := do
  try
    return (← IO.FS.readFile "lean-toolchain").trimAscii.toString
  catch _ =>
    return "leanprover/lean4:v4.28.0-rc1"

/-- 准备工作目录：片段 + 工具链 + 记录用信息。 -/
def prepareWorkDir : IO System.FilePath := do
  let dir : System.FilePath :=
    match (← IO.getEnv "SGSLEAN_WORKDIR") with
    | some p => p
    | none => ".lake" / "sgslean-server-work"
  IO.FS.createDirAll dir
  IO.FS.writeFile (dir / "lean-toolchain") ((← projectToolchain) ++ "\n")
  let imports := (← IO.getEnv "SGSLEAN_IMPORTS").getD defaultImports
  IO.FS.writeFile (dir / snippetFileName) (snippetSource imports)
  -- 用脚本文件承载重定向：`cmd /c` 的参数里带空格/重定向符时，Lean 的 spawn 会加引号，
  -- 实测传过去就不是 cmd 想要的语法（exit=1、日志也没生成）。写进 .cmd 最稳。
  IO.FS.writeFile (dir / childCmdFileName)
    s!"@echo off\r\nlean {snippetFileName} > child.log 2>&1\r\n"
  return dir

/-- 跑一批作业：写 `jobs.json` → spawn `lean` → 读 `out.json`。
返回（与 `jobs` 等长的响应数组，子进程耗时毫秒）。 -/
def runBatch (jobs : Array Json) (workDir : System.FilePath) : IO (Array Json × Nat) := do
  if jobs.isEmpty then return (#[], 0)
  IO.FS.writeFile (workDir / jobsFileName) (Json.arr jobs).compress
  let outPath := workDir / outFileName
  try IO.FS.removeFile outPath catch _ => pure ()
  let start ← IO.monoNanosNow
  -- 子进程输出用 **shell 重定向**落到 child.log：
  -- `IO.withStdout` 只改 Lean 层的流，改不了子进程继承的 OS 句柄（实测子进程的错误消息
  -- 会直接漏进 stdout，破坏协议）；`.piped` 又会被 Lean 按 UTF-8 解码（中文环境的
  -- bsdtar/curl 输出会触发 `non UTF-8 data` panic），所以交给 cmd.exe 重定向最稳。
  let exitCode ← do
    let child ← IO.Process.spawn {
      cmd := "cmd.exe"
      args := #["/c", childCmdFileName]
      cwd := some workDir
      stdin := .null
      stdout := .null
      stderr := .null
    }
    child.wait
  let stop ← IO.monoNanosNow
  let elapsedMs := (stop - start) / 1000000
  let failure := fun () =>
    jobs.map fun job =>
      errResponse (job.getObjValD "id") "internal_error"
        s!"lean 子进程未产出结果（exit={exitCode}）；\
           工作目录 {workDir.toString} 里有 {snippetFileName}/{jobsFileName} 可手工复现，\
           诊断见同目录 child.log"
  match (← (try some <$> IO.FS.readFile outPath catch _ => pure none)) with
  | none => return (failure (), elapsedMs)
  | some text =>
    match Json.parse text with
    | .ok json => return ((json.getArr?.toOption.getD #[]), elapsedMs)
    | .error _ => return (failure (), elapsedMs)

/-- 打印一批响应：`batch` 里每一条作业恰好一条响应，顺序一致。返回（条数，子进程毫秒）。 -/
private def flushBatch (workDir : System.FilePath) (batch : Array Json) : IO (Nat × Nat) := do
  let stdout ← IO.getStdout
  let (responses, elapsedMs) ← runBatch batch workDir
  let mut out : Array String := #[]
  let mut idx := 0
  for job in batch do
    match responses[idx]? with
    | some r => out := out.push r.compress
    | none => out := out.push (errResponse (job.getObjValD "id") "internal_error" "响应缺失").compress
    idx := idx + 1
  for line in out do
    stdout.putStrLn line
  stdout.flush
  return (out.size, elapsedMs)

/-- 主循环：读 JSONL 请求，按批 flush，逐条回响应。 -/
def mainLoop : IO Unit := do
  let stdin ← IO.getStdin
  let stdout ← IO.getStdout
  let workDir ← prepareWorkDir
  let mut batch : Array Json := #[]
  let mut running := true
  while running do
    let line ← stdin.getLine
    if line.isEmpty then
      running := false
      let _ ← flushBatch workDir batch
    else
      let trimmed := line.trimAscii.toString
      if trimmed.isEmpty then
        pure ()
      else
        -- 只有"JSON 对象"才算请求；其它情况一律立刻回 bad_request（这类响应没有可回填的 id）
        match Json.parse trimmed with
        | .error msg =>
          stdout.putStrLn (errResponse Json.null "bad_request" s!"invalid JSON line: {msg}").compress
          stdout.flush
        | .ok json =>
          match json with
          | .obj _ =>
            if ((json.getObjValAs? String "cmd").toOption.getD "") == "flush" then
              let (n, elapsedMs) ← flushBatch workDir batch
              batch := #[]
              stdout.putStrLn (okResponse (json.getObjValD "id")
                (Json.mkObj [("flushed", toJson n), ("frontend_ms", toJson elapsedMs)])).compress
              stdout.flush
            else
              batch := batch.push json
          | _ =>
            stdout.putStrLn (errResponse Json.null "bad_request" "请求必须是 JSON 对象").compress
            stdout.flush

end SgsLean.Server

/-- 可执行入口（`lake exe sgslean-server`）。 -/
def main : IO Unit := SgsLean.Server.mainLoop
