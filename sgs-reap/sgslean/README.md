# sgslean：SG-Lean 的形式化裁判与库物化层

SG-Lean 不训练模型。Lean 侧负责合式检查、非平凡检查、最终证明验证、证明项常量抽取、
活动库物化和常驻批处理服务。它不判断自然语言“有用性”，也不把失败证明中的文本命中算作复用。

```lean
SgsLean.Gate.check    (stmt : String) : TacticM GateResult    -- 能 elaborate 成命题吗
SgsLean.Verify.verify (stmt proof : String) : TacticM VerifyResult  -- 整篇证明过吗
SgsLean.Trace.trace   (stmt proof : String) : TacticM TraceResult   -- 通过项的 constants
```

## 构建与测试（离线）

```powershell
# 只需做一次：复用 reap-fork 已编译好的依赖（不要加 /XD .git）
robocopy ..\reap-fork\.lake\packages .lake\packages /E /NFL /NDL /NJH /NJS /NP

lake build SgsLean SgsLean.Test sgslean-server
lake build SgsLean.GeneratedLibrary
```

测试全部是 `run_tac` 断言，**构建通过 ⟺ 断言通过**，不需要网络也不需要 Mathlib。

## 语义边界

| 判定 | 含义 | 不负责 |
|---|---|---|
| `Gate.check` | 语句能在当前上下文 elaborate，且类型是命题 | 非平凡 / 新颖 / 可证 |
| `Verify.verify` | `proof` 是 `stmt` 的完整证明并通过内核终检 | 是否值得入库 |
| `Trace.trace` | 对验证结果记录子目标与证明项常量 | 因果价值 |
| `Materialize` | 把 active 快照写成可 import 的 Lean 模块 | probation/cold 管理 |

正式 reuse 只允许来自“通过验证的证明项 constants”，按不同、非来源目标去重。
开发期库中仍有 `rfl` 平凡命题，说明非平凡门需要先修复并补反向对照，再重建正式库。

当前规格见 `../../docs/SG-Lean思路文档第二版.pdf`；历史证据见 `../docs/history/`。
