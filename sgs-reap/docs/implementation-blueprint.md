# SG-Lean 实施蓝图（P1–P6）

> 方案本体见 `docs/proposal.md`。本文件只管"哪个阶段写哪些文件、过哪个闸门"。
> 工作协议：一次只推进一个可验收单元，六步（状态检查 → 写码 → 跑 → 反向对照 → 记日志 → 提交推送）。

## 已冻结的三项决定（2026-09-17）

| # | 决定 | 影响 |
|---|---|---|
| 1 | **`/guide` 不删** | `service/proxy.py` 的 `/guide`、GuideClient、rubric、`probe_guide*.py` 全部保留，作为 H2 的对照组；只**新增** `/solve` |
| 2 | **自写 `SgsLean/Server.lean`**（stdio JSON），不用 Pantograph / Lean REPL | 交互层与我们的数据结构完全对齐；代价是元编程工作量落在我们这边 |
| 3 | **统一 `TacticM`** | `Gate` / `Verify` / 后续 `Trivial` / `Novelty` / `Measure` / `Driver` / `Server` 全部沿用 `TacticM`，不混 `MetaM` |

> 历史提醒：蓝图早期版本曾写"P2 删除 `/guide`"，该写法作废，以决定 1 为准。
> 蓝图早期把 `Gate.check` 写成 `MetaM`，实际 P1.1 已按 `TacticM` 实现，以决定 3 为准。

## 阶段与闸门

```
P0 基础（已完成）→ P1 环境与交互层 → P2 轨迹与需求 → P3 价值与引理库 → P4 闭环与对照 → P5 评测写作 → P6 结题
                       │G1                │G2                │G3                  │G4
                 Solver 能不能证      需求信号成不成立     cover 能不能算       H1/H2
```

P0/P1.1 已完成；每个阶段独立冻结自己的接口，任一闸门不过时前面产物仍然成立。

## P1 环境与交互层

### P1.1（已完成，commit `b264d4b`）

| 文件 | 作用 | 状态 |
|---|---|---|
| `sgslean/lakefile.toml` / `lean-toolchain` / `lake-manifest.json` | 新 lake 包，`require reap`（path，reap 必须排在 Mathlib 之前） | ✅ |
| `sgslean/SgsLean/Basic.lean` | `ℕ` 记法补丁、`classifyError`、`elabStmtType`、行尾/空白归一化 | ✅ |
| `sgslean/SgsLean/Gate.lean` | 门检：`have <probe> : stmt := ?_` 探针 + `isProp` 判据 | ✅ |
| `sgslean/SgsLean/Verify.lean` | 整篇验证：孤立义务目标 + `wrapProofScriptAsTactic` + reap `checkProof` kernel 终检 | ✅ |
| `sgslean/SgsLean/{Test}.lean`、`Test/{Gate,Verify}.lean` | 35 个离线判定点（含三类必测负例） | ✅ |
| `sgslean/README.md`、`docs/phase3-log.md` | 用法与阶段日志（含 `sorry` 穿过 `checkTacticSyntax` 的实测） | ✅ |

### P1.2（待做：**需要联网下载 Mathlib + 长时间编译，动手前先问**）

| 文件 | 作用 |
|---|---|
| `sgslean/lakefile.toml`（改）、`lean-toolchain`、`lake-manifest.json` | 加入并锁定 Mathlib 版本；reap 仍排在 Mathlib 之前 |
| `docs/upstream.md`（改） | 补 Mathlib 版本锁定行 |
| `sgslean/SgsLean/Server.lean` | **自写 stdio JSON 服务**（协议 v1 见下），是 Python 侧唯一入口 |
| `sgslean/SgsLean/Trace.lean` | 轨迹记录最小版：statement → k 次采样 → Verify → JSONL（分层标记留到 P2 完整化） |
| `service/prompts.py`（改） | 新增 `/solve` 提示词（整篇证明，只输出 proof body，不输出 `theorem` 头） |
| `service/proxy.py`（改） | 新增 `POST /solve`；`/guide` 保留 |
| `service/mock_server.py`（改） | 同步实现 `/solve`，保证离线可测 |
| `tests/run_server_smoke.py` | 离线：假服务 + `Server.lean` 往返（JSONL 进 JSONL 出） |
| `tests/run_solve_e2e.py` | 真实模型：`/solve` → `Server.lean` 验证 → 轨迹落盘 |
| `data/workload.jsonl`、`data/heldout.jsonl` | 工作负载 W 与 held-out 划分（初版） |
| `docs/phase4-log.md` | 本阶段日志 |

### P1.3（G1 闸门）

