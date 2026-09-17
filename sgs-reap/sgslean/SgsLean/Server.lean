/-
# `SgsLean/Server.lean`：stdio JSON 服务（协议 v1）

Python 侧唯一入口，协议见 `docs/implementation-blueprint.md`。要点：

* **传输**：stdin / stdout 各一行一条 JSON（JSONL），UTF-8；诊断只走 stderr，
  **stdout 只放响应**，调用方可以放心逐行 `json.loads`。
* **命令**：`ping` / `check` / `verify` / `flush`。写若干条请求后发 `{"cmd":"flush"}` 取回本批响应，
  或在 EOF 时自动 flush；每条请求恰好一条响应，顺序与请求一致。
* **判定**：直接复用 P1.1 的 `Gate.check` / `Verify.verify`（`TacticM`），服务端与库内同源。

## 为什么是"批处理 + 一次 frontend"

`Gate` / `Verify` 是 `TacticM` 动作，需要 elaboration 上下文，而 standalone 可执行文件里没有环境。
因此本文件生成一段**内容固定**的片段（只有 `import` 和一个 `example`），用
`Lean.Elab.runFrontend` 跑一次；片段里的 `run_tac` 调 `SgsLean.Server.runJobs`，
从进程内的 `IO.Ref` 取作业、把响应写回 `IO.Ref`。

1. 片段文本固定，**请求内容不参与拼字符串**，不存在注入/转义问题；
2. 一次 frontend 处理整批请求，`import` 开销被摊薄（P1.3 要跑 50–100 条引理 × k 次采样，
   逐条重启 frontend 在带 Mathlib 时不可接受）；
3. 单条请求的内部错误被 `handleJob` 捕获成协议响应，不会让整批失败。

文件刻意**不是** `module` 文件：`main` 必须是普通（非 meta）定义才能作为可执行入口，
所以只有判定层放在 `meta section` 里。
-/
import SgsLean

open Lean

namespace SgsLean.Server

/-- 协议版本。 -/
def protocolVersion : String := "v1"

/-- 作业槽：主循环写入待处理请求，片段里的 `runJobs` 读走并把响应写回 `jobResults`。 -/
initialize pendingJobs : IO.Ref (Array Json) ← IO.mkRef #[]

/-- 结果槽。 -/
initialize jobResults : IO.Ref (Array Json) ← IO.mkRef #[]

/-- `runJobs` 是否真的跑过；为 `false` 时整批回 `internal_error`。 -/
initialize jobsHandled : IO.Ref Bool ← IO.mkRef false

private def okResponse (id : Json) (result : Json) : Json :=
  Json.mkObj [("id", id), ("ok", Json.bool true), ("result", result)]

private def errResponse (id : Json) (code message : String) : Json :=
  Json.mkObj [
    ("id", id),
    ("ok", Json.bool false),
    ("error", Json.mkObj [("code", toJson code), ("message", toJson message)])]

/-! ## 判定层（meta，直接复用 Gate / Verify） -/

meta section

open Lean Meta Elab Tactic

/-- `ping` 的环境信息：调用方据此判断这个进程能否做 Mathlib 级判定。 -/
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
    | _ => return errResponse id "bad_request" s!"unknown cmd: {repr cmd}"
  catch ex =>
    return errResponse id "internal_error" (← ex.toMessageData.toString)

/-- 由生成的片段调用：处理 `pendingJobs` 全部作业，响应写进 `jobResults`。 -/
def runJobs : TacticM Unit := do
  let jobs ← pendingJobs.get
  let mut out : Array Json := #[]
  for job in jobs do
    out := out.push (← handleJob job)
  jobResults.set out
  jobsHandled.set true

end

/-! ## frontend 与主循环（普通 IO） -/

/-- 交给 frontend 的片段：**内容固定**，不含任何请求数据。 -/
def snippet : String :=
  String.intercalate "\n" [
    "import SgsLean",
    "import SgsLean.Server",
    "open Lean Meta Elab Tactic",
    "set_option autoImplicit true",
    "set_option Elab.async false",
    "set_option maxHeartbeats 4000000",
    "example : True := by",
    "  run_tac SgsLean.Server.runJobs",
    "  trivial",
    ""]

/-- 跑一次 frontend；诊断由 frontend 自己写到 stderr。 -/
def runFrontendOnce : IO Bool := do
  -- Lean 的 frontend 用 `IO.print` 报告消息，即"当前 stdout"。
  -- 协议流必须是纯 JSON，所以跑 frontend 期间把 stdout 临时换成 stderr。
  let stderr ← IO.getStderr
  IO.withStdout stderr do
    let env? ← Lean.Elab.runFrontend snippet {} "sgslean_server_snippet.lean" `SgsLeanServer
    return env?.isSome

/-- 初始化 Lean 的搜索路径。

standalone 可执行文件启动时搜索路径是**空的**（`lean_init_search_path` 只对 `lean` 主程序调），
不初始化就会报 `unknown module prefix 'Init'`。`initSearchPath` 会把 `LEAN_PATH`
（`lake exe` 注入，含本包 + reap + batteries + 工具链 lib/lean）一起并进来。 -/
def initSearchPathOnce : IO Unit := do
  try
    Lean.initSearchPath (← Lean.findSysroot)
  catch e =>
    let _ := e
    IO.eprintln "sgslean-server: 无法定位 Lean sysroot（设 LEAN_SYSROOT 可绕过），仅依赖 LEAN_PATH"
    try
      Lean.initSearchPath ""
    catch _ =>
      pure ()

/-- 处理一批作业，返回（与 `jobs` 等长的响应数组，frontend 耗时毫秒）。
耗时单独回传，P1.3 的成本核算要用真实数字，不要估算。 -/
def runBatch (jobs : Array Json) : IO (Array Json × Nat) := do
  if jobs.isEmpty then return (#[], 0)
  pendingJobs.set jobs
  jobResults.set #[]
  jobsHandled.set false
  let start ← IO.monoNanosNow
  let _ ← runFrontendOnce
  let stop ← IO.monoNanosNow
  let elapsedMs := (stop - start) / 1000000
  let handled ← jobsHandled.get
  if !handled then
    return (jobs.map fun job =>
      errResponse (job.getObjValD "id") "internal_error"
        "frontend 未能执行（细节见 stderr）：通常是 SgsLean olean 缺失或片段 elaboration 出错", elapsedMs)
  return (← jobResults.get, elapsedMs)

/-- 处理并打印一批响应：`batch` 里每一条作业恰好一条响应，顺序一致。
返回（响应条数，frontend 毫秒）。 -/
private def flushBatch (batch : Array Json) : IO (Nat × Nat) := do
  let stdout ← IO.getStdout
  let (responses, elapsedMs) ← runBatch batch
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
  initSearchPathOnce
  let mut batch : Array Json := #[]
  let mut running := true
  while running do
    let line ← stdin.getLine
    if line.isEmpty then
      running := false
      let _ ← flushBatch batch
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
              let (n, elapsedMs) ← flushBatch batch
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
