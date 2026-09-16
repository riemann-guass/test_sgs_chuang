# 阶段 3 执行日志（SG-Lean P1.1：sgslean 包与 Gate/Verify）

编号说明：`docs/phase0..2-log.md` 覆盖的是 P0（SGS 推理期复现 + 真实模型标定）。
本文件是 P1.1 的执行记录，因此续在 phase2 之后。

本单元范围（只做 P1.1，不碰 P1.2/P1.3）：
新建离线可构建的 lake 包 `sgs-reap/sgslean/`，实现最小接口
`Gate.check : String → TacticM GateResult` 与 `Verify.verify : String → String → TacticM VerifyResult`，
并用纯 Lean 测试覆盖正例与三类必测负例。**未引入 Mathlib，未调用任何外部服务**。

## 交付物

| 文件 | 作用 |
|---|---|
| `sgslean/lakefile.toml` | `[[require]] name = "reap", path = "../reap-fork"`（reap 排在前面，P1.2 接 mathlib 时同理） |
| `sgslean/lean-toolchain` | `leanprover/lean4:v4.28.0-rc1`，与 reap-fork 一致 |
| `sgslean/SgsLean.lean`、`SgsLean/{Basic,Gate,Verify}.lean` | 门检与验证接口 |
| `sgslean/SgsLean/Test.lean`、`SgsLean/Test/{Gate,Verify}.lean` | 离线测试（35 个判定点） |
| `sgslean/lake-manifest.json` | 依赖锁定（入档；`inherited: true` 的三个 git 依赖 rev 与 reap-fork 完全一致） |

接口实现复用 reap 已经验证过的原语，没有改动 `reap-fork` 一行代码
（构建仍为 209 jobs，见下文命令）。

## 验收命令与真实输出

```powershell
# 1) 离线准备（新环境只需做一次；robocopy 不能加 /XD .git，见阶段 0 记录）
robocopy sgs-reap\reap-fork\.lake\packages sgs-reap\sgslean\.lake\packages /E /NFL /NDL /NJH /NJS /NP

# 2) 构建库 + 跑离线测试（断言在 elaboration 期执行，构建通过 ⟺ 断言通过）
cd sgs-reap\sgslean
lake build SgsLean SgsLean.Test
```

`SgsLean`：`Build completed successfully (203 jobs).`

`SgsLean SgsLean.Test`（清空 `.lake` 后从零重跑，验证离线可复现）：

```
info: sgslean: no previous manifest, creating one from scratch
✔ [199/207] Built SgsLean.Basic (13s)
✔ [200/207] Built SgsLean.Gate (7.5s)
✔ [201/207] Built SgsLean.Verify (7.3s)
✔ [202/207] Built SgsLean (7.1s)
✔ [204/207] Built SgsLean.Test.Gate (13s)
✔ [205/207] Built SgsLean.Test.Verify (13s)
✔ [206/207] Built SgsLean.Test (7.8s)
Build completed successfully (207 jobs).
```

```powershell
cd sgs-reap\reap-fork
lake build Reap Reap.Test     # Build completed successfully (209 jobs).
```

## 关键设计决定

### 1. `Gate.check`：完全按任务书用探针原语

```lean
evalTacticStrNoFinalCheck ctx ("have sgs_probe_ : " ++ stmt ++ " := ?_") 200000
```

这条动作与搜索里真实的猜想动作走**同一个**函数，所以「门检通过 ⟺ 该候选进了搜索也能被
组装成动作」。`?_` 是 synthetic hole，会留下一个真子目标，不会把目标标记为已解决。