| 文件 | 作用 |
|---|---|
| `data/lemmas_g1.jsonl` | 50–100 条初等引理（带标准答案与难度标注） |
| `tests/run_gate_g1.py` | 每条 × k 次采样，测 solve_rate 分布 |
| `experiments/results/g1_solver_capability.json` | 分布直方图、成功样例、按难度分桶 |
| `docs/phase5-log.md` | 闸门结论与降级判定 |

**G1 判据**：solve_rate 不能几乎全 0。不过则走预案（换模型 / 退 Nat 域 / 整篇生成降级为骨架 + 搜索），
并**停下来问**。

## P2 轨迹与需求信号（N1）

| 文件 | 作用 |
|---|---|
| `sgslean/SgsLean/Trace.lean`（扩） | 子目标状态签名；轨迹分层导出（成功轨迹 / 失败轨迹） |
| `graph/schema.py` | 轨迹、需求、库的数据模式与校验 |
| `graph/demand.py` | `mine(traces)` → `[(sig, freq, cost, score)]`，含分层统计与跨目标一致性过滤 |
| `docs/design/demand.md` | `d(g)`、阈值 θ 与最少目标数 m 的定义冻结 |
| `service/prompts.py`（改） | conjecture 提示词加入"需求状态""已入库范例"两个输入 |
| `service/proxy.py`、`mock_server.py`（改） | `/conjecture` 接受 `demand` / `seeds` 字段 |
| `tests/run_gate_g2.py` → `experiments/results/g2_demand.json` | G2 实测（伪影过滤前后对比） |
| `docs/phase6-log.md` | 本阶段日志 |

**G2 判据**：重复子目标常见且可与死路伪影区分；否则 N1 降级，主线只留 N2 + N3。

## P3 形式化价值与引理库（N2/N3）

| 文件 | 作用 |
|---|---|
| `sgslean/SgsLean/Trivial.lean` | 非平凡性：`simp`/`aesop`/`decide` 在预算内解不出 |
| `sgslean/SgsLean/Novelty.lean` | 新颖性：不 α-等价于已有引理/目标 + 库检索未命中 |
| `sgslean/SgsLean/Measure/Compression.lean` | `Δlen`：搜索展开数、证明 token 数 |
| `sgslean/SgsLean/Measure/Dependency.lean` | 依赖抽取（复用 `collectConstNames`），判断最终证明是否真的用到该引理 |
| `sgslean/SgsLean/Verify.lean`（改） | `materialize`：把已验证引理提交为**有名常量**（压缩/依赖可测的前提） |
| `sgslean/SgsLean/Driver.lean` | 单目标编排：门检 → 硬门 → 求解 → 实体化 → 测量 |
| `graph/library.py` | 库读写、检索、统计（尝试数 / 解出数 / 被依赖数 / Δ） |
| `graph/coverage.py` | `cover(S)` 与 `greedy_select(pool, W, B)` |
| `docs/design/measurements.md` | 每个信号的定义、预算、成本 |
| `docs/design/library.md` | 库模式、选择算法、停止准则 |
| `tests/run_gate_g3.py` → `experiments/results/g3_selection.json` | G3 实测 |
| `docs/phase7-log.md` | 本阶段日志 |

**G3 判据**：cover 有便宜且仍具子模性的代理；否则放弃近似保证，改述为启发式选择。

## P4 闭环与三组对照

| 文件 | 作用 |
|---|---|
| `graph/runner.py` | 轮次驱动：采集 → 需求 → 生成 → 门检 → 求解 → 选择 → 库更新 → 记忆注入 → held-out 评测 |
| `service/prompts.py`（改） | 记忆注入（出题器注入入库引理；求解器可选注入已验证证明） |
| `tests/run_round.py` | 配置驱动：SGS 式 Guide / 形式化价值 G / 无库 三组 + "只硬门"消融 |
| `experiments/runs/<round>/` | 每轮轨迹、需求、库快照、结果 |
| `experiments/results/g4_h1.json`、`g4_h2.json` | H1 / H2 对比数据 |
| `docs/phase8-log.md` | 本阶段日志 |

**G4 判据**：产出 H1/H2 数据；负结果同样算通过。

## P5 评测、审计与写作

| 文件 | 作用 |
|---|---|
| `tests/audit_gains.py` | G 的四个分量与配对反事实增益 Δ 的相关性审计 |
| `tests/aggregate.py` | 分钟表、置信区间、成本表 |
| `docs/repro.md` | 一条命令复现全部实验 |
| `docs/paper-draft.md` | 论文/技术报告草稿（含相关工作的划界表，见 `proposal.md`） |

## P6 结题交付

