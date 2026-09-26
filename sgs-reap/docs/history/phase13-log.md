# 阶段 13 执行日志（P3：软分第二件——依赖抽取）

"这条引理到底有没有被用上"必须可判，否则 Δlen 与"被依赖率"都会被重述式候选刷满。
`Measure.dependencies` 的做法是：**先验证，再从赋值后的证明项里抽常量名**（复用 reap 的
`collectConstNames`），最后回答"是否引用了指定引理"。

## 交付物

| 文件 | 作用 |
|---|---|
| `sgslean/SgsLean/Measure/Dependency.lean` | `Measure.dependencies (stmt) (proof) (target := "")`：常量集合 + `usesTarget` |
| `sgslean/SgsLean/Test/Dependency.lean` | 离线测试：必然引用/必然不引用/验证失败不抽依赖 |
| `sgslean/SgsLean/Server.lean`（改） | 协议新增 `dependencies` 命令 |

**关键约束**：只有验证通过的证明才抽依赖。没过的证明本身就是垃圾，从它里面抽出的"依赖"
没有意义——这类情况直接返回 `verified=false` 且 `constants` 为空。

## 离线测试（无 Mathlib，3 组断言）

```powershell
cd sgs-reap\sgslean
lake build SgsLean SgsLean.Test sgslean-server   # Build completed successfully (420 jobs).
```

| 用例 | 期望 |
|---|---|
| `∀ (a b : Nat), a + b = b + a` / `intro a b`+`exact Nat.add_comm a b`，查 `Nat.add_comm` | `usesTarget=true`，且 `constants` 含 `Nat.add_comm` |
| 同一证明，查 `Nat.mul_comm` | `usesTarget=false` |
| `∀ (n : Nat), n + 0 = n` / `sorry` | `verified=false` 且 `constants` 为空 |

## 测试抓到的真 bug（这轮最有价值的产出）

第一版 `dependencies` 把**多行证明脚本直接丢给** `evalTacticStrNoFinalCheck`，
结果是**只执行了第一行**：server 上实测返回 `verified=true` 但 `constants=[]`，
探针打印出真相——

```
tactic ok=false goals=1
raw term=fun (a : Nat) => ?_uniq.10848 a
consts=#[Nat]
```

即 `intro a b` 跑完之后 `exact Nat.add_comm a b` **根本没跑**，证明项是个未闭合的 lambda。
根因是 P1.1 已经踩过的同一个坑：多行脚本必须先包成 `exact by ...`
（`MCTS.wrapProofScriptAsTactic`），`Verify` 一直是这么做的，本模块漏了。
修好后 `constants` 正常含 `Nat.add_comm`、`usesTarget` 正确。

> 教训：凡是"把用户的证明脚本交给 Lean"的地方，都必须走 `wrapProofScriptAsTactic` 这一条路；
> 漏掉就会得到**看起来通过、其实只跑了一行**的结果——这是最危险的一类假阳性。

## 反向对照

把 `usesTarget` 改成"只要给了 target 就说用到了"（不查常量表）后重建 `SgsLean.Test`：

```
error: SgsLean/Test/Dependency.lean:18:2: 不应检测到引用了 Nat.mul_comm
error: Lean exited with code 1
```

负例断言如期失败；还原后重建 `420 jobs` 全绿。

## 局限（v1）

* 只抽**直接出现**在证明项里的常量，不做传递闭包（A 用到 B 时只报告 A 被引用）；
* 常量名按字符串比较（`Nat.add_comm` 全名），不做命名空间解析；
* 过滤掉以 `_` 开头的内部名（`_uniq.*`、aux decl），它们不是用户可见的引理。

## 现在在哪 / 下一步

硬门三件 + 软分两件（Δlen、依赖）都落地了。P3 只剩最后一块：

1. `Verify` 增 `materialize`：把已验证引理落成**有名常量**（让"被依赖"变成可查的环境事实，
   也是库的物理形态）；
2. `graph/library.py` + `graph/coverage.py`：库的读写检索、`cover(S)` 与子模贪心
   → 闸门 **G3**（cover 有没有便宜且仍具子模性的代理）。
