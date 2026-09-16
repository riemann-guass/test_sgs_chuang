# sgslean：SG-Lean 的环境与交互层

SG-Lean 把 SGS 的三角色结构搬到推理期，只换两样东西：Guide 的判据换成形式化测量 G，
「变强」的载体换成引理库 + prompt 记忆注入（不做任何梯度训练）。

本包目前（P1.1）只提供 G 的第一道硬门与验证接口，不接任何外部服务、不依赖 Mathlib。

```lean
SgsLean.Gate.check    (stmt : String) : TacticM GateResult    -- 能 elaborate 成命题吗
SgsLean.Verify.verify (stmt proof : String) : TacticM VerifyResult  -- 整篇证明过吗
```

## 构建与测试（离线）

```powershell
# 只需做一次：复用 reap-fork 已编译好的依赖（不要加 /XD .git）
robocopy ..\reap-fork\.lake\packages .lake\packages /E /NFL /NDL /NJH /NJS /NP

lake build SgsLean SgsLean.Test
```

测试全部是 `run_tac` 断言，**构建通过 ⟺ 断言通过**，不需要网络也不需要 Mathlib。

## 语义边界

| 判定 | 含义 | 不负责 |
|---|---|---|
| `Gate.check` | 语句能在当前上下文 elaborate，且类型是命题 | 非平凡 / 新颖 / 可证 |
| `Verify.verify` | `proof` 是 `stmt` 的一篇完整证明，并通过 reap 的 kernel 终检 | 非平凡性、是否值得入库 |

「非平凡（simp/aesop/decide 解不出）」「新颖（不 α-等价于已有引理）」「可证（solve_rate > 0）」
属于 G 的其余部分，在 P2/P3 实现。

细节与实测证据见 `../docs/phase3-log.md`。
