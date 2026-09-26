# 阶段 4 执行日志（P1.2 离线切片：自写 `SgsLean/Server.lean`）

本单元范围：按冻结决定 2，自写 stdio JSON 服务 `sgslean/SgsLean/Server.lean`（协议 v1），
并配离线冒烟测试 `tests/run_server_smoke.py`。**不引入 Mathlib、不联网、不调用任何外部模型**。
Mathlib 工程与 `/solve` 端点仍属 P1.2 未做部分（需联网 + 长编译，动手前先问）。

## 交付物

| 文件 | 作用 |
|---|---|
| `sgslean/SgsLean/Server.lean` | 协议 v1 服务：`ping` / `check` / `verify` / `flush`；判定直接复用 P1.1 的 `Gate.check` / `Verify.verify` |
| `sgslean/lakefile.toml`（改） | 新增 `[[lean_exe]] name = "sgslean-server"`，`supportInterpreter = true` |
| `tests/run_server_smoke.py` | 离线冒烟：协议纯度 + 13 条判定语义 + 两种 flush 路径 |
| `experiments/results/server_smoke.json` | 冒烟报告（含真实 frontend 耗时） |
| `docs/implementation-blueprint.md`（改） | 协议章节按实现结果同步（`flush`、`frontend_ms`、非法行的处理） |

## 验收命令与真实输出

```powershell
cd sgs-reap\sgslean
lake build sgslean-server          # Build completed successfully (402 jobs).

cd sgs-reap
C:\Users\gaosen\anaconda3\python.exe tests\run_server_smoke.py
```

```
[server-smoke] 批大小 16，stdout 17 行，48.0s
[server-smoke] frontend_ms=18862（含 13 条作业的整批 elaboration）
[server-smoke] PASS（报告写入 ...\experiments\results\server_smoke.json）
```

手工往返抽样（`lake exe sgslean-server`，请求 → 响应）：

```
{"id":"1","cmd":"ping"}                       → {"result":{"importedModules":2072,"mathlib":false,"status":"ok","version":"v1"},"ok":true}
{"cmd":"check","stmt":"∀ (n : Nat), n + 0 = n"} → {"result":{"elaboratedType":"∀ (n : ℕ), n + 0 = n","isProp":true,"ok":true,"reason":"ok"}}
{"cmd":"check","stmt":"Nat"}                  → {"result":{"isProp":false,"ok":false,"reason":"not_a_prop"}}
{"cmd":"verify",...,"proof":"intro n\nrfl"}   → {"result":{"finalChecked":true,"gateOk":true,"goalsLeft":0,"ok":true,"reason":"ok"}}
{"cmd":"verify",...,"proof":"sorry"}          → {"result":{"ok":false,"reason":"mvar_or_sorry"}}
{"cmd":"verify",...,"proof":"intro n"}        → {"result":{"ok":false,"reason":"unclosed_goals"}}
{"cmd":"solve"}                               → {"ok":false,"error":{"code":"bad_request"}}
{"cmd":"verify","stmt":...}（缺 proof）        → {"ok":false,"error":{"code":"invalid_params"}}
```

## 设计决定

**批处理 + 一次 frontend。** `Gate` / `Verify` 是 `TacticM` 动作，需要 elaboration 上下文，
而 standalone 可执行文件里没有环境。做法是：主循环把请求攒进进程内的 `IO.Ref`，用
`Lean.Elab.runFrontend` 跑一段**内容固定**的片段（只有 `import` + 一个 `example`），片段里的
`run_tac` 调 `SgsLean.Server.runJobs` 处理整批、把响应写回另一个 `IO.Ref`。

片段文本固定 ⇒ 请求内容不参与拼字符串（不存在转义/注入问题），一次 frontend 摊薄 import 开销，
单条请求的内部错误被 `handleJob` 捕获成协议响应、不会让整批失败。

**文件刻意不是 `module`。** `main` 必须是普通（非 meta）定义才能当可执行入口，所以只有判定层
（`environmentInfo` / `handleJob` / `runJobs`）放在 `meta section` 里，frontend 与主循环是普通 IO。

## 现象 / 根因 / 修法

### 现象 1：`unknown module prefix 'Init'`

**现象** 首次跑服务，每个请求都回 `internal_error`，stderr 是
`sgslean_server_snippet.lean:1:0: error: unknown module prefix 'Init'`，并列出一串**空的**搜索路径。

**根因** standalone 可执行文件启动时 Lean 的搜索路径是空的：`lean_init_search_path`
（`initSearchPathInternal`）只对 `lean` 主程序调用，lake 生成的 exe 不调。

**修法** `mainLoop` 开头调 `Lean.initSearchPath (← Lean.findSysroot)`；`findSysroot` 优先读
`LEAN_SYSROOT`（`lake exe` 已注入工具链根），`initSearchPath` 会把 `LEAN_PATH`
（含本包 + reap + batteries + 工具链 `lib/lean`）一起并进来。失败时退化为只用 `LEAN_PATH` 并在
stderr 留一行提示。