| 文件 | 作用 |
|---|---|
| `docs/upstream-diff.md` | `reap-fork` 相对上游的最小 patch 对照 |
| `docs/data-card.md` | 轨迹数据集、需求统计、库快照的说明与许可 |
| `README.md`（改） | 项目定位、快速开始、引用方式 |

## 每阶段边界（防越界）

| 阶段 | 只做 | 明确不做 |
|---|---|---|
| P1 | 环境、门检、整篇验证、交互层、G1 实测 | 不做需求、不做库、不做子模 |
| P2 | 轨迹与需求统计、G2 实测 | 不做选择算法、不做闭环 |
| P3 | 四个信号、实体化、库与子模选择、G3 实测 | 不做多轮闭环 |
| P4 | 闭环与三组对照 | 不加新信号、不改主流程 |
| P5 | 审计、统计、写作 | 不动代码主流程 |

## `SgsLean/Server.lean` 协议 v1 草案（待评审后实现）

**传输**：stdin / stdout 各一行一条 JSON（JSONL），UTF-8；stderr 留给诊断，stdout 只放响应
（Lean frontend 默认把消息打到 stdout，服务端在跑 frontend 期间把 stdout 临时换成 stderr，
见 `docs/phase4-log.md` 现象 2）。每条请求与响应都带 `id`，Python 侧按 `id` 配对（不依赖顺序）。

**批处理**：请求攒批，写一条 `{"id":"...","cmd":"flush"}` 取回本批响应，或在 EOF 时自动 flush。
批内响应与请求 1:1 对应；`flush` 响应额外回报 `flushed`（条数）与 `frontend_ms`（本次
frontend 的真实耗时，P1.3 成本核算用）。

**只有 JSON 对象才算请求**：非 JSON / 非对象行一律**立刻**回一条 `{"id": null, "ok": false,
"error": {"code": "bad_request", ...}}`，不进批（非法行取不到 `id`，只能回 `null`）。

**请求 / 响应**

```json
{"id": "r1", "cmd": "ping"}
{"id": "r1", "ok": true, "result": {"status": "ok", "version": "v1", "importedModules": 2072, "mathlib": false}}

{"id": "r2", "cmd": "check", "stmt": "∀ (n : Nat), n + 0 = n"}
{"id": "r2", "ok": true, "result": {"ok": true, "reason": "ok", "isProp": true, "elaboratedType": "∀ (n : Nat), n + 0 = n"}}

{"id": "r3", "cmd": "verify", "stmt": "∀ (n : Nat), n + 0 = n", "proof": "intro n\nrfl"}
{"id": "r3", "ok": true, "result": {"ok": true, "reason": "ok", "finalChecked": true}}

{"id": "r4", "cmd": "verify", "stmt": "P", "proof": "sorry"}
{"id": "r4", "ok": true, "result": {"ok": false, "reason": "mvar_or_sorry"}}
```

**错误**（协议层失败，与"判定为假"区分开）：

```json
{"id": "rx", "cmd": "flush"}
{"id": "rx", "ok": true, "result": {"flushed": 4, "frontend_ms": 18862}}

{"id": "r5", "ok": false, "error": {"code": "bad_request", "message": "unknown cmd: \"solve\""}}
```

码表沿用 `docs/api-contract.md`：`bad_request` / `invalid_params` / `internal_error`
`backend_unavailable` / `backend_timeout`；判定码沿用 `SgsLean.classifyError` 的输出。

**语句的闭式约定（v1）**：`stmt` 只允许引用全局常量，或自行用 `∀` / `→` 引入变量；
调用方负责把局部上下文**闭包**成一条自足命题（与 `/conjecture` 契约里 `type` 的口径一致）。
局部上下文透传（goal state 直传）留到 v2：等 P2 的轨迹生成落地、确认确实需要之后再加。

**实现（P1.2 离线切片已完成，见 `docs/phase4-log.md`）**：

| 实现要点 | 说明 |
|---|---|
| 入口 | `lake exe sgslean-server`（`[[lean_exe]]` + `supportInterpreter = true`） |
| 驱动方式 | 主循环 + `Lean.Elab.runFrontend` 跑**内容固定**的片段（`import` + 一个 `example`），片段里 `run_tac` 调 `runJobs` |
| 数据通道 | 进程内 `IO.Ref`（作业槽 / 结果槽），不经文件、不做字符串拼接 |
| 搜索路径 | 启动时 `Lean.initSearchPath (← Lean.findSysroot)`（standalone exe 默认搜索路径为空） |
| 实测成本 | 一次 frontend（2072 个模块 + 13 条判定）≈ 18 s；必须批量喂请求 |
