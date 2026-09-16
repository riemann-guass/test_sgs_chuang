# 阶段 0 执行日志

## 环境

| 项 | 值 |
|---|---|
| Lean toolchain | `leanprover/lean4:v4.28.0-rc1`（已由 elan 安装） |
| lake | `C:\Users\gaosen\.elan\bin\lake.exe` |
| reap fork | `sgs-reap/reap-fork/`，上游 commit `1477439` |
| 依赖 | `batteries` / `openAI_client` / `requests`，**不含 mathlib** |

## 步骤 2：编译验证

```powershell
cd sgs-reap\reap-fork
lake build Reap Reap.Test
```

结果：`Build completed successfully (206 jobs).`

### 踩坑记录：复制 fork 时不能排除全部 `.git`

首次 `robocopy ... /XD .git` 会把 `.lake/packages/*/.git` 一并排除，导致 lake 校验依赖时误判仓库地址变化：

```
info: batteries: URL has changed; you might need to delete '...\.lake/packages\batteries' manually
error: external command 'git' exited with code 255
```

正确处理：只排除仓库根目录的 `.git`，随后把上游 `.lake` 完整复制过来（含各依赖包的 `.git`）。

## 关键技术证据（本轮实测）

**1. `?_` 不在禁用名单内，`?` 结尾的 tactic 才是。**

`Reap/Test/Tactic/Step.lean` 里已有的测试与本轮编译输出给出了直接证据：

```
-- 该文件的守卫测试
guardEvalRejects "apply?"      -- 被拒
guardEvalRejects "sorry"       -- 被拒
guardEvalRejects "repeat' trivial"  -- 被拒

-- print_syntax_tree "have theorem? := ?_" 的实际输出
node Lean.Parser.Term.syntheticHole
  atom ?
  atom _
```

`isQuestionTacticKind` 只在 SyntaxNodeKind 的字符串以 `?` 结尾时拒绝，`?_` 的 kind 是 `Lean.Parser.Term.syntheticHole`，因此**合法**。

**2. `have h : P := ?_` 全链路已被上游测试覆盖并通过。**

`Reap/Test/Tactic/MCTS.lean` 的 `deferredHavePolicyValue` 用例：

```lean
example (P : Prop) (hP : P) : P := by
  run_tac do
    let (some nodeIdx, nodes) ← runMCTSForTest (deferredHavePolicyValue unfocusedVisits) ...
    let expected := "have h : P := ?_\n· exact h\n· exact hP"
    guardProofScriptEquals nodes nodeIdx expected
    guardProofScriptChecks ctx expected   -- 走 checkProofScript → kernel 终检
  exact hP
```

该 `example` 依靠 `run_tac` 在 elaboration 期实际执行，构建成功即代表它通过。

**3. 术语/占位符相关的边界。**

`guardEvalRejects "cases (_ : False)"` 说明**未被填上的 `_` 洞会因残余 metavariable 被拒**。这与我们的设计一致：猜想出的 `have aux : P := ?_` 之所以可行，是因为它的洞会被后续 AND 子目标真正证掉，而不是被当作已解决。

## 步骤 3：猜想动作纯 Lean 验证（闸门 M0）

新增 `reap-fork/Reap/Test/Tactic/Conjecture.lean`，并挂到 `Reap.Test` 上。场景：

```lean
example (P Q : Prop) (h : P) (himp : P → Q) : Q := by ...
```

根节点**只允许**通过猜想动作 `have aux : P := ?_` 解题，policy 不提供任何直接动作。三个用例：

| 用例 | 断言 | 结果 |
|---|---|---|
| 正例 | 组装出的脚本等于 `have aux : P := ?_ / · exact himp aux / · exact h`，并通过 `checkProofScript` kernel 终检 | 通过 |
| 结构 | 猜想动作产生 AND 节点，且恰有 2 个 focus 子目标 | 通过 |
| 负例 | `have bad : NotARealType := ?_`（无法 elaborate）与 `this is not a tactic`（解析失败）都不会变成树边，搜索仍找到解 | 通过 |

命令与结果：

```powershell
lake build Reap Reap.Test    # Build completed successfully (207 jobs).
```

### 反向对照（防止测试形同虚设）

把正例的期望脚本故意改成 `· exact h / · exact himp aux`，构建如期失败：

```
error: Reap/Test/Tactic/Conjecture.lean:59:2: unexpected proof script:
error: Lean exited with code 1
```

说明断言真的在执行，测试非空转。随后已还原。

### 发现并修掉的 bug

`throwError "...{focusIdx.size}"` 中的 `Nat` 插值不被接受（期望 `MessageData`），导致该 `run_tac` 块报 `failed to evaluate expression, it contains metavariables`。改为 `{toString focusIdx.size}` 后通过。

## 闸门 M0 结论

**通过。** 「猜想 → 动作 → AND → 重放 → kernel 终检」全链路在纯 Lean 侧成立，无需任何外部服务，原定备用路线（退回一次性分解 tactic）不启用。

## 下一步

阶段 1：冻结 `/conjecture` 与 `/guide` 的接口契约（`docs/api-contract.md`），实现假服务，并在 Lean 侧加 `ConjectureClient` / `GuideClient` 与 `generatePolicyValueWithConjecture` 包装器。