### 现象 2（更重要）：frontend 把诊断打到 stdout，污染协议流

**现象** 修好搜索路径前抓包看到：stdout 的第一行是
`sgslean_server_snippet.lean:1:0: error: unknown module prefix 'Init'`，之后才是响应 JSON。
Python 侧只要 `json.loads` 每一行，立刻就会炸。

**根因** Lean 的 frontend 用 `SnapshotTree.runAndReport` 报告消息，而它内部是 `IO.print s`
（源码注释写明"This function is used by the cmdline driver"），即**当前 stdout**。

**修法** 跑 frontend 期间把当前线程的 stdout 临时换成 stderr：

```lean
let stderr ← IO.getStderr
IO.withStdout stderr do
  let env? ← Lean.Elab.runFrontend snippet {} "sgslean_server_snippet.lean" `SgsLeanServer
  return env?.isSome
```

协议响应在 `withStdout` **之外**打印。冒烟测试把"stdout 每一行都能 `json.loads`，且行数恰好等于
响应数"作为硬断言，防止这条回归。

### 现象 3：`Could not find native implementation of external declaration 'UInt64.ofNatLT'`

**现象** 搜索路径修好后，frontend 仍失败，stderr 提示需要 `supportInterpreter := true`。

**根因** 进程内嵌 frontend 需要 Lean 的解释器，解释器要求 `Init`/`Std`/`Lean` 的原生实现一起链进 exe。

**修法** `lakefile.toml` 的 `[[lean_exe]]` 加 `supportInterpreter = true`。

### 现象 4：非法请求行的响应没有 id

**现象** 首版把"不是 JSON 对象"的行也塞进批里，用占位下标对齐，结果 `flush` 回报的条数
（14）与请求条数（15）不符，且两条 `id=null` 的响应混在批响应中间。

**根因** JSON 解析失败的行取不到 `id`，无法回填；把它塞进批只会让"下标对齐"变复杂。

**修法** 协议 v1 正式约定：**只有 JSON 对象才算请求**。非对象、非 JSON 的行一律**立刻**回一条
`{"id": null, "ok": false, "error": {"code": "bad_request", ...}}`，不进批。这样批内响应与请求
1:1 对应，客户端按 `id` 配对，`id=null` 的条数单独计数即为非法行数。

### 实测成本（P1.3 的输入）

| 指标 | 实测 |
|---|---|
| 一次 frontend（导入 2072 个模块 + 处理 13 条判定） | **18.0–18.9 s** |
| 整批 16 条请求端到端（`lake exe` 包装 + 一次 frontend） | 46–49 s |
| `ping` 报告的环境 | `importedModules=2072`，`mathlib=false` |

**结论**：开销几乎全在"一次 frontend"这个固定成本上，**必须批量喂请求**——P1.3 要跑
50–100 条引理 × k 次采样，逐条启动 frontend 不可行；批内边际成本要等 Mathlib 到位后再测
（届时 `importedModules` 会从 2072 涨到上万，固定成本会显著变大，这是 `/solve` 链路设计的硬约束）。

## 反向对照

把 `handleJob` 的 `verify` 分支改成"调用 `Verify.verify` 但恒回 `ok=true`"（模拟跳过验证）：

```
[server-smoke] FAIL，3 处：
  - verify_sorry: 判定 ok 应为 False，实际 True（reason=ok）
  - verify_unclosed: 判定 ok 应为 False，实际 True（reason=ok）
  - verify_bad: 判定 ok 应为 False，实际 True（reason=ok）
```

恰好命中三条验证负例 ⟹ 冒烟测试真的在走 Lean 侧的 `Verify`，不是空转；随后已还原，
重跑 `PASS`、`exit=0`。

## 冒烟覆盖（16 条请求 + 1 次 flush）

`ping`（环境信息）；`check` 4 条：`∀ (n : Nat), n + 0 = n`（ok）、`Nat`（`not_a_prop`）、
`fun h : True ∧ True => h.right`（`type_error`，证明项当命题的闭式写法）、
`∀ (n : ℕ), n ≤ n * n + 1`（ℕ 记法回归）、`NotARealType`（`unknown_identifier`）；
`verify` 5 条：正常证明（`finalChecked=true`）、CRLF 证明、`sorry`（`mvar_or_sorry`）、
`intro n`（`unclosed_goals`）、`exact 1`（`type_error`）；协议 3 条：缺字段（`invalid_params`）、
未知命令（`bad_request`）、非法行 2 条（`id=null` + `bad_request`）；
两条 flush 路径：显式 `{"cmd":"flush"}` 与 EOF 自动 flush。

## 现在在哪 / 下一步

P1.2 的**离线一半完成**：Python 侧现在可以把 Lean 当服务用，且判定与 P1.1 完全同源。
剩余 P1.2 内容（Mathlib 工程 + 锁版本、`/solve` 端点与提示词、`Trace.lean` 轨迹落盘、
`data/workload.jsonl`/`heldout.jsonl`）需要**联网下载 Mathlib 与长时间编译**，按约定先问。