在探针之上加了一条**独立**判据：语句必须 elaborate 成一个**命题**。
实现是 `elabStmtType`（`Parser.runParserCategory env `term` + `Term.elabType`）+ `Meta.isProp`。
之所以不用探针留下的子目标去取类型：探针之后目标列表是 `[续目标, 探针义务]`，
两者类型都可能是同一个 metavariable，靠位置取类型不稳（实测见「现象 2」）。

因此 `Gate.check` 的判定比「能 elaborate」严格一档：`Nat`、`Prop`、`Nat → Nat` 这类
**类型**会被判 `not_a_prop`。这是任务书「能否 elaborate 成一个 Proposition」的字面要求，
结果结构里保留了 `isProp`/`elaboratedType` 字段，需要放宽时调用方可以直接读字段。
注意门检**不**判可证性：`False`、`¬ (P ∧ ¬ P)` 都过门（可证性属于 G 的第三件，留给后续阶段）。

### 2. `Verify.verify`：偏离了任务书给的写法，理由是实测出来的

任务书建议的实现是
`evalTacticStrNoFinalCheck ctx ("have sgs_probe : " ++ stmt ++ " := by\n" ++ indent proof)`。
实测这条写法有一个硬缺陷：外层目标始终未闭合，`evalTacticStrCore` 里的
`if (← getGoals).isEmpty then checkProof ctx` **永远不会触发**，于是 reap 唯一那道
kernel 终检（`assignedProofHasMVarOrSorry` + `checkPreDefinitions`）在这条路径上等于不存在。

实际实现改为等同构但能跑到终检的形式（与 `Reap.Tactic.TreeSearch.checkProofScript` 同构）：

1. `Gate.check stmt`（语句必须是命题）；
2. `elabStmtType stmt` → 用 `mkFreshExprSyntheticOpaqueMVar` 造一个**孤立**义务目标 `⊢ stmt`，
   用 `setGoals` 把它设为唯一目标（不依赖外层目标，也不依赖 `sorry` 兜底）；
3. `evalTacticStrNoFinalCheck ctx (MCTS.wrapProofScriptAsTactic proof)`（含 reap 的
   `checkTacticSyntax` 守卫）；
4. 子目标清零后调用 reap 的 `checkProof ctx`：赋值里不许有 `sorryAx`/残余 metavariable，
   并把赋值交给 kernel 复核。

第 4 步不是锦上添花，见下面「现象 1」。`VerifyResult` 里因此有 `finalChecked` 字段，
测试断言「通过 ⟹ `finalChecked = true`」。

## 现象 / 根因 / 修法

### 现象 1（最重要）：`sorry` 能穿过 reap 的语法守卫

**现象** 直接探针（`evalTacticStrNoFinalCheck` + `have ... := by sorry`）与
`evalTacticStrNoFinalCheck "sorry"` 都返回 `ok = true`，`sorry`/`admit` 都不会被拒。

**根因** 本版本 Lean 里 `sorry` 的语法节点 kind 是 `Lean.Parser.Tactic.tacticSorry`，
而 reap 的 `isQuestionTacticKind` 比的是 `` kind == `sorry ``（Name `sorry`），**匹配不到**；
`admit` 同理。实测语法树 kind：

```
[verify_sorry] kinds=[Lean.Parser.Tactic.tacticSorry, null, ... Lean.Parser.Tactic.tacticHave__]
[tactic_sorry] kinds=[Lean.Parser.Tactic.tacticSorry]
[question]     err={"forbiddenTactic":{"kind":"Lean.Parser.Tactic.apply?"}}   -- `?` 后缀规则仍然有效
```

（`?_` 的 kind 是 `Lean.Parser.Term.syntheticHole`，所以猜想动作本身不受影响，与阶段 0 结论一致。）

**修法** 不把可靠性寄托在语法守卫上，改用语义检查：让证明真正闭合孤立义务目标后调用
reap 的 `checkProof`。`sorry`/`admit` 的赋值含 `sorryAx`，被判
`assignedProofHasMVarOrSorry`；同时补上真正需要的 kernel 终检。

**证据（反向对照，生产代码级）** 临时把 `Verify.verify` 里的 `checkProof` 调用改成恒真：

```
✖ [205/207] Building SgsLean.Test.Verify
error: SgsLean/Test/Verify.lean:52:2: 验证应当拒绝 "P" / "sorry"，实际通过
```

证明测试确实在拦这件事；随后已还原，构建恢复全绿。

### 现象 2：`have` 之后的目标顺序与 lctx 归属

**现象** `have sgs_probe_ : T := ?_` 之后 `getUnsolvedGoals` 是两个目标：
`[续目标（lctx 里含 sgs_probe_）, 探针义务（lctx 不含 sgs_probe_）]`；
语句是 `Nat` 时第二个目标的类型才是 `Nat`。另外两者类型都可能打印成同一个 metavariable
（`_uniq.NNN`），必须 `instantiateMVars` 才有意义。

**根因** Lean 的 `have` tactic 用「新 mvar 顶替原目标 + 新增义务 mvar」实现，原目标被赋值掉，
所以前后目标的 MVarId 并不相同（`before=1 after=2`，且两个 after 目标都不在 before 里）。

**修法** Gate 不用位置取类型（改用 `elabType`）；Verify 不用 `have`，直接造孤立义务目标。
两处都不依赖「目标顺序」这个实现细节。

