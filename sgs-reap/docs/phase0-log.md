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

## 下一步

步骤 3：新增 `Reap/Test/Tactic/Conjecture.lean`，用纯 Lean 的假猜想源验证「猜想动作 → AND 节点 → proof script → 终检」整条链，并补一个非法引理的负例。
