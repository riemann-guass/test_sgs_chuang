# 阶段 11 执行日志（P3：硬门第二件——新颖性）

`Novelty.isNew` 判定候选引理**是否新**：不 α-等价于父目标，也不 α-等价于库里任何一条。
它替代 SGS rubric 那句"与目标等价的引理给 0 分"——推理期同样没价值，而且会在辅助引理的
子目标上引发自指猜想（P1 阶段实测过这个坑）。

## 交付物

| 文件 | 作用 |
|---|---|
| `sgslean/SgsLean/Novelty.lean` | `Novelty.isNew (stmt) (against : Array String)`；逐条比等价，命中即返回下标 |
| `sgslean/SgsLean/Test/Novelty.lean` | 离线测试：7 个判定点（含 α-等价这一关键用例） |
| `sgslean/SgsLean/Server.lean`（改） | 协议新增 `novelty` 命令（`stmt` + `against` 数组） |

## 判定方式与它的方向性

把两边都 elaborate 成 `Expr`，用 **`isDefEq` 双向**比较。这比纯 α-等价**更严格**：
`isDefEq` 还做定义展开（δ）与实例化简，所以"定义上相同但写法不同"的语句也会被判为重复。
这是**安全方向**——宁可把新引理误判成旧引理（少入库一条），也不要把重述当新知识。

语句 elaborate 失败时返回 `detail` 非空且 `new=false`（**保守判重复**），
准确的拒绝码由 `Gate` 给出——两个模块职责不重叠。

库检索 v1 的做法：把库里已有引理的语句文本作为 `against` 传进来，本模块只负责"逐条比等价"；
真正的检索（按符号/模块/相似度召回）属 `graph/library.py`（P3 后续）。

## 离线测试（无 Mathlib 也能跑，7 个判定点）

```powershell
cd sgs-reap\sgslean
lake build SgsLean SgsLean.Test sgslean-server   # Build completed successfully (414 jobs).
```

| 用例 | 期望 | 说明 |
|---|---|---|
| `isNew "∀ n, n + 0 = n" #[]` | 新 | 空库 |
| `isNew "∀ (n : Nat), n + 0 = n" #["∀ (m : Nat), m + 0 = m"]` | **重复，命中 0** | **α-等价：只换了变量名** |
| `isNew "∀ (n : Nat), n + 0 = n" #["∀ (n : Nat), n * 2 = n"]` | 新 | 只是"像"，不等价 |
| `isNew "True" #["1 = 1", "True"]` | 重复，命中 **1** | 多条库里命中正确下标 |
| `isNew "∀ (P : Prop), P → P" #["∀ (Q : Prop), Q → Q"]` | 重复，命中 0 | α-等价的 Prop 级例子 |
| `compared` 计数 | 恰为库大小 | 每条都比过 |
| 库大小 3、无命中 | 新 + `compared=3` | 统计如实 |

核心是第二行：**变量名不同但逻辑等价必须判重复**。这条一旦错，N1/N2 会把重述反复当新知识入库。

## 反向对照

把 `isNew` 的等价循环去掉、永远返回 `new := true`（模拟"不做查重"）后重建 `SgsLean.Test`：

```
error: SgsLean/Test/Novelty.lean:32:2: 「∀ (n : Nat), n + 0 = n」应判重复，实际判成新颖（compared=1）
error: Lean exited with code 1
```

α-等价用例如期炸掉；还原后重建 `414 jobs` 全绿。

> 这一轮刻意用**正确的 target 名**（`lake build SgsLean.Test`）——上一阶段踩过
> "`lake build SgsLean Test` 不是那个 target、导致反向对照假通过"的坑（见 phase10-log 教训）。

## 现在在哪 / 下一步

硬门三件已完成两件（非平凡、新颖），第三件"可证"由 `Verify` + G1 的 solve_rate 提供 ✓。
下一步进**软分与库**：

1. `SgsLean/Measure/Compression.lean`：Δlen（证明变短的两个度量）+ 依赖抽取；
2. `SgsLean/Verify.lean` 增 `materialize`：把已验证引理落成**有名常量**（压缩/依赖可测的前提）；
3. `graph/library.py` + `graph/coverage.py`：`cover(S)` 与子模贪心 → 闸门 **G3**
   （cover 有没有便宜且仍子模的代理）。