### 现象 3：`autoImplicit` 又吞掉一个标识符（旧坑复发）

**现象** `SgsLean/Basic.lean` 里 `def classifyError (err : EvalError) : String := match err with | .parseError _ => ...`
报：

```
error: Invalid dotted identifier notation: The expected type of `.parseError`
  EvalError
is not of the form `C ...` or `... → C ...` where C is a constant
```

**根因** 老坑：`EvalError` 实际定义在 `Reap.TreeSearch` 命名空间里，本文件没有 `open Reap.TreeSearch`，
于是它被 `autoImplicit` 当成隐式绑定变量；错误信息里的 `EvalError` 是那个**变量**，不是类型。
`normalizeProp` 同理（`Unknown identifier`）。

**修法** 与 `reap-fork/tests/Calibrate.lean` 一致，加 `open Reap.TreeSearch`。
教训：在这个环境里出现「看起来像实例/类型错误」的报错，先怀疑 autoImplicit。

### 现象 4：`module` 文件不能导入非 `module` 模块

**现象** `error: SgsLean/Test.lean:1:0: cannot import non-`module` SgsLean.Test.Gate from `module``

**修法** 测试聚合入口 `SgsLean/Test.lean` 写成普通文件（普通文件可以导入 `module` 包），
与 `reap-fork/Reap/Test.lean` 的做法一致；库代码 `SgsLean*.lean` 继续用 `module` + `public meta`。

### 现象 5：离线依赖解析

**现象** 新包需要 reap 的三个 git 依赖（batteries / openAI_client / requests）。沙箱无网络。

**修法** 把 `reap-fork/.lake/packages` **完整**复制到 `sgslean/.lake/packages`
（robocopy 不能加 `/XD .git`，否则 lake 会误判 `batteries: URL has changed`，阶段 0 已踩过），
再运行 `lake build`。lake 从现成目录解析依赖，生成的 `lake-manifest.json` 里三个依赖
`inherited: true`，rev 与 reap-fork 完全一致（`6032d2c1` / `7c98d4cc` / `9edc459e`），全程无网络。

**验证** 删除整个 `sgslean/.lake` 后按上面两步重跑，从零构建成功（见「验收命令与真实输出」）。

## 测试清单（35 个判定点）

`Gate.check`（18）：命题/复合命题/含新变量命题/`ℕ` 级语句 7 条正例；
`False`、`¬ (P ∧ ¬ P)` 2 条边界（声明门检不判可证性）；
证明项当命题 2 条、类型当命题 3 条、未知标识符 2 条、解析失败 1 条负例；
空白/CRLF 归一化 1 条。

`Verify.verify`（17）：单步/多步/分号/bullet/`ℕ`/CRLF 6 条正例；
`sorry`、`admit`、嵌套 `exact sorry` 3 条；未闭合 `intro hp`、`constructor`、
残留占位符 `exact ?_` 3 条；证明类型不匹配、空证明 2 条；
语句过不了门检时回传门检判定码 3 条。

**反向对照（测试级）** 把 `expectGateReject "Nat" "not_a_prop"` 的期望码改成 `"ok"`：

```
✖ [204/206] Building SgsLean.Test.Gate
error: SgsLean/Test/Gate.lean:66:2: 门检对 "Nat" 的判定码应为 ok，实际为 not_a_prop（detail=）
```

构建如期失败 ⟹ 断言不是空转；随后已还原。

## 与任务书的偏差（三处，均已在上面说明理由）

1. `sgs-reap/README.md` 不存在：仓库 README 在仓库根（`大创/README.md`）。
2. `classifyError` 不在 `reap-fork/Reap/Test/Tactic/Conjecture.lean`，而在
   `reap-fork/tests/Calibrate.lean`；该文件不在 lake 的 glob 内、无法被库导入，
   因此在 `SgsLean/Basic.lean` 复刻了一份（码表改动需两处同步），并新增
   `"unclosed_goals"` 一类（`solve_rate` 统计需要把「没闭合」与「类型错误」分开）。
3. `Verify.verify` 的实现形态与任务书给的 `have ... := by` 探针不同，理由见「关键设计决定 2」。

## 现在在哪 / 下一步

**P1.1 完成**（离线可构建、有正负例、有反向对照），闸门层面停在 P1 的入口，
尚未触碰 G1。下一步按任务书是 **P1.2（Mathlib 工程 + 整篇证明生成→验证→轨迹记录）**，
该步骤需要联网与长时间编译，**动手前先问**。
